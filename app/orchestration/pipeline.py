"""Conversation orchestration: shutdown switch -> input masking -> context
assembly -> model gateway (stub) -> tool executor loop (max 5) -> SSE events.

SSE protocol order: message_start ... tool/delta/citation events ... message_end.
The terminal ``message_end`` event is always the last event. Cancellation uses
an asyncio.Event flag checked at every yield checkpoint; the cancelled terminal
event is yielded before returning. Client disconnects surface as GeneratorExit:
the generator's finally persists finish_reason=cancelled (no yield during
GeneratorExit). Every generation releases its concurrency slot in finally.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.gateway.stub import ModelGateway
from app.models import Conversation, Message
from app.orchestration.context import assemble_context
from app.orchestration.masking import mask_input
from app.orchestration.tokens import estimate_tokens
from app.prompts import PROMPT_VERSION, get_system_prompt
from app.review.queue import ReviewQueueStub
from app.sse import events as ev
from app.tools.executor import ToolExecutor

TOOL_SPECS = [
    {
        "name": "search_knowledge_base",
        "description": "Search the platform knowledge base for SOPs and articles.",
        "parameters": {"query": "string"},
    }
]

# Connection point (S2-IAM): role description comes from the identity service.
DEFAULT_ROLE_DESCRIPTION = "employee (demo identity stub)"

TOOL_LIMIT_NOTICE = (
    "I reached the tool call limit without producing a final answer. "
    "Please rephrase the question."
)

CONFIRMATION_NOTICE = (
    "The requested tool requires user confirmation, which is not available in "
    "this session, so the tool was not executed. "
    "Please ask your administrator how to enable tool confirmation."
)


def _sse(event: str, data: dict) -> str:
    return ev.format_sse(event, data)


def summarize_citations(citations: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in citations:
        key = c.get("source")
        if key and key not in seen:
            seen.add(key)
            out.append({"source": key, "title": c.get("title") or key})
    return out


class Orchestrator:
    def __init__(
        self,
        settings,
        gateway: ModelGateway,
        executor: ToolExecutor,
        review_queue: ReviewQueueStub,
        limiter,
        cancel_registry: dict[str, asyncio.Event],
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.executor = executor
        self.review_queue = review_queue
        self.limiter = limiter
        self.cancel_registry = cancel_registry
        # session_factory=None enables in-memory persistence (offline tests and
        # development before S5 integration); production always passes the
        # async session factory wired in the app lifespan.
        self.session_factory = session_factory
        self._memory_messages: list[dict] = []
        self._bg_tasks: set[asyncio.Task] = set()

    async def _load_history(
        self, conversation_id: uuid.UUID, org_id: str, exclude_message_id: uuid.UUID
    ) -> list[dict]:
        if self.session_factory is None:
            return [
                {"role": m["role"], "content": m["content"]}
                for m in self._memory_messages
                if m["conversation_id"] == conversation_id
                and m["id"] != exclude_message_id
                and m["role"] in ("user", "assistant")
            ]
        async with self.session_factory() as session:
            rows = await session.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.org_id == org_id,
                    Message.deleted_at.is_(None),
                    Message.role.in_(["user", "assistant"]),
                    Message.id != exclude_message_id,
                )
                .order_by(Message.created_at.asc())
            )
            return [{"role": r.role, "content": r.content} for r in rows.scalars()]

    async def _persist_message(self, **kwargs) -> None:
        # Mask everything before persistence: tool results can carry PII from
        # backend systems just like user input. Builds a new dict (no mutation
        # of the caller's arguments).
        row = dict(kwargs, content=mask_input(kwargs.get("content", "")))
        if self.session_factory is None:
            self._memory_messages.append(row)
            return
        async with self.session_factory() as session:
            session.add(Message(**row))
            await session.commit()

    async def run(
        self,
        conversation: Conversation,
        user_text: str,
        user_id: str,
        page_summary: str,
        cancel_event: asyncio.Event,
        assistant_message_id: uuid.UUID,
        role_description: str = DEFAULT_ROLE_DESCRIPTION,
    ) -> AsyncGenerator[str, None]:
        settings = self.settings
        prompt_text, prompt_version = get_system_prompt()
        citations: list[dict] = []

        # 1. Mask user input (original text is never persisted).
        masked = mask_input(user_text)
        user_message_id = uuid.uuid4()
        await self._persist_message(
            id=user_message_id,
            conversation_id=conversation.id,
            org_id=conversation.org_id,
            site_id=conversation.site_id,
            role="user",
            content=masked,
            created_by=user_id,
        )

        # 2. Assemble context: system prompt + role + page summary + last N rounds.
        history = await self._load_history(conversation.id, conversation.org_id, user_message_id)
        messages, _stats = assemble_context(
            system_prompt=prompt_text,
            role_description=role_description,
            page_summary=page_summary,
            history=history,
            user_message=masked,
            token_budget=settings.context_token_budget,
            max_rounds=settings.history_max_rounds,
        )

        prompt_tokens = sum(estimate_tokens(m["content"]) for m in messages)
        completion_tokens = 0
        finish_reason: str | None = None
        final_text: str | None = None
        tool_rounds = 0
        persisted = {"done": False}

        async def persist_terminal_once(finish_reason: str, content: str, final_citations: list) -> None:
            if persisted["done"]:
                return
            persisted["done"] = True
            await self._persist_terminal(
                conversation, assistant_message_id, user_id, prompt_version,
                finish_reason=finish_reason, content=content,
                citations=final_citations, prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        def schedule_detached_persist(finish_reason: str) -> None:
            """Persist the terminal state in a detached task.

            The client-disconnect path runs inside a cancelled anyio cancel
            scope where every await raises CancelledError again; a detached
            task escapes that scope so the cancelled terminal state can still
            be persisted.
            """
            if persisted["done"]:
                return
            persisted["done"] = True
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self._persist_terminal(
                    conversation, assistant_message_id, user_id, prompt_version,
                    finish_reason=finish_reason, content="",
                    citations=[], prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

        try:
            # 3. Stream start.
            yield _sse(
                ev.EVENT_MESSAGE_START,
                {
                    "message_id": str(assistant_message_id),
                    "conversation_id": str(conversation.id),
                    "prompt_version": prompt_version,
                },
            )

            # 4. Tool call loop: until final answer or the tool-call limit.
            while True:
                if cancel_event.is_set():
                    finish_reason = "cancelled"
                    break
                response = await self.gateway.complete(messages, TOOL_SPECS)
                completion_tokens += max(1, response.completion_tokens)
                if response.tool_call is None:
                    final_text = response.text or ""
                    finish_reason = "stop"
                    break
                if tool_rounds >= settings.max_tool_calls:
                    # Tool-call limit reached: do not execute further tools.
                    finish_reason = "tool_limit"
                    break
                tool_rounds += 1
                call = response.tool_call
                yield _sse(
                    ev.EVENT_TOOL_CALL,
                    {
                        "message_id": str(assistant_message_id),
                        "tool_name": call.name,
                        "arguments_summary": ev.summarize_arguments(call.arguments),
                    },
                )
                result = await self.executor.execute(call.name, call.arguments)
                if result.requires_confirmation:
                    # v1: report the confirmation requirement and close the
                    # generation cleanly (no client confirmation loop exists in
                    # this work order, so leaving the loop open would strand
                    # the model waiting for a tool result that never comes).
                    yield _sse(
                        ev.EVENT_TOOL_CONFIRMATION_REQUIRED,
                        {
                            "message_id": str(assistant_message_id),
                            "tool_name": call.name,
                            "reason": result.error or "tool requires user confirmation",
                        },
                    )
                    finish_reason = "confirmation_required"
                    break
                yield _sse(
                    ev.EVENT_TOOL_RESULT,
                    {
                        "message_id": str(assistant_message_id),
                        "tool_name": call.name,
                        "summary": result.summary[:300],
                        "ok": result.ok,
                    },
                )
                if result.ok:
                    # Backfill the tool result into the running context and
                    # persist it as a tool message (placeholder backfill -> DB).
                    messages.append({"role": "tool", "content": result.summary})
                    await self._persist_message(
                        conversation_id=conversation.id,
                        org_id=conversation.org_id,
                        site_id=conversation.site_id,
                        role="tool",
                        content=f"[{call.name}] {result.summary}",
                        meta={"tool": call.name, "arguments": call.arguments},
                        created_by=user_id,
                    )
                    if result.citation:
                        citations.append({"source": result.citation, "title": result.citation_title})
                else:
                    messages.append({"role": "tool", "content": f"[{call.name}] failed: {result.error}"})

            # 5. Stream the final answer (or the notices) as deltas.
            if finish_reason == "tool_limit":
                final_text = TOOL_LIMIT_NOTICE
            elif finish_reason == "confirmation_required":
                final_text = CONFIRMATION_NOTICE
            if final_text is not None:
                completion_tokens += estimate_tokens(final_text)
                chunks = [
                    final_text[i : i + settings.delta_chars]
                    for i in range(0, len(final_text), settings.delta_chars)
                ]
                for chunk in chunks:
                    if cancel_event.is_set():
                        finish_reason = "cancelled"
                        break
                    yield _sse(ev.EVENT_DELTA, {"message_id": str(assistant_message_id), "text": chunk})
                    await self._interruptible_sleep(settings.delta_delay, cancel_event)
                if finish_reason == "stop":
                    for c in summarize_citations(citations):
                        yield _sse(ev.EVENT_CITATION, {"message_id": str(assistant_message_id), **c})

            if finish_reason == "cancelled":
                await persist_terminal_once("cancelled", "", [])
                yield _sse(
                    ev.EVENT_MESSAGE_END,
                    {
                        "message_id": str(assistant_message_id),
                        "finish_reason": "cancelled",
                        "tokens": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": prompt_tokens + completion_tokens,
                        },
                    },
                )
                return

            # 6. Persist the assistant answer and push the ex-post review entry.
            # Every answer that reaches persisted final text (stop, tool_limit,
            # confirmation_required) is pushed; cancelled/error generations have
            # no final-answer text and produce no entry.
            final_citations = summarize_citations(citations)
            await persist_terminal_once(finish_reason or "stop", final_text or "", final_citations)
            if final_text:
                self.review_queue.push(
                    conversation_id=str(conversation.id),
                    message_id=str(assistant_message_id),
                    answer_text=final_text,
                    citations=final_citations,
                    prompt_version=prompt_version,
                )

            yield _sse(
                ev.EVENT_MESSAGE_END,
                {
                    "message_id": str(assistant_message_id),
                    "finish_reason": finish_reason or "stop",
                    "tokens": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                },
            )
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnect path: the framework closes the generator
            # (GeneratorExit at the current yield point, or task cancellation).
            # Persist the cancelled terminal state WITHOUT awaiting (we are
            # inside a cancelled anyio scope where awaits re-raise immediately;
            # yielding during GeneratorExit raises RuntimeError).
            schedule_detached_persist("cancelled")
            raise
        except Exception as exc:  # never let the stream die with a bare exception
            await persist_terminal_once("error", "", [])
            yield _sse(ev.EVENT_ERROR, {"message_id": str(assistant_message_id), "code": "internal", "message": str(exc)[:200]})
            yield _sse(
                ev.EVENT_MESSAGE_END,
                {
                    "message_id": str(assistant_message_id),
                    "finish_reason": "error",
                    "tokens": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                },
            )
        finally:
            self.cancel_registry.pop(str(assistant_message_id), None)

    async def _interruptible_sleep(self, delay: float, cancel_event: asyncio.Event) -> None:
        """Sleep that wakes up immediately when the cancel flag flips."""
        sleeper = asyncio.create_task(asyncio.sleep(delay))
        waiter = asyncio.create_task(cancel_event.wait())
        try:
            done, _pending = await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (sleeper, waiter):
                if not t.done():
                    t.cancel()

    async def _persist_terminal(
        self, conversation, message_id, user_id, prompt_version, *,
        finish_reason, content, citations, prompt_tokens, completion_tokens,
    ) -> None:
        await self._persist_message(
            conversation_id=conversation.id,
            org_id=conversation.org_id,
            site_id=conversation.site_id,
            role="assistant",
            content=content,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_version=prompt_version,
            citations=citations,
            created_by=user_id,
        )
