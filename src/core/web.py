"""fetch_content / read_content: SSRF-guarded download, extraction chain, compact views.

Pipeline: validate every hop -> stream (8 MiB; 32 MiB for PDFs; binary types rejected
early) -> PDF (pdfium) | markdown/plain passthrough | HTML (XenForo adapter, else
trafilatura) -> headless browser on 403/429/503, bot challenges, short extractions and JS
app shells. A host that needed the browser is remembered for 1 h; once the browser has
cleared a challenge its cookies are replayed over plain HTTP with the browser's UA.
The extracted doc is cached 15 min; outline/excerpts views are derived from that copy.
"""

from __future__ import annotations

import asyncio
import importlib
import ipaddress
import json
import logging
import math
import re
import socket
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from html import unescape
from typing import TYPE_CHECKING
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import httpx

from .browser import Browser, challenge_title
from .errors import (
    INVALID_ARGUMENT,
    RATE_LIMITED,
    TOO_LARGE,
    UNSUPPORTED_CONTENT,
    UPSTREAM_BLOCKED,
    UPSTREAM_ERROR,
    UPSTREAM_TIMEOUT,
    Fail,
    for_status,
)
from .pages import CONTENT, DOC_TTL, Doc
from .pdf import extract_pdf
from .text import clip, join, oneline, tidy, trunc

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger("web_mcp.web")

MAX_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 32 * 1024 * 1024
MAX_TEXT = 8 * 1024 * 1024
MAX_REDIRECTS = 6
HEADERS_TIMEOUT = 12.0  # time to response headers per hop; the body is bounded by the tool deadline
SHORT = 300  # extraction shorter than this retries with recall, then tries the browser
SHELL_TEXT = 1500  # JS-shell markers only count when the static text is this thin
CHALLENGE_PAGE = 10_000
SNIFF = 1024
LINKS_CAP = 50
JAR_TTL = 3600.0
MAX_JARS = 64
DNS_TTL = 60.0
BLOCK_STATUSES = (403, 429, 503)
MODES = ("content", "outline", "excerpts")
KEY = "web:"
MEMORY = "web:browser:"
UA_MEMORY = "web:ua:"
ACCEPT = "text/markdown, text/html;q=0.9, */*;q=0.5"
LANGUAGE = "en-US,en;q=0.9"
# Some sites (Wikimedia) 403 browser UAs from non-browser clients but accept an honest one.
BOT_UA = "web-mcp/3 (+https://github.com/kengbailey/web-mcp)"
PEEK_BYTES = 256 * 1024

PDF_TYPES = ("application/pdf", "application/x-pdf")
MEDIA_PREFIXES = ("image/", "video/", "audio/", "font/")
BINARY_TYPES = frozenset({
    "application/octet-stream", "binary/octet-stream", "application/zip", "application/gzip",
    "application/x-gzip", "application/x-tar", "application/x-7z-compressed", "application/vnd.rar",
    "application/x-rar-compressed", "application/x-msdownload", "application/wasm",
})
SECURITY_PHRASES = (
    "this website is using a security service to protect itself from online attacks",
    "we are checking your browser to establish a secure connection",
    "this website uses a security service to protect against malicious bots",
    "performing security verification",
)

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SHELL = re.compile(
    r"<div\s+(?:[^>]*\s)?id=[\"']?(?:root|app|__next|__nuxt)[\"']?[^>]*>\s*</div>"
    r"|__NEXT_DATA__"
    r"|<noscript\b[^>]*>(?:(?!</noscript>).){0,600}?enable\s+javascript",
    re.I | re.S,
)
_CHARSET = re.compile(r"charset=[\"']?([\w.:-]+)", re.I)
_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", re.I)
_TARGET = r"((?:[^()\s]|\([^()\s]*\))*)(?:\s+\"[^\"]*\")?\)"
_CODE = r"(`[^`\n]+`)"
_IMAGE = re.compile(_CODE + r"|!\[([^\]]*)\]\(" + _TARGET + r"([ \t]*)")
_LINK = re.compile(_CODE + r"|\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(" + _TARGET)
_NOISE_ALT = re.compile(r"^image(\s*\d+)?\s*(:|$)", re.I)
_MARKER_LINK = re.compile(r"^(?:\\?\[[^\]]{0,30}\\?\]|edit|\^)$", re.I)  # [1], \[2\], edit: not worth a footer line
# trafilatura leftovers: citation/[edit] markers, inline <sup>/<sub>/<span>, escaped brackets.
_ARTIFACTS = re.compile(
    _CODE
    + r"|(<sup\b[^>]*>\s*\\?\[[^<\]]{0,30}\\?\]\s*</sup>|\\\[\s*(?:\d{1,3}|[a-z]|edit|citation needed)\s*\\\])"
    + r"|(<sup\b[^>]*>)|(<sub\b[^>]*>)|(</?(?:sup|sub|span|small|abbr|mark|ins|del|u|s)\b[^>]*>)"
    + r"|\\([\[\]])",
    re.I,
)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^#{1,6}\s+\S")
_PARAGRAPHS = re.compile(r"\n\s*\n")
_WORD = re.compile(r"\w+")


# ---------------------------------------------------------------- URL safety


def _target(url: str) -> tuple[str, int]:
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        raise Fail(INVALID_ARGUMENT, "malformed URL") from None
    if p.scheme not in ("http", "https") or not p.hostname or p.username is not None or p.password is not None:
        raise Fail(INVALID_ARGUMENT, "only public http(s) URLs without credentials are supported")
    return p.hostname.lower(), port or (443 if p.scheme == "https" else 80)


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


async def _check_addresses(host: str, port: int) -> None:
    try:
        addresses = await _resolve(host, port)
    except (OSError, UnicodeError, ValueError):
        raise Fail(UPSTREAM_ERROR, "URL hostname could not be resolved") from None
    if not addresses or not all(_is_public(a) for a in addresses):
        raise Fail(INVALID_ARGUMENT, "private, loopback and reserved addresses are not supported")


async def validate_url(url: str) -> None:
    """http(s) only, no credentials, and every resolved address must be global."""
    await _check_addresses(*_target(url))


def _normalize(url: str) -> str:
    host, port = _target(url)
    p = urlsplit(url)
    netloc = f"[{host}]" if ":" in host else host
    if port != (443 if p.scheme == "https" else 80):
        netloc += f":{port}"
    return urlunsplit((p.scheme, netloc, p.path or "/", p.query, ""))


def _doc_key(url: str, links: bool) -> str:
    return f"{KEY}{int(links)}:{_normalize(url)}"


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _is_pdf_url(url: str) -> bool:
    path = urlsplit(url.lower()).path
    return path.endswith(".pdf") or "/pdf/" in path


# ---------------------------------------------------------------- page classification


def is_challenge(html: str) -> bool:
    """Bot/security interstitial: by its title, or a short page with challenge wording.

    Never by bare markers such as ``/.bunny-shield/``: real XDA pages contain them.
    """
    if not html:
        return False
    match = _TITLE.search(html)
    if match and challenge_title(unescape(match.group(1))):
        return True
    if len(html) >= CHALLENGE_PAGE:
        return False
    normalized = " ".join(html.casefold().split())
    return any(phrase in normalized for phrase in SECURITY_PHRASES)


def is_app_shell(html: str, text_chars: int) -> bool:
    """Empty SPA mount, Next.js data blob or an "enable JavaScript" noscript, with little text."""
    return text_chars < SHELL_TEXT and _SHELL.search(html) is not None


def _kind(ctype: str, head: bytes, url: str) -> str:
    if head.lstrip()[:4] == b"%PDF" or ctype in PDF_TYPES:
        return "pdf"
    if ctype in ("text/html", "application/xhtml+xml"):
        return "html"
    if ctype in ("text/markdown", "text/x-markdown"):
        return "markdown"
    if ctype in BINARY_TYPES or ctype.startswith(MEDIA_PREFIXES):
        return "binary"
    if ctype:
        if ctype == "text/plain" and urlsplit(url).path.lower().endswith((".md", ".markdown")):
            return "markdown"
        if ctype.startswith("text/") or ctype.endswith(("json", "xml", "javascript")):
            return "text"
    sniff = head.lstrip()[:256].lower()
    if sniff.startswith((b"<!doctype html", b"<html")) or b"<head" in sniff or b"<body" in sniff:
        return "html"
    return "binary" if b"\x00" in head else "text"


def _decode(data: bytes, charset: str) -> str:
    match = None if charset else _META_CHARSET.search(data[:4096])
    encoding = charset or (match.group(1).decode("ascii", "ignore") if match else "utf-8")
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- markdown cleanup


def _segments(md: str) -> list[tuple[bool, str]]:
    """Split markdown into (is_code, text) runs so cleanup never touches fenced code."""
    out: list[tuple[bool, str]] = []
    buf: list[str] = []
    fence = ""
    for line in md.split("\n"):
        match = _FENCE.match(line)
        if not fence and match:
            if buf:
                out.append((False, "\n".join(buf)))
            fence, buf = match.group(1), [line]
        elif fence and match and line.strip().startswith(fence) and not line.strip().strip(fence[0]):
            buf.append(line)
            out.append((True, "\n".join(buf)))
            fence, buf = "", []
        else:
            buf.append(line)
    if buf:
        out.append((bool(fence), "\n".join(buf)))
    return out


def clean_markdown(md: str, base: str, links: bool) -> tuple[str, str]:
    """Images -> alt text, ``[text](url)`` -> text; returns (body, ``Links:`` footer or "")."""
    found: dict[str, str] = {}
    page = urldefrag(base)[0]

    def image(m: re.Match) -> str:
        if m.group(1):
            return m.group(1)
        alt = oneline(m.group(2))
        return "" if not alt or _NOISE_ALT.match(alt) else alt + m.group(4)

    def artifact(m: re.Match) -> str:
        if m.group(1) or m.group(6):
            return m.group(1) or m.group(6)
        return "^" if m.group(3) else "_" if m.group(4) else ""

    def link(m: re.Match) -> str:
        if m.group(1):
            return m.group(1)
        text = oneline(m.group(2))
        if links and len(found) < LINKS_CAP and not _MARKER_LINK.match(text):
            href = m.group(3)
            href = href[1:-1] if href.startswith("<") and href.endswith(">") else href
            url = urldefrag(urljoin(base, href))[0]
            if url.startswith(("http://", "https://")) and url != page and url not in found:
                found[url] = trunc(text, 80)
        return text

    parts = []
    for code, segment in _segments(md):
        if code:
            parts.append("\n".join(line.rstrip() for line in segment.split("\n")).strip("\n"))
        else:
            parts.append(tidy(_ARTIFACTS.sub(artifact, _LINK.sub(link, _IMAGE.sub(image, segment)))))
    body = "\n\n".join(p for p in parts if p)
    footer = "Links:\n" + "\n".join(f"- {t}: {u}" if t else f"- {u}" for u, t in found.items()) if found else ""
    return body, footer


def _compose(body: str, footer: str) -> str:
    return body + ("\n\n" + footer if footer and body else footer)


def _plain(text: str) -> str:
    """Plain text keeps its indentation (code, logs); only trailing space and blank runs go."""
    lines = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    return re.sub(r"\n{3,}", "\n\n", lines).strip()


# ---------------------------------------------------------------- HTML extraction (worker thread)


def _trafilatura(html: str, url: str, fast: bool) -> tuple[str, str]:
    import trafilatura

    out = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_images=False,
        with_metadata=True,
        fast=fast,
        favor_recall=not fast,
    ) or ""
    title = ""
    if out.startswith("---\n") and (end := out.find("\n---\n", 3)) > 0:
        for line in out[4:end].split("\n"):
            if line.startswith("title: "):
                title = line[7:].strip()
                if title.startswith('"'):
                    try:
                        title = json.loads(title)
                    except ValueError:
                        pass
        out = out[end + 5:]
    return title, out


def _html_title(html: str) -> str:
    match = _TITLE.search(html)
    return oneline(unescape(match.group(1))) if match else ""


def _forum(html: str, url: str, links: bool) -> Doc | None:
    try:
        xenforo = importlib.import_module(f"{__package__}.sites.xenforo")
    except ImportError:
        return None
    try:
        if xenforo.is_thread(html):
            return xenforo.parse_thread(html, url, links)
    except Exception:
        log.exception("xenforo adapter failed")
    return None


_TITLE_SUFFIX = re.compile(r"\s+[|\-–—·:]\s+([^|\-–—·:]{2,40})$")


def _site_title(title: str, url: str) -> str:
    """Drop a trailing " | Site" / " - Wikipedia" when the suffix names the host."""
    title = oneline(title)
    match = _TITLE_SUFFIX.search(title)
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if match and match.start() > 0:
        site = re.sub(r"[^a-z0-9.]", "", match.group(1).lower())
        if site and (site in host or host.split(".")[0] in site):
            return title[: match.start()]
    return title


def _drop_title_lines(body: str, title: str) -> str:
    """The header already carries the title; drop copies of it at the top of the body."""
    if not title:
        return body
    key = oneline(title).casefold()
    lines = body.split("\n")
    i = 0
    while i < min(len(lines), 6) and (not lines[i].strip() or oneline(lines[i].lstrip("#")).casefold() == key):
        i += 1
    return "\n".join(lines[i:]) if i and any(lines[:i]) else body


def extract_html(html: str, url: str, links: bool, source: str) -> tuple[Doc | None, str]:
    """Returns (doc, reason); a non-empty reason asks for the browser.

    reason: "challenge" (doc is None), "short" (< 300 chars) or "shell" (JS app shell).
    """
    if is_challenge(html):
        return None, "challenge"
    forum = _forum(html, url, links)
    if forum is not None:
        return forum, ""
    title, md = _trafilatura(html, url, fast=True)
    body, footer = clean_markdown(md, url, links)
    if len(body) < SHORT:
        title2, md2 = _trafilatura(html, url, fast=False)
        body2, footer2 = clean_markdown(md2, url, links)
        if len(body2) > len(body):
            title, body, footer = title2 or title, body2, footer2
    title = _site_title(title or _html_title(html), url)
    body = _drop_title_lines(body, title)
    doc = Doc(url=url, source=source, text=_compose(body, footer), title=trunc(title, 200))
    if len(body) < SHORT:
        return doc, "short"
    return doc, "shell" if is_app_shell(html, len(body)) else ""


# ---------------------------------------------------------------- views


def outline(text: str) -> str:
    return "\n".join(
        line.strip()
        for code, segment in _segments(text)
        if not code
        for line in segment.split("\n")
        if _HEADING.match(line.strip())
    )


def _terms(text: str) -> list[str]:
    """Lower-cased words with a plural "s" folded (dogs -> dog), applied to query and text alike."""
    return [w[:-1] if len(w) > 3 and w[-1] == "s" and w[-2] != "s" else w for w in _WORD.findall(text.casefold())]


def excerpts(text: str, query: str, max_chars: int, k1: float = 1.2, b: float = 0.75) -> str:
    """BM25-ranked paragraphs within ``max_chars``, in document order, tagged ``[¶n]``."""
    paragraphs = [p.strip() for p in _PARAGRAPHS.split(text)]
    paragraphs = [p for p in paragraphs if p]
    terms = list(dict.fromkeys(_terms(query)))
    if not paragraphs or not terms:
        return ""
    bags = [Counter(_terms(p)) for p in paragraphs]
    lengths = [sum(bag.values()) for bag in bags]
    avg = (sum(lengths) / len(bags)) or 1.0
    n = len(bags)
    idf = {}
    for t in terms:
        df = sum(1 for bag in bags if t in bag)
        if df:
            idf[t] = math.log(1 + (n - df + 0.5) / (df + 0.5))
    scored = []
    for i, bag in enumerate(bags):
        norm = k1 * (1 - b + b * lengths[i] / avg)
        score = sum(idf[t] * bag[t] * (k1 + 1) / (bag[t] + norm) for t in idf if t in bag)
        if score > 0:
            scored.append((score, i))
    if not scored:  # e.g. CJK text without word breaks: fall back to substring matching
        words = query.casefold().split()
        scored = [(1.0, i) for i, p in enumerate(paragraphs) if any(w in p.casefold() for w in words)]
    chosen: dict[int, str] = {}
    used = 0
    for _, i in sorted(scored, key=lambda item: (-item[0], item[1])):
        piece = f"[¶{i + 1}] {paragraphs[i]}"
        if used + len(piece) + 2 > max_chars:
            if chosen:
                break
            piece = clip(piece, max_chars).rstrip()
        chosen[i] = piece
        used += len(piece) + 2
    return "\n\n".join(chosen[i] for i in sorted(chosen))


# ---------------------------------------------------------------- fetcher


@dataclass
class Fetched:
    url: str
    status: int
    kind: str = ""  # pdf | html | markdown | text
    data: bytes = b""
    charset: str = ""
    challenge: bool = False  # a 403/429/503 whose body is a bot challenge


@dataclass
class Attempt:
    doc: Doc | None = None
    reason: str = ""  # why the browser should try ("HTTP 403", "challenge", "short", "shell", ...)
    status: int = 0
    error: BaseException | None = None
    pdf: bool = False


@dataclass
class Jar:
    """Cookies a browser context earned for a host, replayed over plain HTTP with its UA."""

    user_agent: str
    cookies: list[dict]
    expires: float

    def header(self, url: str) -> str:
        p = urlsplit(url)
        host, path, now = (p.hostname or "").lower(), p.path or "/", time.time()
        pairs = []
        for c in self.cookies:
            domain = str(c.get("domain") or "").lower()
            bare = domain.lstrip(".")
            if not bare or not (host == bare or (domain.startswith(".") and host.endswith("." + bare))):
                continue
            if not path.startswith(c.get("path") or "/") or (c.get("secure") and p.scheme != "https"):
                continue
            expires = c.get("expires") or -1
            if 0 < expires < now:
                continue
            pairs.append(f"{c.get('name')}={c.get('value')}")
        return "; ".join(pairs)


class Web:
    def __init__(self, services: Services):
        self.s = services
        self.browser = Browser(self._allowed, proxy=services.settings.proxy_url)
        self._jars: OrderedDict[str, Jar] = OrderedDict()
        self._dns: dict[str, float] = {}

    # -- tool entry points

    async def fetch(
        self, owner: str, url: str, max_chars: int = 8000, mode: str = "content", query: str = "", links: bool = False
    ) -> str:
        url = url.strip()
        if mode not in MODES:
            raise Fail(INVALID_ARGUMENT, "mode must be content, outline or excerpts")
        if mode == "excerpts" and not query.strip():
            raise Fail(INVALID_ARGUMENT, "excerpts mode needs a query")
        key = _doc_key(url, links)
        doc = await self._doc(key, url, links)
        if mode != "content":
            key, doc = self._view(key, doc, mode, query.strip(), max_chars)
        return self.s.pages.page(owner, key, doc, 0, max_chars).text

    async def read(self, owner: str, cursor: str, max_chars: int = 8000) -> str:
        chunk = self.s.pages.read(owner, cursor, max_chars)
        doc = chunk.exhausted_doc
        if doc is None:
            return chunk.text
        key, _ = self.s.cursors.get(owner, cursor, CONTENT)
        links = key.startswith(f"{KEY}1:")
        next_key = _doc_key(doc.next_url, links)
        nxt = await self._doc(next_key, doc.next_url, links)
        return self.s.pages.page(owner, next_key, nxt, 0, max_chars).text

    async def prewarm(self) -> None:
        try:
            await self.browser.start()
        except Exception as exc:  # a missing Chromium must not stop the server; fetches degrade
            log.warning("browser prewarm failed: %s: %s", type(exc).__name__, str(exc)[:200])

    async def aclose(self) -> None:
        await self.browser.aclose()

    # -- documents and views

    async def _doc(self, key: str, url: str, links: bool) -> Doc:
        return await self.s.cache.get_or_load(key, DOC_TTL, lambda: self._load(url, links))

    def _view(self, key: str, doc: Doc, mode: str, query: str, max_chars: int) -> tuple[str, Doc]:
        view_key = f"{key}|outline" if mode == "outline" else f"{key}|excerpts|{max_chars}|{query.casefold()}"
        view = self.s.cache.get(view_key)
        if view is None:
            if mode == "outline":
                text = outline(doc.text) or "(no headings; use mode=content)"
            else:
                text = excerpts(doc.text, query, max_chars) or f"(no paragraphs match {trunc(query, 80)!r})"
            view = Doc(url=doc.url, source=doc.source, text=text, title=doc.title, note=join(doc.note, mode))
            self.s.pages.store(view_key, view)
        return view_key, view

    async def _load(self, url: str, links: bool) -> Doc:
        started = time.perf_counter()
        host = _host(url)
        memory = MEMORY + host
        # "cookies": blocked/challenged until the browser earned cookies (replay them over HTTP);
        # "browser": static is doomed (JS shell, thin text, network-level failure).
        remembered = self.s.hosts.get(memory)
        pdf_url = _is_pdf_url(url)
        static_first = pdf_url or remembered is None or self._jar(host) is not None
        first = await self._static(url, links) if static_first else Attempt(reason="remembered")
        if first.doc is not None and not first.reason:
            return self._done(first.doc, host, started, "static")
        if pdf_url or first.pdf:
            raise self._failure(first, "")  # headless Chromium downloads PDFs instead of rendering them
        cookies = first.status in BLOCK_STATUSES or first.reason == "challenge" or remembered == "cookies"
        doc, reason, status = await self._browse(url, links, cookies)
        if doc is not None and self._better(first, doc, status):
            self.s.hosts.set(memory, "cookies" if cookies else "browser")
            return self._done(doc, host, started, f"browser ({first.reason})")
        if not static_first:
            self.s.hosts.forget(memory)
            first = await self._static(url, links)
            if first.doc is not None and not first.reason:
                return self._done(first.doc, host, started, "static after browser")
        if first.doc is not None and first.doc.text:
            return self._done(first.doc, host, started, f"static ({first.reason})")
        if status in (404, 410):
            raise for_status(status, "page")
        raise self._failure(first, reason)

    @staticmethod
    def _better(first: Attempt, doc: Doc, status: int) -> bool:
        if not doc.text or (status >= 400 and len(doc.text) < SHORT):
            return False
        static = first.doc
        if static is None or not static.text:
            return True
        if first.reason == "shell":
            return len(doc.text) >= min(SHORT, len(static.text))
        return len(doc.text) > len(static.text)

    @staticmethod
    def _failure(first: Attempt, browser_reason: str) -> Fail:
        if isinstance(first.error, (TimeoutError, httpx.TimeoutException)):
            return Fail(UPSTREAM_TIMEOUT, "the site did not respond in time")
        if first.error is not None:
            return Fail(UPSTREAM_ERROR, "could not connect to the site")
        if first.status == 429:
            return Fail(RATE_LIMITED, "the site returned HTTP 429 (rate limited)")
        if first.status:
            return Fail(UPSTREAM_BLOCKED, f"the site returned HTTP {first.status} and blocks automated access")
        if "challenge" in (first.reason, browser_reason):
            return Fail(UPSTREAM_BLOCKED, "the site served a bot challenge that did not clear")
        return Fail(UPSTREAM_BLOCKED, "the page could not be loaded")

    def _done(self, doc: Doc, host: str, started: float, path: str) -> Doc:
        if not doc.text.strip():
            raise Fail(UNSUPPORTED_CONTENT, "the page has no extractable text")
        if len(doc.text) > MAX_TEXT:
            raise Fail(TOO_LARGE, "extracted text exceeds 8 MiB")
        log.info("web %s via %s: %d chars in %.2fs", host, path, len(doc.text), time.perf_counter() - started)
        return doc

    # -- static path

    async def _static(self, url: str, links: bool) -> Attempt:
        host = _host(url)
        ua = BOT_UA if self.s.hosts.get(UA_MEMORY + host) == "bot" else ""
        try:
            got = await self._download(url, ua)
            if got.status == 403 and not got.challenge and not ua and self._jar(host) is None:
                retry = await self._download(url, BOT_UA)
                if retry.status not in BLOCK_STATUSES:
                    if retry.status < 300:
                        self.s.hosts.set(UA_MEMORY + host, "bot")
                    got = retry
        except (TimeoutError, httpx.TimeoutException) as exc:
            return Attempt(reason="timeout", error=exc)
        except httpx.TransportError as exc:
            return Attempt(reason="unreachable", error=exc)
        if got.status in BLOCK_STATUSES:
            return Attempt(reason="challenge" if got.challenge else f"HTTP {got.status}", status=got.status)
        if got.status >= 300:
            raise for_status(got.status, "page")
        doc, reason = await self._extract(got, links)
        return Attempt(doc, reason, pdf=got.kind == "pdf")

    async def _download(self, url: str, ua: str = "") -> Fetched:
        client = self.s.net.client("web")
        for _ in range(MAX_REDIRECTS + 1):
            await validate_url(url)
            headers = {"Accept": ACCEPT, "Accept-Language": LANGUAGE}
            if ua:
                headers["User-Agent"] = ua
            jar = self._jar(_host(url))
            if jar is not None:
                headers["User-Agent"] = jar.user_agent
                if cookie := jar.header(url):
                    headers["Cookie"] = cookie
            async with asyncio.timeout(HEADERS_TIMEOUT) as deadline:
                async with client.stream("GET", url, headers=headers) as r:
                    deadline.reschedule(None)
                    if r.is_redirect:
                        url = urljoin(url, r.headers["location"])
                        continue
                    if r.status_code >= 300:
                        challenge = r.status_code in BLOCK_STATUSES and is_challenge(await self._peek(r))
                        return Fetched(url, r.status_code, challenge=challenge)
                    return await self._body(url, r)
        raise Fail(UPSTREAM_ERROR, "too many redirects")

    @staticmethod
    async def _peek(r: httpx.Response) -> str:
        data = bytearray()
        async for chunk in r.aiter_bytes():
            data += chunk
            if len(data) > PEEK_BYTES:
                return ""  # challenge pages are small
        return data.decode("utf-8", errors="replace")

    async def _body(self, url: str, r: httpx.Response) -> Fetched:
        header = r.headers.get("content-type", "")
        ctype = header.split(";", 1)[0].strip().lower()
        if ctype.startswith(MEDIA_PREFIXES):
            raise Fail(UNSUPPORTED_CONTENT, f"{ctype} content is not supported")
        try:
            length = int(r.headers.get("content-length", "0"))
        except ValueError:
            length = 0
        may_be_pdf = not ctype or ctype in PDF_TYPES or ctype in BINARY_TYPES
        if length > MAX_PDF_BYTES or (length > MAX_BYTES and not may_be_pdf):
            raise Fail(TOO_LARGE, "the document is larger than the download limit")
        data, kind, limit = bytearray(), "", MAX_PDF_BYTES
        async for chunk in r.aiter_bytes():
            data += chunk
            if not kind and len(data) >= SNIFF:
                kind, limit = self._sniffed(ctype, bytes(data[:SNIFF]), url)
            if len(data) > limit:
                raise Fail(TOO_LARGE, f"the document is larger than {limit >> 20} MiB")
        if not kind:
            kind, _ = self._sniffed(ctype, bytes(data[:SNIFF]), url)
        match = _CHARSET.search(header)
        return Fetched(url, r.status_code, kind, bytes(data), match.group(1) if match else "")

    @staticmethod
    def _sniffed(ctype: str, head: bytes, url: str) -> tuple[str, int]:
        kind = _kind(ctype, head, url)
        if kind == "binary":
            raise Fail(UNSUPPORTED_CONTENT, f"{ctype or 'binary'} content is not supported")
        return kind, MAX_PDF_BYTES if kind == "pdf" else MAX_BYTES

    async def _extract(self, got: Fetched, links: bool) -> tuple[Doc | None, str]:
        if got.kind == "pdf":
            return Doc(url=got.url, source="pdf", text=await self.s.run("pdf", extract_pdf, got.data)), ""
        text = _decode(got.data, got.charset)
        if got.kind == "markdown":
            return Doc(url=got.url, source="web", text=_compose(*clean_markdown(text, got.url, links))), ""
        if got.kind == "text":
            return Doc(url=got.url, source="web", text=_plain(text)), ""
        return await self.s.run("cpu", extract_html, text, got.url, links, "web")

    # -- browser path

    async def _browse(self, url: str, links: bool, keep_cookies: bool) -> tuple[Doc | None, str, int]:
        if not keep_cookies:
            self._jars.pop(_host(url), None)  # cookies don't fix JS shells or network failures
        try:
            rendered = await self.browser.render(url)
        except Exception as exc:  # BrowserError, Playwright missing, browser crash
            log.warning("browser failed for %s: %s: %s", _host(url), type(exc).__name__, str(exc)[:200])
            return None, "browser failed", 0
        doc, reason = await self.s.run("cpu", extract_html, rendered.html, rendered.url, links, "browser")
        if keep_cookies and reason != "challenge" and rendered.cookies:
            jar = Jar(rendered.user_agent, rendered.cookies, time.monotonic() + JAR_TTL)
            for host in {_host(url), _host(rendered.url)}:
                self._jars[host] = jar
                self._jars.move_to_end(host)
            while len(self._jars) > MAX_JARS:
                self._jars.popitem(last=False)
        return doc, reason, rendered.status

    def _jar(self, host: str) -> Jar | None:
        jar = self._jars.get(host)
        if jar is not None and jar.expires <= time.monotonic():
            del self._jars[host]
            return None
        return jar

    async def _allowed(self, url: str) -> bool:
        """SSRF check for every browser sub-request; resolved hosts are cached briefly."""
        if url.split(":", 1)[0].lower() in ("data", "blob", "about"):
            return True
        try:
            host, port = _target(url)
            now = time.monotonic()
            if self._dns.get(host, 0.0) > now:
                return True
            await _check_addresses(host, port)
        except Fail:
            return False
        if len(self._dns) > 2048:
            self._dns.clear()
        self._dns[host] = now + DNS_TTL
        return True
