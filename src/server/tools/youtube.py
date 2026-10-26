"""fetch_youtube_content, fetch_youtube_transcript, fetch_youtube_comments."""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from .common import owner, result, svc, tool
from .web import DEFAULT_MAX_CHARS, MaxChars

Limit = Annotated[int, Field(ge=1, le=10)]


def _youtube():
    from ...core.youtube import YouTube

    return svc().component("youtube", YouTube)


def register(mcp: FastMCP) -> None:
    @tool(mcp)
    async def fetch_youtube_content(reference: str):
        """YouTube video metadata and description (video id or URL). No transcript or comments."""
        return result(await _youtube().video(owner(), reference))

    @tool(mcp, deadline=300)
    async def fetch_youtube_transcript(
        ctx: Context,
        reference: str,
        language: str = "",
        max_chars: MaxChars = DEFAULT_MAX_CHARS,
        source: Literal["captions", "stt"] = "captions",
        fallback_to_stt: bool = True,
        timestamps: bool = True,
    ):
        """Video transcript as timestamped paragraphs. Captions first, speech-to-text if none (slow, up to 5 min); source=stt forces it. language: ISO code, default original."""
        return result(
            await _youtube().transcript(
                owner(), reference, language, max_chars, source, fallback_to_stt, timestamps,
                progress=ctx.report_progress,
            )
        )

    @tool(mcp)
    async def fetch_youtube_comments(
        reference: str = "",
        limit: Limit = 10,
        cursor: str = "",
        order: Literal["relevance", "time"] = "relevance",
        parent_id: str = "",
        query: str = "",
    ):
        """YouTube comments (needs YOUTUBE_API_KEY). parent_id lists replies; query filters. cursor continues."""
        return result(await _youtube().comments(owner(), reference, limit, cursor, order, parent_id, query))
