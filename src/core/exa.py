"""Exa web and YouTube search: one paid call per query; cursors page the saved rest.

Exa bills 1–10 results at the same price, so every search asks for 10, shows ``limit``
and keeps the remainder behind a cursor. Query-relevant highlights (≤300 chars) are
preferred over leading page text. Results are cached 10 min by normalized payload.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, NamedTuple
from urllib.parse import urlsplit

import httpx

from .errors import (
    INVALID_ARGUMENT, NOT_CONFIGURED, RATE_LIMITED, UPSTREAM_BLOCKED, UPSTREAM_ERROR, UPSTREAM_TIMEOUT, Fail,
)
from .services import Services
from .text import more, oneline, trunc
from .youtube_ids import video_id, watch_url

URL = "https://api.exa.ai/search"
SEARCH_TYPE = "auto"
NUM_RESULTS = 10  # same Exa price tier as fewer
SNIPPET = 300
MAX_BYTES = 1024 * 1024
TTL = 600
WEB, VIDEO = "web_search", "video_search"
VIDEO_DOMAINS = ["youtube.com/watch", "youtube.com/shorts", "youtu.be"]
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ELLIPSIS = re.compile(r"^(?:\.{3,}|…)\s*|\s*(?:\.{3,}|…)$")
log = logging.getLogger("web_mcp.exa")


class Row(NamedTuple):
    title: str
    url: str
    meta: str  # "channel, 2026-09-01" | "2026-09-01" | ""
    snippet: str


class Exa:
    def __init__(self, services: Services):
        self.services = services

    async def search(
        self,
        owner: str,
        query: str,
        limit: int = 5,
        cursor: str = "",
        include_domains: Iterable[str] = (),
        exclude_domains: Iterable[str] = (),
        start_published_date: str = "",
        end_published_date: str = "",
    ) -> str:
        _check_limit(limit)
        if cursor:
            return self._page(owner, WEB, *self.services.cursors.get(owner, cursor, WEB), limit)
        payload = _payload(query)
        for name, domains in (("includeDomains", include_domains), ("excludeDomains", exclude_domains)):
            if clean := _domains(domains):
                payload[name] = clean
        start = _date(start_published_date, "start_published_date")
        end = _date(end_published_date, "end_published_date")
        if start and end and start > end:
            raise Fail(INVALID_ARGUMENT, "start_published_date must not follow end_published_date")
        if start:
            payload["startPublishedDate"] = _iso(start)
        if end:
            payload["endPublishedDate"] = _iso(end)
        return self._page(owner, WEB, await self._rows(payload, video=False), 0, limit)

    async def videos(self, owner: str, query: str, limit: int = 5, cursor: str = "") -> str:
        _check_limit(limit)
        if cursor:
            return self._page(owner, VIDEO, *self.services.cursors.get(owner, cursor, VIDEO), limit)
        payload = _payload(query) | {"includeDomains": VIDEO_DOMAINS}
        return self._page(owner, VIDEO, await self._rows(payload, video=True), 0, limit)

    def _page(self, owner: str, kind: str, rows: tuple[Row, ...], start: int, limit: int) -> str:
        end = min(start + limit, len(rows))
        lines = []
        for n, row in enumerate(rows[start:end], start + 1):
            head = f"{n}. {row.title} — {row.url}" if row.title else f"{n}. {row.url}"
            lines.append(head + (f" ({row.meta})" if row.meta else ""))
            if row.snippet:
                lines.append("   " + row.snippet)
        if not lines:
            return "no results"
        if end < len(rows):
            lines.append(more(self.services.cursors.put(owner, kind, (rows, end))))
        return "\n".join(lines)

    async def _rows(self, payload: dict, video: bool) -> tuple[Row, ...]:
        key = self.services.settings.exa_api_key
        if not key:
            raise Fail(NOT_CONFIGURED, "EXA_API_KEY is not configured")
        blob = json.dumps([video, payload], sort_keys=True, separators=(",", ":"))
        cache_key = "exa:" + hashlib.sha256(blob.encode()).hexdigest()[:32]
        return await self.services.cache.get_or_load(cache_key, TTL, lambda: self._fetch(key, payload, video))

    async def _fetch(self, key: str, payload: dict, video: bool) -> tuple[Row, ...]:
        client = self.services.net.client("api")
        data = bytearray()
        try:
            async with client.stream("POST", URL, json=payload, headers={"x-api-key": key}) as response:
                if response.status_code != 200:
                    raise _status(response.status_code)
                async for chunk in response.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise Fail(UPSTREAM_ERROR, "Exa response exceeds 1 MiB")
        except httpx.TimeoutException:
            raise Fail(UPSTREAM_TIMEOUT, "Exa search timed out") from None
        except httpx.HTTPError:
            raise Fail(UPSTREAM_ERROR, "Exa connection failed") from None
        try:
            raw = json.loads(data)
            results = raw["results"]
            if not isinstance(results, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise Fail(UPSTREAM_ERROR, "Exa returned an invalid response") from None
        cost = raw.get("costDollars")
        log.debug("exa results=%d cost=%s", len(results), cost.get("total") if isinstance(cost, dict) else "?")
        if raw.get("warnings"):
            log.warning("exa request warnings: %s", str(raw["warnings"])[:500])
        return _normalize(results, video)


def _status(status: int) -> Fail:
    if status == 401:
        return Fail(UPSTREAM_BLOCKED, "Exa rejected the API key")
    if status == 403:
        return Fail(UPSTREAM_BLOCKED, "Exa denied this request for the account")
    if status == 429:
        return Fail(RATE_LIMITED, "Exa rate limit exceeded; retry shortly")
    if status == 402:
        return Fail(UPSTREAM_ERROR, "Exa account is out of credits")
    return Fail(UPSTREAM_ERROR, f"Exa returned HTTP {status}")


def _check_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise Fail(INVALID_ARGUMENT, "limit must be 1–10")


def _payload(query: str) -> dict[str, Any]:
    query = oneline(query)
    if not query:
        raise Fail(INVALID_ARGUMENT, "query is required")
    if len(query) > 500:
        raise Fail(INVALID_ARGUMENT, "query must be at most 500 chars")
    return {
        "query": query,
        "type": SEARCH_TYPE,
        "numResults": NUM_RESULTS,
        "contents": {"highlights": {"maxCharacters": SNIPPET}, "text": {"maxCharacters": SNIPPET}},
    }


def _domains(domains: Iterable[str] | str | None) -> list[str]:
    items = [domains] if isinstance(domains, str) else list(domains or ())
    if len(items) > 20 or any(
        not isinstance(d, str) or not d.strip() or len(d) > 200 or "://" in d or any(c.isspace() for c in d)
        for d in items
    ):
        raise Fail(INVALID_ARGUMENT, "use at most 20 domain or domain/path filters, without scheme or spaces")
    clean = set()
    for d in items:
        host, sep, path = d.partition("/")
        clean.add(host.lower() + sep + path)
    return sorted(clean)


def _date(value: str | None, name: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise Fail(INVALID_ARGUMENT, f"{name} must be ISO-8601 with timezone, e.g. 2026-01-01T00:00:00Z")
    return parsed


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _str(entry: dict, name: str) -> str:
    value = entry.get(name)
    return value if isinstance(value, str) else ""


def _url(value: Any, video: bool) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        return ""
    try:
        p = urlsplit(value)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            return ""
        return watch_url(video_id(value)) if video else value.split("#", 1)[0]
    except (ValueError, Fail):
        return ""


def _snippet(entry: dict, title: str) -> str:
    highlights = entry.get("highlights")
    highlights = highlights if isinstance(highlights, list) else []
    parts = [_ELLIPSIS.sub("", oneline(h)) for h in highlights if isinstance(h, str)]
    text = " … ".join(p for p in parts if p) or oneline(_str(entry, "text"))
    if title and text.startswith(title):
        text = text[len(title):].lstrip(" -—–:|·")
    return trunc(text, SNIPPET)


def _normalize(results: list, video: bool) -> tuple[Row, ...]:
    rows: list[Row] = []
    seen: set[str] = set()
    for entry in results:
        if not isinstance(entry, dict):
            continue
        url = _url(entry.get("url"), video)
        if not url or url in seen:
            continue
        seen.add(url)
        title = oneline(_str(entry, "title"))
        date = _str(entry, "publishedDate")[:10]
        author = oneline(_str(entry, "author")) if video else ""
        meta = [a for a in (author if len(author) <= 60 else "", date if _DATE.fullmatch(date) else "") if a]
        rows.append(Row(trunc(title, 150), url, ", ".join(meta), _snippet(entry, title)))
    return tuple(rows)
