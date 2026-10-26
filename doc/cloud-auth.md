# Cloud Authentication

Web MCP supports two connection types, composed with FastMCP's `MultiAuth`
(see `src/server/auth.py`):

1. **OAuth 2.1 (WorkOS AuthKit, DCR)** — interactive clients (OpenWebUI,
   Cursor, Claude Desktop, …). Clients auto-register via Dynamic Client
   Registration and complete a browser consent flow. Token audience is bound
   to this server's resource URL (RFC 8707).
2. **HMAC JWT (HS256)** — headless clients / coding harnesses that can't run a
   browser flow. Send `Authorization: Bearer <jwt>`.

Both are optional. If `MCP_AUTHKIT_DOMAIN` / `MCP_BASE_URL` are unset, the
server runs unauthenticated (the legacy LAN behavior).

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `MCP_AUTHKIT_DOMAIN` | for OAuth | `https://<project>.authkit.app` |
| `MCP_BASE_URL` | for OAuth | Public base URL, e.g. `https://mcp.example.com` |
| `MCP_RESOURCE_URL` | optional | Protected-resource URL advertised in OAuth metadata. Defaults to `<MCP_BASE_URL>/mcp`. **Must match** the WorkOS Resource Indicator **and** the `aud` of machine JWTs. |
| `MCP_REQUIRED_SCOPES` | optional | Space/comma-separated scopes required on OAuth tokens |
| `MCP_MACHINE_SECRET` | for headless | Shared HMAC secret for machine JWTs. Generate with `python -c 'import secrets;print(secrets.token_urlsafe(48))'` |
| `MCP_MACHINE_ISSUER` | optional | Expected `iss` on machine JWTs (default `webintel-internal`) |

## WorkOS AuthKit setup

1. Create a WorkOS project at https://workos.com and note your
   **AuthKit Domain** (`https://<project>.authkit.app`) → `MCP_AUTHKIT_DOMAIN`.
2. In the WorkOS dashboard, go to **Connect → Configuration** and enable
   **Dynamic Client Registration**.
3. Enable **Resource Indicators (RFC 8707)** and add the resource URL FastMCP
   advertises. Start the server once and copy the URL it logs on startup, e.g.
   `https://mcp.example.com/mcp`. Set `MCP_RESOURCE_URL` to the same value.
   (WorkOS mints tokens whose `aud` matches this URL; FastMCP validates it.)
4. Set `MCP_BASE_URL` to the public base URL (e.g. `https://mcp.example.com`).

## Headless harness JWTs

```bash
export MCP_MACHINE_SECRET='<secret>'
export MCP_RESOURCE_URL='https://mcp.example.com/mcp'
python3 scripts/mint-machine-jwt.py --ttl-hours 24
```

Send the output as a header:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9....
```

## Client quick reference

- **OpenWebUI**: Admin → External Tools → Add Server → Type
  `MCP (Streamable HTTP)` → URL `https://mcp.example.com/mcp` → Auth
  `OAuth 2.1` (or `OAuth 2.1 (Static)`). It auto-discovers via
  `/.well-known/oauth-protected-resource`. Ensure OpenWebUI has
  `WEBUI_SECRET_KEY` set, and don't set OAuth 2.1 tools as model-defaults
  (enable per chat).
- **Headless harness**: mint a machine JWT and send it as a Bearer header.
