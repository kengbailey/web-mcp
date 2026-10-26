"""Benchmark an MCP server's catalog size, response size and latency.

Works against v2 (JSON envelopes) and v3 (compact text) servers so results compare
like-for-like. Raw responses are saved so token counts can be computed later.

    python scripts/bench.py --url http://127.0.0.1:3091/mcp --label v3 --out bench-v3.json
    python scripts/bench.py --report bench-v2.json bench-v3.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

XDA = "https://xdaforums.com/t/unlock-magisk-twrp-firetv-3nd-gen-cube-gazelle-ps7717.4800530/"
REDDIT_POST = "https://www.reddit.com/r/Python/comments/1x1t161/"
CASES: dict[str, tuple[str, dict]] = {
    "fetch_article": ("fetch_content", {"url": "https://peps.python.org/pep-0008/", "max_chars": 8000}),
    "fetch_outline": ("fetch_content", {"url": "https://peps.python.org/pep-0008/", "mode": "outline"}),
    "fetch_pdf": ("fetch_content", {"url": "https://arxiv.org/pdf/1706.03762", "max_chars": 8000}),
    "fetch_xda": ("fetch_content", {"url": XDA, "max_chars": 8000}),
    "search": ("search", {"query": "fastmcp tool output schema", "limit": 5}),
    "search_videos": ("search_videos", {"query": "parakeet tdt speech recognition", "limit": 5}),
    "search_reddit": ("search_reddit", {"query": "fastmcp", "limit": 5}),
    "subreddit": ("fetch_subreddit", {"subreddit": "Python", "sort": "top", "time_filter": "month", "limit": 5}),
    "subreddit_info": ("fetch_subreddit_info", {"subreddit": "Python"}),
    "reddit_post": ("fetch_reddit_post", {"reference": REDDIT_POST}),
    "reddit_comments": ("fetch_reddit_comments", {"reference": REDDIT_POST, "limit": 10}),
    "yt_meta": ("fetch_youtube_content", {"reference": "zjkBMFhNj_g"}),
    "yt_transcript": ("fetch_youtube_transcript", {"reference": "zjkBMFhNj_g", "max_chars": 8000, "fallback_to_stt": False}),
    "yt_comments": ("fetch_youtube_comments", {"reference": "zjkBMFhNj_g", "limit": 10}),
    "yt_stt": ("fetch_youtube_transcript", {"reference": "dQw4w9WgXcQ", "source": "stt", "max_chars": 8000}),
}
DEFAULT_CASES = [name for name in CASES if name != "yt_stt"]


def model_text(result) -> tuple[str, str]:
    """Return (text content, structured JSON) as an LLM client would see them."""
    text = "\n".join(getattr(c, "text", "") for c in result.content)
    structured = json.dumps(result.structured_content, separators=(",", ":")) if result.structured_content else ""
    return text, structured


async def run(url: str, cases: list[str], repeat: int, timeout: float) -> dict:
    from fastmcp import Client

    out: dict = {"url": url, "cases": {}}
    async with Client(url, timeout=timeout) as client:
        tools = await client.list_tools()
        catalog = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]
        out["catalog"] = {
            "tools": len(tools),
            "bytes": len(json.dumps(catalog, separators=(",", ":"))),
            "per_tool": {t["name"]: len(json.dumps(t, separators=(",", ":"))) for t in catalog},
            "raw": catalog,
        }
        for name in cases:
            tool, args = CASES[name]
            runs = []
            for _ in range(repeat):
                start = time.perf_counter()
                try:
                    result = await client.call_tool(tool, args, raise_on_error=False)
                    text, structured = model_text(result)
                    error = result.is_error
                except Exception as exc:  # timeouts, transport errors
                    text, structured, error = f"{type(exc).__name__}: {exc}", "", True
                runs.append(
                    {
                        "ms": round((time.perf_counter() - start) * 1000),
                        "error": error,
                        "text": text,
                        "structured": structured,
                    }
                )
            out["cases"][name] = runs
            first = runs[0]
            print(
                f"{name:16} {'ERR' if first['error'] else 'ok ':3} "
                f"{' / '.join(str(r['ms']) for r in runs):>15} ms  "
                f"text={len(first['text']):6}  structured={len(first['structured']):6}",
                flush=True,
            )
    return out


def tokens(text: str) -> int:
    try:
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode(text, disallowed_special=()))
    except ImportError:
        return len(text) // 4


def context_chars(run: dict) -> str:
    """What OpenWebUI forwards: text, plus structured JSON when it differs from text."""
    if run["structured"] and run["structured"] != run["text"].strip():
        try:
            if json.loads(run["text"]) == json.loads(run["structured"]):
                return run["text"]
        except ValueError:
            pass
        return run["text"] + "\n" + run["structured"]
    return run["text"]


def report(paths: list[str]) -> None:
    data = [json.load(open(p)) for p in paths]
    labels = [d.get("label", p) for d, p in zip(data, paths)]
    print("| case | " + " | ".join(f"{l} ms (1st/2nd) | {l} tokens" for l in labels) + " |")
    print("|---" * (1 + 2 * len(data)) + "|")
    row = ["catalog"]
    for d in data:
        row += ["–", str(tokens(json.dumps(d["catalog"]["raw"], separators=(",", ":"))))]
    print("| " + " | ".join(row) + " |")
    for name in CASES:
        if not any(name in d["cases"] for d in data):
            continue
        row = [name]
        for d in data:
            runs = d["cases"].get(name)
            if not runs:
                row += ["–", "–"]
                continue
            ms = "/".join(str(r["ms"]) for r in runs[:2])
            flag = " ERR" if runs[0]["error"] else ""
            row += [ms + flag, str(tokens(context_chars(runs[0])))]
        print("| " + " | ".join(row) + " |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:3091/mcp")
    parser.add_argument("--label", default="")
    parser.add_argument("--cases", default=",".join(DEFAULT_CASES))
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=320)
    parser.add_argument("--out")
    parser.add_argument("--report", nargs="+", metavar="JSON")
    args = parser.parse_args()
    if args.report:
        report(args.report)
        return
    result = asyncio.run(run(args.url, args.cases.split(","), args.repeat, args.timeout))
    result["label"] = args.label or args.url
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(result, fh, ensure_ascii=False)
    print(f"catalog: {result['catalog']['tools']} tools, {result['catalog']['bytes']} bytes")


if __name__ == "__main__":
    main()
