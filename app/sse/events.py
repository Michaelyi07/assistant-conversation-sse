"""SSE wire format helpers.

Every event is emitted as:
    event: <type>\\n
    data: <single-line JSON>\\n
    \\n
``data`` JSON is compacted with no raw newlines inside (json.dumps never emits
literal newlines in strings; control chars are escaped).
"""
from __future__ import annotations

import json
from typing import Any

# Event types in protocol order (see SSE_PROTOCOL.md).
EVENT_MESSAGE_START = "message_start"
EVENT_DELTA = "delta"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_CONFIRMATION_REQUIRED = "tool_confirmation_required"
EVENT_TOOL_RESULT = "tool_result"
EVENT_CITATION = "citation"
EVENT_MESSAGE_END = "message_end"
EVENT_ERROR = "error"

ALL_EVENTS = (
    EVENT_MESSAGE_START,
    EVENT_DELTA,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CONFIRMATION_REQUIRED,
    EVENT_TOOL_RESULT,
    EVENT_CITATION,
    EVENT_MESSAGE_END,
    EVENT_ERROR,
)


def format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def summarize_arguments(arguments: dict) -> str:
    """Short deterministic summary of tool call arguments for the tool_call event."""
    if not arguments:
        return "{}"
    parts = [f"{k}={str(v)[:60]}" for k, v in sorted(arguments.items())]
    return ", ".join(parts)[:200]
