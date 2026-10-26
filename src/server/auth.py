"""
Authentication for Web MCP (cloud / multi-client deployments).

Two connection types are supported and composed via FastMCP ``MultiAuth``:

1. **OAuth 2.1 (WorkOS AuthKit, DCR)** — for interactive clients
   (OpenWebUI, Cursor, Claude Desktop, ...). Clients auto-register through
   Dynamic Client Registration and obtain tokens via a browser consent flow.
   Token audience is bound to this server's resource URL (RFC 8707 resource
   indicators); the binding is handled automatically by ``AuthKitProvider``.

2. **HMAC-signed JWT (HS256)** — for headless clients / coding harnesses that
   cannot run a browser consent flow. Mint a JWT with the shared
   ``MCP_MACHINE_SECRET`` and send it as ``Authorization: Bearer <jwt>``.

When neither ``MCP_AUTHKIT_DOMAIN`` nor ``MCP_BASE_URL`` is set, authentication
is disabled, preserving the legacy no-auth behavior for local / LAN use.
"""
from __future__ import annotations

import os

from fastmcp.server.auth import JWTVerifier, MultiAuth
from fastmcp.server.auth.providers.workos import AuthKitProvider

DEFAULT_MACHINE_ISSUER = "webintel-internal"
DEFAULT_MCP_PATH = "/mcp"


def build_auth():
    """Construct the FastMCP auth provider from environment variables.

    Returns ``None`` (unauthenticated) when cloud auth env vars are absent,
    so local / LAN usage is unchanged.
    """
    authkit_domain = os.getenv("MCP_AUTHKIT_DOMAIN", "").strip()
    base_url = os.getenv("MCP_BASE_URL", "").strip()

    if not authkit_domain or not base_url:
        # Legacy mode: no auth (LAN / local dev).
        return None

    # Protected-resource URL FastMCP advertises in its
    # /.well-known/oauth-protected-resource metadata = base URL + mount path.
    # This MUST match the Resource Indicator configured in the WorkOS dashboard
    # AND the `aud` claim carried by machine JWTs.
    resource_url = (
        os.getenv("MCP_RESOURCE_URL", "").strip()
        or base_url.rstrip("/") + DEFAULT_MCP_PATH
    )

    required_scopes_raw = os.getenv("MCP_REQUIRED_SCOPES", "").strip()
    required_scopes = (
        [s for s in required_scopes_raw.replace(",", " ").split() if s]
        if required_scopes_raw
        else None
    )

    # 1) Interactive clients -> WorkOS AuthKit (DCR). AuthKitProvider auto-wires
    #    a JWTVerifier against <authkit_domain>/oauth2/jwks (RS256) and binds
    #    its audience to `resource_url` once the MCP mount path is known.
    oauth = AuthKitProvider(
        authkit_domain=authkit_domain,
        base_url=base_url,
        required_scopes=required_scopes,
    )

    # 2) Headless clients -> symmetric HMAC JWTs (HS256). The shared secret is
    #    the sole gate; scope checks are intentionally not applied here so that
    #    minting a machine token stays trivial.
    machine_secret = os.getenv("MCP_MACHINE_SECRET", "").strip()
    if not machine_secret:
        return oauth  # OAuth-only

    machine = JWTVerifier(
        public_key=machine_secret,
        algorithm="HS256",
        issuer=os.getenv("MCP_MACHINE_ISSUER", DEFAULT_MACHINE_ISSUER),
        audience=resource_url,
    )

    return MultiAuth(server=oauth, verifiers=[machine])
