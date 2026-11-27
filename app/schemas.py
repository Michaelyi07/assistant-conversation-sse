"""Pydantic request/response schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    entry: str = Field(..., min_length=1, max_length=64, description="Conversation entry point")
    page_context: dict[str, Any] = Field(default_factory=dict)


class ConversationOut(BaseModel):
    id: uuid.UUID
    org_id: str
    site_id: str
    entry: str
    page_context: dict[str, Any]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=8000)


class MessageCancelAck(BaseModel):
    accepted: bool
    message_id: str
