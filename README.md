# Assistant Conversation Orchestration API

SSE streaming conversation interface with a tool-call loop for the employee AI
assistant of the member space platform. This work order implements:

- `POST /api/assistant/conversations` — create a session (`entry`, `page_context`)
- `POST /api/assistant/conversations/{id}/messages` — send a user message, returns the SSE event stream
  (`message_start`, `delta`, `tool_call`, `tool_confirmation_required`, `tool_result`, `citation`,
  `message_end`, `error` — protocol details in [SSE_PROTOCOL.md](SSE_PROTOCOL.md))
- `POST /api/assistant/conversations/{id}/messages/{msg_id}/cancel` — cancel an in-flight
  generation (stops within 2 s, `finish_reason=cancelled`); client disconnects behave the same
- `POST /api/assistant/discuss` — alias of conversation creation (order-text compatibility)

Orchestration pipeline: shutdown switch -> input masking (PII placeholders; original text is
never persisted) -> context assembly (system prompt, user role, page summary, last 10 rounds of
history, earliest-round truncation over the token budget) -> model gateway (stub) -> tool
executor loop (max 5 tool calls, results backfilled as tool messages and persisted) -> final
answer streamed as text deltas -> persisted -> ex-post review queue entry.

Every final answer is pushed to the review queue as an ex-post review entry (non-blocking);
answers without citations or with sensitive-word hits are flagged priority. The system prompt
v1 enforces: answer only from tool results and knowledge-base citations; say "cannot find it"
rather than guessing; never claim anything was sent or notified externally; never compute
amounts. `messages.prompt_version` records the prompt version used for each answer.

The same user can have at most 2 concurrent generations; the 3rd concurrent request receives
HTTP 429 before any stream starts (counters are released on completion, cancellation, error,
and disconnect).

## Stack

Python 3.11+, FastAPI, SQLAlchemy 2 (async, psycopg 3), Alembic, PostgreSQL 16.
No third-party SDK that sends data overseas is used.

## Run

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg://postgres@127.0.0.1:5433/mydb"
alembic upgrade head          # create tables (no create_all anywhere)
uvicorn app.main:create_app --factory --port 8000
```

Configuration is read from environment variables only:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres@127.0.0.1:5433/postgres` | PostgreSQL 16 connection |
| `ASSISTANT_DISABLED` | `0` | Shutdown (kill) switch: `1` rejects new generations with HTTP 503 |
| `CONTEXT_TOKEN_BUDGET` | `4000` | Context token budget (earliest rounds truncated first) |
| `HISTORY_MAX_ROUNDS` | `10` | History rounds kept in context |
| `MAX_TOOL_CALLS` | `5` | Tool-call loop limit |
| `MAX_CONCURRENT_GENERATIONS` | `2` | Per-user in-flight generation limit |
| `DELTA_DELAY` / `DELTA_CHARS` | `0.05` / `32` | Stub-gateway streaming pace (dev/demo only) |
| `SENSITIVE_WORDS` | comma list | Words that flag priority review |

## Test

```bash
pytest -v
```

On Windows the test suite sets a selector event loop policy (psycopg async
requires it; see `tests/conftest.py`). Linux production deployments are
unaffected.

The suite runs against a real PostgreSQL 16 server (default `127.0.0.1:5433`, user `postgres`,
trust auth): it creates an ephemeral database, migrates it with Alembic (`alembic upgrade head`,
never `create_all`), starts the app under a real uvicorn server, drives it with real httpx
streams, and drops the ephemeral database afterwards. Set `DATABASE_URL` to point elsewhere.

## Integration points (interface stubs)

| Dependency | Work order | Stub in this repo | Connection point |
|---|---|---|---|
| Model gateway + AI review queue | S5-GW | `app/gateway/stub.py` (`ModelGateway`, scripted/`DefaultStubGateway`) and `app/review/queue.py` (`ReviewQueueStub`) | Implement `ModelGateway.complete` / replace the review queue client |
| Tool executor | S5-ASST-003 | `app/tools/executor.py` (`StubToolExecutor`, one fabricated KB search tool) | Implement `ToolExecutor.execute` |
| Identity, roles, data scope | S2-IAM | `app/api/identity.py` (`X-User-Id` / `X-Org-Id` / `X-Site-Id` headers) | Replace `get_identity` with real IAM dependency |

All stubs are deterministic and documented in their docstrings; no real model, no real
external service is contacted, and all data in tests/stubs is fabricated.

## v1 scope notes

- `tool_confirmation_required`: emitted when the executor reports a tool requires
  confirmation. v1 has no client confirmation loop, so the generation closes
  cleanly (`finish_reason=confirmation_required`) with an explanatory answer;
  the tool is NOT executed.
- All persisted content (user input, tool results, answers) passes the masking
  layer before it reaches the database.
- Review entries are pushed for every generation that reaches persisted final
  text (stop, tool_limit, confirmation_required); cancelled and error
  generations have no final-answer text and produce no entry.
- Client disconnect cannot receive a terminal event by definition; the server
  persists `finish_reason=cancelled` for the assistant message (see
  `Orchestrator` disconnect path).

## Layout

```
app/
  main.py            # application factory (dependency injection for stubs)
  config.py          # env-only settings
  models.py          # SQLAlchemy 2 models (UUID id, org_id, site_id, soft delete)
  schemas.py         # Pydantic request/response schemas
  db.py              # async engine/session
  api/assistant.py   # conversation/message/cancel endpoints
  api/identity.py    # S2-IAM stub
  orchestration/     # pipeline, context assembly, masking, tokens, limiter
  gateway/stub.py    # S5-GW model gateway stub
  tools/executor.py  # S5-ASST-003 tool executor stub
  review/queue.py    # S5-GW review queue stub
  sse/events.py      # SSE wire format helpers
  prompts/           # system prompt v1 + version record
alembic/             # migrations (0001: conversations, messages)
tests/               # unit + integration suite (real PG16 + real uvicorn)
SSE_PROTOCOL.md      # event protocol contract (validated by tests)
VERIFICATION.md      # real command outputs on this machine
```
