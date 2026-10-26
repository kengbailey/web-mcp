"""YouTube: video metadata, transcripts (captions, then speech-to-text) and comments.

One yt-dlp extraction per video (cached 1 h) feeds metadata (without an API key), json3
captions, the audio language and STT format selection. Transcripts are cached 24 h as
paragraphs, so timestamps on/off and paging never refetch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from . import stt
from .errors import (
    INVALID_ARGUMENT,
    NOT_CONFIGURED,
    NOT_FOUND,
    RATE_LIMITED,
    TOO_LARGE,
    UNAVAILABLE,
    UPSTREAM_BLOCKED,
    Fail,
    for_status,
)
from .pages import Doc
from .text import ago, block, join, more, num, oneline
from .youtube_ids import video_id, watch_url

log = logging.getLogger("web_mcp.youtube")

API_URL = "https://www.googleapis.com/youtube/v3/"
INFO_TTL = 3600
TX_TTL = 86400
API_TIMEOUT = 20.0
API_BLOCK = "yt:transcript-api"
COMMENTS = "youtube_comments"
PAGE_SIZE = 20
MAX_CAPTION_BYTES = 8 * 1024 * 1024
COMMENTS_TTL = 300  # same as Reddit
DESCRIPTION_CHARS = 1500  # long descriptions are mostly links/chapters; rest via cursor
COMMENT_CHARS = 500
# Caption failures that STT cannot fix (the audio path needs the same extraction).
NO_FALLBACK = {INVALID_ARGUMENT, NOT_FOUND, UPSTREAM_BLOCKED, RATE_LIMITED}
_API_ERRORS = {
    "commentsDisabled": (UNAVAILABLE, "comments are disabled for this video"),
    "quotaExceeded": (RATE_LIMITED, "YouTube API quota exceeded"),
    "videoNotFound": (NOT_FOUND, "video not found"),
    "commentNotFound": (NOT_FOUND, "comment not found"),
    "keyInvalid": (NOT_CONFIGURED, "YOUTUBE_API_KEY was rejected"),
}
_LANG = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*")
_COMMENT_ID = re.compile(r"[A-Za-z0-9_.-]{8,100}")
_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")
_NOISE = re.compile(
    r"[♪♫]+|>>|[\[(][^\])]{0,30}?\b(?:music|applause|laugh\w*|cheer\w*|inaudible|silence|noise|foreign)\b"
    r"[^\])]{0,30}[\])]",
    re.I,
)
_EMPTY = re.compile(r"[\[(][\s\W]*[\])]")
_SENTENCE_END = re.compile(r"[.!?…。！？][\"'”’)]*$")
_SENTENCES = re.compile(r"(?<=[.!?…。！？])\s+")


# ---- transcript formatting -------------------------------------------------------------

def clock(seconds: float) -> str:
    s = int(seconds)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _key(word: str) -> str:
    return re.sub(r"\W+", "", word.lower())


def paragraphs(fragments: list[tuple[float, str]], soft: float = 45, hard: float = 60) -> list[list]:
    """Merge caption fragments into ~45–60 s paragraphs: ``[[start, text], ...]``.

    Drops noise tags and adjacent/rolling repeats (a fragment that restates the tail of
    the previous text), and breaks at a sentence end once ``soft`` seconds have passed.
    """
    out: list[list] = []
    words: list[str] = []  # current paragraph
    recent: list[str] = []  # normalized tail across paragraph breaks
    prev = ""
    for start, raw in fragments:
        text = " ".join(_EMPTY.sub(" ", _NOISE.sub(" ", raw)).split())
        if not text:
            continue
        norm = text.lower()
        if norm == prev:
            continue
        prev = norm
        new = text.split()
        keys = [_key(w) for w in new]
        overlap = next((k for k in range(min(len(recent), len(keys)), 0, -1) if recent[-k:] == keys[:k]), 0)
        if overlap == len(new) and overlap >= 2 or 3 <= overlap < len(new):
            new, keys = new[overlap:], keys[overlap:]
        if not new:
            continue
        if out and words:
            elapsed = start - out[-1][0]
            if elapsed >= hard or (elapsed >= soft and _SENTENCE_END.search(words[-1])):
                out[-1][1] = " ".join(words)
                words = []
        if not words:
            out.append([start, ""])
        words.extend(new)
        recent = (recent + keys)[-40:]
    if out and words:
        out[-1][1] = " ".join(words)
    return [p for p in out if p[1]]


def stt_paragraphs(text: str, size: int = 600) -> list[str]:
    """Untimed STT text as ~``size``-char paragraphs, split at sentence ends."""
    out: list[str] = []
    current = ""
    for sentence in _SENTENCES.split(oneline(text)):
        while len(sentence) > size * 2:  # unpunctuated run: cut at a word boundary
            cut = sentence.rfind(" ", 0, size)
            cut = cut if cut > 0 else size
            head, sentence = sentence[:cut].strip(), sentence[cut:].strip()
            current = f"{current} {head}".strip()
            out.append(current)
            current = ""
        current = f"{current} {sentence}".strip()
        if len(current) >= size:
            out.append(current)
            current = ""
    if current:
        out.append(current)
    return out


def render(paras: list[list], timestamps: bool) -> str:
    return "\n\n".join(
        f"[{clock(start)}] {text}" if timestamps and start is not None else text for start, text in paras
    )


def parse_json3(raw: dict) -> list[tuple[float, str]]:
    out = []
    for event in raw.get("events") or []:
        text = "".join(seg.get("utf8", "") for seg in event.get("segs") or [])
        if text.strip():
            out.append((event.get("tStartMs", 0) / 1000, text))
    return out


# ---- language / track selection ---------------------------------------------------------

def _base(code: str) -> str:
    return re.split(r"[-_]", code.lower())[0]


def _match(codes: Any, want: str) -> str | None:
    """Exact code, else case-insensitive, else same base language (``en`` ~ ``en-US``)."""
    codes = list(codes)
    if want in codes:
        return want
    lower = want.lower()
    return next((c for c in codes if c.lower() == lower), None) or next(
        (c for c in codes if _base(c) == _base(want)), None
    )


def pick_track(info: dict, lang: str) -> tuple[str, str, str, bool] | None:
    """json3 track for ``lang`` (or the original language): (url, source, code, translated)."""
    subs = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    orig = stt.audio_language(info)
    want = lang or orig
    choice = None
    if want:
        if code := _match(subs, want):
            choice = (subs, code, "captions")
        elif code := f"{want}-orig" if f"{want}-orig" in auto else _match(auto, want):
            choice = (auto, code, "auto-captions")
    if not choice and not lang and subs:
        choice = (subs, next(iter(subs)), "captions")
    if not choice:
        return None
    table, code, source = choice
    track = next((t for t in table[code] if t.get("ext") == "json3" and t.get("url")), None)
    if not track:
        return None
    code = code.removesuffix("-orig")
    translated = source == "auto-captions" and bool(orig) and _base(code) != orig
    return track["url"], source, code, translated


# ---- blocking helpers (run in threads; tests stub these) --------------------------------

def _slim(info: dict) -> dict:
    """Keep json3 caption tracks only and drop bulky fields nothing here uses."""
    for key in ("subtitles", "automatic_captions"):
        tables = (info.get(key) or {}).items()
        json3 = {lang: [t for t in tracks if t.get("ext") == "json3"] for lang, tracks in tables}
        info[key] = {lang: tracks for lang, tracks in json3.items() if tracks}
    for key in ("thumbnails", "heatmap"):
        info.pop(key, None)
    return info


def extract_info(ident: str, proxy: str | None) -> dict:
    import yt_dlp

    opts = {**stt.ydl_options(proxy), "skip_download": True, "ignore_no_formats_error": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(watch_url(ident), download=False)
            info = ydl.sanitize_info(info, remove_private_keys=True)
    except yt_dlp.utils.DownloadError as exc:
        raise stt.ytdlp_fail(exc, "YouTube extraction") from None
    return _slim(info)


def _session() -> Any:
    import requests

    class Session(requests.Session):
        def request(self, *args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("timeout", 10)
            return super().request(*args, **kwargs)

    return Session()


def _pick_api(listing: Any, lang: str) -> tuple[Any, bool]:
    """(transcript, translated): requested language, else the original (manual first)."""
    manual = {t.language_code: t for t in listing if not t.is_generated}
    generated = {t.language_code: t for t in listing if t.is_generated}
    if lang:
        for pool in (manual, generated):
            if code := _match(pool, lang):
                return pool[code], False
        for transcript in listing:
            codes = [x.language_code for x in transcript.translation_languages]
            if code := _match(codes, lang):
                return transcript.translate(code), True
        return None, False
    if generated:  # the ASR track is in the original audio language
        orig = next(iter(generated))
        code = _match(manual, orig)
        return (manual[code] if code else generated[orig]), False
    return next(iter(manual.values()), None), False


def fetch_api_transcript(ident: str, lang: str, proxy: str | None) -> dict | None:
    """youtube-transcript-api fast path (3 HTTP calls, no JS). None when no track fits."""
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import GenericProxyConfig

    api = YouTubeTranscriptApi(
        proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy) if proxy else None,
        http_client=_session(),
    )
    transcript, translated = _pick_api(api.list(ident), lang)
    if transcript is None:
        return None
    fetched = transcript.fetch()
    paras = paragraphs([(s.start, s.text) for s in fetched])
    if not paras:
        return None
    return {
        "source": "auto-captions" if fetched.is_generated else "captions",
        "lang": fetched.language_code,
        "note": "translated" if translated else "",
        "paras": paras,
    }


def _blocked(exc: BaseException) -> bool:
    try:
        from youtube_transcript_api import RequestBlocked
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, RequestBlocked)


# ---- misc ---------------------------------------------------------------------------------

def _seconds(duration: str) -> int:
    match = _DURATION.fullmatch(duration or "")
    if not match:
        return 0
    return sum(int(v or 0) * unit for v, unit in zip(match.groups(), (86400, 3600, 60, 1)))


def _timestamp(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0


def _language(language: str) -> str:
    language = (language or "").strip()
    if language and not _LANG.fullmatch(language):
        raise Fail(INVALID_ARGUMENT, "language must be an ISO code such as en or pt-BR")
    return language


class YouTube:
    def __init__(self, services: Any):
        self.services = services
        self.settings = services.settings

    # -- shared extraction --

    async def info(self, ident: str) -> dict:
        proxy = self.settings.youtube_proxy_url
        return await self.services.cache.get_or_load(
            f"yt:info:{ident}", INFO_TTL, lambda: self.services.run("yt", extract_info, ident, proxy)
        )

    async def _api(self, endpoint: str, params: dict) -> dict:
        key = self.settings.youtube_api_key
        if not key:
            raise Fail(NOT_CONFIGURED, "YOUTUBE_API_KEY is required")
        # The Data API is keyed and never bot-checked, so it skips YOUTUBE_PROXY_URL ("api"
        # route); only page/caption/audio traffic needs the proxy.
        response = await self.services.net.client("api").get(
            API_URL + endpoint, params=params, headers={"x-goog-api-key": key}, timeout=15
        )
        if response.status_code >= 400:
            try:
                reason = response.json()["error"]["errors"][0]["reason"]
            except (KeyError, IndexError, TypeError, ValueError):
                reason = ""
            if reason in _API_ERRORS:
                raise Fail(*_API_ERRORS[reason])
            raise for_status(response.status_code, "YouTube API")
        return response.json()

    def _title(self, ident: str) -> str:
        cache = self.services.cache
        data = cache.get(f"yt:meta:{ident}") or cache.get(f"yt:info:{ident}") or {}
        return oneline(data.get("title"))

    # -- video --

    async def _meta(self, ident: str) -> dict:
        if self.settings.youtube_api_key:
            data = await self._api("videos", {"id": ident, "part": "snippet,statistics,contentDetails"})
            if not data.get("items"):
                raise Fail(NOT_FOUND, "video not found or private")
            item = data["items"][0]
            snippet, stats = item.get("snippet") or {}, item.get("statistics") or {}
            live = snippet.get("liveBroadcastContent")
            return dict(
                title=snippet.get("title"),
                channel=snippet.get("channelTitle"),
                duration=_seconds((item.get("contentDetails") or {}).get("duration", "")),
                date=(snippet.get("publishedAt") or "")[:10],
                views=stats.get("viewCount"),
                likes=stats.get("likeCount"),
                comments=stats.get("commentCount"),
                description=snippet.get("description") or "",
                live=live if live in ("live", "upcoming") else "",
            )
        info = await self.info(ident)
        day = info.get("upload_date") or ""
        status = info.get("live_status")
        return dict(
            title=info.get("title"),
            channel=info.get("channel") or info.get("uploader"),
            duration=info.get("duration") or 0,
            date=f"{day[:4]}-{day[4:6]}-{day[6:]}" if re.fullmatch(r"\d{8}", day) else "",
            views=info.get("view_count"),
            likes=info.get("like_count"),
            comments=info.get("comment_count"),
            description=info.get("description") or "",
            live="live" if status == "is_live" else "upcoming" if status == "is_upcoming" else "",
        )

    async def video(self, owner: str, reference: str) -> str:
        ident = video_id(reference)
        meta = await self.services.cache.get_or_load(f"yt:meta:{ident}", INFO_TTL, lambda: self._meta(ident))
        url = watch_url(ident)

        def count(value: Any, unit: str) -> str:
            return f"{num(value)} {unit}" if value not in (None, "") else ""

        line = join(
            oneline(meta["channel"]),
            clock(meta["duration"]) if meta["duration"] else "",
            meta["live"],
            meta["date"],
            count(meta["views"], "views"),
            count(meta["likes"], "likes"),
            count(meta["comments"], "comments"),
            url,
        )
        head = (oneline(meta["title"]) or ident) + "\n" + line
        description = meta["description"].strip()
        if not description:
            return head
        inline, suffix = self.services.pages.body(
            owner, f"yt:desc:{ident}", url, "description", description, DESCRIPTION_CHARS
        )
        return block(head, inline + (f" {suffix}" if suffix else ""))

    # -- transcript --

    async def _captions(self, ident: str, lang: str) -> dict:
        hosts = self.services.hosts
        if hosts.get(API_BLOCK) != "blocked":
            proxy = self.settings.youtube_proxy_url
            try:
                found = await asyncio.wait_for(
                    self.services.run("yt", fetch_api_transcript, ident, lang, proxy), API_TIMEOUT
                )
            except Exception as exc:  # noqa: BLE001 - any fast-path failure falls through
                if _blocked(exc):
                    hosts.set(API_BLOCK, "blocked", 3600)
                log.info("transcript-api failed for %s: %s", ident, type(exc).__name__)
                found = None
            if found:
                return found
        return await self._json3(ident, lang)

    async def _json3(self, ident: str, lang: str) -> dict:
        info = await self.info(ident)
        pick = pick_track(info, lang)
        if pick is None:
            raise Fail(UNAVAILABLE, f"no captions{' in ' + lang if lang else ''} for this video")
        url, source, code, translated = pick
        host = (urlparse(url).hostname or "").lower()
        if not url.startswith("https://") or not (host == "youtube.com" or host.endswith(".youtube.com")):
            raise Fail(UNAVAILABLE, "unexpected caption URL")
        body = bytearray()
        try:  # any failure here is a caption failure, so STT fallback still applies
            async with self.services.net.client("youtube").stream("GET", url, timeout=15) as response:
                if response.status_code != 200:
                    raise Fail(UNAVAILABLE, f"caption download returned HTTP {response.status_code}")
                async for part in response.aiter_bytes():
                    body += part
                    if len(body) > MAX_CAPTION_BYTES:
                        raise Fail(TOO_LARGE, "caption file is too large")
        except httpx.HTTPError:
            raise Fail(UNAVAILABLE, "caption download failed") from None
        try:
            raw = json.loads(bytes(body))
        except ValueError:
            raise Fail(UNAVAILABLE, "caption track is not valid json3") from None
        paras = paragraphs(parse_json3(raw))
        if not paras:
            raise Fail(UNAVAILABLE, "caption track is empty")
        return {"source": source, "lang": code, "note": "translated" if translated else "", "paras": paras}

    async def _stt(self, ident: str, lang: str, progress: stt.Progress) -> dict:
        if not self.settings.stt_api_key:
            raise Fail(NOT_CONFIGURED, "STT_API_KEY is required for speech-to-text")
        info = await self.info(ident)
        text, model, cues = await stt.transcribe(self.services, info, lang, progress)
        log.info("stt %s done with %s (%d chars, %d segments)", ident, model, len(text), len(cues))
        return {
            "source": "stt",
            "lang": stt.audio_language(info, lang),
            "note": "",
            # Timed segments merge like captions ([m:ss] paragraphs); untimed text is chunked.
            "paras": paragraphs(cues) if cues else [[None, p] for p in stt_paragraphs(text)],
        }

    async def transcript(
        self,
        owner: str,
        reference: str,
        language: str = "",
        max_chars: int = 8000,
        source: str = "captions",
        fallback_to_stt: bool = True,
        timestamps: bool = True,
        progress: stt.Progress = None,
    ) -> str:
        ident = video_id(reference)
        lang = _language(language)
        if source not in ("captions", "stt"):
            raise Fail(INVALID_ARGUMENT, "source must be captions or stt")
        tag = lang or "auto"
        cache = self.services.cache
        record, fallback = None, False
        if source == "captions":
            try:
                record = await cache.get_or_load(
                    f"yt:tx:{ident}:{tag}:captions", TX_TTL, lambda: self._captions(ident, lang)
                )
            except Fail as exc:
                if not fallback_to_stt or exc.code in NO_FALLBACK or not self.settings.stt_api_key:
                    raise
                log.info("captions failed for %s (%s); falling back to STT", ident, exc.code)
                fallback = True
        if record is None:
            record = await cache.get_or_load(
                f"yt:tx:{ident}:{tag}:stt", TX_TTL, lambda: self._stt(ident, lang, progress)
            )
        kind = "stt" if record["source"] == "stt" else "captions"
        doc = Doc(
            url=watch_url(ident),
            source=record["source"],
            text=render(record["paras"], timestamps),
            title=self._title(ident),
            note=join(record["lang"], record["note"], "stt fallback" if fallback else ""),
        )
        key = f"yt:doc:{ident}:{tag}:{kind}:{int(bool(timestamps))}"
        pages = self.services.pages
        pages.store(key, doc)
        budget = max(500, min(int(max_chars or self.settings.default_max_chars), 20000))
        return pages.page(owner, key, doc, 0, budget).text

    # -- comments --

    def _comment_line(self, owner: str, c: dict, video: str) -> str:
        url = f"{watch_url(video)}&lc={c['id']}" if video else ""
        inline, suffix = self.services.pages.body(
            owner, f"yt:cm:{c['id']}", url, "comment", oneline(c["text"]), COMMENT_CHARS
        )
        head = " ".join(p for p in (c["id"], oneline(c["author"]), f"{num(c['likes'] or 0)}↑",
                                    ago(_timestamp(c["at"]))) if p)
        line = f"- {head}: {inline}" + (f" {suffix}" if suffix else "")
        if n := c["replies"]:
            line += f" (↳ {n} {'reply' if n == 1 else 'replies'})"
        return line

    async def comments(
        self,
        owner: str,
        reference: str = "",
        limit: int = 10,
        cursor: str = "",
        order: str = "relevance",
        parent_id: str = "",
        query: str = "",
    ) -> str:
        if not self.settings.youtube_api_key:
            raise Fail(NOT_CONFIGURED, "YOUTUBE_API_KEY is required for comments")
        limit = max(1, min(int(limit or 10), 10))
        cursors = self.services.cursors
        if cursor:
            saved = cursors.get(owner, cursor, COMMENTS)
            state = {**saved, "pending": list(saved["pending"])}  # old cursor stays replayable
        else:
            params: dict = {"part": "snippet", "maxResults": PAGE_SIZE, "textFormat": "plainText"}
            if parent_id:
                if not _COMMENT_ID.fullmatch(parent_id):
                    raise Fail(INVALID_ARGUMENT, "parent_id must be a YouTube comment id")
                ident = video_id(reference) if reference else ""
                endpoint, label = "comments", f"replies to {parent_id}"
                params["parentId"] = parent_id
            else:
                if not reference:
                    raise Fail(INVALID_ARGUMENT, "reference (video id or URL) is required")
                if order not in ("relevance", "time"):
                    raise Fail(INVALID_ARGUMENT, "order must be relevance or time")
                ident = video_id(reference)
                endpoint, label = "commentThreads", order
                params.update(videoId=ident, order=order)
                if query.strip():
                    params["searchTerms"] = query.strip()[:200]
            state = {"endpoint": endpoint, "params": params, "next": "", "pending": [], "video": ident,
                     "label": label}
        pending = state["pending"]
        for _ in range(2):  # top up from at most two upstream pages per call
            if len(pending) >= limit or state["next"] is None:
                break
            params = dict(state["params"], **({"pageToken": state["next"]} if state["next"] else {}))
            page_key = f"yt:cm:page:{state['endpoint']}:{json.dumps(params, sort_keys=True)}"
            data = await self.services.cache.get_or_load(
                page_key, COMMENTS_TTL, lambda p=params: self._api(state["endpoint"], p)
            )
            for item in data.get("items") or []:
                thread = item.get("snippet") or {}
                top = thread.get("topLevelComment") or item
                s = top.get("snippet") or {}
                pending.append({
                    "id": top.get("id", ""),
                    "author": s.get("authorDisplayName", ""),
                    "likes": s.get("likeCount", 0),
                    "at": s.get("publishedAt", ""),
                    "text": s.get("textDisplay") or s.get("textOriginal") or "",
                    "replies": thread.get("totalReplyCount", 0) if "topLevelComment" in thread else 0,
                })
            token = data.get("nextPageToken")
            state["next"] = token if token and token != state["next"] else None
        shown, state["pending"] = pending[:limit], pending[limit:]
        has_more = bool(state["pending"]) or state["next"] is not None
        next_cursor = cursors.put(owner, COMMENTS, state) if has_more else None
        lines = [self._comment_line(owner, c, state["video"]) for c in shown]
        header = join(state["video"], state["label"], f"{len(shown)} comments")
        return block(header, "\n".join(lines), more(next_cursor))
