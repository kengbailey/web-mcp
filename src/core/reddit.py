"""Reddit via the app-only OAuth Data API: listings, posts, comment trees, subreddit info.

Everything renders to compact text. Listings fetch 25 upstream items per page and keep the
overflow plus Reddit's ``after`` token in the cursor. Comment trees keep undelivered rows and
``more`` ids; ids are expanded through /api/morechildren once the loaded rows run out.
Cursor states are immutable, so replaying a cursor returns the same page.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from .errors import (
    INVALID_ARGUMENT,
    NOT_CONFIGURED,
    NOT_FOUND,
    RATE_LIMITED,
    UPSTREAM_BLOCKED,
    UPSTREAM_ERROR,
    Fail,
    for_status,
)
from .pages import Doc
from .services import Services
from .text import ago, join, more, num, oneline, tidy, trunc

BASE = "https://oauth.reddit.com"
WWW = "https://www.reddit.com"
TOKEN_URL = WWW + "/api/v1/access_token"
TTL = 300  # upstream pages, posts, comment trees
PAGE = 25  # upstream items per listing request
BATCH = 30  # ids per /api/morechildren call
EXPANSIONS = 3  # max upstream calls per tool call while filling a page
COMMENT_CHARS, INFO_CHARS = 500, 3000
ID = re.compile(r"[a-z0-9]{1,20}", re.IGNORECASE)  # base36; comment ids reached 13 chars in 2026
SUB = re.compile(r"[A-Za-z0-9_]{1,100}")
GONE = {"[deleted]", "[removed]", "[ removed by reddit ]"}
MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>~|])")
ZW = ("\u200b", "&#x200B;")
SEARCH, LISTING, COMMENTS = "reddit_search", "reddit_subreddit", "reddit_comments"


class Post(NamedTuple):
    id: str
    sub: str
    score: int
    comments: int
    created: float
    title: str
    domain: str  # link posts only
    nsfw: bool
    preview: str  # self posts only


class Comment(NamedTuple):
    id: str
    depth: int  # relative to the shown root
    author: str
    score: int
    created: float
    body: str  # single-lined
    url: str
    parent: str = ""  # set when the parent is not the row above (expanded replies)
    gone: bool = False


Node = tuple[str, dict, int, str]  # kind, data, depth, parent marker
Stub = tuple[str, int]  # unloaded comment id, depth


# --- references ---------------------------------------------------------------------------


def _reddit_host(host: str) -> bool:
    return host == "reddit.com" or host.endswith(".reddit.com")


def _split(ref: str):
    if ref.startswith("/"):
        ref = WWW + ref
    elif "://" not in ref:
        ref = "https://" + ref
    return urlsplit(ref)


def _parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def subreddit_name(value: str) -> str:
    """Accepts ``python``, ``r/python``, ``/r/python/`` or a subreddit URL."""
    name = value.strip()
    if "reddit.com/" in name:
        name = name.split("reddit.com/", 1)[1]
    name = name.strip("/").removeprefix("r/").split("/")[0]
    if not SUB.fullmatch(name):
        raise Fail(INVALID_ARGUMENT, "invalid subreddit name")
    return name


def parse_reference(reference: str) -> tuple[str, str, str]:
    """``(post_id, subreddit, comment_id)`` from an id, t3_ id, Reddit URL or redd.it link."""
    ref = reference.strip()
    plain = ref.removeprefix("t3_")
    if ID.fullmatch(plain):
        return plain.lower(), "", ""
    url = _split(ref)
    host = (url.hostname or "").lower()
    if url.scheme not in ("http", "https") or not (_reddit_host(host) or host == "redd.it"):
        raise Fail(INVALID_ARGUMENT, "expected a Reddit post URL, redd.it link or post id")
    parts = _parts(url.path)
    pid = sub = cid = ""
    if host == "redd.it" and len(parts) == 1:
        pid = parts[0]
    elif len(parts) >= 2 and parts[0].lower() == "gallery":
        pid = parts[1]
    elif "comments" in parts:
        i = parts.index("comments")
        pid = parts[i + 1] if i + 1 < len(parts) else ""
        if i >= 2 and parts[i - 2].lower() == "r":
            sub = subreddit_name(parts[i - 1])
        if i + 3 < len(parts) and ID.fullmatch(parts[i + 3]):
            cid = parts[i + 3].lower()
    if not ID.fullmatch(pid):
        raise Fail(INVALID_ARGUMENT, "no post id in that Reddit URL")
    return pid.lower(), sub, cid


def _is_share(ref: str) -> bool:
    url = _split(ref)
    parts = _parts(url.path)
    return (
        _reddit_host((url.hostname or "").lower())
        and len(parts) >= 4
        and parts[0].lower() == "r"
        and parts[2].lower() == "s"
    )


# --- parsing --------------------------------------------------------------------------------


def _abs(link: str) -> str:
    return WWW + link if link.startswith("/") else link


def _md(text: str) -> str:
    """Selftext cleanup that keeps markdown structure (code-block indentation included)."""
    for z in ZW:
        text = text.replace(z, "")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.splitlines())).strip("\n")


def _plain(text: str) -> str:
    """Preview text: markdown links become their label, emphasis and escapes are dropped."""
    text = MD_LINK.sub(lambda m: m.group(1) if m.group(1).strip() else m.group(2), _md(text))
    return MD_ESCAPE.sub(r"\1", text.replace("**", ""))


def _post(d: dict) -> Post:
    is_self = bool(d.get("is_self"))
    domain = "" if is_self or str(d.get("domain", "")).startswith("self.") else d.get("domain") or ""
    return Post(
        id=d.get("id") or "",
        sub=d.get("subreddit") or "",
        score=d.get("score") or 0,
        comments=d.get("num_comments") or 0,
        created=d.get("created_utc") or 0,
        title=trunc(d.get("title"), 200),
        domain=domain,
        nsfw=bool(d.get("over_18")),
        preview=trunc(_plain(d.get("selftext") or ""), 150) if is_self else "",
    )


def _media(d: dict) -> list[str]:
    """Gallery images and hosted video, compactly; nothing when absent."""
    lines = []
    meta = d.get("media_metadata") or {}
    shots = []
    for item in (d.get("gallery_data") or {}).get("items") or []:
        mid = item.get("media_id") or ""
        m = meta.get(mid) or {}
        mime, src = m.get("m") or "", m.get("s") or {}
        if m.get("e") == "Image" and mime.startswith("image/"):
            url = f"https://i.redd.it/{mid}.{mime.split('/')[-1]}"
        else:
            url = src.get("u") or src.get("gif") or src.get("mp4") or ""
        if url:
            caption = trunc(item.get("caption"), 100)
            shots.append(f"{url} ({caption})" if caption else url)
    if shots:
        lines.append("gallery: " + " · ".join(shots))
    video = ((d.get("secure_media") or d.get("media") or {}).get("reddit_video") or {}).get("fallback_url")
    if video:
        lines.append("video: " + video.split("?")[0])
    return lines


def _detail(d: dict) -> dict:
    """The few fields fetch_reddit_post renders (cached instead of the raw ~10 KB object)."""
    permalink = _abs(d.get("permalink") or "")
    link = "" if d.get("is_self") else _abs(d.get("url") or "")
    text = _md(d.get("selftext") or "")
    media = _media(d)
    if media:
        text = (text + "\n\n" if text else "") + "\n".join(media)
    return dict(
        title=oneline(d.get("title")),
        sub=d.get("subreddit") or "",
        author=d.get("author") or "[deleted]",
        score=d.get("score") or 0,
        comments=d.get("num_comments") or 0,
        created=d.get("created_utc") or 0,
        nsfw=bool(d.get("over_18")),
        permalink=permalink,
        link="" if link == permalink else link,
        text=text,
    )


def _flat(text: str) -> str:
    """Comment bodies stay on one line: lines and paragraphs are joined with ' / '."""
    for z in ZW:
        text = text.replace(z, "")
    return " / ".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _comment(d: dict, depth: int, parent: str) -> Comment:
    body = d.get("body") or ""
    return Comment(
        id=d.get("id") or "",
        depth=depth,
        author=d.get("author") or "[deleted]",
        score=d.get("score") or 0,
        created=d.get("created_utc") or 0,
        body=_flat(body),
        url=_abs(d.get("permalink") or ""),
        parent=parent,
        gone=body.strip().lower() in GONE,
    )


def _walk(children: list[dict]) -> list[Node]:
    """Nested comment tree → display order (iterative, so deep threads cannot overflow)."""
    nodes: list[Node] = []
    stack = [(c, 0) for c in reversed(children)]
    while stack:
        child, depth = stack.pop()
        kind, data = child.get("kind") or "", child.get("data") or {}
        nodes.append((kind, data, depth, ""))
        replies = data.get("replies") if kind == "t1" else None
        if isinstance(replies, dict):
            grand = (replies.get("data") or {}).get("children") or []
            stack.extend((r, depth + 1) for r in reversed(grand))
    return nodes


def _name(data: dict) -> str:
    return data.get("name") or "t1_" + str(data.get("id") or "")


def _order(things: list[dict], base: dict[str, int]) -> list[Node]:
    """/api/morechildren's flat list → display order. Each subtree root sits at the depth of
    the stub it came from and names its parent comment (that parent was shown earlier)."""
    items = [(t.get("kind") or "", t.get("data") or {}) for t in things]
    names = {_name(d) for k, d in items if k == "t1"}
    kids: dict[str, list[tuple[str, dict]]] = {}
    roots: list[tuple[str, dict]] = []
    for kind, data in items:
        parent = data.get("parent_id") or ""
        (kids.setdefault(parent, []) if parent in names else roots).append((kind, data))
    sibling = {d.get("parent_id"): base[d["id"]] for _, d in items if d.get("id") in base}
    default = min(base.values(), default=0)
    nodes: list[Node] = []
    for kind, data in roots:
        parent = str(data.get("parent_id") or "")
        depth = base.get(data.get("id") or "", sibling.get(parent, default))
        mark = parent[3:] if kind == "t1" and parent.startswith("t1_") else ""
        stack = [(kind, data, depth, mark)]
        while stack:
            node = stack.pop()
            nodes.append(node)
            if node[0] == "t1":
                stack.extend((k, d, node[2] + 1, "") for k, d in reversed(kids.get(_name(node[1]), [])))
    return nodes


def _select(nodes: list[Node]) -> tuple[tuple[Comment, ...], tuple[Stub, ...]]:
    """Rows to show plus unloaded ids. Drops stickied AutoModerator subtrees, ``more`` stubs
    (their ids are kept) and deleted/removed comments without shown replies."""
    rows: list[Comment] = []
    stubs: dict[str, int] = {}
    cut = -1
    for kind, data, depth, mark in nodes:
        if cut >= 0:
            if depth > cut:
                continue
            cut = -1
        if kind == "more":
            for cid in data.get("children") or []:
                if ID.fullmatch(cid):
                    stubs.setdefault(cid, depth)
        elif kind == "t1":
            if data.get("stickied") and data.get("author") == "AutoModerator":
                cut = depth
            else:
                rows.append(_comment(data, depth, mark))
    kept: list[Comment] = []
    for row in reversed(rows):  # a kept row right after a deeper-indented one is its reply
        if row.gone and not (kept and kept[-1].depth > row.depth):
            continue
        kept.append(row)
    ordered = sorted(stubs.items(), key=lambda s: s[1])  # breadth first: more top-level comments
    return tuple(reversed(kept)), tuple(ordered)


# --- http -----------------------------------------------------------------------------------


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise Fail(UPSTREAM_ERROR, "Reddit returned a non-JSON response") from None


def _status(response: httpx.Response, what: str) -> Fail:
    code = response.status_code
    if code == 403:
        return Fail(UPSTREAM_BLOCKED, f"Reddit denied access to {what} (private, quarantined or banned)")
    if code in (404, 410):
        return Fail(NOT_FOUND, f"{what} not found")
    if code == 429:
        wait = response.headers.get("retry-after") or response.headers.get("x-ratelimit-reset") or ""
        try:
            hint = f"; retry in {int(float(wait))}s" if wait else ""
        except ValueError:
            hint = ""
        return Fail(RATE_LIMITED, "Reddit rate limit reached" + hint)
    if code == 401:
        return Fail(UPSTREAM_BLOCKED, "Reddit rejected the app token")
    return for_status(code, "Reddit")


def _clamp(limit: int) -> int:
    return max(1, min(int(limit or 1), 10))


def _start(kind: str, path: str, params: dict, what: str) -> dict:
    return dict(kind=kind, path=path, params=params, what=what, rows=(), after="", fetched=False, seen=frozenset(), n=0)


def _post_line(i: int, p: Post) -> str:
    line = f"{i}. " + join(p.id, f"r/{p.sub}", f"{num(p.score)}↑ {num(p.comments)}💬", ago(p.created), p.title)
    if p.domain:
        line += f" → {p.domain}"
    if p.nsfw:
        line += " · NSFW"
    return line + (f"\n   {p.preview}" if p.preview else "")


class Reddit:
    def __init__(self, services: Services):
        self.s = services
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    @property
    def _ua(self) -> dict[str, str]:
        return {"User-Agent": self.s.settings.reddit_user_agent}

    def _fresh(self, stale: str) -> bool:
        return bool(self._token) and self._token != stale and time.monotonic() < self._expires

    async def _access_token(self, stale: str = "") -> str:
        """App-only token, cached until 60 s before expiry; one refresh at a time."""
        cfg = self.s.settings
        if not (cfg.reddit_client_id and cfg.reddit_client_secret):
            raise Fail(NOT_CONFIGURED, "Reddit needs REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET")
        if self._fresh(stale):
            return self._token
        async with self._lock:
            if self._fresh(stale):
                return self._token
            r = await self.s.net.client("reddit").post(
                TOKEN_URL,
                auth=(cfg.reddit_client_id, cfg.reddit_client_secret),
                data={"grant_type": "client_credentials"},
                headers=self._ua,
            )
            if r.status_code == 401:
                raise Fail(NOT_CONFIGURED, "Reddit rejected REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET")
            if r.status_code != 200:
                raise _status(r, "Reddit OAuth")
            payload = _json(r)
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not token:
                raise Fail(UPSTREAM_ERROR, "Reddit OAuth returned no token")
            self._token = token
            self._expires = time.monotonic() + max(int(payload.get("expires_in") or 3600) - 60, 0)
            return token

    async def _get(self, path: str, params: dict[str, Any], what: str) -> Any:
        """Authenticated GET; retries once with a fresh token on 401."""
        client = self.s.net.client("reddit")
        url, query, stale = BASE + path, {**params, "raw_json": 1}, ""
        for _ in range(4):
            token = await self._access_token(stale)
            r = await client.get(url, params=query, headers={**self._ua, "Authorization": f"bearer {token}"})
            if r.status_code == 401 and not stale:
                stale = token
                continue
            if not r.is_redirect:
                break
            target = urlsplit(urljoin(url, r.headers.get("location", "")))
            if "/subreddits/search" in target.path:  # Reddit's answer for unknown subreddits
                raise Fail(NOT_FOUND, f"{what} not found")
            if target.hostname != "oauth.reddit.com":
                raise Fail(UPSTREAM_ERROR, f"{what} redirected outside the API")
            url, query = target._replace(query="").geturl(), {**dict(parse_qsl(target.query)), "raw_json": 1}
        if r.status_code != 200:
            raise _status(r, what)
        return _json(r)

    # --- references -------------------------------------------------------------------------

    async def _resolve(self, reference: str) -> tuple[str, str, str]:
        ref = reference.strip()
        if not ref:
            raise Fail(INVALID_ARGUMENT, "reference is required")
        if _is_share(ref):
            return await self.s.cache.get_or_load("reddit:share:" + ref, 3600, lambda: self._share(ref))
        return parse_reference(ref)

    async def _share(self, ref: str) -> tuple[str, str, str]:
        """Share links (/r/x/s/abc) redirect to the canonical post; follow Reddit hops only."""
        url = _split(ref).geturl()
        client = self.s.net.client("reddit")
        for _ in range(5):
            r = await client.get(url, headers=self._ua)
            location = r.headers.get("location")
            if not (r.is_redirect and location):
                if r.status_code >= 400:
                    raise _status(r, "Reddit share link")
                break
            url = urljoin(url, location)
            target = urlsplit(url)
            if target.scheme not in ("http", "https") or not _reddit_host((target.hostname or "").lower()):
                raise Fail(INVALID_ARGUMENT, "share link redirected outside Reddit")
            try:
                return parse_reference(url)
            except Fail:
                continue
        raise Fail(NOT_FOUND, "share link did not resolve to a post")

    # --- listings ---------------------------------------------------------------------------

    async def search(
        self, owner: str, query: str, subreddit: str, limit: int, cursor: str, match: str, sort: str, time_filter: str
    ) -> str:
        if cursor:
            return await self._list(owner, self.s.cursors.get(owner, cursor, SEARCH), limit)
        query = query.strip()
        if not query:
            raise Fail(INVALID_ARGUMENT, "query is required")
        if len(query) > 512:
            raise Fail(INVALID_ARGUMENT, "query is too long (max 512 chars)")
        if match == "title":
            query = 'title:"' + query.replace('"', "") + '"'
        params = {"q": query, "sort": sort, "t": time_filter}
        sub = subreddit_name(subreddit) if subreddit.strip() else ""
        if sub:
            params["restrict_sr"] = "on"
        path, what = (f"/r/{sub}/search", f"r/{sub}") if sub else ("/search", "Reddit search")
        return await self._list(owner, _start(SEARCH, path, params, what), limit)

    async def subreddit(self, owner: str, subreddit: str, sort: str, time_filter: str, limit: int, cursor: str) -> str:
        if cursor:
            return await self._list(owner, self.s.cursors.get(owner, cursor, LISTING), limit)
        if not subreddit.strip():
            raise Fail(INVALID_ARGUMENT, "subreddit is required")
        sub = subreddit_name(subreddit)
        params = {"t": time_filter} if sort in ("top", "controversial") else {}
        return await self._list(owner, _start(LISTING, f"/r/{sub}/{sort}", params, f"r/{sub}"), limit)

    async def _page(self, path: str, params: dict, after: str, what: str) -> tuple[tuple[Post, ...], str]:
        query = {**params, "limit": PAGE, **({"after": after} if after else {})}
        key = "reddit:list:" + path + "?" + "&".join(f"{k}={v}" for k, v in sorted(query.items()))

        async def load() -> tuple[tuple[Post, ...], str]:
            data = await self._get(path, query, what)
            listing = (data.get("data") if isinstance(data, dict) else None) or {}
            children = listing.get("children") or []
            return tuple(_post(c.get("data") or {}) for c in children if c.get("kind") == "t3"), listing.get("after") or ""

        return await self.s.cache.get_or_load(key, TTL, load)

    async def _list(self, owner: str, state: dict, limit: int) -> str:
        limit = _clamp(limit)
        rows, after, fetched, seen = list(state["rows"]), state["after"], state["fetched"], set(state["seen"])
        for _ in range(EXPANSIONS):
            if len(rows) >= limit or (fetched and not after):
                break
            page, nxt = await self._page(state["path"], state["params"], after, state["what"])
            fresh = [p for p in page if p.id not in seen]
            seen.update(p.id for p in fresh)
            rows += fresh
            after, fetched = (nxt if nxt != after else ""), True
        shown, rest, n = rows[:limit], rows[limit:], state["n"]
        lines = [_post_line(i, p) for i, p in enumerate(shown, n + 1)] or ["no posts"]
        if rest or after:
            nxt_state = {**state, "rows": tuple(rest), "after": after, "fetched": True, "seen": frozenset(seen), "n": n + len(shown)}
            lines.append(more(self.s.cursors.put(owner, state["kind"], nxt_state)))
        return "\n".join(lines)

    # --- post and subreddit -----------------------------------------------------------------

    async def _info(self, pid: str) -> dict:
        data = await self._get("/api/info", {"id": f"t3_{pid}"}, f"post {pid}")
        children = ((data.get("data") if isinstance(data, dict) else None) or {}).get("children") or []
        if not children:
            raise Fail(NOT_FOUND, f"post {pid} not found")
        return _detail(children[0].get("data") or {})

    async def post(self, owner: str, reference: str) -> str:
        pid, _, _ = await self._resolve(reference)
        d = await self.s.cache.get_or_load(f"reddit:post:{pid}", TTL, lambda: self._info(pid))
        title = d["title"] or pid
        meta = join(
            f"r/{d['sub']}",
            f"u/{d['author']}",
            f"{num(d['score'])}↑",
            f"{num(d['comments'])}💬",
            ago(d["created"]),
            "NSFW" if d["nsfw"] else "",
            f"→ {d['link']}" if d["link"] else d["permalink"],
        )
        if not d["text"]:
            return f"{title}\n{meta}"
        key = f"reddit:doc:{pid}"
        doc = Doc(url=d["permalink"], source="reddit", text=d["text"], title=title)
        self.s.pages.store(key, doc)
        # First chunk: title line, then the meta line carrying the span/cursor. read_content
        # continues from the stored doc with the standard content header.
        view = replace(doc, url="", source="", title="", note=meta)
        chunk = self.s.pages.page(owner, key, view, 0, self.s.settings.default_max_chars)
        return f"{title}\n{chunk.text}"

    async def _about(self, sub: str) -> dict:
        data = await self._get(f"/r/{sub}/about", {}, f"r/{sub}")
        d = (data.get("data") if isinstance(data, dict) and data.get("kind") == "t5" else None) or {}
        if not d.get("display_name"):
            raise Fail(NOT_FOUND, f"r/{sub} not found")
        keep = ("display_name", "title", "public_description", "description", "subscribers", "over18")
        return {**{k: d.get(k) for k in keep}, "active": d.get("active_user_count") or d.get("accounts_active")}

    async def info(self, owner: str, subreddit: str) -> str:
        sub = subreddit_name(subreddit)
        d = await self.s.cache.get_or_load(f"reddit:about:{sub.lower()}", TTL, lambda: self._about(sub))
        name, subs, active = d["display_name"], d["subscribers"], d["active"]
        lines = [
            join(
                f"r/{name}",
                f"{num(subs)} subscribers" if subs is not None else "",
                f"{num(active)} online" if active else "",
                "NSFW" if d["over18"] else "",
            )
        ]
        if title := oneline(d["title"]):
            lines.append(title)
        desc = tidy(d["public_description"] or d["description"] or "")
        if desc:
            url = f"{WWW}/r/{name}/"
            inline, suffix = self.s.pages.body(owner, f"reddit:about:{name.lower()}", url, "reddit", desc, INFO_CHARS)
            lines.append(inline + (f" {suffix}" if suffix else ""))
        return "\n".join(lines)

    # --- comments ---------------------------------------------------------------------------

    async def _tree(self, pid: str, sort: str, focus: str) -> tuple[int, tuple[Comment, ...], tuple[Stub, ...]]:
        params: dict[str, Any] = {"sort": sort, "limit": 100}
        if focus:
            params["comment"] = focus
        data = await self._get(f"/comments/{pid}", params, f"post {pid}")
        if not (isinstance(data, list) and len(data) == 2):
            raise Fail(UPSTREAM_ERROR, "unexpected Reddit comments response")
        post = (((data[0].get("data") or {}).get("children") or [{}])[0]).get("data") or {}
        rows, stubs = _select(_walk((data[1].get("data") or {}).get("children") or []))
        return int(post.get("num_comments") or 0), rows, stubs

    async def _expand(self, pid: str, sort: str, batch: list[Stub]) -> tuple[tuple[Comment, ...], tuple[Stub, ...]]:
        ids = ",".join(cid for cid, _ in batch)

        async def load() -> tuple[tuple[Comment, ...], tuple[Stub, ...]]:
            params = {"api_type": "json", "link_id": f"t3_{pid}", "children": ids, "sort": sort, "limit_children": "false"}
            data = await self._get("/api/morechildren", params, f"post {pid} comments")
            body = (data.get("json") if isinstance(data, dict) else None) or {}
            if body.get("errors"):
                raise Fail(UPSTREAM_ERROR, "Reddit rejected comment expansion")
            return _select(_order((body.get("data") or {}).get("things") or [], dict(batch)))

        return await self.s.cache.get_or_load(f"reddit:more:{pid}:{sort}:{ids}", TTL, load)

    def _line(self, owner: str, c: Comment) -> str:
        body, suffix = self.s.pages.body(owner, f"reddit:comment:{c.id}", c.url, "reddit", c.body, COMMENT_CHARS)
        head = " ".join(x for x in (f"- {c.id}", f"u/{c.author}", f"{num(c.score)}↑", ago(c.created)) if x)
        if c.parent:
            head += f" re {c.parent}"
        return f"{'  ' * c.depth}{head}: {body}" + (f" {suffix}" if suffix else "")

    async def comments(self, owner: str, reference: str, limit: int, cursor: str, sort: str, parent_id: str) -> str:
        limit = _clamp(limit)
        if cursor:
            state = self.s.cursors.get(owner, cursor, COMMENTS)
        else:
            pid, _, cid = await self._resolve(reference)
            focus = parent_id.strip().removeprefix("t1_").lower() or cid
            if focus and not ID.fullmatch(focus):
                raise Fail(INVALID_ARGUMENT, "invalid parent_id")
            total, tree, stubs = await self.s.cache.get_or_load(
                f"reddit:comments:{pid}:{sort}:{focus}", TTL, lambda: self._tree(pid, sort, focus)
            )
            if focus and not tree and not stubs:
                raise Fail(NOT_FOUND, f"comment {focus} not found in post {pid}")
            state = dict(
                post=pid, sort=sort, focus=focus, total=total, rows=tree, stubs=stubs,
                seen=frozenset(c.id for c in tree), asked=frozenset(), n=0,
            )
        rows, stubs = list(state["rows"]), list(state["stubs"])
        seen, asked = set(state["seen"]), set(state["asked"])
        for _ in range(EXPANSIONS):
            if len(rows) >= limit or not stubs:
                break
            batch, stubs = stubs[:BATCH], stubs[BATCH:]
            asked.update(cid for cid, _ in batch)
            got, extra = await self._expand(state["post"], state["sort"], batch)
            fresh = [c for c in got if c.id not in seen]
            seen.update(c.id for c in fresh)
            rows += fresh
            queued = {cid for cid, _ in stubs}
            stubs += [s for s in extra if s[0] not in seen and s[0] not in asked and s[0] not in queued]
            stubs.sort(key=lambda s: s[1])
        shown, rest, n = rows[:limit], rows[limit:], state["n"]
        span = f"{n + 1}–{n + len(shown)}" if n and len(shown) > 1 else str(n + len(shown))
        focus = state["focus"]
        lines = [join(state["post"], state["sort"], f"thread {focus}" if focus else "", f"{span} of {num(state['total'])} comments")]
        lines += [self._line(owner, c) for c in shown]
        if rest or stubs:
            nxt = {**state, "rows": tuple(rest), "stubs": tuple(stubs), "seen": frozenset(seen), "asked": frozenset(asked), "n": n + len(shown)}
            lines.append(more(self.s.cursors.put(owner, COMMENTS, nxt)))
        return "\n".join(lines)
