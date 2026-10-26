"""Wire contract: small catalog, text-only results, stable error format.

The catalog is resent by OpenWebUI on every turn; v2 measured 25,586 bytes (doc/bench.md).
The budget below is 40% of that.
"""

import json

import pytest
from fastmcp import Client

from src.server.delivery_server import mcp

CATALOG_BUDGET_BYTES = 10_200
TOOLS = [
    "search", "search_videos", "fetch_content", "read_content", "search_reddit",
    "fetch_subreddit", "fetch_subreddit_info", "fetch_reddit_post", "fetch_reddit_comments",
    "fetch_youtube_content", "fetch_youtube_transcript", "fetch_youtube_comments",
]


@pytest.mark.asyncio
async def test_catalog_is_small_stable_and_schema_free():
    async with Client(mcp) as client:
        tools = await client.list_tools()
    assert [t.name for t in tools] == TOOLS
    catalog = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]
    assert len(json.dumps(catalog, separators=(",", ":"))) <= CATALOG_BUDGET_BYTES
    for t in catalog:
        assert "outputSchema" not in t, t["name"]
        assert len(t["description"]) <= 220, t["name"]
        assert t["annotations"]["readOnlyHint"] is True


@pytest.mark.asyncio
async def test_errors_are_text_without_structured_content(make_client):
    async with make_client() as (client, _):
        result = await client.call_tool("read_content", {"cursor": "doesnotexist"}, raise_on_error=False)
    assert result.is_error
    assert result.structured_content is None
    assert result.content[0].text.startswith("ERROR CURSOR_EXPIRED: ")


def test_owner_is_stable_across_token_rotation(monkeypatch):
    from fastmcp.server.auth.auth import AccessToken

    from src.server.tools import common

    def as_token(token, subject):
        return AccessToken(token=token, client_id="openwebui", scopes=[], subject=subject, claims={"iss": "https://auth"})

    monkeypatch.setattr(common, "get_access_token", lambda: as_token("t1", "user-a"))
    first = common.owner()
    monkeypatch.setattr(common, "get_access_token", lambda: as_token("t2-rotated", "user-a"))
    assert common.owner() == first
    monkeypatch.setattr(common, "get_access_token", lambda: as_token("t3", "user-b"))
    assert common.owner() != first
    monkeypatch.setattr(common, "get_access_token", lambda: as_token("t4", None))
    no_sub = common.owner()
    monkeypatch.setattr(common, "get_access_token", lambda: as_token("t5", None))
    assert common.owner() != no_sub  # no subject: never share across tokens
    monkeypatch.setattr(common, "get_access_token", lambda: None)
    assert common.owner() == "anon"
