"""search_reddit, fetch_subreddit, fetch_subreddit_info, fetch_reddit_post, fetch_reddit_comments."""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from .common import owner, result, svc, tool

Limit = Annotated[int, Field(ge=1, le=10)]
Period = Literal["hour", "day", "week", "month", "year", "all"]


def _reddit():
    from ...core.reddit import Reddit

    return svc().component("reddit", Reddit)


def register(mcp: FastMCP) -> None:
    @tool(mcp)
    async def search_reddit(
        query: str = "",
        subreddit: str = "",
        limit: Limit = 5,
        cursor: str = "",
        match: Literal["all", "title"] = "all",
        sort: Literal["relevance", "hot", "top", "new", "comments"] = "relevance",
        time_filter: Period = "all",
    ):
        """Find Reddit posts, optionally within one subreddit. Rows show id, score, comments, age, title. cursor reuses the saved query."""
        return result(await _reddit().search(owner(), query, subreddit, limit, cursor, match, sort, time_filter))

    @tool(mcp)
    async def fetch_subreddit(
        subreddit: str = "",
        sort: Literal["hot", "new", "top", "rising", "controversial"] = "hot",
        time_filter: Period = "day",
        limit: Limit = 5,
        cursor: str = "",
    ):
        """List a subreddit's posts. time_filter applies to top/controversial. cursor reuses saved settings."""
        return result(await _reddit().subreddit(owner(), subreddit, sort, time_filter, limit, cursor))

    @tool(mcp)
    async def fetch_subreddit_info(subreddit: str):
        """Subreddit description, subscribers and activity."""
        return result(await _reddit().info(owner(), subreddit))

    @tool(mcp)
    async def fetch_reddit_post(reference: str):
        """Read a Reddit post (URL or id): metadata and body, no comments."""
        return result(await _reddit().post(owner(), reference))

    @tool(mcp)
    async def fetch_reddit_comments(
        reference: str = "",
        limit: Limit = 10,
        cursor: str = "",
        sort: Literal["confidence", "top", "new", "controversial", "old", "qa"] = "top",
        parent_id: str = "",
    ):
        """Reddit comments as an indented tree. parent_id focuses one thread. cursor continues."""
        return result(await _reddit().comments(owner(), reference, limit, cursor, sort, parent_id))
