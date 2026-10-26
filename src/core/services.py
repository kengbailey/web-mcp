"""Process-wide service container created by the server lifespan.

Area modules register their own components lazily via ``component(name, factory)``,
so web/search/reddit/youtube code stays independent. Tests build a Services with a
mock transport and ``install`` it.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

import httpx

from .cache import CursorStore, HostMemory, TTLCache
from .config import Settings
from .net import Net
from .pages import Pager

T = TypeVar("T")

# Blocking work never uses the default executor: asyncio's getaddrinfo (the SSRF check on
# every fetch) runs there and must not queue behind a big extraction or a download.
POOLS = {"cpu": 4, "pdf": 1, "yt": 4}  # pdfium is not thread-safe: one worker serializes it


class Services:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or Settings.from_env()
        self.net = Net(self.settings, transport)
        self.cache = TTLCache()
        self.cursors = CursorStore()
        self.hosts = HostMemory()
        self.pages = Pager(self.cache, self.cursors)
        self._components: dict[str, Any] = {}
        self._pools = {name: ThreadPoolExecutor(size, thread_name_prefix=f"web-mcp-{name}") for name, size in POOLS.items()}

    def executor(self, pool: str) -> ThreadPoolExecutor:
        return self._pools[pool]

    async def run(self, pool: str, fn: Callable[..., T], *args: Any) -> T:
        """Run blocking ``fn`` on a bounded pool ("cpu", "pdf" or "yt")."""
        return await asyncio.get_running_loop().run_in_executor(self._pools[pool], functools.partial(fn, *args))

    def component(self, name: str, factory: Callable[["Services"], T]) -> T:
        if name not in self._components:
            self._components[name] = factory(self)
        return self._components[name]

    async def aclose(self) -> None:
        components, self._components = self._components, {}
        for item in reversed(list(components.values())):
            close = getattr(item, "aclose", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        await self.net.aclose()
        for pool in self._pools.values():
            pool.shutdown(wait=False, cancel_futures=True)


_current: Services | None = None


def install(services: Services | None) -> None:
    global _current
    _current = services


def current() -> Services:
    if _current is None:
        raise RuntimeError("services are not initialised (server lifespan not running)")
    return _current
