# Web MCP

Fast, context-lean web research tools for AI assistants: search, pages/PDFs/forum
threads, Reddit and YouTube. Every tool returns one compact text block, with cursors for
anything longer; there are no output schemas, so the tool catalog stays small on every turn.

## Tools

| Tool | Purpose |
|---|---|
| `search` | Exa web search: titles, URLs, query-relevant highlights; domain/date filters |
| `search_videos` | Exa YouTube search |
| `fetch_content` | Page, PDF or forum thread as compact markdown; `outline`/`excerpts` views; `links=true` |
| `read_content` | Continue any long text from its `next:` cursor (no refetch) |
| `search_reddit` | Reddit post search, optional subreddit/title scoping |
| `fetch_subreddit` | Subreddit listing |
| `fetch_subreddit_info` | Community description and activity |
| `fetch_reddit_post` | Post metadata and body, no comments |
| `fetch_reddit_comments` | Indented comment tree; `parent_id` focuses a thread |
| `fetch_youtube_content` | Video metadata and description |
| `fetch_youtube_transcript` | Timestamped paragraphs: captions first, speech-to-text fallback |
| `fetch_youtube_comments` | Comments and replies (needs `YOUTUBE_API_KEY`) |

Output format, paging, caches and limits: [doc/v3.md](doc/v3.md). Measurements:
[doc/bench.md](doc/bench.md).

## Run

```sh
cp runtime.env.example runtime.env && chmod 600 runtime.env   # fill in provider keys
docker compose up -d --build web-mcp
```

Endpoint: `http://HOST:3090/mcp` (`MCP_TRANSPORT=sse` serves `/sse`). Each provider is
optional; tools without one return `ERROR NOT_CONFIGURED`.

- `EXA_API_KEY`: `search`, `search_videos`
- `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `REDDIT_USER_AGENT`: Reddit tools
- `YOUTUBE_API_KEY`: official metadata; required for comments
- `STT_API_KEY` (OpenRouter): speech-to-text with `nvidia/parakeet-tdt-0.6b-v3`
- `PROXY_URL`, `REDDIT_PROXY_URL`, `YOUTUBE_PROXY_URL`: egress routing (see `runtime.env.example`)

Compose controls: `WEB_MCP_IMAGE`, `WEB_MCP_ENV_FILE`, `MCP_HOST_PORT`, `MCP_BIND_ADDRESS`.
`--profile vpn` starts Gluetun (needs `VPN_SERVICE_PROVIDER`, `VPN_TYPE`, `OPENVPN_USER`,
`OPENVPN_PASSWORD` in the shell or `.env`). The service keeps a `webintel-mcp` network alias
for clients that used the old name.

Without Docker (Python 3.11+, ffmpeg and Deno on PATH for YouTube):

```sh
pip install -r requirements.txt && playwright install chromium
python -m src.server.delivery_server --host 127.0.0.1 --port 3090
```

Cloud (GCP VM, OAuth 2.1 + machine JWTs): [deploy/README.md](deploy/README.md),
[doc/cloud-auth.md](doc/cloud-auth.md).

## Upgrading from v2

Refresh client tool catalogs: results are now text instead of JSON envelopes, and
some parameters changed (`fetch_youtube_transcript` adds `timestamps`, and `language` defaults
to the video's original language; `fetch_content` adds `links`; the default chunk is 8,000
chars). STT moves to OpenRouter: set `STT_API_KEY` to an OpenRouter key (Groq or local
endpoints still work via `STT_ENDPOINT`/`STT_MODEL`). Jina Reader and SearxNG are gone.

## Tests

```sh
python -m pytest tests/ -q                  # offline
python -m pytest tests/ -q -m integration   # live providers, skipped without keys
python scripts/bench.py --url http://127.0.0.1:3090/mcp --label local --out bench.json
```
