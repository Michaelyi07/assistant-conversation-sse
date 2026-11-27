"""Model gateway interface + stub.

Connection point (S5-GW): the real model gateway work order implements
``ModelGateway``; this module ships the interface contract and a deterministic
scripted stub for development, testing, and integration until S5-GW lands.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class GatewayToolCall:
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class GatewayResponse:
    """One model turn. Either a final text answer or a request to call tools."""
    text: str | None = None
    tool_call: GatewayToolCall | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ModelGateway(ABC):
    @abstractmethod
    async def complete(self, messages: list[dict], tools: list[dict]) -> GatewayResponse:
        """One orchestration turn: given the message list and available tool specs,
        return either a final answer (text) or a tool request."""


class DefaultStubGateway(ModelGateway):
    """Deterministic no-tool stub used as the default until S5-GW connects."""

    async def complete(self, messages: list[dict], tools: list[dict]) -> GatewayResponse:
        return GatewayResponse(
            text="I could not find this in the knowledge base. "
                 "Please rephrase the question or contact the knowledge administrator.",
            prompt_tokens=sum(len(m["content"]) // 4 + 1 for m in messages),
            completion_tokens=24,
        )


class ScriptedGateway(ModelGateway):
    """Plays back a script of GatewayResponse turns in order (used by tests and demos)."""

    def __init__(self, script: list[GatewayResponse]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def complete(self, messages: list[dict], tools: list[dict]) -> GatewayResponse:
        self.calls.append({"messages": messages, "tools": tools})
        if not self._script:
            return GatewayResponse(text="Script exhausted.")
        return self._script.pop(0)
