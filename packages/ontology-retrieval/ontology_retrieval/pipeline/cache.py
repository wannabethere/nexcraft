"""Cache abstractions.

`Cache` is the Protocol. `NullCache` is the no-op default. `LRUCache` is an
in-memory async-friendly LRU with per-entry TTL. A Redis implementation can
land later behind the same Protocol.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Protocol


class Cache(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def clear(self) -> None: ...


class NullCache:
    """No-op cache. Default when no other cache is configured."""

    async def get(self, key: str) -> Any | None:
        return None

    async def set(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None

    async def clear(self) -> None:
        return None


class LRUCache:
    """Bounded in-memory LRU with per-entry TTL.

    Per-instance; not shared across processes. For multi-process deployments,
    swap in a Redis-backed Cache implementation (same Protocol).
    """

    def __init__(self, max_entries: int = 1024) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be > 0")
        self._max = max_entries
        self._store: OrderedDict[str, tuple[float | None, Any]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at is not None and time.monotonic() > expires_at:
                # Lazy expiry
                self._store.pop(key, None)
                return None
            # Touch for LRU
            self._store.move_to_end(key)
            return value

    async def set(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
        async with self._lock:
            if key in self._store:
                self._store.pop(key)
            expires_at = (time.monotonic() + ttl_seconds) if ttl_seconds is not None else None
            self._store[key] = (expires_at, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)
