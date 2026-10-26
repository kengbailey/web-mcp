"""Pooled HTTP clients, one per egress route, owned by the server lifespan.

Routes: ``web`` (PROXY_URL; arbitrary pages), ``api`` (direct; Exa, OpenRouter),
``reddit`` (REDDIT_PROXY_URL) and ``youtube`` (YOUTUBE_PROXY_URL). Clients never follow
redirects on their own: callers that follow redirects re-validate each hop. Clients
never store cookies: they are shared by every caller and every host. Callers that need
cookies (browser-earned jars) send an explicit Cookie header.

The direct ``web`` route also checks the address it actually connects to, so a DNS
answer that flips to a private address after ``validate_url`` (DNS rebinding) is
refused. Behind PROXY_URL the proxy resolves names; restrict its egress instead.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from http.cookiejar import DefaultCookiePolicy

import httpcore
import httpx

from .config import Settings

ROUTES = ("web", "api", "reddit", "youtube")


def is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_global
    except ValueError:
        return False


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, refuse non-public addresses, connect to the vetted IP.

    TLS still verifies and sends SNI for the URL's hostname (httpcore takes it from the
    request origin, not from the address passed here).
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None):
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise httpcore.ConnectError(f"cannot resolve {host}") from exc
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        if not addresses or not all(is_public(a) for a in addresses):
            raise httpcore.ConnectError("refusing to connect to a non-public address")
        error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(address, port, timeout, local_address, socket_options)
            except httpcore.ConnectError as exc:
                error = exc
        raise error or httpcore.ConnectError("connection failed")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


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
            limits = httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=60)
            kwargs: dict = dict(
                timeout=httpx.Timeout(20.0, connect=6.0),
                limits=limits,
                headers={"User-Agent": self.settings.user_agent},
                follow_redirects=False,
            )
            if self._transport is not None:
                kwargs["transport"] = self._transport
            elif proxy := self._proxy(route):
                kwargs["proxy"] = proxy
            elif route == "web":
                kwargs["transport"] = public_only_transport(limits)
            client = self._clients[route] = httpx.AsyncClient(**kwargs)
            # httpx copies any jar passed in, so the policy must go on the client's own jar.
            client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        return client

    async def aclose(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            await client.aclose()


def public_only_transport(limits: httpx.Limits) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(limits=limits)
    pool = transport._pool  # httpx exposes no network-backend option; tests pin this
    if not isinstance(pool, httpcore.AsyncConnectionPool) or not hasattr(pool, "_network_backend"):
        raise RuntimeError("unsupported httpx/httpcore version for the SSRF connect guard")
    pool._network_backend = PublicOnlyBackend()
    return transport
