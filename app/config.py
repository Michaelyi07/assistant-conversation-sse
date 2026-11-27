"""Application configuration. All values are read from environment variables only."""
from __future__ import annotations

import os


def _bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


class Settings:
    """Snapshot of environment configuration. Created once per app instance."""

    def __init__(self) -> None:
        self.database_url: str = os.environ.get(
            "DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5433/postgres"
        )
        # Shutdown (kill) switch for the assistant: ASSISTANT_DISABLED=1 rejects
        # new generations with HTTP 503.
        self.disabled: bool = _bool("ASSISTANT_DISABLED")
        self.context_token_budget: int = _int("CONTEXT_TOKEN_BUDGET", 4000)
        self.history_max_rounds: int = _int("HISTORY_MAX_ROUNDS", 10)
        self.max_tool_calls: int = _int("MAX_TOOL_CALLS", 5)
        self.max_concurrent_generations: int = _int("MAX_CONCURRENT_GENERATIONS", 2)
        # Delta streaming pace (seconds between text chunks) for the stub gateway.
        self.delta_delay: float = float(os.environ.get("DELTA_DELAY", "0.05"))
        self.delta_chars: int = _int("DELTA_CHARS", 32)
        self.sensitive_words: list[str] = [
            w.strip()
            for w in os.environ.get("SENSITIVE_WORDS", "kickback,salary,layoff,competitor quote")
            .split(",")
            if w.strip()
        ]
