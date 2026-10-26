"""Pooled HTTP clients, one per egress route, owned by the server lifespan.

Routes: ``web`` (PROXY_URL; arbitrary pages), ``api`` (direct; Exa, OpenRouter),
``reddit`` (REDDIT_PROXY_URL) and ``youtube`` (YOUTUBE_PROXY_URL). Clients never follow
redirects on their own: callers that follow redirects re-validate each hop. Clients
never store cookies: they are shared by every caller and every host. Callers that need
cookies (browser-earned jars) send an explicit Cookie header.
"""

from __future__ import annotations

from http.cookiejar import DefaultCookiePolicy

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
            # httpx copies any jar passed in, so the policy must go on the client's own jar.
            client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        return client

    async def aclose(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            await client.aclose()
