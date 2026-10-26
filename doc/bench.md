# Bench

`scripts/bench.py` measures catalog size, per-call context tokens (o200k estimate of what
OpenWebUI forwards: text, plus structured JSON when it differs) and latency for the 1st and 2nd call.

## Baseline: v2 on Oak (`webintel-mcp:candidate-v2-20260922`, 2026-10-25, LAN client)

Catalog: 12 tools, 25,586 bytes, sent on every OpenWebUI turn.

| case | v2 ms (1st/2nd) | v2 tokens |
|---|---|---|
| catalog | – | 6488 |
| fetch_article | 943/231 | 2152 |
| fetch_outline | 338/211 | 1095 |
| fetch_pdf | 5079/753 | 1887 |
| fetch_xda | 6409/1734 | 2784 |
| search | 1130/178 | 967 |
| search_videos | 1696/138 | 972 |
| search_reddit | 490/269 | 1042 |
| subreddit | 348/318 | 943 |
| subreddit_info | 175/160 | 151 |
| reddit_post | 205/188 | 676 |
| reddit_comments | 291/257 | 1651 |
| yt_meta | 318/159 | 1184 |
| yt_transcript | 2321/1980 | 2570 |
| yt_comments | 348/345 | 1490 |
| yt_stt | 8593/6692 ERR | 20 |

Notes: `fetch_xda` came through Jina Reader (image/link markdown). `yt_stt` failed on Oak
with "STT returned an empty transcript". `yt_transcript` puts a timestamp on every caption line.
