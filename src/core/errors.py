"""The one exception type tools raise for expected failures.

Codes are stable and shown to the model as ``ERROR <CODE>: <message>``. Messages must
be safe to show: no credentials, tokens or raw upstream bodies.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

INVALID_ARGUMENT = "INVALID_ARGUMENT"
NOT_FOUND = "NOT_FOUND"
NOT_CONFIGURED = "NOT_CONFIGURED"
RATE_LIMITED = "RATE_LIMITED"
UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
UPSTREAM_ERROR = "UPSTREAM_ERROR"
UPSTREAM_BLOCKED = "UPSTREAM_BLOCKED"
CURSOR_EXPIRED = "CURSOR_EXPIRED"
TOO_LARGE = "TOO_LARGE"
UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"
UNAVAILABLE = "UNAVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"

HTTP_CODES = {401: UPSTREAM_BLOCKED, 403: UPSTREAM_BLOCKED, 404: NOT_FOUND, 410: NOT_FOUND, 429: RATE_LIMITED}


class Fail(ToolError):
    """A ToolError whose text is ``ERROR <CODE>: <message>`` (FastMCP shows it verbatim)."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"ERROR {code}: {message}")


def for_status(status: int, what: str) -> Fail:
    """Map an upstream HTTP status to a Fail without leaking the response body."""
    return Fail(HTTP_CODES.get(status, UPSTREAM_ERROR), f"{what} returned HTTP {status}")
