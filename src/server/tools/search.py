"""search, search_videos (Exa)."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from .common import owner, result, svc, tool

Limit = Annotated[int, Field(ge=1, le=10)]


def _exa():
    from ...core.exa import Exa

    return svc().component("exa", Exa)


def register(mcp: FastMCP) -> None:
    @tool(mcp)
    async def search(
        query: str = "",
        limit: Limit = 5,
        cursor: str = "",
        include_domains: list[str] = [],
        exclude_domains: list[str] = [],
        start_published_date: str = "",
        end_published_date: str = "",
    ):
        """Web search: titles, URLs, query-relevant highlights. Read pages with fetch_content. Dates are ISO-8601. cursor pages saved results without a new search."""
        return result(
            await _exa().search(
                owner(), query, limit, cursor, include_domains, exclude_domains,
                start_published_date, end_published_date,
            )
        )

    @tool(mcp)
    async def search_videos(query: str = "", limit: Limit = 5, cursor: str = ""):
        """YouTube video search: titles, URLs, snippets. Use fetch_youtube_* for details or transcripts."""
        return result(await _exa().videos(owner(), query, limit, cursor))
