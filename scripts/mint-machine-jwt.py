#!/usr/bin/env python3
"""Mint an HS256 JWT for headless Web MCP clients.

The Web MCP server accepts machine JWTs signed with ``MCP_MACHINE_SECRET``
(see ``src/server/auth.py``). This mints one with the correct issuer and
audience so a harness can send::

    Authorization: Bearer <jwt>

No third-party dependencies -- HS256 is just HMAC-SHA256 over the
base64url(header).base64url(payload) segments, so the stdlib is enough.

Usage:
    MCP_MACHINE_SECRET=... \\
    MCP_RESOURCE_URL=https://mcp.example.com/mcp \\
        python3 scripts/mint-machine-jwt.py [--ttl-hours 24] [--subject machine]
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint an HS256 machine JWT for Web MCP.")
    ap.add_argument("--ttl-hours", type=float, default=24.0, help="token lifetime in hours (default 24)")
    ap.add_argument("--subject", default="machine", help="JWT `sub` claim (default 'machine')")
    args = ap.parse_args()

    secret = os.getenv("MCP_MACHINE_SECRET")
    resource = os.getenv("MCP_RESOURCE_URL") or os.getenv("MCP_BASE_URL")
    issuer = os.getenv("MCP_MACHINE_ISSUER", "webintel-internal")

    if not secret:
        print("error: MCP_MACHINE_SECRET is required", file=sys.stderr)
        return 2
    if not resource:
        print("error: MCP_RESOURCE_URL (or MCP_BASE_URL) is required", file=sys.stderr)
        return 2

    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "iss": issuer,
        "sub": args.subject,
        "aud": resource,
        "iat": now,
        "exp": now + int(args.ttl_hours * 3600),
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    print(signing_input + "." + _b64url(signature))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
