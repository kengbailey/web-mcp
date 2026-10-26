"""Output budgets and compact renderers shared by every tool.

The model sees plain text only. Rules: no nulls, no defaults, no hashes or timestamps
it doesn't need; one header line, then the body; a cursor only when there is more.
"""

from __future__ import annotations

import re
import time

SEP = " · "
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def estimate_tokens(text: str) -> int:
    """Cheap o200k-ish estimate: ~4 ASCII chars per token, ~1 token per other char."""
    ascii_chars = len(text.encode("ascii", "ignore"))
    return ascii_chars // 4 + (len(text) - ascii_chars)


def clip(text: str, max_chars: int, max_tokens: int | None = None) -> str:
    """Longest prefix within budget, preferring a paragraph/line/sentence boundary.

    Always returns a non-empty prefix for non-empty text so paging makes progress.
    """
    max_tokens = max_chars // 4 if max_tokens is None else max_tokens
    if len(text) <= max_chars and estimate_tokens(text) <= max_tokens:
        return text
    end = min(len(text), max_chars)
    if estimate_tokens(text[:end]) > max_tokens:
        lo, hi = 1, end
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if estimate_tokens(text[:mid]) <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        end = lo
    window = text[:end]
    floor = int(end * 0.8)
    for sep in ("\n\n", "\n", ". ", " "):
        cut = window.rfind(sep, floor)
        if cut > 0:
            return window[: cut + len(sep)]
    return window


def oneline(text: str | None) -> str:
    return " ".join((text or "").split())


def trunc(text: str | None, limit: int) -> str:
    text = oneline(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def tidy(text: str) -> str:
    """Collapse inner whitespace runs and 3+ blank lines; strip trailing spaces.

    Leading indentation is kept (nested lists, code); tabs become four spaces.
    """
    lines = []
    for line in text.splitlines():
        body = line.lstrip(" \t")
        indent = line[: len(line) - len(body)].replace("\t", "    ")
        body = _WS.sub(" ", body).rstrip()
        lines.append(indent + body if body else "")
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip("\n")


def num(value: int | float | str | None) -> str:
    if value is None or value == "":
        return ""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + unit
    return str(int(n))


def ago(timestamp: float | int | None, now: float | None = None) -> str:
    if not timestamp:
        return ""
    seconds = max(0, (now or time.time()) - float(timestamp))
    for unit, size in (("y", 31536000), ("mo", 2592000), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def join(*parts: object) -> str:
    """Join non-empty parts with the standard separator."""
    return SEP.join(str(p) for p in parts if p not in (None, "", [], ()))


def more(cursor: str | None) -> str:
    return f"more: {cursor}" if cursor else ""


def block(header: str, body: str = "", footer: str = "") -> str:
    out = header
    if body:
        out += "\n\n" + body
    if footer:
        out += "\n\n" + footer
    return out
