"""Assistant conversation endpoints (SSE streaming + cancel + concurrency limit)."""
from __future__ import annotations

import asyncio
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity import Identity, get_identity
from app.db import session_dependency
from app.models import Conversation
from app.orchestration.pipeline import Orchestrator
from app.schemas import ConversationCreate, ConversationOut, MessageCancelAck, MessageCreate
from app.sse import events as ev

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def page_summary_from_context(page_context: dict) -> str:
    """Deterministic one-line summary of the page the user is on."""
    if not page_context:
        return "unknown page"
    title = page_context.get("title") or page_context.get("page") or "page"
    extra = {k: v for k, v in page_context.items() if k not in ("title", "page")}
    return f"{title} ({extra})" if extra else str(title)


async def _get_conversation(
    session: AsyncSession, conversation_id: uuid.UUID, identity: Identity
) -> Conversation:
    row = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.org_id == identity.org_id,
            Conversation.deleted_at.is_(None),
        )
    )
    conversation = row.scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conversation


@router.post("/conversations", response_model=ConversationOut, status_code=201)
async def create_conversation(
    body: ConversationCreate,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(session_dependency),
) -> Conversation:
    conversation = Conversation(
        org_id=identity.org_id,
        site_id=identity.site_id,
        entry=body.entry,
        page_context=body.page_context,
        created_by=identity.user_id,
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


# Order-text alias: the machine-translated brief said POST /api/assistant/discuss;
# the authoritative original wording is POST /api/assistant/conversations. Both work.
@router.post("/discuss", response_model=ConversationOut, status_code=201, include_in_schema=True)
async def create_conversation_alias(
    body: ConversationCreate,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(session_dependency),
) -> Conversation:
    return await create_conversation(body, identity, session)


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageCreate,
    request: Request,
    identity: Identity = Depends(get_identity),
) -> StreamingResponse:
    state = request.app.state
    settings = state.settings

    # Shutdown (kill) switch: reject new generations before anything else runs.
    if settings.disabled:
        raise HTTPException(status_code=503, detail="assistant is disabled by the shutdown switch")

    session_factory: async_sessionmaker[AsyncSession] = state.session_factory
    async with session_factory() as session:
        conversation = await _get_conversation(session, conversation_id, identity)

    # Concurrency limit (max N in-flight generations per user); the 3rd request
    # is rejected with 429 before any stream starts.
    limiter = state.limiter
    if not await limiter.try_acquire(identity.user_id):
        raise HTTPException(status_code=429, detail="too many concurrent generations")

    assistant_message_id = uuid.uuid4()
    cancel_event = asyncio.Event()
    state.cancel_registry[str(assistant_message_id)] = cancel_event

    orchestrator: Orchestrator = state.orchestrator

    async def event_stream() -> AsyncGenerator[str, None]:
        agen = orchestrator.run(
            conversation=conversation,
            user_text=body.content,
            user_id=identity.user_id,
            page_summary=page_summary_from_context(conversation.page_context),
            cancel_event=cancel_event,
            assistant_message_id=assistant_message_id,
            role_description=identity.role_description,
        )
        try:
            async for chunk in agen:
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnect: closing the inner generator runs its cleanup
            # path, which persists finish_reason=cancelled (no yield allowed).
            try:
                await agen.aclose()
            except Exception:
                pass
            raise
        finally:
            # Release the concurrency slot on every exit path (normal, cancelled,
            # error, client disconnect).
            limiter.release(identity.user_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/conversations/{conversation_id}/messages/{message_id}/cancel")
async def cancel_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    request: Request,
    identity: Identity = Depends(get_identity),
) -> MessageCancelAck:
    """Flip the cancel flag. Org-scoped: canceling a conversation that does not
    belong to the caller's org returns 404. Idempotent: canceling an unknown or
    already-finished message still returns 200 with accepted=false."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        await _get_conversation(session, conversation_id, identity)
    cancel_event = request.app.state.cancel_registry.get(str(message_id))
    if cancel_event is not None:
        cancel_event.set()
        return MessageCancelAck(accepted=True, message_id=str(message_id))
    return MessageCancelAck(accepted=False, message_id=str(message_id))
