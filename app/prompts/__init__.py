"""System prompt v1 with version record. The version is recorded with every
assistant answer (messages.prompt_version and review queue entries)."""
from __future__ import annotations

from pathlib import Path

PROMPT_VERSION = "v1"
_PROMPT_FILE = Path(__file__).parent / "system_prompt_v1.md"

SYSTEM_PROMPT_V1 = _PROMPT_FILE.read_text(encoding="utf-8")


def get_system_prompt() -> tuple[str, str]:
    """Returns (prompt_text, prompt_version)."""
    return SYSTEM_PROMPT_V1, PROMPT_VERSION
