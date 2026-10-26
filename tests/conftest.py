"""Shared fixtures: an in-memory MCP client over a Services built on a mock transport.

Usage in area tests::

    async def test_x(make_client):
        def handler(request: httpx.Request) -> httpx.Response: ...
        async with make_client(handler, exa_api_key="k") as (client, services):
            result = await client.call_tool("search", {"query": "q"})
"""

from __future__ import annotations

import contextlib
from typing import Callable

import httpx
import pytest
from fastmcp import Client

from src.core.config import Settings
from src.core.services import Services, install
from src.server import delivery_server


def build_services(handler: Callable[[httpx.Request], httpx.Response] | None = None, **settings) -> Services:
    transport = httpx.MockTransport(handler or (lambda request: httpx.Response(404)))
    return Services(Settings(**settings), transport=transport)


@pytest.fixture
def services():
    svc = build_services()
    install(svc)
    yield svc
    install(None)


@pytest.fixture
def make_client(monkeypatch):
    @contextlib.asynccontextmanager
    async def factory(handler=None, **settings):
        svc = build_services(handler, **settings)
        monkeypatch.setattr(delivery_server, "make_services", lambda: svc)
        async with Client(delivery_server.mcp) as client:
            yield client, svc

    return factory
