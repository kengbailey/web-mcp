"""Headless Chromium fallback: one browser, 2 page slots, per-host persistent contexts.

Contexts keep their cookies (e.g. Bunny Shield clearance) for 30 idle minutes, so later
pages on that host skip the challenge; ``Web`` also copies them into a cookie jar so
follow-ups go back to plain HTTP. Every request a page makes passes the SSRF check;
images, media and fonts are never loaded. Playwright is imported lazily.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlsplit

log = logging.getLogger("web_mcp.browser")

ARGS = ["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"]
INIT_SCRIPT = 'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
BLOCKED_TYPES = frozenset({"image", "media", "font"})
VIEWPORT = {"width": 1365, "height": 768}
GOTO_MS = 12_000
CHALLENGE_S = 15.0
POLL_S = 0.25
MAIN = "main, article, [role=main]"


def challenge_title(title: str) -> bool:
    """Bunny Shield ("Establishing a secure connection ...") and Cloudflare ("Just a moment...")."""
    title = " ".join((title or "").split()).casefold()
    return "establishing a secure connection" in title or title.startswith("just a moment")


def browser_user_agent(version: str) -> str:
    """A normal Chrome UA matching the bundled Chromium (no "HeadlessChrome")."""
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def _proxy_config(url: str) -> dict:
    p = urlsplit(url)
    config = {"server": f"{p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else "")}
    if p.username:
        config.update(username=unquote(p.username), password=unquote(p.password or ""))
    return config


@dataclass
class Rendered:
    url: str  # final URL after redirects / challenge reloads
    html: str
    status: int  # last main-frame navigation status (0 if unknown)
    cookies: list[dict]
    user_agent: str


class BrowserError(Exception):
    """The page could not be loaded in the browser (message is safe to log, not to show)."""


@dataclass
class _Context:
    context: Any
    used: float
    busy: int = 0


class Browser:
    def __init__(
        self,
        allowed: Callable[[str], Awaitable[bool]],
        proxy: str | None = None,
        slots: int = 2,
        max_contexts: int = 4,
        idle: float = 1800.0,
    ):
        self._allowed = allowed
        self._proxy = proxy
        self._slots = asyncio.Semaphore(slots)
        self._launch_lock = asyncio.Lock()
        self._context_lock = asyncio.Lock()
        self._max_contexts, self._idle = max_contexts, idle
        self._contexts: OrderedDict[str, _Context] = OrderedDict()
        self._pw: Any = None
        self._browser: Any = None
        self.user_agent = ""

    async def start(self) -> Any:
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            await self._shutdown()
            from playwright.async_api import async_playwright

            started = time.perf_counter()
            self._pw = await async_playwright().start()
            kwargs: dict = dict(headless=True, args=ARGS)
            if self._proxy:
                kwargs["proxy"] = _proxy_config(self._proxy)
            try:
                self._browser = await self._pw.chromium.launch(**kwargs)
            except BaseException:
                await self._shutdown()
                raise
            self.user_agent = browser_user_agent(self._browser.version)
            log.info("chromium %s launched in %.2fs", self._browser.version, time.perf_counter() - started)
            return self._browser

    async def render(self, url: str) -> Rendered:
        """Load ``url`` in its host context; wait out a challenge, then for stable text."""
        host = (urlsplit(url).hostname or "").lower()
        async with self._slots:
            entry, page = await self._new_page(host)  # entry is marked busy
            try:
                status = await self._load(page, url)
                html = await self._html(page)
                cookies = await entry.context.cookies()
                return Rendered(page.url, html, status, cookies, self.user_agent)
            finally:
                entry.busy -= 1
                entry.used = time.monotonic()
                with contextlib.suppress(Exception):
                    await page.close()

    async def _new_page(self, host: str) -> tuple[_Context, Any]:
        for attempt in (0, 1):
            async with self._context_lock:
                entry = await self._context(host)
                entry.busy += 1  # before releasing the lock, so LRU eviction skips it
            try:
                return entry, await entry.context.new_page()
            except Exception:
                # Context or browser died; drop it and retry once with a fresh one.
                entry.busy -= 1
                await self._drop(host)
                if attempt:
                    raise
        raise AssertionError("unreachable")

    async def _context(self, host: str) -> _Context:
        browser = await self.start()
        now = time.monotonic()
        for key, item in list(self._contexts.items()):
            if not item.busy and now - item.used > self._idle:
                await self._drop(key)
        entry = self._contexts.get(host)
        if entry is None:
            for key in [k for k, v in self._contexts.items() if not v.busy]:
                if len(self._contexts) < self._max_contexts:
                    break
                await self._drop(key)
            context = await browser.new_context(
                user_agent=self.user_agent,
                locale="en-US",
                viewport=VIEWPORT,
                service_workers="block",  # keeps every request visible to the route check
                accept_downloads=False,
            )
            await context.add_init_script(INIT_SCRIPT)
            await context.route("**/*", self._route)
            with contextlib.suppress(AttributeError):
                await context.route_web_socket("**/*", self._route_ws)
            entry = self._contexts[host] = _Context(context, now)
        entry.used = now
        self._contexts.move_to_end(host)
        return entry

    async def _drop(self, host: str) -> None:
        entry = self._contexts.pop(host, None)
        if entry is not None:
            with contextlib.suppress(Exception):
                await entry.context.close()

    async def _route(self, route: Any) -> None:
        request = route.request
        try:
            ok = request.resource_type not in BLOCKED_TYPES and await self._allowed(request.url)
        except Exception:
            ok = False
        with contextlib.suppress(Exception):
            await (route.continue_() if ok else route.abort())

    async def _route_ws(self, ws: Any) -> None:
        url = ws.url
        http_url = "http" + url[2:] if url.startswith("ws") else url
        try:
            ok = await self._allowed(http_url)
        except Exception:
            ok = False
        with contextlib.suppress(Exception):
            if ok:
                ws.connect_to_server()
            else:
                await ws.close()

    async def _load(self, page: Any, url: str) -> int:
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        status = [0]

        def on_response(response: Any) -> None:
            with contextlib.suppress(Exception):
                if response.request.is_navigation_request() and response.frame.parent_frame is None:
                    status[0] = response.status

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=GOTO_MS)
        except PlaywrightTimeout:
            pass  # a partially loaded DOM is still worth extracting
        except Exception as exc:
            raise BrowserError(str(exc).splitlines()[0][:200]) from None
        if challenge_title(await self._title(page)):
            await self._clear(page)
        await self._settle(page)
        return status[0]

    async def _clear(self, page: Any) -> None:
        """The challenge solves itself in a real engine; wait for its title to go away."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CHALLENGE_S
        while loop.time() < deadline:
            await asyncio.sleep(POLL_S)
            title = await self._title(page)
            if title and not challenge_title(title):
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                return
        log.info("challenge did not clear within %.0fs", CHALLENGE_S)

    async def _settle(self, page: Any) -> None:
        """Bounded text-stability wait; don't wait for analytics or polling."""
        previous, stable = "", 0
        for attempt in range(20):
            try:
                main = page.locator(MAIN).first
                has_main = await main.count() > 0
                current = await (main if has_main else page.locator("body")).inner_text(timeout=1000)
            except Exception:
                has_main, current = False, ""
            stable = stable + 1 if current == previous and len(current.strip()) > 100 else 0
            if stable >= 2 and (has_main or attempt >= 10):
                return
            previous = current
            await asyncio.sleep(0.15)

    @staticmethod
    async def _title(page: Any) -> str:
        try:
            return await page.title()
        except Exception:
            return ""  # navigation in progress

    @staticmethod
    async def _html(page: Any) -> str:
        for _ in range(8):
            try:
                return await page.content()
            except Exception:
                await asyncio.sleep(POLL_S)  # "page is navigating"
        return await page.content()

    async def aclose(self) -> None:
        async with self._launch_lock:
            await self._shutdown()

    async def _shutdown(self) -> None:
        for host in list(self._contexts):
            await self._drop(host)
        browser, self._browser = self._browser, None
        pw, self._pw = self._pw, None
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        if pw is not None:
            with contextlib.suppress(Exception):
                await pw.stop()
