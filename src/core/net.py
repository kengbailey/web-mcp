"""Pooled HTTP clients, one per egress route, owned by the server lifespan.

Routes: ``web`` (PROXY_URL; arbitrary pages), ``api`` (direct; Exa, OpenRouter),
``reddit`` (REDDIT_PROXY_URL) and ``youtube`` (YOUTUBE_PROXY_URL). Clients never follow
redirects on their own: callers that follow redirects re-validate each hop.
"""

from __future__ import annotations

import httpx

from .config import Settings

ROUTES = ("web", "api", "reddit", "youtube")


class Net:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport
        self._clients: dict[str, httpx.AsyncClient] = {}

    def _proxy(self, route: str) -> str | None:
        s = self.settings
        return {"web": s.proxy_url, "reddit": s.reddit_proxy_url, "youtube": s.youtube_proxy_url}.get(route)

    def client(self, route: str) -> httpx.AsyncClient:
        if route not in ROUTES:
            raise ValueError(f"unknown route {route}")
        client = self._clients.get(route)
        if client is None:
            kwargs: dict = dict(
                timeout=httpx.Timeout(20.0, connect=6.0),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=60),
                headers={"User-Agent": self.settings.user_agent},
                follow_redirects=False,
            )
            if self._transport is not None:
                kwargs["transport"] = self._transport
            elif proxy := self._proxy(route):
                kwargs["proxy"] = proxy
            client = self._clients[route] = httpx.AsyncClient(**kwargs)
        return client

    async def aclose(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            await client.aclose()
