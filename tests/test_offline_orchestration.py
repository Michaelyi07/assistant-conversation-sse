"""Offline orchestration tests (in-memory persistence): no database, no server,
no network. Exercises the real Orchestrator state machine deterministically."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.config import Settings
from app.gateway.stub import GatewayResponse, GatewayToolCall, ScriptedGateway
from app.orchestration.limiter import UserConcurrencyLimiter
from app.orchestration.pipeline import CONFIRMATION_NOTICE, TOOL_LIMIT_NOTICE, Orchestrator
from app.review.queue import ReviewQueueStub
from app.tools.executor import StubToolExecutor
from tests.conftest import parse_sse


def make_orchestrator(gateway, executor) -> tuple[Orchestrator, SimpleNamespace]:
    settings = Settings()
    orch = Orchestrator(
        settings=settings,
        gateway=gateway,
        executor=executor,
        review_queue=ReviewQueueStub(sensitive_words=["layoff"]),
        limiter=UserConcurrencyLimiter(2),
        cancel_registry={},
        session_factory=None,  # in-memory persistence mode
    )
    conversation = SimpleNamespace(id=uuid.uuid4(), org_id="org-1", site_id="site-1")
    return orch, conversation


async def collect(orch, conversation, text="question", cancel_event=None):
    cancel_event = cancel_event or asyncio.Event()
    events = []
    async for chunk in orch.run(
        conversation, text, "user-1", "page X", cancel_event, uuid.uuid4()
    ):
        events.extend(parse_sse(chunk))
    return events


async def test_offline_orchestration_event_sequence():
    gateway = ScriptedGateway([
        GatewayResponse(tool_call=GatewayToolCall(name="search_knowledge_base", arguments={"query": "q"})),
        GatewayResponse(text="Found the answer citing the tool result."),
    ])
    orch, conv = make_orchestrator(gateway, StubToolExecutor())
    events = await collect(orch, conv)
    kinds = [e for e, _ in events]
    assert kinds[0] == "message_start" and kinds[-1] == "message_end"
    assert kinds.count("message_start") == 1 and kinds.count("message_end") == 1
    assert "tool_call" in kinds and "tool_result" in kinds and "citation" in kinds
    tool_idx = [i for i, e in enumerate(kinds) if e.startswith("tool")]
    assert all(0 < i < len(kinds) - 1 for i in tool_idx)
    assert events[-1][1]["finish_reason"] == "stop"
    # In-memory persistence: user + tool + assistant messages; assistant content
    # matches exactly the streamed text.
    roles = [m["role"] for m in orch._memory_messages]
    assert roles.count("user") == 1 and roles.count("tool") == 1 and roles.count("assistant") == 1
    streamed = "".join(d["text"] for e, d in events if e == "delta")
    assert [m for m in orch._memory_messages if m["role"] == "assistant"][0]["content"] == streamed


async def test_offline_orchestration_tool_limit():
    gateway = ScriptedGateway([
        GatewayResponse(tool_call=GatewayToolCall(name="search_knowledge_base", arguments={"query": f"q{i}"}))
        for i in range(7)
    ])
    orch, conv = make_orchestrator(gateway, StubToolExecutor())
    events = await collect(orch, conv)
    kinds = [e for e, _ in events]
    assert kinds.count("tool_call") == 5  # 6th tool request refused
    assert events[-1][1]["finish_reason"] == "tool_limit"
    streamed = "".join(d["text"] for e, d in events if e == "delta")
    assert streamed == TOOL_LIMIT_NOTICE


async def test_offline_orchestration_confirmation_closes_generation():
    gateway = ScriptedGateway([
        GatewayResponse(tool_call=GatewayToolCall(name="send_message", arguments={"to": "x"})),
        GatewayResponse(text="this second turn must never be reached"),
    ])
    orch, conv = make_orchestrator(
        gateway, StubToolExecutor(tools_requiring_confirmation={"send_message"})
    )
    events = await collect(orch, conv)
    kinds = [e for e, _ in events]
    assert "tool_confirmation_required" in kinds
    assert events[-1][1]["finish_reason"] == "confirmation_required"
    streamed = "".join(d["text"] for e, d in events if e == "delta")
    assert streamed == CONFIRMATION_NOTICE  # second scripted turn never reached
    # The confirmation notice IS a persisted final answer -> one review entry
    # (no citations -> priority review).
    entries = orch.review_queue.snapshot()
    assert len(entries) == 1
    assert entries[0].priority is True and entries[0].citations_present is False


async def test_offline_orchestration_masks_user_and_tool_content():
    gateway = ScriptedGateway([
        GatewayResponse(tool_call=GatewayToolCall(name="search_knowledge_base", arguments={"query": "q"})),
        GatewayResponse(text="Answer."),
    ])
    executor = StubToolExecutor(
        kb_answer="Contact the member at 13911112222 for follow-up.",
        kb_citation="KB-9#1",
    )
    orch, conv = make_orchestrator(gateway, executor)
    events = await collect(orch, conv, text="call me at 13812345678")
    stored_user = [m for m in orch._memory_messages if m["role"] == "user"][0]["content"]
    stored_tool = [m for m in orch._memory_messages if m["role"] == "tool"][0]["content"]
    assert "13812345678" not in stored_user and "[MASKED_PHONE]" in stored_user
    assert "13911112222" not in stored_tool and "[MASKED_PHONE]" in stored_tool


async def test_offline_orchestration_cancel_flag():
    gateway = ScriptedGateway([GatewayResponse(text="chunk " * 400)])
    orch, conv = make_orchestrator(gateway, StubToolExecutor())
    cancel_event = asyncio.Event()
    cancel_event.set()  # cancelled before any gateway call
    events = await collect(orch, conv, cancel_event=cancel_event)
    kinds = [e for e, _ in events]
    assert kinds[0] == "message_start"
    assert kinds[-1] == "message_end"
    assert events[-1][1]["finish_reason"] == "cancelled"
    assert "delta" not in kinds
    assistant_rows = [m for m in orch._memory_messages if m["role"] == "assistant"]
    assert assistant_rows[0]["finish_reason"] == "cancelled"
    assert conv.id is not None


async def test_offline_orchestration_tool_limit_pushes_review_entry():
    gateway = ScriptedGateway([
        GatewayResponse(tool_call=GatewayToolCall(name="search_knowledge_base", arguments={"query": "q"}))
        for _ in range(7)
    ])
    orch, conv = make_orchestrator(gateway, StubToolExecutor())
    events = await collect(orch, conv)
    assert events[-1][1]["finish_reason"] == "tool_limit"
    # The tool-limit notice is a persisted final answer -> one review entry
    # (citations from the 5 executed tool results are attached).
    entries = orch.review_queue.snapshot()
    assert len(entries) == 1 and entries[0].citations_present is True
