"""Paged text documents: one cached copy, many cheap cursor reads.

A Doc lives in the TTLCache under its key. A content cursor stores only
``(doc_key, offset)``. When a doc is exhausted and has ``next_url`` (e.g. forum page 2),
``read`` hands back control so the owning fetcher can load and page the next doc.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cache import CursorStore, TTLCache
from .errors import CURSOR_EXPIRED, INVALID_ARGUMENT, Fail
from .text import SEP, clip, join, num

CONTENT = "content"
DOC_TTL = 900


@dataclass
class Doc:
    url: str
    source: str
    text: str
    title: str = ""
    note: str = ""  # extra header detail, e.g. "thread page 1/95 · posts 1–20"
    next_url: str = ""  # continuation document once text is exhausted


@dataclass
class Chunk:
    text: str  # rendered header + body
    cursor: str | None
    exhausted_doc: Doc | None = None  # set when the caller should load doc.next_url


class Pager:
    def __init__(self, cache: TTLCache, cursors: CursorStore):
        self.cache = cache
        self.cursors = cursors

    def store(self, key: str, doc: Doc, ttl: float = DOC_TTL) -> None:
        self.cache.set(key, doc, ttl, size=len(doc.text) + len(doc.url) + 256)

    def page(self, owner: str, key: str, doc: Doc, offset: int = 0, max_chars: int = 8000) -> Chunk:
        """Render ``doc`` from ``offset``; issues a cursor if more text or a next page exists."""
        total = len(doc.text)
        if not 0 <= offset <= total:
            raise Fail(INVALID_ARGUMENT, "offset is outside this document")
        body = clip(doc.text[offset:], max_chars) if offset < total else ""
        end = offset + len(body)
        has_more = end < total or bool(doc.next_url)
        cursor = self.cursors.put(owner, CONTENT, (key, end)) if has_more else None
        if offset == 0 and end == total:
            span = f"{total} chars"
        else:
            span = f"chars {offset}–{end}/{total}"
        if cursor:
            tail = f"next: {cursor}" if end < total else f"next page: {cursor}"
        else:
            tail = "end" if offset else ""
        head = join(doc.title, doc.url, doc.source, doc.note, span, tail)
        return Chunk(head + ("\n\n" + body if body else ""), cursor)

    def read(self, owner: str, cursor: str, max_chars: int = 8000) -> Chunk:
        key, offset = self.cursors.get(owner, cursor, CONTENT)
        doc = self.cache.get(key, touch=DOC_TTL)
        if doc is None:
            raise Fail(CURSOR_EXPIRED, "document expired; fetch it again")
        if offset >= len(doc.text) and doc.next_url:
            return Chunk("", None, exhausted_doc=doc)
        return self.page(owner, key, doc, offset, max_chars)

    def body(self, owner: str, key: str, url: str, source: str, text: str, limit: int) -> tuple[str, str | None]:
        """Inline a short body (comment, post, description); long ones get a cursor.

        Returns (inline text, suffix) where suffix is like ``[+1.8k chars: <cursor>]``.
        """
        if len(text) <= limit:
            return text, None
        inline = clip(text, limit)
        doc = Doc(url=url, source=source, text=text)
        self.store(key, doc)
        cursor = self.cursors.put(owner, CONTENT, (key, len(inline)))
        rest = len(text) - len(inline)
        return inline.rstrip(), f"[+{num(rest)} chars: {cursor}]"


__all__ = ["Doc", "Chunk", "Pager", "CONTENT", "SEP"]
