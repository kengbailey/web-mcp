"""YouTube video reference parsing (shared by search and YouTube tools)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .errors import INVALID_ARGUMENT, Fail

_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com")


def video_id(reference: str) -> str:
    reference = reference.strip()
    if _ID.fullmatch(reference):
        return reference
    p = urlparse(reference)
    host = (p.hostname or "").lower()
    parts = p.path.strip("/").split("/")
    ident = None
    if p.scheme in ("https", "http"):
        if host == "youtu.be":
            ident = parts[0]
        elif host in _HOSTS:
            ident = parse_qs(p.query).get("v", [None])[0]
            if not ident and len(parts) == 2 and parts[0] in ("shorts", "embed", "live", "watch"):
                ident = parts[1]
    if ident and _ID.fullmatch(ident):
        return ident
    raise Fail(INVALID_ARGUMENT, "expected a YouTube video id or URL")


def watch_url(ident: str) -> str:
    return "https://www.youtube.com/watch?v=" + ident
