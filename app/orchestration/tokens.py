"""Deterministic token estimation (counts are for budgeting, not billing)."""
from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """CJK chars count ~1 token each; other chars ~1/4 token each. Min 1 for non-empty."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    other = len(text) - cjk
    return max(1, cjk + other // 4)
