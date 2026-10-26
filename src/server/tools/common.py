"""Helpers every tool module uses: registration, caller identity, text results."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
from typing import Any, Callable

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations

from ...core import errors
from ...core.errors import Fail
from ...core.services import Services, current

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
DEFAULT_DEADLINE = 45.0
log = logging.getLogger("web_mcp")


def guarded(fn: Callable, deadline: float) -> Callable:
    """Bound a tool call and map every failure to ``ERROR <CODE>: <message>``.

    This must wrap the tool function itself: FastMCP masks non-ToolError exceptions
    before middleware can see them.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            async with asyncio.timeout(deadline):
                return await fn(*args, **kwargs)
        except ToolError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise Fail(errors.UPSTREAM_TIMEOUT, "request deadline exceeded") from None
        except httpx.HTTPStatusError as exc:
            raise errors.for_status(exc.response.status_code, "upstream") from None
        except httpx.HTTPError:
            raise Fail(errors.UPSTREAM_ERROR, "upstream connection failed") from None
        except Exception:
            log.exception("tool %s failed", fn.__name__)
            raise Fail(errors.INTERNAL_ERROR, "request could not be completed") from None

    return wrapper


def tool(mcp: FastMCP, deadline: float = DEFAULT_DEADLINE) -> Callable:
    """Register a read-only, text-only tool (no output schema, no structuredContent)."""

    def decorator(fn: Callable) -> Callable:
        return mcp.tool(output_schema=None, annotations=READ)(guarded(fn, deadline))

    return decorator


def owner() -> str:
    """Cursor scope: bearer-token hash when authenticated, else the shared LAN principal."""
    token = get_access_token()
    return hashlib.sha256(token.token.encode()).hexdigest() if token else "anon"


def svc() -> Services:
    return current()


def result(text: str, **meta: Any) -> ToolResult:
    """The only way tools return: one text block, diagnostics in _meta (not shown to models)."""
    clean = {k: v for k, v in meta.items() if v not in (None, "", [], {})}
    return ToolResult(content=[TextContent(type="text", text=text)], meta=clean or None)
