"""Shared building blocks: text budgets, caches, cursors, paging."""

import asyncio

import pytest

from src.core.cache import CursorStore, HostMemory, TTLCache
from src.core.errors import CURSOR_EXPIRED, INVALID_ARGUMENT, Fail, for_status
from src.core.pages import Doc, Pager
from src.core.text import ago, clip, estimate_tokens, join, num, tidy, trunc


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_estimate_tokens_counts_non_ascii_heavier():
    assert estimate_tokens("abcd" * 100) == 100
    assert estimate_tokens("漢字" * 50) == 100


def test_clip_prefers_paragraph_boundary_and_makes_progress():
    text = ("word " * 150).strip() + "\n\n" + "tail " * 100
    out = clip(text, 800)
    assert out.endswith("\n\n") and len(out) <= 800
    assert clip("x" * 5000, 100) == "x" * 100
    assert clip("short", 100) == "short"


def test_clip_respects_token_budget_for_cjk():
    text = "漢" * 5000
    out = clip(text, 4000)
    assert estimate_tokens(out) <= 1000 and len(out) == 1000


def test_renderers():
    assert num(1234) == "1.2k" and num(2_500_000) == "2.5M" and num(1_712_300_000) == "1.7B" and num(12) == "12" and num(None) == ""
    assert ago(1000, now=1000 + 3 * 86400) == "3d" and ago(None) == ""
    assert trunc("a  b\n c", 10) == "a b c" and trunc("x" * 20, 10) == "x" * 9 + "…"
    assert join("a", "", None, "b") == "a · b"
    assert tidy("a  b \n\n\n\nc") == "a b\n\nc"
    assert tidy("- a\n  - b   c\n\tcode") == "- a\n  - b c\n    code"


def test_ttl_cache_expiry_lru_and_touch():
    clock = Clock()
    cache = TTLCache(max_bytes=10, clock=clock)
    cache.set("a", "12345", ttl=5)
    cache.set("b", "12345", ttl=5)
    cache.get("a")
    cache.set("c", "12345", ttl=5)  # evicts least recently used "b"
    assert cache.get("b") is None and cache.get("a") == "12345"
    clock.now += 4
    cache.get("a", touch=10)
    clock.now += 6
    assert cache.get("a") == "12345" and cache.get("c") is None


@pytest.mark.asyncio
async def test_ttl_cache_single_flight_and_failures_not_cached():
    cache = TTLCache()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "v"

    results = await asyncio.gather(*(cache.get_or_load("k", 60, load) for _ in range(5)))
    assert results == ["v"] * 5 and calls == 1

    async def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        await cache.get_or_load("bad", 60, boom)
    assert cache.get("bad") is None


def test_cursor_store_scope_kind_and_expiry():
    clock = Clock()
    store = CursorStore(ttl=10, clock=clock)
    cursor = store.put("alice", "listing", {"n": 1})
    assert len(cursor) == 12
    assert store.get("alice", cursor, "listing") == {"n": 1}
    with pytest.raises(Fail) as exc:
        store.get("bob", cursor, "listing")
    assert exc.value.code == CURSOR_EXPIRED
    with pytest.raises(Fail) as exc:
        store.get("alice", cursor, "other")
    assert exc.value.code == INVALID_ARGUMENT
    clock.now += 11
    with pytest.raises(Fail):
        store.get("alice", cursor, "listing")


def test_host_memory_expires():
    clock = Clock()
    memory = HostMemory(ttl=5, clock=clock)
    memory.set("xda", "browser")
    assert memory.get("xda") == "browser"
    clock.now += 6
    assert memory.get("xda") is None


def test_pager_round_trip_is_lossless():
    cache, store = TTLCache(), CursorStore()
    pager = Pager(cache, store)
    text = "\n\n".join(f"Paragraph {i} " + "lorem ipsum " * 40 for i in range(40))
    doc = Doc(url="https://e.x/a", source="web", text=text, title="T")
    pager.store("doc:a", doc)
    chunk = pager.page("me", "doc:a", doc, 0, 2000)
    head = chunk.text.split("\n\n", 1)[0]
    assert head.startswith("T · https://e.x/a · web · chars 0–") and "next: " + chunk.cursor in head
    collected = chunk.text.split("\n\n", 1)[1]
    while chunk.cursor:
        chunk = pager.read("me", chunk.cursor, 2000)
        collected += chunk.text.split("\n\n", 1)[1]
    assert collected == text and chunk.text.split("\n\n", 1)[0].endswith(" · end")


def test_pager_short_doc_has_no_cursor_and_body_suffix():
    pager = Pager(TTLCache(), CursorStore())
    doc = Doc(url="u", source="web", text="hello")
    chunk = pager.page("me", "k", doc)
    assert chunk.cursor is None and chunk.text == "u · web · 5 chars\n\nhello"
    inline, suffix = pager.body("me", "body:1", "u", "reddit", "x " * 600, 100)
    assert len(inline) <= 100 and suffix.startswith("[+1.1k chars: ")
    cursor = suffix.removesuffix("]").rsplit(" ", 1)[1]
    assert pager.read("me", cursor).text.rstrip().endswith("x")


def test_pager_next_url_hands_back_exhausted_doc():
    pager = Pager(TTLCache(), CursorStore())
    doc = Doc(url="u/page-1", source="xenforo", text="posts", next_url="u/page-2")
    pager.store("k", doc)
    chunk = pager.page("me", "k", doc)
    assert "next page: " in chunk.text
    follow = pager.read("me", chunk.cursor)
    assert follow.exhausted_doc is doc


def test_fail_text_and_status_mapping():
    assert str(Fail("NOT_FOUND", "gone")) == "ERROR NOT_FOUND: gone"
    assert for_status(429, "Exa").code == "RATE_LIMITED"
    assert for_status(500, "Exa").code == "UPSTREAM_ERROR"


@pytest.mark.asyncio
async def test_pooled_clients_never_store_cookies():
    import httpx

    from src.core.config import Settings
    from src.core.net import Net

    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"set-cookie": "sid=secret; Path=/"})

    net = Net(Settings(), transport=httpx.MockTransport(handler))
    client = net.client("web")
    for url in ("https://a.example/", "https://a.example/again", "https://b.example/"):
        await client.get(url)
    await net.aclose()
    assert seen == [None, None, None]
