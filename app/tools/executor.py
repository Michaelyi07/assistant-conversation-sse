"""Tool executor interface + stub.

Connection point (S5-ASST-003): the tool-executor work order implements
``ToolExecutor``; this module ships the interface contract and a deterministic
stub (one fabricated knowledge-base search tool) until S5-ASST-003 lands.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolResult:
    ok: bool
    summary: str
    citation: str | None = None
    citation_title: str | None = None
    requires_confirmation: bool = False
    error: str | None = None
    meta: dict = field(default_factory=dict)


class ToolExecutor(ABC):
    @abstractmethod
    async def execute(self, name: str, arguments: dict) -> ToolResult:
        """Execute one tool call and return a summarized result."""


class StubToolExecutor(ToolExecutor):
    """Deterministic stub with one fabricated KB search tool.

    v1 behavior for tools that require confirmation: the executor *reports*
    ``requires_confirmation=True``; the orchestrator emits the
    ``tool_confirmation_required`` SSE event and does NOT execute the tool
    (no client confirmation loop is in scope for this work order).
    """

    def __init__(
        self,
        tools_requiring_confirmation: set[str] | None = None,
        kb_answer: str = "The quick-start SOP for new member onboarding is defined in the knowledge base.",
        kb_citation: str = "KB-SOP-001#sec-2",
        kb_title: str = "Member Onboarding SOP",
        delay: float = 0.0,
    ) -> None:
        self.tools_requiring_confirmation = tools_requiring_confirmation or set()
        self.kb_answer = kb_answer
        self.kb_citation = kb_citation
        self.kb_title = kb_title
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, name: str, arguments: dict) -> ToolResult:
        self.calls.append((name, arguments))
        if name in self.tools_requiring_confirmation:
            return ToolResult(ok=False, summary="", requires_confirmation=True,
                              error="tool requires user confirmation")
        if name == "search_knowledge_base":
            if self.delay:
                import asyncio

                await asyncio.sleep(self.delay)
            return ToolResult(
                ok=True,
                summary=self.kb_answer,
                citation=self.kb_citation,
                citation_title=self.kb_title,
                meta={"query": arguments.get("query", "")},
            )
        return ToolResult(ok=False, summary="", error=f"unknown tool: {name}")
