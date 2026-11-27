"""Ex-post review queue stub (S5-GW AI review queue connection point).

Every FINAL assistant answer is pushed here as an "ex post review" entry
(non-blocking, never delays the SSE stream). Entries are flagged priority when
the answer has no citations, hits a sensitive word, or receives a bad rating.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone


def answer_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class ReviewEntry:
    conversation_id: str
    message_id: str
    answer_digest: str
    citations_present: bool
    priority: bool
    flags: dict = field(default_factory=dict)
    prompt_version: str = "v1"
    rating: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ReviewQueueStub:
    """In-memory review queue pile. ``push`` never blocks the generation path.

    Connection point (S5-GW): replace this stub with the real AI review queue
    client; ``push`` maps to the queue enqueue operation.
    """

    def __init__(self, sensitive_words: list[str] | None = None) -> None:
        self.entries: list[ReviewEntry] = []
        self.sensitive_words = sensitive_words or []

    def push(
        self,
        conversation_id: str,
        message_id: str,
        answer_text: str,
        citations: list,
        prompt_version: str,
    ) -> ReviewEntry:
        """Non-blocking push of one ex-post review entry for a final answer."""
        citations_present = bool(citations)
        hits = [w for w in self.sensitive_words if w.lower() in answer_text.lower()]
        flags = {"no_citations": not citations_present, "sensitive_hits": hits}
        priority = (not citations_present) or bool(hits)
        entry = ReviewEntry(
            conversation_id=conversation_id,
            message_id=message_id,
            answer_digest=answer_digest(answer_text),
            citations_present=citations_present,
            priority=priority,
            flags=flags,
            prompt_version=prompt_version,
        )
        self.entries.append(entry)
        return entry

    def mark_rating(self, message_id: str, rating: int) -> ReviewEntry | None:
        """Record a member rating for an answer; rating < 0 flags priority review."""
        for entry in reversed(self.entries):
            if entry.message_id == message_id:
                entry.rating = rating
                if rating < 0:
                    entry.flags["bad_rating"] = True
                    entry.priority = True
                return entry
        return None

    def snapshot(self) -> list[ReviewEntry]:
        return list(self.entries)
