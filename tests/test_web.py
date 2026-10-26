"""Web fetch: SSRF guard, bounded download, extraction chain, browser fallback, views, paging.

Offline: httpx.MockTransport for HTTP, a patched ``socket.getaddrinfo`` for DNS and a stub
in place of Chromium. The one live test (XDA through Bunny Shield) runs only with
``-m integration``.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from src.core import browser as browser_module
from src.core import web as web_module
from src.core.browser import Browser, Rendered, browser_user_agent, challenge_title
from src.core.errors import Fail
from src.core.pages import Doc
from src.core.web import Jar, Web, clean_markdown, excerpts, is_app_shell, is_challenge, outline, validate_url
from tests.conftest import build_services

FIXTURES = Path(__file__).parent / "fixtures"
ARTICLE = (FIXTURES / "web_article.html").read_text()
CHALLENGE = (FIXTURES / "web_bunny_challenge.html").read_text()
XDA_LIKE = (FIXTURES / "web_xda_like.html").read_text()
PUBLIC_IP = "93.184.215.14"
PRIVATE = {"internal.test": "10.0.0.5", "mapped.test": "::ffff:127.0.0.1", "localhost": "127.0.0.1"}


@pytest.fixture(autouse=True)
def dns(request, monkeypatch):
    """Every name resolves to a public address except the PRIVATE map and literal IPs."""
    if request.node.get_closest_marker("integration"):
        return  # live tests need real DNS

    def getaddrinfo(host, port, *args, **kwargs):
        if host == "nxdomain.test":
            raise socket.gaierror("no such host")
        address = PRIVATE.get(host)
        if address is None:
            try:
                address = str(ipaddress.ip_address(host))
            except ValueError:
                address = PUBLIC_IP
        if ":" in address:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


class StubBrowser:
    """Stands in for Chromium: returns queued results (the last one repeats)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[str] = []

    async def render(self, url: str) -> Rendered:
        self.calls.append(url)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result

    async def start(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


def rendered(html: str, url: str = "https://forum.test/t/1/", cookies: list | None = None,
             status: int = 200) -> Rendered:
    return Rendered(url=url, html=html, status=status, cookies=cookies or [], user_agent="BrowserUA/149")


@pytest_asyncio.fixture
async def env():
    """env(handler, browser=None) -> (web, requests); services are closed afterwards."""
    made = []

    def factory(handler, browser=None, **settings):
        requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return handler(request)

        services = build_services(record, **settings)
        web = services.component("web", Web)
        web.browser = browser or StubBrowser(RuntimeError("no browser in unit tests"))
        made.append(services)
        return web, requests

    yield factory
    for services in made:
        await services.aclose()


def html_response(html: str = ARTICLE, status: int = 200) -> httpx.Response:
    return httpx.Response(status, html=html)


def make_pdf(*pages: str) -> bytes:
    """A minimal valid PDF (Helvetica text, one line per page) with a correct xref table."""
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        content = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


async def code_of(awaitable) -> str:
    with pytest.raises(Fail) as exc:
        await awaitable
    return exc.value.code


# ---------------------------------------------------------------- URL safety and download


@pytest.mark.asyncio
async def test_validate_url_rejects_private_credentials_and_other_schemes():
    for url in [
        "http://127.0.0.1/",
        "http://localhost:8080/admin",
        "http://internal.test/",
        "http://mapped.test/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://user:secret@example.com/",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "not a url",
    ]:
        assert await code_of(validate_url(url)) == "INVALID_ARGUMENT", url
    assert await code_of(validate_url("https://nxdomain.test/")) == "UPSTREAM_ERROR"
    await validate_url("https://example.com/page?q=1")


@pytest.mark.asyncio
async def test_redirects_are_followed_and_every_hop_is_validated(env):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        if request.url.path == "/b":
            return httpx.Response(301, headers={"location": "https://other.test/c"})
        if request.url.host == "other.test":
            return html_response()
        if request.url.path == "/evil":
            return httpx.Response(307, headers={"location": "http://internal.test/secret"})
        if request.url.path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        raise AssertionError(request.url)

    web, requests = env(handler)
    text = await web.fetch("anon", "https://example.com/a", 8000)
    assert text.splitlines()[0].startswith("Tuning Sourdough Hydration · https://other.test/c · web · ")
    assert [str(r.url) for r in requests] == ["https://example.com/a", "https://example.com/b", "https://other.test/c"]

    requests.clear()
    assert await code_of(web.fetch("anon", "https://example.com/evil", 8000)) == "INVALID_ARGUMENT"
    assert all(r.url.host != "internal.test" for r in requests)
    assert await code_of(web.fetch("anon", "https://example.com/loop", 8000)) == "UPSTREAM_ERROR"
    assert web.browser.calls == []


@pytest.mark.asyncio
async def test_binary_types_are_rejected_early_and_size_is_capped(env, monkeypatch):
    big = b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/photo":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG" + big)
        if path == "/blob":
            return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01" + big)
        if path == "/disguised.bin":
            pdf = make_pdf("Hidden PDF")
            return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=pdf)
        if path == "/huge":
            headers = {"content-type": "text/html", "content-length": str(9 << 20)}
            return httpx.Response(200, headers=headers, content=b"<p>")
        if path == "/stream":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>" + big * 3)
        raise AssertionError(path)

    web, _ = env(handler)
    assert await code_of(web.fetch("anon", "https://example.com/photo", 8000)) == "UNSUPPORTED_CONTENT"
    assert await code_of(web.fetch("anon", "https://example.com/blob", 8000)) == "UNSUPPORTED_CONTENT"
    assert "Hidden PDF" in await web.fetch("anon", "https://example.com/disguised.bin", 8000)
    assert await code_of(web.fetch("anon", "https://example.com/huge", 8000)) == "TOO_LARGE"
    monkeypatch.setattr(web_module, "MAX_BYTES", 10_000)
    assert await code_of(web.fetch("anon", "https://example.com/stream", 8000)) == "TOO_LARGE"
    assert web.browser.calls == []


@pytest.mark.asyncio
async def test_pdf_is_extracted_locally_page_by_page(env):
    pdf = make_pdf("First page text", "Second page text")
    web, _ = env(lambda r: httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf))
    text = await web.fetch("anon", "https://example.com/paper.pdf", 8000)
    head, body = text.split("\n\n", 1)
    assert head == "https://example.com/paper.pdf · pdf · 33 chars"
    assert body == "First page text\n\nSecond page text"


@pytest.mark.asyncio
async def test_markdown_and_plain_text_pass_through(env, monkeypatch):
    monkeypatch.setattr(web_module, "_trafilatura", lambda *a: (_ for _ in ()).throw(AssertionError("no HTML parse")))
    md = (
        "# Notes\n\n![Image 1: logo](/l.png) Read the [docs](https://example.com/docs) now."
        "\n\n\n\n```\n  keep  [x](y)\n```"
    )
    plain = "def f():\n    return  1\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/notes":
            return httpx.Response(200, headers={"content-type": "text/markdown; charset=utf-8"}, text=md)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text=plain)

    web, requests = env(handler)
    text = await web.fetch("anon", "https://example.com/notes", 8000)
    assert text.split("\n\n", 1)[1] == "# Notes\n\nRead the docs now.\n\n```\n  keep  [x](y)\n```"
    assert requests[0].headers["accept"].startswith("text/markdown")
    assert (await web.fetch("anon", "https://example.com/f.py", 8000)).endswith("def f():\n    return  1")


# ---------------------------------------------------------------- cleanup, views, caching


def test_clean_markdown_drops_images_and_inline_links_and_builds_footer():
    md = (
        "Intro ![A loaf](/a.jpg) ![](/b.gif) ![Image 3: chart](/c.png) see [the guide](/guide) and "
        "[Foo (bar)](https://en.wikipedia.org/wiki/Foo_(bar)) [[1]](#cite) [dup](/guide) `a[i](x)` "
        "[Rel](<https://en.wikipedia.org/wiki/Relevance_(ir)>)"
    )
    body, footer = clean_markdown(md, "https://site.test/post", links=False)
    assert body == "Intro A loaf see the guide and Foo (bar) [1] dup `a[i](x)` Rel"
    assert footer == ""
    _, footer = clean_markdown(md, "https://site.test/post", links=True)
    assert footer == (
        "Links:\n- the guide: https://site.test/guide\n- Foo (bar): https://en.wikipedia.org/wiki/Foo_(bar)\n"
        "- Rel: https://en.wikipedia.org/wiki/Relevance_(ir)"
    )
    many = " ".join(f"[l{i}](/p{i})" for i in range(80))
    assert clean_markdown(many, "https://site.test/", links=True)[1].count("\n- ") == 50


@pytest.mark.asyncio
async def test_html_extraction_strips_images_and_links_with_optional_footer(env):
    web, _ = env(lambda r: html_response())
    plain = await web.fetch("anon", "https://bread.test/hydration", 8000)
    assert "Tuning Sourdough Hydration · https://bread.test/hydration · web" in plain.splitlines()[0]
    for absent in ("](", "![", "hydration chart", "loaf-hero", "http"):
        assert absent not in plain.split("\n\n", 1)[1], absent
    assert "    return round(100 * water_g / flour_g, 1)" in plain  # code indentation survives
    linked = await web.fetch("anon", "https://bread.test/hydration", 8000, links=True)
    footer = linked.rsplit("\n\nLinks:\n", 1)[1].splitlines()
    assert footer[0] == "- levain: https://bread.test/glossary/levain"
    assert "- flour guide: https://example.org/flour-guide" in footer
    assert len(footer) == len(set(footer)) == 3


@pytest.mark.asyncio
async def test_outline_and_excerpts_reuse_one_cached_fetch(env):
    web, requests = env(lambda r: html_response())
    url = "https://bread.test/hydration#top"
    await web.fetch("anon", url, 8000)
    head = await web.fetch("anon", url, 8000, mode="outline")
    assert head.split("\n\n", 1)[1] == (
        "# Tuning Sourdough Hydration\n## Why hydration matters\n## Adjusting for whole grain flour\n"
        "## Reading the dough during bulk fermentation\n### Quick checklist"
    )
    assert " · outline · " in head.splitlines()[0]
    found = await web.fetch("anon", url, 1200, mode="excerpts", query="whole wheat bran water")
    body = found.split("\n\n", 1)[1]
    tags = [int(p[2:p.index("]")]) for p in body.split("\n\n")]
    assert tags == sorted(tags) and len(body) <= 1200
    assert body.split("\n\n")[0].startswith("[¶") and "bran and germ soak up" in body
    assert "Temperature changes everything" not in body
    await web.fetch("anon", "https://bread.test/hydration", 500, mode="excerpts", query="temperature")
    assert len(requests) == 1
    assert await code_of(web.fetch("anon", url, 8000, mode="excerpts")) == "INVALID_ARGUMENT"


def test_excerpts_rank_with_bm25_and_respect_budget():
    paragraphs = [
        "Cats sleep most of the day.",
        "Dogs enjoy long walks and dogs love fetch.",
        "A paragraph about weather and rain.",
        "Dog training needs patience; a dog learns by repetition.",
    ]
    text = "\n\n".join(paragraphs)
    assert excerpts(text, "dogs", 2000) == f"[¶2] {paragraphs[1]}\n\n[¶4] {paragraphs[3]}"
    assert excerpts(text, "dogs", 50) == f"[¶2] {paragraphs[1]}"
    assert excerpts(text, "zebra", 2000) == ""
    assert outline("# A\ntext\n```\n# not a heading\n```\n## B") == "# A\n## B"


@pytest.mark.asyncio
async def test_read_content_pages_through_a_long_document(env):
    paragraphs = [f"Paragraph {i}: " + " ".join(["lorem ipsum dolor sit amet"] * 12) for i in range(60)]
    md = "\n\n".join(paragraphs)
    web, requests = env(lambda r: httpx.Response(200, headers={"content-type": "text/markdown"}, text=md))
    text = await web.fetch("anon", "https://long.test/doc.md", 2000)
    head, body = text.split("\n\n", 1)
    assert head.startswith("https://long.test/doc.md · web · chars 0–") and " · next: " in head
    pieces = [body]
    while " · next: " in head:
        cursor = head.rsplit("next: ", 1)[1]
        head, body = (await web.read("anon", cursor, 2000)).split("\n\n", 1)
        pieces.append(body)
    assert head.endswith(" · end")
    assert "".join(pieces) == md
    assert len(requests) == 1
    assert await code_of(web.read("someone-else", cursor, 2000)) == "CURSOR_EXPIRED"


# ---------------------------------------------------------------- challenges and browser fallback


def test_challenge_detection_by_title_or_short_page_never_by_marker():
    assert is_challenge(CHALLENGE)
    assert is_challenge("<html><head><title>Just a moment...</title></head><body></body></html>")
    short = "<html><body><h1>Hold tight</h1><p>Performing security verification</p></body></html>"
    assert is_challenge(short)
    assert "/.bunny-shield/" in XDA_LIKE and len(XDA_LIKE) > 10_000
    assert not is_challenge(XDA_LIKE)
    assert not is_challenge('<html><head><script src="/.bunny-shield/x.js"></script><title>Post</title></head></html>')
    article = "<html><title>Bots</title><p>" + "We study bot defenses. " * 600
    article += " performing security verification</p></html>"
    assert not is_challenge(article)
    assert not is_challenge("JavaScript is disabled. For a better experience, please enable JavaScript.")
    assert challenge_title("Establishing a secure connection ...") and not challenge_title("XDA Forums")


def test_app_shell_detection_needs_markers_and_little_text():
    assert is_app_shell('<body><div id="root"></div><script src="/app.js"></script></body>', 0)
    assert is_app_shell('<div class="x" id=\'app\'>\n</div>', 200)
    assert is_app_shell('<script id="__NEXT_DATA__" type="application/json">{}</script>', 800)
    assert is_app_shell("<noscript><p>You need to enable JavaScript to run this app.</p></noscript>", 50)
    assert not is_app_shell('<div id="root"></div>', 5000)
    assert not is_app_shell('<div data-id="root"></div><p>hello</p>', 10)


@pytest.mark.asyncio
async def test_bunny_challenge_goes_to_browser_then_cookies_are_reused_over_http(env):
    cookies = [
        {"name": "bunny_shield", "value": "ok", "domain": ".forum.test", "path": "/", "expires": -1, "secure": True},
        {"name": "other", "value": "x", "domain": "elsewhere.test", "path": "/", "expires": -1},
    ]
    stub = StubBrowser(rendered(ARTICLE, url="https://forum.test/t/1/", cookies=cookies))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("cookie") == "bunny_shield=ok":
            assert request.headers["user-agent"] == "BrowserUA/149"
            return html_response()
        return html_response(CHALLENGE, status=403)

    web, requests = env(handler, stub)
    first = await web.fetch("anon", "https://forum.test/t/1/", 8000)
    assert " · browser · " in first.splitlines()[0] and "Why hydration matters" in first
    assert "cookie" not in requests[0].headers and "BrowserUA" not in requests[0].headers["user-agent"]
    assert web.s.hosts.get("web:browser:forum.test") == "cookies"

    second = await web.fetch("anon", "https://forum.test/t/2/", 8000)
    assert " · web · " in second.splitlines()[0]
    assert stub.calls == ["https://forum.test/t/1/"]
    assert requests[-1].headers["cookie"] == "bunny_shield=ok"


@pytest.mark.asyncio
async def test_plain_403_retries_once_with_an_honest_user_agent_and_remembers_it(env):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["user-agent"] == web_module.BOT_UA:
            return html_response()
        return httpx.Response(403, text="Please set a user-agent and respect our robot policy")

    stub = StubBrowser(RuntimeError("must not be used"))
    web, requests = env(handler, stub)
    assert " · web · " in await web.fetch("anon", "https://wiki.test/A", 8000)
    assert len(requests) == 2
    await web.fetch("anon", "https://wiki.test/B", 8000)
    assert len(requests) == 3 and requests[-1].headers["user-agent"] == web_module.BOT_UA
    assert stub.calls == []


def test_trafilatura_artifacts_are_removed_outside_code():
    md = (  # shapes seen in trafilatura output for Wikipedia
        "[City University](https://w.test/wiki/City)<sup>[\\[1\\]](https://w.test#cite_note-1)</sup> in the 1980s."
        "\\[2\\] x<sup>2</sup> H<sub>2</sub>O <span class=\"a\">kept</span>\n\n## Ranking\n\n"
        "\\[\n[edit](https://w.test/w/index.php?action=edit)\\]\n\nSee `a\\[1\\]` and \\[note\\]."
    )
    body, footer = clean_markdown(md, "https://w.test/wiki/BM25", links=True)
    assert body == "City University in the 1980s. x^2 H_2O kept\n\n## Ranking\n\nSee `a\\[1\\]` and [note]."
    assert footer == "Links:\n- City University: https://w.test/wiki/City"


@pytest.mark.asyncio
async def test_remembered_host_skips_static_and_short_pages_keep_static(env):
    cookies = [{"name": "sid", "value": "1", "domain": "spa.test", "path": "/"}]
    stub = StubBrowser(rendered(ARTICLE, url="https://spa.test/a", cookies=cookies))
    shell = '<html><head><title>App</title></head><body><div id="root"></div></body></html>'
    web, requests = env(lambda r: html_response(shell), stub)
    assert " · browser · " in await web.fetch("anon", "https://spa.test/a", 8000)
    assert len(requests) == 1 and web._jar("spa.test") is None  # cookies don't fix a JS shell
    assert " · browser · " in await web.fetch("anon", "https://spa.test/b", 8000)
    assert len(requests) == 1 and len(stub.calls) == 2  # memory skips the doomed static attempt

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("tarpit", request=request)

    web, requests = env(unreachable, StubBrowser(rendered(ARTICLE, url="https://slow.test/", cookies=cookies)))
    assert " · browser · " in await web.fetch("anon", "https://slow.test/a", 8000)
    assert " · browser · " in await web.fetch("anon", "https://slow.test/b", 8000)
    assert len(requests) == 1 and web.s.hosts.get("web:browser:slow.test") == "browser"

    tiny = "<html><head><title>Tiny</title></head><body><p>Just a short note.</p></body></html>"
    stub = StubBrowser(rendered(tiny, url="https://tiny.test/"))
    web, requests = env(lambda r: html_response(tiny), stub)
    text = await web.fetch("anon", "https://tiny.test/", 8000)
    assert text == "Tiny · https://tiny.test/ · web · 18 chars\n\nJust a short note."
    assert stub.calls == ["https://tiny.test/"] and web.s.hosts.get("web:browser:tiny.test") is None


@pytest.mark.asyncio
async def test_blocked_everywhere_is_upstream_blocked(env):
    web, _ = env(lambda r: html_response(CHALLENGE, status=403), StubBrowser(rendered(CHALLENGE)))
    with pytest.raises(Fail) as exc:
        await web.fetch("anon", "https://forum.test/t/1/", 8000)
    assert exc.value.code == "UPSTREAM_BLOCKED" and "403" in exc.value.message
    assert web._jar("forum.test") is None  # a failed challenge earns no cookies

    web, _ = env(lambda r: html_response(CHALLENGE), StubBrowser(rendered(CHALLENGE)))
    with pytest.raises(Fail) as exc:
        await web.fetch("anon", "https://forum.test/t/1/", 8000)
    assert exc.value.code == "UPSTREAM_BLOCKED" and "challenge" in exc.value.message

    stub = StubBrowser(RuntimeError("must not be used"))
    web, _ = env(lambda r: httpx.Response(403), stub)
    assert await code_of(web.fetch("anon", "https://example.com/paper.pdf", 8000)) == "UPSTREAM_BLOCKED"
    assert await code_of(web.fetch("anon", "https://example.com/missing", 8000)) == "UPSTREAM_BLOCKED"
    web, _ = env(lambda r: httpx.Response(404), stub)
    assert await code_of(web.fetch("anon", "https://example.com/gone", 8000)) == "NOT_FOUND"
    assert stub.calls == ["https://example.com/missing"]


def test_cookie_jar_matches_domain_path_secure_and_expiry():
    now = time.time()
    jar = Jar("UA", [
        {"name": "a", "value": "1", "domain": ".forum.test", "path": "/", "expires": -1},
        {"name": "b", "value": "2", "domain": "forum.test", "path": "/t/", "expires": now + 60},
        {"name": "c", "value": "3", "domain": "forum.test", "path": "/", "expires": now - 60},
        {"name": "d", "value": "4", "domain": "forum.test", "path": "/", "secure": True},
    ], expires=0)
    assert jar.header("https://forum.test/t/1/") == "a=1; b=2; d=4"
    assert jar.header("http://forum.test/other") == "a=1"
    assert jar.header("https://www.forum.test/") == "a=1"
    assert jar.header("https://evilforum.test/") == ""


# ---------------------------------------------------------------- forum adapter hook


@pytest.mark.asyncio
async def test_xenforo_adapter_docs_continue_across_forum_pages(env, monkeypatch):
    def parse_thread(html: str, url: str, links: bool) -> Doc:
        page = 2 if url.endswith("page-2") else 1
        return Doc(
            url=url,
            source="xenforo",
            text=f"#{page * 20 - 19} · user{page} · 2026-09-0{page}\n" + f"post body on page {page}. " * 10,
            title="Thread title",
            note=f"thread page {page}/2 · posts #{page * 20 - 19}–{page * 20}",
            next_url="https://forum.test/t/x.1/page-2" if page == 1 else "",
        )

    stub = types.ModuleType("src.core.sites.xenforo")
    stub.is_thread = lambda html: 'data-template="thread_view"' in html
    stub.parse_thread = parse_thread
    monkeypatch.setitem(sys.modules, "src.core.sites.xenforo", stub)

    web, requests = env(lambda r: html_response(XDA_LIKE))
    first = await web.fetch("anon", "https://forum.test/t/x.1/", 8000)
    head = first.splitlines()[0]
    assert head.startswith("Thread title · https://forum.test/t/x.1/ · xenforo · thread page 1/2 · posts #1–20 · ")
    assert " · next page: " in head
    second = await web.read("anon", head.rsplit("next page: ", 1)[1], 8000)
    head = second.splitlines()[0]
    assert head.startswith("Thread title · https://forum.test/t/x.1/page-2 · xenforo · thread page 2/2")
    assert "#21 · user2" in second
    assert [str(r.url) for r in requests] == ["https://forum.test/t/x.1/", "https://forum.test/t/x.1/page-2"]


# ---------------------------------------------------------------- browser layer (fake Playwright objects)


class FakeLocator:
    def __init__(self, text: str):
        self.text = text
        self.first = self

    async def count(self) -> int:
        return 1

    async def inner_text(self, timeout: int = 0) -> str:
        return self.text


class FakePage:
    def __init__(self, titles: list[str], html: str):
        self.titles, self.html, self.url = list(titles), html, ""
        self.goto_kwargs: dict = {}
        self.closed = False

    def on(self, event, handler) -> None:
        pass

    async def goto(self, url: str, **kwargs) -> None:
        self.url, self.goto_kwargs = url, kwargs

    async def title(self) -> str:
        return self.titles.pop(0) if len(self.titles) > 1 else self.titles[0]

    async def wait_for_load_state(self, *args, **kwargs) -> None:
        pass

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator("Real thread text. " * 20)

    async def content(self) -> str:
        return self.html

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page_factory):
        self.page_factory = page_factory
        self.pages: list[FakePage] = []
        self.closed = False
        self.route = AsyncMock()
        self.route_web_socket = AsyncMock()
        self.add_init_script = AsyncMock()

    async def new_page(self) -> FakePage:
        self.pages.append(self.page_factory())
        return self.pages[-1]

    async def cookies(self) -> list[dict]:
        return [{"name": "bunny", "value": "1", "domain": ".forum.test", "path": "/"}]

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    version = "149.0.7827.0"

    def __init__(self, page_factory):
        self.page_factory = page_factory
        self.contexts: list[FakeContext] = []
        self.context_kwargs: dict = {}

    def is_connected(self) -> bool:
        return True

    async def new_context(self, **kwargs) -> FakeContext:
        self.context_kwargs = kwargs
        self.contexts.append(FakeContext(self.page_factory))
        return self.contexts[-1]


def fake_browser(monkeypatch, page_factory, **kwargs) -> tuple[Browser, FakeChromium]:
    chromium = FakeChromium(page_factory)
    browser = Browser(AsyncMock(return_value=True), **kwargs)
    browser.user_agent = browser_user_agent(chromium.version)
    monkeypatch.setattr(browser, "start", AsyncMock(return_value=chromium))
    monkeypatch.setattr(browser_module, "POLL_S", 0.01)
    return browser, chromium


@pytest.mark.asyncio
async def test_browser_waits_out_the_challenge_title_and_reuses_host_contexts(monkeypatch):
    titles = ["Establishing a secure connection ...", "Establishing a secure connection ...", "", "Thread | XDA Forums"]
    browser, chromium = fake_browser(monkeypatch, lambda: FakePage(titles, "<html>thread</html>"), max_contexts=2)
    result = await browser.render("https://forum.test/t/1/")
    page = chromium.contexts[0].pages[0]
    assert result.html == "<html>thread</html>" and result.url == "https://forum.test/t/1/"
    assert page.goto_kwargs == {"wait_until": "domcontentloaded", "timeout": 12000}
    assert page.closed and page.titles == ["Thread | XDA Forums"]
    assert result.cookies[0]["name"] == "bunny" and result.user_agent.endswith("Chrome/149.0.7827.0 Safari/537.36")
    assert chromium.context_kwargs["locale"] == "en-US" and chromium.context_kwargs["service_workers"] == "block"
    assert chromium.context_kwargs["viewport"] == {"width": 1365, "height": 768}
    chromium.contexts[0].add_init_script.assert_awaited_once_with(browser_module.INIT_SCRIPT)

    await browser.render("https://forum.test/t/2/")
    assert len(chromium.contexts) == 1  # same host, same context (cookies survive)
    await browser.render("https://a.test/")
    await browser.render("https://b.test/")
    assert len(chromium.contexts) == 3 and chromium.contexts[0].closed  # LRU beyond 2 contexts
    await browser.aclose()
    assert all(c.closed for c in chromium.contexts)


@pytest.mark.asyncio
async def test_browser_route_blocks_heavy_resources_and_private_hosts(env):
    web, _ = env(lambda r: httpx.Response(404))
    real = Browser(web._allowed)

    class Route:
        def __init__(self, url: str, kind: str):
            self.request = types.SimpleNamespace(url=url, resource_type=kind)
            self.outcome = ""

        async def abort(self) -> None:
            self.outcome = "abort"

        async def continue_(self) -> None:
            self.outcome = "continue"

    cases = {
        ("https://cdn.test/app.js", "script"): "continue",
        ("https://cdn.test/hero.jpg", "image"): "abort",
        ("https://cdn.test/f.woff2", "font"): "abort",
        ("http://internal.test/admin", "xhr"): "abort",
        ("http://169.254.169.254/latest", "fetch"): "abort",
        ("file:///etc/passwd", "document"): "abort",
    }
    for (url, kind), expected in cases.items():
        route = Route(url, kind)
        await real._route(route)
        assert route.outcome == expected, url


# ---------------------------------------------------------------- MCP wiring


@pytest.mark.asyncio
async def test_fetch_and_read_tools_return_plain_text(make_client, monkeypatch):
    monkeypatch.setattr(web_module, "Browser", lambda *a, **k: StubBrowser(RuntimeError("no browser")))
    paragraphs = "\n\n".join(f"Paragraph {i} " + "words " * 40 for i in range(20))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/markdown"}, text=paragraphs)

    async with make_client(handler) as (client, _):
        first = await client.call_tool("fetch_content", {"url": "https://example.com/a.md", "max_chars": 1000})
        assert first.structured_content is None and not first.is_error
        head = first.content[0].text.splitlines()[0]
        assert head.startswith("https://example.com/a.md · web · chars 0–")
        nxt = await client.call_tool("read_content", {"cursor": head.rsplit("next: ", 1)[1], "max_chars": 1000})
        assert nxt.content[0].text.startswith("https://example.com/a.md · web · chars 9")
        assert "words" in nxt.content[0].text.split("\n\n", 1)[1]
        bad = await client.call_tool("fetch_content", {"url": "http://127.0.0.1/"}, raise_on_error=False)
        assert bad.is_error and bad.content[0].text.startswith("ERROR INVALID_ARGUMENT: ")


# ---------------------------------------------------------------- live (pytest -m integration)

XDA_THREAD = "https://xdaforums.com/t/unlock-magisk-twrp-firetv-3nd-gen-cube-gazelle-ps7717.4800530/"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_xda_thread_through_bunny_shield(request):
    if "integration" not in (request.config.getoption("markexpr") or ""):
        pytest.skip("live test; run with -m integration")
    pytest.importorskip("playwright.async_api")
    from src.core.config import Settings
    from src.core.services import Services

    services = Services(Settings.from_env())
    web = services.component("web", Web)
    try:
        started = time.perf_counter()
        first = await web.fetch("live", XDA_THREAD, 8000)
        first_s = time.perf_counter() - started
        head = first.splitlines()[0]
        assert "#1 · " in first or "Pro-me3us" in first, head
        cursor = head.rsplit(": ", 1)[1]
        started = time.perf_counter()
        text = await web.read("live", cursor, 20000)
        while "#21 · " not in text and " · next" in text.splitlines()[0]:
            text = await web.read("live", text.splitlines()[0].rsplit(": ", 1)[1], 20000)
        second_s = time.perf_counter() - started
        print(f"\nXDA first call {first_s:.2f}s, next page {second_s:.2f}s\n{head}\n{text.splitlines()[0]}")
        assert "#21 · " in text
        assert second_s < first_s
    finally:
        await services.aclose()
