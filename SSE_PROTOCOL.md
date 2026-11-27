# SSE Event Protocol — Assistant Conversation Streaming (v1)

Wire format: each event is two lines + a blank line separator.

```
event: <event-type>\n
data: <compact JSON>\n
\n
```

Response headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`,
`Connection: keep-alive`, `X-Accel-Buffering: no`.

## Event types (in a typical stream)

| Event | Payload fields | Emitted when |
|---|---|---|
| `message_start` | `message_id`, `conversation_id`, `prompt_version` | First event of every generation |
| `delta` | `message_id`, `text` | Each text increment of the final answer |
| `tool_call` | `message_id`, `tool_name`, `arguments_summary` | The model requests a tool |
| `tool_confirmation_required` | `message_id`, `tool_name`, `reason` | The requested tool requires user confirmation; v1 closes the generation cleanly (tool NOT executed, `finish_reason=confirmation_required`) |
| `tool_result` | `message_id`, `tool_name`, `summary`, `ok` | Tool execution finished (summary only) |
| `citation` | `message_id`, `source`, `title` | Sources backing the final answer |
| `message_end` | `message_id`, `finish_reason`, `tokens{prompt_tokens, completion_tokens, total_tokens}` | Last event of every generation |
| `error` | `message_id`, `code`, `message` | An internal failure occurred (followed by `message_end`) |

## Ordering invariants

1. `message_start` is always the first event; exactly one per generation.
2. `message_end` is always the last event; exactly one per generation.
3. Tool events (`tool_call`, `tool_confirmation_required`, `tool_result`) appear
   strictly between `message_start` and `message_end`.
4. `delta` events stream answer text; each `text` is a text increment. On
   cancellation the already-streamed text is a partial answer (the terminal
   `message_end` carries `finish_reason=cancelled`). Citation events only
   accompany `finish_reason=stop`.

## `finish_reason` values

| Value | Meaning |
|---|---|
| `stop` | Final answer produced normally |
| `tool_limit` | 5 tool calls executed; the 6th tool request was refused |
| `confirmation_required` | A requested tool needs user confirmation; generation closed cleanly without executing it |
| `cancelled` | Cancel endpoint called or client disconnected (within 2 s) |
| `error` | Internal failure (an `error` event precedes `message_end`) |

## Endpoints

- `POST /api/assistant/conversations` — create a session, body `{"entry", "page_context"}`. 201.
- `POST /api/assistant/conversations/{id}/messages` — send a user message; returns the SSE stream above.
- `POST /api/assistant/conversations/{id}/messages/{msg_id}/cancel` — cancel an
  in-flight generation; idempotent `{"accepted": bool}`.
- `POST /api/assistant/discuss` — alias of conversation creation (order-text compatibility).

Identity headers (S2-IAM stub): `X-User-Id`, `X-Org-Id`, `X-Site-Id`.

## Example stream

```
event: message_start
data: {"message_id":"8b1f...","conversation_id":"5c2a...","prompt_version":"v1"}

event: tool_call
data: {"message_id":"8b1f...","tool_name":"search_knowledge_base","arguments_summary":"query=onboarding"}

event: tool_result
data: {"message_id":"8b1f...","tool_name":"search_knowledge_base","summary":"The quick-start SOP...","ok":true}

event: delta
data: {"message_id":"8b1f...","text":"Per the onboarding SOP, "}

event: delta
data: {"message_id":"8b1f...","text":"new members submit the intake form first."}

event: citation
data: {"message_id":"8b1f...","source":"KB-SOP-001#sec-2","title":"Member Onboarding SOP"}

event: message_end
data: {"message_id":"8b1f...","finish_reason":"stop","tokens":{"prompt_tokens":412,"completion_tokens":38,"total_tokens":450}}

```
