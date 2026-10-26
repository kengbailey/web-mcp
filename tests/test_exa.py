"""Exa search: payload shape, compact rendering, cursor paging, cache, errors, videos."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from src.core.config import Settings
from src.core.errors import Fail
from src.core.exa import VIDEO_DOMAINS, Exa
from src.core.services import Services
from tests.conftest import build_services

VID, VID2, VID3 = "dQw4w9WgXcQ", "9bZkp7q19f0", "kJQP7kiw5Fk"


def row(i: int, **extra) -> dict:
    base = {
        "id": f"https://site{i}.example/post/{i}",
        "url": f"https://site{i}.example/post/{i}",
        "title": f"Result {i}",
        "publishedDate": "2026-09-01T00:00:00.000Z",
        "highlights": [f"Highlight {i}."],
        "highlightScores": [0.42],
        "text": f"Result {i} leading text",
        "image": "https://cdn.example/x.png",
        "favicon": "https://cdn.example/favicon.ico",
    }
    return base | extra


class Upstream:
    """Mock Exa endpoint recording request payloads."""

    def __init__(self, results: list | None = None, status: int = 200, body: bytes | None = None):
        self.results = results if results is not None else [row(i) for i in range(1, 11)]
        self.status, self.body = status, body
        self.calls: list[httpx.Request] = []

    @property
    def payloads(self) -> list[dict]:
        return [json.loads(r.content) for r in self.calls]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.body is not None:
            return httpx.Response(self.status, content=self.body)
        if self.status != 200:
            return httpx.Response(self.status, text="sensitive upstream diagnostic k-secret")
        return httpx.Response(200, json={"requestId": "r1", "results": self.results, "costDollars": {"total": 0.007}})


def make(upstream=None, **settings) -> Exa:
    settings.setdefault("exa_api_key", "k-secret")
    return Exa(build_services(upstream or Upstream(), **settings))


async def fail(coro) -> Fail:
    with pytest.raises(Fail) as exc:
        await coro
    return exc.value


@pytest.mark.asyncio
async def test_payload_shape_and_rendering():
    up = Upstream([
        row(1, url="https://example.org/a#frag", title="Alpha guide", author="Someone",
            highlights=["First point...", "...second point"]),
        row(2, url="https://example.org/a"),  # duplicate after fragment strip
        row(3, url="javascript:alert(1)"),
        row(4, url="https://user:pw@example.org/x"),
        row(5, url="https://docs.rs/tokio?v=2", title="Tokio docs", publishedDate=None, highlights=[],
            text="Tokio docs — An asynchronous runtime"),
        row(6, url="https://example.org/z", title="", highlights=None, text=""),
    ])
    exa = make(up)
    out = await exa.search(
        "anon", "  rust \n async ", 5, "", ["Example.org", "docs.rs/tokio", "example.org"], ["spam.com"],
        "2026-01-01T00:00:00Z", "2026-06-01T00:00:00+02:00",
    )
    assert len(up.calls) == 1
    req = up.calls[0]
    assert req.url == "https://api.exa.ai/search" and req.headers["x-api-key"] == "k-secret"
    assert up.payloads[0] == {
        "query": "rust async",
        "type": "fast",
        "numResults": 10,
        "contents": {"highlights": {"maxCharacters": 300}, "text": {"maxCharacters": 300}},
        "includeDomains": ["docs.rs/tokio", "example.org"],
        "excludeDomains": ["spam.com"],
        "startPublishedDate": "2026-01-01T00:00:00.000Z",
        "endPublishedDate": "2026-05-31T22:00:00.000Z",
    }
    assert out == (
        "1. Alpha guide — https://example.org/a (2026-09-01)\n"
        "   First point … second point\n"
        "2. Tokio docs — https://docs.rs/tokio?v=2\n"
        "   An asynchronous runtime\n"
        "3. https://example.org/z (2026-09-01)"
    )
    assert "Someone" not in out  # author only for videos


@pytest.mark.asyncio
async def test_optional_filters_omitted_and_empty_results():
    up = Upstream([])
    out = await make(up).search("anon", "q", 5, "", [], [], "", "")
    assert out == "no results"
    assert set(up.payloads[0]) == {"query", "type", "numResults", "contents"}


@pytest.mark.asyncio
async def test_cursor_drains_saved_results_without_second_call():
    up = Upstream()
    exa = make(up)
    first = await exa.search("anon", "q", 4)
    lines = first.splitlines()
    assert lines[0].startswith("1. Result 1 — ") and lines[-1].startswith("more: ")
    cursor = lines[-1].removeprefix("more: ")
    assert len(cursor) == 12
    second = await exa.search("anon", "", 4, cursor, ["ignored.com"], [], "garbage", "")
    assert second.splitlines()[0].startswith("5. Result 5 — ")
    again = await exa.search("anon", "", 4, cursor)  # cursors are re-readable
    assert again.splitlines()[:-1] == second.splitlines()[:-1]
    last = await exa.search("anon", "", 4, second.splitlines()[-1].removeprefix("more: "))
    assert last.splitlines()[0].startswith("9. ") and "10. Result 10" in last and "more:" not in last
    assert len(up.calls) == 1
    assert (await fail(exa.videos("anon", "", 4, cursor))).code == "INVALID_ARGUMENT"
    assert (await fail(exa.search("someone-else", "", 4, cursor))).code == "CURSOR_EXPIRED"
    assert (await fail(exa.search("anon", "", 4, "nope"))).code == "CURSOR_EXPIRED"


@pytest.mark.asyncio
async def test_identical_search_is_cached():
    up = Upstream()
    exa = make(up)
    a = await exa.search("anon", "rust async", 3, "", ["b.com", "a.com"])
    b = await exa.search("other", " rust  async ", 3, "", ["a.com", "B.com"])
    assert len(up.calls) == 1
    assert a.splitlines()[:-1] == b.splitlines()[:-1]  # same rows, different cursors
    await exa.search("anon", "rust async", 3, "", ["a.com"])
    await exa.videos("anon", "rust async", 3)
    assert len(up.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,message", [
    (401, "UPSTREAM_BLOCKED", "Exa rejected the API key"),
    (403, "UPSTREAM_BLOCKED", "Exa denied this request for the account"),
    (402, "UPSTREAM_ERROR", "Exa account is out of credits"),
    (429, "RATE_LIMITED", "Exa rate limit exceeded; retry shortly"),
    (500, "UPSTREAM_ERROR", "Exa returned HTTP 500"),
])
async def test_http_errors_are_safe_and_not_cached(status, code, message):
    up = Upstream(status=status)
    exa = make(up)
    err = await fail(exa.search("anon", "q", 5))
    assert (err.code, err.message) == (code, message)
    assert "sensitive" not in str(err) and "k-secret" not in str(err)
    await fail(exa.search("anon", "q", 5))
    assert len(up.calls) == 2  # one request per call, failures not cached


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"invalid", b'{"results": {}}', b"[]", b"x" * (1024 * 1024 + 1)])
async def test_invalid_and_oversized_response(body):
    err = await fail(make(Upstream(body=body)).search("anon", "q", 5))
    assert err.code == "UPSTREAM_ERROR"


@pytest.mark.asyncio
async def test_timeout_and_connection_errors():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    assert (await fail(make(timeout).search("anon", "q", 5))).code == "UPSTREAM_TIMEOUT"
    assert (await fail(make(refused).videos("anon", "q", 5))).code == "UPSTREAM_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    dict(query=" "),
    dict(query="x" * 501),
    dict(query="q", limit=0),
    dict(query="q", limit=11),
    dict(query="q", include_domains=["https://example.org"]),
    dict(query="q", exclude_domains=["exa mple.org"]),
    dict(query="q", include_domains=[" example.org"]),
    dict(query="q", include_domains=[f"d{i}.com" for i in range(21)]),
    dict(query="q", start_published_date="yesterday"),
    dict(query="q", start_published_date="2026-01-01"),  # no timezone
    dict(query="q", start_published_date="2026-01-02T00:00:00Z", end_published_date="2026-01-01T00:00:00Z"),
])
async def test_validation_happens_before_any_request(args):
    exa = make(lambda request: pytest.fail("unexpected request"))
    args.setdefault("limit", 5)
    err = await fail(exa.search("anon", **args))
    assert err.code == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_missing_key_is_not_configured():
    exa = make(lambda request: pytest.fail("unexpected request"), exa_api_key=None)
    assert (await fail(exa.search("anon", "q", 5))).code == "NOT_CONFIGURED"
    assert (await fail(exa.videos("anon", "q", 5))).code == "NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_videos_canonicalize_filter_and_dedupe():
    up = Upstream([
        {"url": "https://www.youtube.com/@channel", "title": "Channel page"},
        {"url": f"https://www.youtube.com/watch?v={VID}&t=42s", "title": "Never Gonna", "author": "Rick Astley",
         "publishedDate": "2009-10-25T06:57:33.000Z", "highlights": ["Official video..."]},
        {"url": f"https://youtu.be/{VID}", "title": "Never Gonna (dupe)"},
        {"url": f"https://evil.example/watch?v={VID}", "title": "Phish"},
        {"url": f"https://www.youtube.com/shorts/{VID2}", "title": "Short", "author": "x" * 80},
        {"url": f"https://m.youtube.com/watch?v={VID3}", "title": "Mobile", "author": "Luis Fonsi"},
        {"url": "https://www.youtube.com/playlist?list=PL123", "title": "Playlist"},
    ])
    out = await make(up).videos("anon", "never gonna", 5)
    assert up.payloads[0]["includeDomains"] == VIDEO_DOMAINS and up.payloads[0]["numResults"] == 10
    assert out == (
        f"1. Never Gonna — https://www.youtube.com/watch?v={VID} (Rick Astley, 2009-10-25)\n"
        "   Official video\n"
        f"2. Short — https://www.youtube.com/watch?v={VID2}\n"
        f"3. Mobile — https://www.youtube.com/watch?v={VID3} (Luis Fonsi)"
    )


@pytest.mark.asyncio
async def test_five_results_stay_compact():
    long_hl = ["Exa found this passage relevant to the question being asked by the agent. " * 3,
               "A second passage adds more supporting evidence for the answer. " * 3]
    results = [
        row(i, title=f"A realistic article title about topic number {i:02d} here",  # 50 chars
            url=f"https://www.news-site{i}.example/2026/09/article-{i}",  # ~45 chars
            highlights=long_hl)
        for i in range(1, 11)
    ]
    out = await make(Upstream(results)).search("anon", "q", 5)
    rows = [line for line in out.splitlines() if line[:1].isdigit()]
    snippets = [line for line in out.splitlines() if line.startswith("   ")]
    assert len(rows) == 5 and len(snippets) == 5 and out.splitlines()[-1].startswith("more: ")
    assert all(len(s) <= 303 for s in snippets) and all(s.endswith("…") for s in snippets)
    # Worst case (every highlight at the 300-char cap) is ~2.1k chars; typical results are shorter.
    assert len(out) <= 2200, len(out)
    for noise in ("{", "null", "highlightScores", "favicon", "cdn.example", "requestId", "0.007"):
        assert noise not in out


@pytest.mark.asyncio
async def test_mcp_tools_return_plain_text_and_page_by_cursor(make_client):
    up = Upstream()
    async with make_client(up, exa_api_key="k-secret") as (client, _):
        first = await client.call_tool("search", {"query": "q", "limit": 6, "include_domains": ["a.com"]})
        assert first.structured_content is None and len(first.content) == 1
        text = first.content[0].text
        assert text.startswith("1. Result 1 — https://site1.example/post/1 (2026-09-01)\n   Highlight 1.")
        cursor = text.splitlines()[-1].removeprefix("more: ")
        second = await client.call_tool("search", {"cursor": cursor, "limit": 6})
        assert second.content[0].text.startswith("7. ") and "more:" not in second.content[0].text
        bad = await client.call_tool("search_videos", {"cursor": cursor}, raise_on_error=False)
        assert bad.is_error and bad.content[0].text.startswith("ERROR INVALID_ARGUMENT: ")
        empty = await client.call_tool("search", {}, raise_on_error=False)
        assert empty.content[0].text == "ERROR INVALID_ARGUMENT: query is required"
    assert len(up.calls) == 1 and up.payloads[0]["includeDomains"] == ["a.com"]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("EXA_API_KEY"), reason="EXA_API_KEY not set")
async def test_live_exa():
    services = Services(Settings(exa_api_key=os.environ["EXA_API_KEY"]))
    try:
        exa = Exa(services)
        web = await exa.search("anon", "python asyncio timeout context manager", 3)
        assert web.startswith("1. ") and "https://" in web and web.splitlines()[-1].startswith("more: ")
        assert len(web) <= 1500, web
        videos = await exa.videos("anon", "rick astley never gonna give you up", 3)
        assert "https://www.youtube.com/watch?v=" in videos
    finally:
        await services.aclose()
