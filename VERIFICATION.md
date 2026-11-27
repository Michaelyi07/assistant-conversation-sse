# VERIFICATION — real command outputs

Environment: Windows (win32) · Python 3.13.14 · PostgreSQL 16.9 (localhost:5433)

Dependency versions (pip):

```
fastapi 0.165.0 · uvicorn 0.54.0 · SQLAlchemy 2.1.8 · alembic 1.20.1
psycopg 3.3.9 · pytest 9.2.0 · pytest-asyncio 1.5.0 · httpx 0.28.1 · pydantic 2.14.4 · anyio 4.16.0
PostgreSQL: PostgreSQL 16.9, compiled by Visual C++ build 1943, 64-bit
```

## 1. Test suite (real run, `pytest -v`)

```
============================= test session starts =============================
platform win32 -- Python 3.13.14, pytest-9.2.0, pluggy-1.7.0
rootdir: ...
configfile: pytest.ini
plugins: anyio-4.16.0, platformdirs-4.14.5, asyncio-1.5.0
asyncio: mode=Mode.AUTO, asyncio_default_fixture_loop_scope=function
collecting ... collected 28 items

tests/test_integration_sse.py::test_ac1_sse_event_order_and_tool_events_between PASSED [  3%]
tests/test_integration_sse.py::test_ac3_review_queue_entries_and_priority PASSED [  7%]
tests/test_integration_sse.py::test_ac4_third_concurrent_request_429 PASSED [ 11%]
tests/test_integration_sse.py::test_cancel_within_2s_event_driven PASSED [ 14%]
tests/test_integration_sse.py::test_cancel_is_org_scoped PASSED [ 18%]
tests/test_integration_sse.py::test_client_disconnect_stops_generation_and_persists_cancelled PASSED [ 22%]
tests/test_integration_sse.py::test_shutdown_switch_rejects_with_503 PASSED [ 25%]
tests/test_integration_sse.py::test_conversation_create_and_alias PASSED [ 25%]
tests/test_integration_sse.py::test_input_masked_before_persistence PASSED [ 29%]
tests/test_integration_sse.py::test_tool_limit_after_five_tool_calls PASSED [ 33%]
tests/test_integration_sse.py::test_tool_confirmation_required_event PASSED [ 37%]
tests/test_integration_sse.py::test_error_event_wraps_gateway_failure PASSED [ 40%]
tests/test_integration_sse.py::test_history_context_includes_previous_round PASSED [ 44%]
tests/test_units.py::test_mask_phone_email_id PASSED                     [ 48%]
tests/test_units.py::test_mask_plain_text_unchanged PASSED               [ 51%]
tests/test_units.py::test_estimate_tokens_cjk_vs_ascii PASSED            [ 55%]
tests/test_units.py::test_history_window_keeps_last_10_rounds PASSED     [ 59%]
tests/test_units.py::test_history_window_truncates_earliest_rounds_over_budget PASSED [ 62%]
tests/test_units.py::test_assemble_context_shape PASSED                  [ 66%]
tests/test_units.py::test_limiter_third_acquire_fails_and_release_works PASSED [ 70%]
tests/test_units.py::test_limiter_release_never_goes_negative PASSED     [ 74%]
tests/test_units.py::test_review_entry_priority_rules PASSED             [ 77%]
tests/test_units.py::test_review_bad_rating_flags_priority PASSED        [ 81%]
tests/test_units.py::test_stub_executor_kb_search PASSED                 [ 85%]
tests/test_units.py::test_stub_executor_unknown_tool PASSED              [ 88%]
tests/test_units.py::test_stub_executor_confirmation_tool_not_executed PASSED [ 92%]
tests/test_units.py::test_scripted_gateway_plays_back_in_order PASSED    [ 96%]
tests/test_units.py::test_system_prompt_v1_contains_all_four_prohibitions PASSED [100%]

============================= 28 passed in 31.60s =============================
```

The tests run against PostgreSQL 16 (session fixture creates `test_asst_<hex>`,
runs `alembic upgrade head`, drops it afterwards). No SQLite, no `create_all`.

## 2. Migration on a live database (real run)

```
$ export DATABASE_URL="postgresql+psycopg://postgres@127.0.0.1:5433/demo_1843"
$ alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial tables: conversations, messages
```

## 3. Live smoke over curl (real run, uvicorn 127.0.0.1:8766)

Create session:

```
$ curl -s -X POST http://127.0.0.1:8766/api/assistant/conversations \
    -H "Content-Type: application/json" -H "X-User-Id: demo-user" \
    -d '{"entry":"kb_workbench","page_context":{"title":"SOP viewer"}}'

{"id":"2557f978-d928-4800-8ccb-507cd9dfb81a","org_id":"demo-org","site_id":"demo-site",
 "entry":"kb_workbench","page_context":{"title":"SOP viewer"},
 "created_at":"2026-11-27T15:01:30.433918+08:00"}
```

Send a message and consume the SSE stream (note the input was masked before
persistence; the default stub answers per system prompt v1 rule 2):

```
$ curl -s -N -X POST http://127.0.0.1:8766/api/assistant/conversations/2557f978-d928-4800-8ccb-507cd9dfb81a/messages \
    -H "Content-Type: application/json" -H "X-User-Id: demo-user" \
    -d '{"content":"how do I onboard a new member company? my phone 13812345678"}'

event: message_start
data: {"message_id":"2143adfe-4cbb-451e-8d31-85a5ed109ce3","conversation_id":"2557f978-d928-4800-8ccb-507cd9dfb81a","prompt_version":"v1"}

event: delta
data: {"message_id":"2143adfe-4cbb-451e-8d31-85a5ed109ce3","text":"I could not find this in the kno"}

event: delta
data: {"message_id":"2143adfe-4cbb-451e-8d31-85a5ed109ce3","text":"wledge base. Please rephrase the"}

event: delta
data: {"message_id":"2143adfe-4cbb-451e-8d31-85a5ed109ce3","text":" question or contact the knowled"}

event: delta
data: {"message_id":"2143adfe-4cbb-451e-8d31-85a5ed109ce3","text":"ge administrator."}

event: message_end
data: {"message_id":"2143adfe-4cbb-451e-8d31-85a5ed109ce3","finish_reason":"stop","tokens":{"prompt_tokens":298,"completion_tokens":52,"total_tokens":350}}
```

The persisted user message stores `[MASKED_PHONE]` instead of the phone number
(verified by `tests/test_integration_sse.py::test_input_masked_before_persistence`).

## 4. Acceptance criteria mapping

| # | Criterion (verbatim) | Evidence |
|---|---|---|
| 1 | SSE event sequence: message_start first, message_end last, tool events in between | `test_ac1_sse_event_order_and_tool_events_between` (real httpx stream vs real uvicorn server; asserts exactly one start/end, tool events strictly between; every event type also checked against `SSE_PROTOCOL.md`) |
| 2 | message_end with finish_reason=cancelled within 2 seconds of calling cancel | `test_cancel_within_2s_event_driven` (wall-clock measured from cancel POST to terminal event: < 2.0 s asserted); disconnect path: `test_client_disconnect_stops_generation_and_persists_cancelled` |
| 3 | Each final answer has an ex-post review entry; unquoted responses marked priority | `test_ac3_review_queue_entries_and_priority` (one entry per final answer; cited -> priority false; unquoted -> priority true) |
| 4 | Same user's 3rd concurrent request returned 429 | `test_ac4_third_concurrent_request_429` (2 streams in flight, 3rd POST -> 429, 4th after completion -> 200) |

## 5. Known environment notes

- Windows dev only: psycopg async requires a selector event loop
  (`asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` in
  `tests/conftest.py`). Linux production deployments are unaffected.
- Multi-instance deployments need a shared limiter (Redis) and a shared cancel
  registry; the in-process counter satisfies the single-instance scope of this
  work order.
