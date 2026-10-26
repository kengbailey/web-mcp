"""fetch_content, read_content."""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from ...core.config import Settings
from ...core.services import Services
from .common import owner, result, svc, tool

MaxChars = Annotated[int, Field(ge=500, le=20000)]
DEFAULT_MAX_CHARS = Settings.from_env().default_max_chars


def _web(services: Services | None = None):
    from ...core.web import Web

    return (services or svc()).component("web", Web)


async def prewarm(services: Services) -> None:
    await _web(services).prewarm()


def register(mcp: FastMCP) -> None:
    @tool(mcp)
    async def fetch_content(
        url: str,
        max_chars: MaxChars = DEFAULT_MAX_CHARS,
        mode: Literal["content", "outline", "excerpts"] = "content",
        query: str = "",
        links: bool = False,
    ):
        """Read a web page, PDF or forum thread as compact markdown. outline: headings only; excerpts: paragraphs matching query. links=true appends page links."""
        return result(await _web().fetch(owner(), url, max_chars, mode, query, links))

    @tool(mcp)
    async def read_content(cursor: str, max_chars: MaxChars = DEFAULT_MAX_CHARS):
        """Continue long text (page, thread, post, comment, description, transcript) from its next: cursor."""
        return result(await _web().read(owner(), cursor, max_chars))
