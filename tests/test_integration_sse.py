"""Integration tests over a real uvicorn server + real httpx streams against
PostgreSQL 16 (migrated via Alembic). Covers the four acceptance criteria:
  AC1 SSE event order, AC2 cancel within 2s, AC3 review-queue entries,
  AC4 3rd concurrent request -> 429.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx

from app.gateway.stub import GatewayResponse, GatewayToolCall, ScriptedGateway
from app.tools.executor import StubToolExecutor
from tests.conftest import parse_sse, rewire, sync_db

H = {"X-User-Id": "u1", "X-Org-Id": "org-1", "X-Site-Id": "site-1"}
LONG_TEXT = "This is a long streamed answer for cancel testing. " * 40  # ~2000 chars


async def create_conversation(client: httpx.AsyncClient, headers=H, entry="web", page_context=None) -> str:
    resp = await client.post(
        "/api/assistant/conversations",
        json={"entry": entry, "page_context": page_context or {"title": "KB workbench"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def consume_stream(client: httpx.AsyncClient, url: str, **kwargs):
    """Consume an SSE stream to completion; returns (status, events)."""
    buf = ""
    async with client.stream("POST", url, **kwargs) as resp:
        status = resp.status_code
        if status != 200:
            return status, [( "error_body", {"text": (await resp.aread()).decode()} )]
        async for chunk in resp.aiter_bytes():
            buf += chunk.decode("utf-8", errors="replace")
    return status, parse_sse(buf)


async def start_stream(client: httpx.AsyncClient, cid: str, content="hello", headers=H):
    return asyncio.create_task(
        consume_stream(
            client,
            f"/api/assistant/conversations/{cid}/messages",
            json={"content": content},
            headers=headers,
        )
    )


def assistant_rows(test_db_url, cid):
    with sync_db(test_db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT role, content, finish_reason, prompt_version, citations FROM messages "
            "WHERE conversation_id = %s AND role = 'assistant' ORDER BY created_at",
            (cid,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------- AC1: event order

async def test_ac1_sse_event_order_and_tool_events_between(clean_state, server, test_db_url):
    gateway = ScriptedGateway([
        GatewayResponse(tool_call=GatewayToolCall(name="search_knowledge_base", arguments={"query": "onboarding"})),
        GatewayResponse(text="Per the onboarding SOP, new members submit the intake form first. " * 3),
    ])
    executor = StubToolExecutor()
    rewire(clean_state, gateway=gateway, executor=executor)
    client = httpx.AsyncClient(base_url=server)
    try:
        cid = await create_conversation(client)
        status, events = await consume_stream(
            client, f"/api/assistant/conversations/{cid}/messages", json={"content": "how to onboard?"}, headers=H
        )
        assert status == 200
        kinds = [e for e, _ in events]
        assert kinds[0] == "message_start"
        assert kinds[-1] == "message_end"
        assert kinds.count("message_start") == 1
        assert kinds.count("message_end") == 1
        # Tool events strictly between start and end.
        tool_idx = [i for i, e in enumerate(kinds) if e in ("tool_call", "tool_result", "tool_confirmation_required")]
        assert tool_idx, "expected tool events in between"
        assert all(0 < i < len(kinds) - 1 for i in tool_idx)
        assert kinds[tool_idx[0]] == "tool_call"
        assert kinds[tool_idx[-1]] == "tool_result"
        # Delta events are text increments; citation event present.
        assert "delta" in kinds and "citation" in kinds
        for e, data in events:
            if e == "delta":
                assert isinstance(data["text"], str) and data["text"]
        end = events[-1][1]
        assert end["finish_reason"] == "stop"
        assert end["tokens"]["total_tokens"] == end["tokens"]["prompt_tokens"] + end["tokens"]["completion_tokens"]
        start = events[0][1]
        assert start["prompt_version"] == "v1"
        # Every event type is documented in SSE_PROTOCOL.md (doc/code cannot drift).
        protocol = (Path(__file__).resolve().parents[1] / "SSE_PROTOCOL.md").read_text(encoding="utf-8")
        for e in set(kinds):
            assert f"`{e}`" in protocol, f"event {e} missing from SSE_PROTOCOL.md"
    finally:
        await client.aclose()


async def test_ac3_review_queue_entries_and_priority(clean_state, server, test_db_url):
    from app.review.queue import ReviewQueueStub

    queue = ReviewQueueStub(sensitive_words=["layoff"])
    gateway = ScriptedGateway([
        GatewayResponse(tool_call=GatewayToolCall(name="search_knowledge_base", arguments={"query": "sop"})),
        GatewayResponse(text="Answer with a source from the tool result."),
        GatewayResponse(text="I could not find this in the knowledge base."),
    ])
    executor = StubToolExecutor()
    rewire(clean_state, gateway=gateway, executor=executor, review_queue=queue)
    client = httpx.AsyncClient(base_url=server)
    try:
        cid = await create_conversation(client)
        url = f"/api/assistant/conversations/{cid}/messages"
        _, ev1 = await consume_stream(client, url, json={"content": "q1"}, headers=H)
        assert ev1[-1][1]["finish_reason"] == "stop"
        _, ev2 = await consume_stream(client, url, json={"content": "q2"}, headers=H)
        assert ev2[-1][1]["finish_reason"] == "stop"
        entries = queue.snapshot()
        assert len(entries) == 2  # one ex-post entry per final answer
        by_msg = {e.message_id: e for e in entries}
        msg1 = ev1[0][1]["message_id"]
        assert by_msg[msg1].citations_present is True
        assert by_msg[msg1].priority is False
        msg2 = ev2[0][1]["message_id"]
        assert by_msg[msg2].citations_present is False
        assert by_msg[msg2].priority is True  # unquoted -> priority review
        # Prompt version recorded with each entry.
        assert all(e.prompt_version == "v1" for e in entries)
        # The entries reference persisted assistant messages.
        rows = assistant_rows(test_db_url, cid)
        assert len(rows) == 2
        assert all(r[3] == "v1" for r in rows)
    finally:
        await client.aclose()


async def test_ac4_third_concurrent_request_429(clean_state, server):
    gateway = ScriptedGateway([
        GatewayResponse(text=LONG_TEXT),
        GatewayResponse(text=LONG_TEXT),
        GatewayResponse(text="short answer"),
    ])
    rewire(clean_state, gateway=gateway, settings_overrides={"delta_delay": 0.15})
    client = httpx.AsyncClient(base_url=server)
    try:
        cid = await create_conversation(client)
        url = f"/api/assistant/conversations/{cid}/messages"
        t1 = await start_stream(client, cid, content="first")
        await asyncio.sleep(0.5)
        t2 = await start_stream(client, cid, content="second")
        await asyncio.sleep(0.5)
        assert not t1.done() and not t2.done()  # first two still streaming
        resp = await client.post(url, json={"content": "third"}, headers=H)
        assert resp.status_code == 429  # AC4: same user's 3rd concurrent request
        # Wind down: cancel both in-flight generations, then a new request succeeds.
        _, ev1 = await t1
        _, ev2 = await t2
        assert ev1[-1][1]["finish_reason"] == "stop" or ev1[-1][1]["finish_reason"] == "cancelled"
        # Slots released -> 4th request is accepted (not 429).
        status, events = await consume_stream(client, url, json={"content": "fourth"}, headers=H)
        assert status == 200
        assert events[-1][1]["finish_reason"] == "stop"
    finally:
        await client.aclose()


async def test_cancel_within_2s_event_driven(clean_state, server, test_db_url):
    """AC2 exact flow: start slow stream, read message_start + first deltas
    incrementally, call cancel, assert message_end(cancelled) within 2s."""
    gateway = ScriptedGateway([GatewayResponse(text=LONG_TEXT)])
    rewire(clean_state, gateway=gateway, settings_overrides={"delta_delay": 0.15})
    client = httpx.AsyncClient(base_url=server, timeout=30)
    try:
        cid = await create_conversation(client)
        url = f"/api/assistant/conversations/{cid}/messages"
        events: list[tuple[str, dict]] = []
        message_id = None

        async def drive():
            nonlocal message_id
            buf = ""
            async with client.stream("POST", url, json={"content": "slow please"}, headers=H) as resp:
                assert resp.status_code == 200
                async for chunk in resp.aiter_bytes():
                    buf += chunk.decode("utf-8", errors="replace")
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        parsed = parse_sse(block + "\n\n")
                        if parsed:
                            events.extend(parsed)
                            if parsed[0][0] == "message_start":
                                message_id = parsed[0][1]["message_id"]

        drive_task = asyncio.create_task(drive())
        deadline = time.monotonic() + 5
        while message_id is None:
            assert time.monotonic() < deadline, "never received message_start"
            await asyncio.sleep(0.05)
        t0 = time.monotonic()
        ack = await client.post(f"{url}/{message_id}/cancel", headers=H)
        assert ack.status_code == 200
        assert ack.json()["accepted"] is True
        await asyncio.wait_for(drive_task, timeout=5)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"cancel SLA violated: {elapsed:.2f}s"
        assert events[-1][0] == "message_end"
        assert events[-1][1]["finish_reason"] == "cancelled"
        # delta events preceded the terminal event (stream was mid-generation)
        assert any(e == "delta" for e, _ in events)
        rows = assistant_rows(test_db_url, cid)
        assert rows[-1][2] == "cancelled"  # persisted finish_reason
        # Cancel flag registry entry cleaned up.
        assert message_id not in clean_state.state.cancel_registry
        # Slot released: a new generation is accepted immediately.
        status, _ = await consume_stream(client, url, json={"content": "again"}, headers=H)
        assert status == 200
    finally:
        await client.aclose()


async def test_cancel_is_org_scoped(clean_state, server):
    gateway = ScriptedGateway([GatewayResponse(text=LONG_TEXT)])
    rewire(clean_state, gateway=gateway, settings_overrides={"delta_delay": 0.15})
    client = httpx.AsyncClient(base_url=server, timeout=30)
    try:
        cid = await create_conversation(client)  # created under X-Org-Id: org-1
        other_org = {**H, "X-Org-Id": "org-other"}
        resp = await client.post(
            f"/api/assistant/conversations/{cid}/messages/00000000-0000-0000-0000-000000000000/cancel",
            headers=other_org,
        )
        assert resp.status_code == 404  # cross-org cancel is rejected
        # Same-org cancel of an unknown/finished message is idempotent.
        resp2 = await client.post(
            f"/api/assistant/conversations/{cid}/messages/00000000-0000-0000-0000-000000000000/cancel",
            headers=H,
        )
        assert resp2.status_code == 200
        assert resp2.json()["accepted"] is False
    finally:
        await client.aclose()


async def test_client_disconnect_stops_generation_and_persists_cancelled(clean_state, server, test_db_url):
    gateway = ScriptedGateway([GatewayResponse(text=LONG_TEXT)])
    rewire(clean_state, gateway=gateway, settings_overrides={"delta_delay": 0.2})
    client = httpx.AsyncClient(base_url=server, timeout=30)
    try:
        cid = await create_conversation(client)
        url = f"/api/assistant/conversations/{cid}/messages"
        async with client.stream("POST", url, json={"content": "disconnect me"}, headers=H) as resp:
            assert resp.status_code == 200
            stream = resp.aiter_bytes()
            await anext(stream)  # read one chunk, then abandon the connection
        # Exiting the context closes the TCP connection abruptly.
        await asyncio.sleep(1.5)  # allow the server to notice and wind down
        rows = assistant_rows(test_db_url, cid)
        assert rows, "assistant message should be persisted"
        assert rows[-1][2] == "cancelled"
    finally:
        await client.aclose()


async def test_shutdown_switch_rejects_with_503(clean_state, server):
    clean_state.state.settings.disabled = True
    client = httpx.AsyncClient(base_url=server)
    try:
        cid = await create_conversation(client)
        resp = await client.post(
            f"/api/assistant/conversations/{cid}/messages", json={"content": "hi"}, headers=H
        )
        assert resp.status_code == 503
        assert "shutdown switch" in resp.json()["detail"]
    finally:
        clean_state.state.settings.disabled = False
        await client.aclose()


async def test_conversation_create_and_alias(clean_state, server, test_db_url):
    client = httpx.AsyncClient(base_url=server)
    try:
        resp = await client.post(
            "/api/assistant/conversations",
            json={"entry": "kb_workbench", "page_context": {"title": "SOP viewer", "doc_id": "SOP-7"}},
            headers=H,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["entry"] == "kb_workbench"
        assert body["page_context"]["doc_id"] == "SOP-7"
        assert body["org_id"] == "org-1" and body["site_id"] == "site-1"
        # Alias endpoint (machine-translated order text) behaves identically.
        resp2 = await client.post(
            "/api/assistant/discuss",
            json={"entry": "web", "page_context": {}},
            headers=H,
        )
        assert resp2.status_code == 201
        # Unknown conversation -> 404.
        resp3 = await client.post(
            "/api/assistant/conversations/00000000-0000-0000-0000-000000000000/messages",
            json={"content": "hi"}, headers=H,
        )
        assert resp3.status_code == 404
    finally:
        await client.aclose()


async def test_input_masked_before_persistence(clean_state, server, test_db_url):
    gateway = ScriptedGateway([GatewayResponse(text="Answer.")])
    rewire(clean_state, gateway=gateway)
    client = httpx.AsyncClient(base_url=server)
    try:
        cid = await create_conversation(client)
        status, events = await consume_stream(
            client,
            f"/api/assistant/conversations/{cid}/messages",
            json={"content": "my phone is 13812345678, email me at x.y@corp.com"},
            headers=H,
        )
        assert status == 200
        with sync_db(test_db_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT content FROM messages WHERE conversation_id = %s AND role = 'user'", (cid,))
            (content,) = cur.fetchone()
        assert "13812345678" not in content
        assert "x.y@corp.com" not in content
        assert "[MASKED_PHONE]" in content and "[MASKED_EMAIL]" in content
    finally:
        await client.aclose()


async def test_tool_limit_after_five_tool_calls(clean_state, server, test_db_url):
    gateway = ScriptedGateway(
        [GatewayResponse(tool_call=GatewayToolCall(name="search_knowledge_base", arguments={"query": f"q{i}"})) for i in range(7)]
    )
    executor = StubToolExecutor()
    rewire(clean_state, gateway=gateway, executor=executor)
    client = httpx.AsyncClient(base_url=server)
    try:
        cid = await create_conversation(client)
        status, events = await consume_stream(
            client, f"/api/assistant/conversations/{cid}/messages", json={"content": "loop"}, headers=H
        )
        kinds = [e for e, _ in events]
        assert kinds.count("tool_call") == 5  # 5 tool calls executed, 6th request refused
        assert kinds.count("tool_result") == 5
        assert "delta" in kinds  # the tool-limit notice is streamed to the client
        assert kinds[-1] == "message_end"
        assert events[-1][1]["finish_reason"] == "tool_limit"
        rows = assistant_rows(test_db_url, cid)
        assert rows[-1][2] == "tool_limit"
        # Persisted content matches exactly what was streamed (tool-limit notice).
        streamed = "".join(d["text"] for e, d in events if e == "delta")
        with sync_db(test_db_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT content FROM messages WHERE conversation_id = %s AND role = 'assistant' "
                "ORDER BY created_at DESC LIMIT 1",
                (cid,),
            )
            (content,) = cur.fetchone()
        assert content == streamed
        assert content
    finally:
        await client.aclose()


async def test_tool_confirmation_required_event(clean_state, server):
    gateway = ScriptedGateway([
        GatewayResponse(tool_call=GatewayToolCall(name="send_external_message", arguments={"to": "boss"})),
        GatewayResponse(text="The external send was not executed because it requires confirmation."),
    ])
    executor = StubToolExecutor(tools_requiring_confirmation={"send_external_message"})
    rewire(clean_state, gateway=gateway, executor=executor)
    client = httpx.AsyncClient(base_url=server)
    try:
        cid = await create_conversation(client)
        status, events = await consume_stream(
            client, f"/api/assistant/conversations/{cid}/messages", json={"content": "notify the boss"}, headers=H
        )
        kinds = [e for e, _ in events]
        assert "tool_confirmation_required" in kinds
        i = kinds.index("tool_confirmation_required")
        assert kinds[i - 1] == "tool_call"  # confirmation follows its tool_call
        assert kinds[-1] == "message_end"
        assert events[-1][1]["finish_reason"] == "stop"
        # The confirmed tool never produced a tool_result.
        assert "tool_result" not in kinds
    finally:
        await client.aclose()


async def test_error_event_wraps_gateway_failure(clean_state, server, test_db_url):
    class BoomGateway(ScriptedGateway):
        async def complete(self, messages, tools):
            raise RuntimeError("gateway exploded")

    rewire(clean_state, gateway=BoomGateway([]))
    client = httpx.AsyncClient(base_url=server)
    try:
        cid = await create_conversation(client)
        status, events = await consume_stream(
            client, f"/api/assistant/conversations/{cid}/messages", json={"content": "boom"}, headers=H
        )
        kinds = [e for e, _ in events]
        assert kinds[0] == "message_start"
        assert "error" in kinds
        assert kinds[-1] == "message_end"
        assert events[-1][1]["finish_reason"] == "error"
        rows = assistant_rows(test_db_url, cid)
        assert rows[-1][2] == "error"
    finally:
        await client.aclose()


async def test_history_context_includes_previous_round(clean_state, server, test_db_url):
    gateway = ScriptedGateway([
        GatewayResponse(text="First answer."),
        GatewayResponse(text="Second answer."),
    ])
    rewire(clean_state, gateway=gateway)
    client = httpx.AsyncClient(base_url=server)
    try:
        cid = await create_conversation(client)
        url = f"/api/assistant/conversations/{cid}/messages"
        await consume_stream(client, url, json={"content": "first question"}, headers=H)
        await consume_stream(client, url, json={"content": "second question"}, headers=H)
        # The gateway saw the previous round in context on the 2nd call.
        second_call_messages = gateway.calls[1]["messages"]
        user_contents = [m["content"] for m in second_call_messages if m["role"] == "user"]
        assert "first question" in user_contents
        assert user_contents[-1] == "second question"
    finally:
        await client.aclose()
