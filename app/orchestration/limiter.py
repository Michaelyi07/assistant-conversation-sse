"""Per-user concurrency limiter for assistant generations (max N in flight)."""
from __future__ import annotations

import threading
from collections import defaultdict


class UserConcurrencyLimiter:
    """In-process counter guarded by a threading.Lock (safe from both sync
    finally blocks and async handlers). Check and increment happen under one
    lock; release happens in the generation's ``finally`` (all exit paths)."""

    def __init__(self, max_per_user: int = 2) -> None:
        self.max_per_user = max_per_user
        self._lock = threading.Lock()
        self._active: dict[str, int] = defaultdict(int)

    async def try_acquire(self, user_id: str) -> bool:
        with self._lock:
            if self._active[user_id] >= self.max_per_user:
                return False
            self._active[user_id] += 1
            return True

    def release(self, user_id: str) -> None:
        with self._lock:
            if self._active.get(user_id, 0) > 0:
                self._active[user_id] -= 1
                if self._active[user_id] == 0:
                    del self._active[user_id]

    def active_count(self, user_id: str) -> int:
        return self._active.get(user_id, 0)
