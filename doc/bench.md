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

## v2 vs v3, same machine and network (Mac, 2026-10-25)

Both servers ran locally with the same Reddit credentials (no Exa/YouTube API/OpenRouter
keys, so search, YouTube comments and STT are not compared here). Content cases request
the same `max_chars=8000` so chunks are like-for-like. Tokens = o200k count of what
OpenWebUI forwards.

| case | v2 ms (1st/2nd) | v2 tokens | v3 ms (1st/2nd) | v3 tokens |
|---|---|---|---|---|
| catalog | – | 6488 | – | 1646 |
| fetch_article | 464/177 | 2159 | 402/2 | 1810 |
| fetch_outline | 169/169 | 1095 | 2/2 | 254 |
| fetch_pdf | 518/301 | 1886 | 255/2 | 1533 |
| fetch_xda | 17339/5537 | 2780 | 473/3 | 2215 |
| search_reddit | 607/294 | 1042 | 663/5 | 288 |
| subreddit | 366/359 | 943 | 512/6 | 368 |
| subreddit_info | 165/151 | 151 | 106/5 | 56 |
| reddit_post | 198/293 | 675 | 168/6 | 553 |
| reddit_comments | 292/310 | 1658 | 407/6 | 622 |
| yt_meta | 3115/1279 | 1178 | 1643/3 | 416 |
| yt_transcript | 1614/1749 | 2564 | 1034/3 | 1611 |

- Catalog: **−75%** (6,488 → 1,646 tokens per turn).
- Listings/comments: **−60–72%**; content chunks: **−16–37%** at the same chunk size
  (v3's default chunk is also 8,000 chars vs v2's 20,000).
- Second calls are served from the data cache (2–7 ms); v2 re-fetched every time.
- XDA: 17.3 s → 0.47 s (v2 went browser → Jina; v3 parses the XenForo thread directly).
- Outline on a page already fetched: 169 ms → 2 ms (no refetch).

## All tools with live keys (v3 on the Mac vs the v2 baseline on Oak)

Run after keys were added to `runtime.env` (Exa, YouTube Data API, OpenRouter, Reddit).
Machines differ (Oak egresses through its proxies), so compare tokens exactly and latency
loosely.

| case | v2 ms (1st/2nd) | v2 tokens | v3 ms (1st/2nd) | v3 tokens |
|---|---|---|---|---|
| catalog | – | 6488 | – | 1646 |
| fetch_article | 943/231 | 2152 | 484/2 | 1811 |
| fetch_outline | 338/211 | 1095 | 2/2 | 254 |
| fetch_pdf | 5079/753 | 1887 | 305/2 | 1531 |
| fetch_xda | 6409/1734 | 2784 | 398/3 | 2215 |
| search | 1130/178 | 967 | 600/5 | 410 |
| search_videos | 1696/138 | 972 | 450/9 | 476 |
| search_reddit | 490/269 | 1042 | 653/12 | 287 |
| subreddit | 348/318 | 943 | 486/8 | 369 |
| subreddit_info | 175/160 | 151 | 118/9 | 56 |
| reddit_post | 205/188 | 676 | 116/8 | 553 |
| reddit_comments | 291/257 | 1651 | 581/9 | 623 |
| yt_meta | 318/159 | 1184 | 281/9 | 419 |
| yt_transcript | 2321/1980 | 2570 | 1005/4 | 1610 |
| yt_comments | 348/345 | 1490 | 268/8 | 703 |
| yt_stt | 8593/6692 ERR | 20 | 3129/13 | 410 |

- Exa `type=fast` (now the default; `EXA_SEARCH_TYPE` overrides) measured ≈470 ms vs
  ≈1,120 ms for `auto` on three queries, with identical top results.
- STT (OpenRouter Parakeet): a 3.5-min video in 3.1 s; a 60-min video in 26.7 s end to end
  (one 22 MB upload, no re-encode, $0.09). v2 STT on Oak was failing ("empty transcript").
