"""Process-local caches: data TTL cache, cursor store, and per-host strategy memory.

All state is lost on restart, matching v2. One process per deployment is assumed.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from .errors import CURSOR_EXPIRED, INVALID_ARGUMENT, Fail

MISSING = object()
LOAD_TIMEOUT = 900.0  # hard cap for a detached load (STT of a long video is the slowest)


def _retrieve(task: asyncio.Task) -> None:
    """Mark a finished load's exception as retrieved when no caller is left waiting."""
    if not task.cancelled():
        task.exception()


def approx_size(value: Any) -> int:
    if isinstance(value, (str, bytes)):
        return len(value)
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return 1024


class TTLCache:
    """Byte-bounded LRU with per-entry TTL and single-flight loading.

    Concurrent ``get_or_load`` calls for the same key share one upstream call; a failed
    load is not cached and propagates to every waiter.
    """

    def __init__(self, max_bytes: int = 128 * 1024 * 1024, clock: Callable[[], float] = time.monotonic):
        self.max_bytes = max_bytes
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, int, Any]] = OrderedDict()
        self._bytes = 0
        self._inflight: dict[str, asyncio.Future] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str, default: Any = None, touch: float | None = None) -> Any:
        entry = self._entries.get(key)
        if entry is None:
            return default
        expiry, size, value = entry
        now = self._clock()
        if expiry <= now:
            self._drop(key)
            return default
        if touch is not None and expiry < now + touch:
            self._entries[key] = (now + touch, size, value)
        self._entries.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl: float, size: int | None = None) -> None:
        size = approx_size(value) if size is None else size
        if size > self.max_bytes:
            return
        if key in self._entries:
            self._drop(key)
        self._entries[key] = (self._clock() + ttl, size, value)
        self._bytes += size
        while self._bytes > self.max_bytes and self._entries:
            self._drop(next(iter(self._entries)))

    def _drop(self, key: str) -> None:
        _, size, _ = self._entries.pop(key)
        self._bytes -= size

    async def get_or_load(
        self, key: str, ttl: float, load: Callable[[], Awaitable[Any]], timeout: float = LOAD_TIMEOUT
    ) -> Any:
        """Load once per key. The load runs as its own bounded task, so a caller that is
        cancelled (deadline, disconnect) neither cancels it for the other waiters nor
        wastes paid work: the result is still cached for the next call."""
        value = self.get(key, MISSING)
        if value is not MISSING:
            return value
        task = self._inflight.get(key)
        if task is None:

            async def run() -> Any:
                try:
                    async with asyncio.timeout(timeout):
                        result = await load()
                    self.set(key, result, ttl)
                    return result
                finally:
                    self._inflight.pop(key, None)

            task = self._inflight[key] = asyncio.get_running_loop().create_task(run())
            task.add_done_callback(_retrieve)
        return await asyncio.shield(task)


class CursorStore:
    """Short random cursors bound to a caller and a tool kind.

    States are small Python objects (pending rows, document keys, offsets). Large text
    lives in the TTLCache and is referenced by key, never copied per chunk.
    """

    def __init__(self, ttl: float = 900, max_entries: int = 20000, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.max_entries = ttl, max_entries
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, str, str, Any]] = OrderedDict()

    def put(self, owner: str, kind: str, state: Any) -> str:
        now = self._clock()
        while self._entries:
            key, entry = next(iter(self._entries.items()))
            if entry[0] > now and len(self._entries) < self.max_entries:
                break
            del self._entries[key]
        cursor = secrets.token_urlsafe(9)
        self._entries[cursor] = (now + self.ttl, owner, kind, state)
        return cursor

    def get(self, owner: str, cursor: str, kind: str | tuple[str, ...]) -> Any:
        entry = self._entries.get(cursor or "")
        if entry is None or entry[0] <= self._clock() or not secrets.compare_digest(entry[1], owner):
            raise Fail(CURSOR_EXPIRED, "cursor expired or unknown; repeat the original call")
        kinds = (kind,) if isinstance(kind, str) else kind
        if entry[2] not in kinds:
            raise Fail(INVALID_ARGUMENT, f"cursor belongs to another tool ({entry[2]})")
        self._entries.move_to_end(cursor)
        return entry[3]

    def kind(self, owner: str, cursor: str) -> str | None:
        entry = self._entries.get(cursor or "")
        if entry is None or entry[0] <= self._clock() or not secrets.compare_digest(entry[1], owner):
            return None
        return entry[2]


class HostMemory:
    """Remembers, per key (usually a host), which strategy worked recently."""

    def __init__(self, ttl: float = 3600, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._entries: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry[0] <= self._clock():
            del self._entries[key]
            return None
        return entry[1]

    def set(self, key: str, value: str, ttl: float | None = None) -> None:
        if len(self._entries) > 4096:
            now = self._clock()
            self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
        self._entries[key] = (self._clock() + (self.ttl if ttl is None else ttl), value)

    def forget(self, key: str) -> None:
        self._entries.pop(key, None)
