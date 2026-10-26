"""Reddit: OAuth, references, listings, posts, comment trees, subreddit info (offline)."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from src.core.errors import Fail
from src.core.reddit import Reddit, parse_reference, subreddit_name
from tests.conftest import build_services

CREDS = dict(reddit_client_id="id", reddit_client_secret="secret")
NOW = time.time()
ROW = re.compile(r"^(\d+)\. (\w+) · ")
CURSOR = re.compile(r"more: (\S+)$")
SUFFIX = re.compile(r"\[\+[\d.k]+ chars: (\S+)\]$")


class Api:
    """Fake Reddit: the token endpoint plus JSON routes keyed by URL path."""

    def __init__(self, routes: dict[str, Any] | None = None, expires_in: int = 3600):
        self.routes = routes or {}
        self.expires_in = expires_in
        self.calls: list[httpx.Request] = []
        self.token_requests: list[httpx.Request] = []

    def paths(self) -> list[str]:
        return [c.url.path for c in self.calls]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            self.token_requests.append(request)
            n = len(self.token_requests)
            return httpx.Response(200, json={"access_token": f"tok{n}", "expires_in": self.expires_in})
        self.calls.append(request)
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"message": "Not Found"})
        out = route(request) if callable(route) else route
        return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)


def make(api: Callable, **settings) -> Reddit:
    return Reddit(build_services(api, **{**CREDS, **settings}))


def t3(pid: str, **kw) -> dict:
    data = dict(
        id=pid, subreddit="python", author="alice", score=412, num_comments=87, created_utc=NOW - 3 * 86400,
        title=f"Post {pid}", is_self=True, selftext="", domain="self.python", over_18=False,
        permalink=f"/r/python/comments/{pid}/post/", url=f"https://www.reddit.com/r/python/comments/{pid}/post/",
    )
    return {"kind": "t3", "data": {**data, **kw}}


def listing(children: list[dict], after: str | None = None) -> dict:
    return {"kind": "Listing", "data": {"children": children, "after": after}}


def t1(cid: str, body: str = "", replies: tuple = (), parent: str = "t3_p1", **kw) -> dict:
    data = dict(
        id=cid, name=f"t1_{cid}", author=f"u{cid}", body=body or f"body {cid}", score=5, created_utc=NOW - 7200,
        parent_id=parent, permalink=f"/r/python/comments/p1/_/{cid}/",
        replies=listing([r for r in replies]) if replies else "",
    )
    return {"kind": "t1", "data": {**data, **kw}}


def stub(*ids: str, parent: str = "t3_p1", depth: int = 0) -> dict:
    first = ids[0] if ids else "_"
    return {"kind": "more", "data": {"id": first, "name": f"t1_{first}", "children": list(ids), "parent_id": parent, "depth": depth, "count": len(ids)}}


def rows(text: str) -> list[str]:
    return [m.group(2) for line in text.splitlines() if (m := ROW.match(line))]


def cursor_of(text: str) -> str:
    m = CURSOR.search(text)
    return m.group(1) if m else ""


async def fails(code: str, coro) -> Fail:
    with pytest.raises(Fail) as exc:
        await coro
    assert exc.value.code == code, exc.value
    return exc.value


def about(name: str = "python", **kw) -> dict:
    data = dict(display_name=name, title="Python", public_description="News about Python.", subscribers=1_400_000, active_user_count=1234, over18=False)
    return {"kind": "t5", "data": {**data, **kw}}


# --- OAuth ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_token_is_cached_and_sent_with_reddit_user_agent():
    api = Api({"/r/python/about": about(), "/r/rust/about": about("rust")})
    r = make(api)
    await r.info("me", "python")
    await r.info("me", "rust")
    assert len(api.token_requests) == 1
    token = api.token_requests[0]
    assert token.url == "https://www.reddit.com/api/v1/access_token"
    assert token.headers["authorization"] == "Basic " + base64.b64encode(b"id:secret").decode()
    assert token.content == b"grant_type=client_credentials"
    call = api.calls[-1]
    assert str(call.url).startswith("https://oauth.reddit.com/r/rust/about?")
    assert call.headers["authorization"] == "bearer tok1"
    assert call.headers["user-agent"] == "python:web-mcp:v3" == token.headers["user-agent"]
    assert call.url.params["raw_json"] == "1"


@pytest.mark.asyncio
async def test_401_refreshes_token_once_then_reuses_it():
    def about_route(request: httpx.Request):
        return httpx.Response(401) if request.headers["authorization"] == "bearer tok1" else about()

    api = Api({"/r/python/about": about_route, "/r/rust/about": about_route})
    r = make(api)
    assert (await r.info("me", "python")).startswith("r/python · ")
    await r.info("me", "rust")
    assert len(api.token_requests) == 2 and len(api.calls) == 3

    api = Api({"/r/python/about": httpx.Response(401)})
    error = await fails("UPSTREAM_BLOCKED", make(api).info("me", "python"))
    assert "token" in error.message and len(api.token_requests) == 2 and len(api.calls) == 2


@pytest.mark.asyncio
async def test_token_refresh_is_single_flight_and_respects_expiry_margin():
    api = Api({f"/r/s{i}/about": about(f"s{i}") for i in range(5)})

    async def slow(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            await asyncio.sleep(0.02)
        return api(request)

    r = make(slow)
    await asyncio.gather(*(r.info("me", f"s{i}") for i in range(5)))
    assert len(api.token_requests) == 1

    short = Api({"/r/python/about": about(), "/r/rust/about": about("rust")}, expires_in=60)
    r = make(short)
    await r.info("me", "python")
    await r.info("me", "rust")
    assert len(short.token_requests) == 2  # 60 s token is inside the 60 s safety margin


@pytest.mark.asyncio
async def test_missing_or_rejected_credentials():
    api = Api({"/r/python/about": about()})
    error = await fails("NOT_CONFIGURED", Reddit(build_services(api)).info("me", "python"))
    assert "REDDIT_CLIENT_ID" in error.message and not api.calls and not api.token_requests

    def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    await fails("NOT_CONFIGURED", make(rejected).info("me", "python"))


# --- references -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("1abcde", ("1abcde", "", "")),
        ("t3_1ABCDE", ("1abcde", "", "")),
        ("https://redd.it/1abcde", ("1abcde", "", "")),
        ("redd.it/1abcde", ("1abcde", "", "")),
        ("https://www.reddit.com/r/python/comments/1abcde/example_title/", ("1abcde", "python", "")),
        ("/r/python/comments/1abcde/example_title/", ("1abcde", "python", "")),
        ("https://old.reddit.com/r/Python/comments/1abcde/t/19rkmkzdscsw5/?context=3", ("1abcde", "Python", "19rkmkzdscsw5")),
        ("https://www.reddit.com/comments/a/old_post/", ("a", "", "")),
        ("https://www.reddit.com/gallery/1abcde", ("1abcde", "", "")),
    ],
)
def test_parse_reference(reference, expected):
    assert parse_reference(reference) == expected


@pytest.mark.parametrize("reference", ["https://example.com/comments/1abcde", "https://www.reddit.com/r/python/", "ftp://redd.it/abc"])
def test_parse_reference_rejects(reference):
    with pytest.raises(Fail) as exc:
        parse_reference(reference)
    assert exc.value.code == "INVALID_ARGUMENT"


def test_subreddit_names():
    assert [subreddit_name(x) for x in ("python", "r/python", "/r/python/", "https://www.reddit.com/r/python/top")] == ["python"] * 4
    with pytest.raises(Fail):
        subreddit_name("no spaces!")


@pytest.mark.asyncio
async def test_share_links_follow_reddit_redirects_only():
    share = "/r/python/s/AbC123xyz"
    api = Api({share: httpx.Response(302, headers={"location": "https://www.reddit.com/r/python/comments/1abcde/title/?share_id=x"})})
    r = make(api)
    assert await r._resolve("https://www.reddit.com" + share) == ("1abcde", "python", "")
    assert await r._resolve("https://www.reddit.com" + share) == ("1abcde", "python", "")
    assert len(api.calls) == 1 and "authorization" not in api.calls[0].headers  # cached, unauthenticated

    api = Api({share: httpx.Response(302, headers={"location": "https://evil.example/r/python/comments/1abcde/"})})
    await fails("INVALID_ARGUMENT", make(api)._resolve("https://www.reddit.com" + share))


# --- listings ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_keeps_overflow_in_cursor_then_continues_upstream():
    pages = {"": ([t3(f"a{i}") for i in range(25)], "t3_a24"), "t3_a24": ([t3(f"b{i}") for i in range(3)], None)}

    def hot(request: httpx.Request):
        assert request.url.params["limit"] == "25" and "t" not in request.url.params
        children, after = pages[request.url.params.get("after", "")]
        return listing(children, after)

    api = Api({"/r/python/hot": hot})
    r = make(api)
    out = await r.subreddit("me", "python", "hot", "day", 5, "")
    ids, numbers = rows(out), [int(ROW.match(x).group(1)) for x in out.splitlines() if ROW.match(x)]
    for _ in range(4):
        cursor = cursor_of(out)
        out = await r.subreddit("me", "", "hot", "day", 5, cursor)
        ids += rows(out)
        numbers += [int(ROW.match(x).group(1)) for x in out.splitlines() if ROW.match(x)]
    assert len(api.calls) == 1 and len(ids) == 25  # overflow served from cursor state
    out = await r.subreddit("me", "", "hot", "day", 5, cursor_of(out))
    ids += rows(out)
    assert ids == [f"a{i}" for i in range(25)] + ["b0", "b1", "b2"] and len(set(ids)) == 28
    assert numbers == list(range(1, 26)) and out.splitlines()[0].startswith("26. b0 ")
    assert "more:" not in out and [c.url.params.get("after") for c in api.calls] == [None, "t3_a24"]


@pytest.mark.asyncio
async def test_listing_tops_up_from_next_upstream_page_and_cursor_ignores_new_filters():
    pages = {"": ([t3(f"a{i}") for i in range(7)], "t3_a6"), "t3_a6": ([t3("a6"), t3("b0"), t3("b1")], "t3_b1"), "t3_b1": ([], None)}

    def top(request: httpx.Request):
        assert request.url.params["t"] == "week"
        return listing(*pages[request.url.params.get("after", "")])

    api = Api({"/r/python/top": top})
    r = make(api)
    first = await r.subreddit("me", "python", "top", "week", 5, "")
    cursor = cursor_of(first)
    second = await r.subreddit("me", "rust", "new", "day", 5, cursor)  # new filters are ignored
    assert rows(second) == ["a5", "a6", "b0", "b1"]  # duplicate a6 dropped
    assert await r.subreddit("me", "", "top", "week", 5, cursor) == second  # cursors replay
    assert "more:" not in second and api.paths() == ["/r/python/top"] * 3
    await fails("CURSOR_EXPIRED", r.subreddit("someone-else", "", "hot", "day", 5, cursor))
    await fails("INVALID_ARGUMENT", r.search("me", "", "", 5, cursor, "all", "relevance", "all"))


@pytest.mark.asyncio
async def test_search_parameters_and_cache():
    api = Api({"/r/python/search": listing([t3("abc")]), "/search": listing([])})
    r = make(api)
    out = await r.search("me", 'async "io"', "r/python", 5, "", "title", "top", "week")
    params = api.calls[0].url.params
    assert params["q"] == 'title:"async io"' and params["restrict_sr"] == "on"
    assert (params["sort"], params["t"], params["limit"]) == ("top", "week", "25")
    assert rows(out) == ["abc"] and "more:" not in out
    await r.search("me", 'async "io"', "python", 3, "", "title", "top", "week")
    assert len(api.calls) == 1  # same upstream page from the 5 min cache
    assert await r.search("me", "nothing", "", 5, "", "all", "relevance", "all") == "no posts"
    assert "restrict_sr" not in api.calls[-1].url.params
    await fails("INVALID_ARGUMENT", r.search("me", "  ", "", 5, "", "all", "relevance", "all"))


@pytest.mark.asyncio
async def test_listing_row_format():
    posts = [
        t3("abc123", title="How do I   parse\nJSON?", selftext="I have a [big file](https://x.y/z) **now**\\.\n\nIt is big. " + "x" * 300),
        t3("def456", title="Cool article", is_self=False, selftext="", domain="example.com", url="https://example.com/a", over_18=True, score=1234, num_comments=5),
        t3("ghi789", title="Empty self post"),
    ]
    out = await make(Api({"/r/python/new": listing(posts)})).subreddit("me", "python", "new", "day", 5, "")
    lines = out.splitlines()
    assert lines[0] == "1. abc123 · r/python · 412↑ 87💬 · 3d · How do I parse JSON?"
    assert lines[1].startswith("   I have a big file now. It is big. xxx") and lines[1].endswith("…") and len(lines[1]) == 153
    assert lines[2] == "2. def456 · r/python · 1.2k↑ 5💬 · 3d · Cool article → example.com · NSFW"
    assert lines[3] == "3. ghi789 · r/python · 412↑ 87💬 · 3d · Empty self post"
    assert len(lines) == 4


@pytest.mark.asyncio
async def test_five_row_listing_is_compact():
    posts = [
        t3(f"1g{i}xk2", title=f"What is the best way to structure a medium sized Python project {i}?", selftext="I keep going back and forth between src layout and flat layout. " * 8, score=1200 + i, num_comments=230)
        for i in range(3)
    ] + [
        t3(f"1h{i}zz9", title="Python 3.15 released with free-threading on by default", is_self=False, domain="docs.python.org", url="https://docs.python.org/3.15/whatsnew.html")
        for i in range(2)
    ]
    out = await make(Api({"/r/python/hot": listing(posts, "t3_x")})).subreddit("me", "python", "hot", "day", 5, "")
    assert len(rows(out)) == 5 and out.splitlines()[-1].startswith("more: ")
    assert len(out) <= 1200, len(out)


# --- post -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_uses_api_info_and_renders_header_meta_and_body():
    post = t3("abc123", title="How do I X?", selftext="First paragraph.  \n\n\n\n    pip  list\n\nDone.\u200b")

    def info(request: httpx.Request):
        assert request.url.params["id"] == "t3_abc123"
        return listing([post])

    api = Api({"/api/info": info})
    r = make(api)
    out = await r.post("me", "https://www.reddit.com/r/python/comments/abc123/how/")
    title, meta, blank, *body = out.split("\n")
    assert title == "How do I X?" and blank == ""
    assert meta == "r/python · u/alice · 412↑ · 87💬 · 3d · https://www.reddit.com/r/python/comments/abc123/post/ · 38 chars"
    assert body == ["First paragraph.", "", "    pip  list", "", "Done."]  # code indentation survives
    assert api.paths() == ["/api/info"]  # never the comment endpoint
    assert await r.post("me", "abc123") == out and len(api.calls) == 1  # cached


@pytest.mark.asyncio
async def test_link_and_gallery_posts():
    link = t3("lnk1", title="An article", is_self=False, domain="example.com", url="https://example.com/a")
    gallery = t3(
        "gal1", title="Pics", is_self=False, domain="reddit.com", url="https://www.reddit.com/gallery/gal1", is_gallery=True,
        gallery_data={"items": [{"media_id": "m1", "caption": "first"}, {"media_id": "m2"}]},
        media_metadata={"m1": {"e": "Image", "m": "image/jpg", "s": {"u": "https://preview.redd.it/m1.jpg?width=1"}}, "m2": {"e": "Image", "m": "image/png", "s": {}}},
    )
    video = t3("vid1", title="Clip", is_self=False, domain="v.redd.it", url="https://v.redd.it/xyz", secure_media={"reddit_video": {"fallback_url": "https://v.redd.it/xyz/DASH_720.mp4?source=fallback"}})
    by_id = {"t3_lnk1": link, "t3_gal1": gallery, "t3_vid1": video}
    r = make(Api({"/api/info": lambda request: listing([by_id[request.url.params["id"]]])}))
    assert await r.post("me", "lnk1") == "An article\nr/python · u/alice · 412↑ · 87💬 · 3d · → https://example.com/a"
    out = await r.post("me", "gal1")
    assert out.endswith("\n\ngallery: https://i.redd.it/m1.jpg (first) · https://i.redd.it/m2.png")
    assert (await r.post("me", "vid1")).endswith("\n\nvideo: https://v.redd.it/xyz/DASH_720.mp4")


@pytest.mark.asyncio
async def test_long_post_body_pages_losslessly():
    text = "\n\n".join(f"Paragraph {i}. " + ("lorem ipsum dolor " * 30).strip() for i in range(40))
    services = build_services(Api({"/api/info": listing([t3("big1", title="Big", selftext=text)])}), **CREDS)
    out = await Reddit(services).post("me", "big1")
    title, meta, rest = out.split("\n", 2)
    assert title == "Big" and " · chars 0–" in meta and f"/{len(text)} · next: " in meta
    body = rest.removeprefix("\n")
    assert len(body) <= 8000
    cursor = meta.rsplit("next: ", 1)[1]
    while cursor:
        chunk = services.pages.read("me", cursor, 8000)
        head, part = chunk.text.split("\n\n", 1)
        assert head.startswith("Big · https://www.reddit.com/r/python/comments/big1/post/ · reddit · chars ")
        body += part
        cursor = chunk.cursor
    assert body == text and head.endswith(" · end")


@pytest.mark.asyncio
async def test_post_not_found():
    await fails("NOT_FOUND", make(Api({"/api/info": listing([])})).post("me", "zzz9"))


# --- subreddit info ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_info_renders_counts_title_and_bounded_description():
    services = build_services(Api({"/r/python/about": about(), "/r/nsfwsub/about": about("nsfwsub", over18=True, active_user_count=None, public_description="", description="Long rules. " * 400)}), **CREDS)
    r = Reddit(services)
    assert await r.info("me", "r/python") == "r/python · 1.4M subscribers · 1.2k online\nPython\nNews about Python."
    head, _, desc = (await r.info("me", "nsfwsub")).split("\n")
    assert head == "r/nsfwsub · 1.4M subscribers · NSFW"
    match = SUFFIX.search(desc)
    assert match and len(desc) < 3100
    rest = services.pages.read("me", match.group(1)).text.split("\n\n", 1)[1]
    assert rest.endswith("Long rules.")


# --- comments ---------------------------------------------------------------------------------


def thread() -> list:
    return [
        {"kind": "Listing", "data": {"children": [t3("p1", num_comments=42)]}},
        listing([
            t1("am1", "Please follow the rules", (t1("am2", parent="t1_am1"),), author="AutoModerator", stickied=True),
            t1("c1", "Top level\n\n> quoted\nsecond line", (
                t1("c2", "a reply", parent="t1_c1"),
                t1("c3", "[deleted]", parent="t1_c1", author="[deleted]"),
                t1("c4", "[removed]", (t1("c5", "reply to removed", parent="t1_c4"),), parent="t1_c1", author="[deleted]"),
                stub("m1", "19rkmkzdscsw5", parent="t1_c1", depth=1),  # 13-char ids are real
            ), score=1234),
            t1("c7", "[removed]"),
            t1("c8", "last top"),
            stub("t1x", "t2x"),
        ]),
    ]


@pytest.mark.asyncio
async def test_comments_flatten_indent_and_drop_rules():
    api = Api({"/comments/p1": thread()})
    r = make(api)
    out = await r.comments("me", "p1", 5, "", "top", "")
    params = api.calls[0].url.params
    assert (params["sort"], params["limit"], "depth" in params, "comment" in params) == ("top", "100", False, False)
    lines = out.splitlines()
    assert lines[0] == "p1 · top · 5 of 42 comments"
    assert lines[1:6] == [
        "- c1 u/uc1 1.2k↑ 2h: Top level / > quoted / second line",
        "  - c2 u/uc2 5↑ 2h: a reply",
        "  - c4 u/[deleted] 5↑ 2h: [removed]",
        "    - c5 u/uc5 5↑ 2h: reply to removed",
        "- c8 u/uc8 5↑ 2h: last top",
    ]
    assert lines[6].startswith("more: ") and len(lines) == 7
    assert api.paths() == ["/comments/p1"]  # page was full; no expansion yet


@pytest.mark.asyncio
async def test_comments_expand_more_ids_without_repeats():
    expansions = [
        [  # breadth first: top-level ids, then the nested stub's ids
            t1("t1x", "more top", parent="t3_p1", depth=0),
            t1("t1r", "reply to t1x", parent="t1_t1x", depth=1),
            stub("z1", parent="t1_t1x", depth=1),
            t1("m1", "late reply", parent="t1_c1", depth=1),
            t1("c2", "a reply", parent="t1_c1", depth=1),  # already shown
            t1("t2x", "[deleted]", parent="t3_p1", depth=0, author="[deleted]"),
        ],
        [t1("z1", "deep", parent="t1_t1x", depth=1)],
    ]
    seen_children = []

    def morechildren(request: httpx.Request):
        p = request.url.params
        assert (p["api_type"], p["link_id"], p["sort"], p["limit_children"]) == ("json", "t3_p1", "top", "false")
        seen_children.append(p["children"])
        return {"json": {"errors": [], "data": {"things": expansions[len(seen_children) - 1]}}}

    api = Api({"/comments/p1": thread(), "/api/morechildren": morechildren})
    r = make(api)
    pages = [await r.comments("me", "https://redd.it/p1", 5, "", "top", "")]
    while cursor := cursor_of(pages[-1]):
        pages.append(await r.comments("me", "", 3, cursor, "new", "ignored"))
    assert seen_children == ["t1x,t2x,m1,19rkmkzdscsw5", "z1"]
    assert pages[1].splitlines() == [
        "p1 · top · 6–8 of 42 comments",
        "- t1x u/ut1x 5↑ 2h: more top",
        "  - t1r u/ut1r 5↑ 2h: reply to t1x",
        "  - m1 u/um1 5↑ 2h re c1: late reply",
        pages[1].splitlines()[-1],
    ]
    assert pages[2].splitlines() == ["p1 · top · 9 of 42 comments", "  - z1 u/uz1 5↑ 2h re t1x: deep"]
    ids = [line.split()[1] for page in pages for line in page.splitlines() if line.lstrip().startswith("- ")]
    assert len(ids) == len(set(ids)) == 9


@pytest.mark.asyncio
async def test_comments_focus_thread_relative_indent():
    focused = [
        {"kind": "Listing", "data": {"children": [t3("p1", num_comments=42)]}},
        listing([t1("c4", "focus", (t1("c5", "child", (t1("c6", "grandchild", parent="t1_c5"),), parent="t1_c4"),), parent="t1_c1")]),
    ]
    api = Api({"/comments/p1": focused})
    r = make(api)
    out = await r.comments("me", "p1", 10, "", "top", "t1_c4")
    assert api.calls[0].url.params["comment"] == "c4"
    assert out.splitlines() == [
        "p1 · top · thread c4 · 3 of 42 comments",
        "- c4 u/uc4 5↑ 2h: focus",
        "  - c5 u/uc5 5↑ 2h: child",
        "    - c6 u/uc6 5↑ 2h: grandchild",
    ]
    assert await r.comments("me", "https://www.reddit.com/r/python/comments/p1/t/c4/", 10, "", "top", "") == out
    await fails("INVALID_ARGUMENT", r.comments("me", "p1", 10, "", "top", "bad id!"))
    empty = Api({"/comments/p1": [focused[0], listing([])]})
    await fails("NOT_FOUND", make(empty).comments("me", "p1", 10, "", "top", "zz1"))


@pytest.mark.asyncio
async def test_deep_threads_do_not_overflow():
    node = t1("d49")
    for i in range(48, -1, -1):
        node = t1(f"d{i}", replies=(node,))
    tree = [{"kind": "Listing", "data": {"children": [t3("p1", num_comments=50)]}}, listing([node])]
    out = await make(Api({"/comments/p1": tree})).comments("me", "p1", 10, "", "top", "")
    assert out.splitlines()[10] == "  " * 9 + "- d9 u/ud9 5↑ 2h: body d9"


@pytest.mark.asyncio
async def test_long_comment_body_gets_read_content_suffix():
    long_body = "\n\n".join(f"Point {i}: " + "word " * 60 for i in range(6))
    tree = [{"kind": "Listing", "data": {"children": [t3("p1", num_comments=1)]}}, listing([t1("c1", long_body)])]
    services = build_services(Api({"/comments/p1": tree}), **CREDS)
    line = (await Reddit(services).comments("me", "p1", 10, "", "top", "")).splitlines()[1]
    prefix, match = "- c1 u/uc1 5↑ 2h: ", SUFFIX.search(line)
    assert line.startswith(prefix + "Point 0: word") and match and " / Point 1: " in line
    inline = line[len(prefix) : match.start()].rstrip()
    assert len(inline) <= 500
    chunk = services.pages.read("me", match.group(1))
    head, rest = chunk.text.split("\n\n", 1)
    assert head.startswith("https://www.reddit.com/r/python/comments/p1/_/c1/ · reddit · chars ") and head.endswith(" · end")
    full = " / ".join(" ".join(p.split()) for p in long_body.split("\n\n"))
    assert full.endswith(rest) and full[: len(full) - len(rest)].rstrip() == inline


# --- errors -----------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code", "text"),
    [
        (httpx.Response(403, json={"reason": "private"}), "UPSTREAM_BLOCKED", "private, quarantined or banned"),
        (httpx.Response(404, json={"reason": "banned"}), "NOT_FOUND", "r/python not found"),
        (httpx.Response(429, headers={"x-ratelimit-reset": "42.0"}), "RATE_LIMITED", "retry in 42s"),
        (httpx.Response(503), "UPSTREAM_ERROR", "HTTP 503"),
        (httpx.Response(302, headers={"location": "https://oauth.reddit.com/subreddits/search?q=python"}), "NOT_FOUND", "r/python not found"),
        (httpx.Response(302, headers={"location": "https://example.com/"}), "UPSTREAM_ERROR", "redirected"),
        (httpx.Response(200, text="<html>blocked</html>"), "UPSTREAM_ERROR", "non-JSON"),
    ],
)
async def test_upstream_errors_map_to_codes(response, code, text):
    error = await fails(code, make(Api({"/r/python/hot": response})).subreddit("me", "python", "hot", "day", 5, ""))
    assert text in error.message


@pytest.mark.asyncio
async def test_argument_errors():
    r = make(Api())
    await fails("INVALID_ARGUMENT", r.subreddit("me", "", "hot", "day", 5, ""))
    await fails("INVALID_ARGUMENT", r.subreddit("me", "bad name", "hot", "day", 5, ""))
    await fails("INVALID_ARGUMENT", r.comments("me", "", 5, "", "top", ""))
    await fails("INVALID_ARGUMENT", r.post("me", "https://example.com/x"))


# --- through the MCP server -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_return_plain_text(make_client):
    api = Api({"/r/python/hot": listing([t3("abc")], "t3_abc"), "/api/info": listing([t3("abc", title="T", selftext="hi")])})
    async with make_client(api, **CREDS) as (client, _):
        result = await client.call_tool("fetch_subreddit", {"subreddit": "python", "limit": 1})
        assert result.structured_content is None and len(result.content) == 1
        assert rows(result.content[0].text) == ["abc"] and "more: " in result.content[0].text
        post = await client.call_tool("fetch_reddit_post", {"reference": "abc"})
        assert post.content[0].text.startswith("T\nr/python · u/alice")
        error = await client.call_tool("fetch_subreddit_info", {"subreddit": "bad name"}, raise_on_error=False)
        assert error.is_error and error.content[0].text == "ERROR INVALID_ARGUMENT: invalid subreddit name"


@pytest.mark.asyncio
async def test_long_comment_continues_through_read_content(make_client):
    pytest.importorskip("src.core.web")
    long_body = "word " * 600
    tree = [{"kind": "Listing", "data": {"children": [t3("p1", num_comments=1)]}}, listing([t1("c1", long_body)])]
    async with make_client(Api({"/comments/p1": tree}), **CREDS) as (client, _):
        text = (await client.call_tool("fetch_reddit_comments", {"reference": "p1"})).content[0].text
        cursor = SUFFIX.search(text.splitlines()[1]).group(1)
        more_text = (await client.call_tool("read_content", {"cursor": cursor})).content[0].text
    assert more_text.split("\n\n", 1)[1].rstrip().endswith("word") and " · end" in more_text.split("\n", 1)[0]


# --- live -------------------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_reddit():
    if not (os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET")):
        pytest.skip("needs REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET")
    from src.core.config import Settings
    from src.core.services import Services

    services = Services(Settings.from_env())
    try:
        r = Reddit(services)
        top = await r.subreddit("me", "python", "top", "month", 3, "")
        ids = rows(top)
        assert len(ids) == 3 and cursor_of(top)
        assert len(rows(await r.subreddit("me", "", "top", "month", 3, cursor_of(top)))) == 3
        post = await r.post("me", ids[0])
        assert post.split("\n")[1].lower().startswith("r/python · u/")
        comments = await r.comments("me", ids[0], 5, "", "top", "")
        assert comments.startswith(f"{ids[0]} · top · ") and comments.count("\n- ") >= 1
        if cursor_of(comments):
            await r.comments("me", "", 10, cursor_of(comments), "top", "")
        assert (await r.info("me", "python")).lower().startswith("r/python · ")
        found = await r.search("me", "asyncio", "python", 3, "", "all", "relevance", "year")
        assert rows(found)
        await fails("NOT_FOUND", r.info("me", "thissubdoesnotexist9x7q"))

        # A big thread: page past the ~100 loaded comments so /api/morechildren expands stubs.
        hot = await r.subreddit("me", "AskReddit", "top", "day", 10, "")
        counts = re.findall(r"^\d+\. (\w+) · .*? ([\d.]+k?)💬", hot, re.MULTILINE)
        big = max(counts, key=lambda c: float(c[1].rstrip("k")) * (1000 if c[1].endswith("k") else 1))[0]
        page, seen_ids = await r.comments("me", big, 10, "", "top", ""), []
        for _ in range(14):
            seen_ids += [x.split()[1] for x in page.splitlines() if x.lstrip().startswith("- ")]
            if not cursor_of(page):
                break
            page = await r.comments("me", "", 10, cursor_of(page), "top", "")
        assert len(seen_ids) == len(set(seen_ids)) > 100
        assert any(k.startswith("reddit:more:") for k in services.cache._entries)
    finally:
        await services.aclose()
