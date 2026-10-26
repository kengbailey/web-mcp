"""Process-wide service container created by the server lifespan.

Area modules register their own components lazily via ``component(name, factory)``,
so web/search/reddit/youtube code stays independent. Tests build a Services with a
mock transport and ``install`` it.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, TypeVar

import httpx

from .cache import CursorStore, HostMemory, TTLCache
from .config import Settings
from .net import Net
from .pages import Pager

T = TypeVar("T")


class Services:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or Settings.from_env()
        self.net = Net(self.settings, transport)
        self.cache = TTLCache()
        self.cursors = CursorStore()
        self.hosts = HostMemory()
        self.pages = Pager(self.cache, self.cursors)
        self._components: dict[str, Any] = {}

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


_current: Services | None = None


def install(services: Services | None) -> None:
    global _current
    _current = services


def current() -> Services:
    if _current is None:
        raise RuntimeError("services are not initialised (server lifespan not running)")
    return _current
