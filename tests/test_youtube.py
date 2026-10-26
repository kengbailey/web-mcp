"""YouTube: transcript formatting, captions→STT fallback order, caches, video and comments."""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.core import youtube
from src.core.errors import (
    INVALID_ARGUMENT,
    NOT_CONFIGURED,
    NOT_FOUND,
    RATE_LIMITED,
    UNAVAILABLE,
    Fail,
)
from src.core.youtube import YouTube, clock, paragraphs, pick_track, render, stt_paragraphs
from tests.conftest import build_services

VID = "dQw4w9WgXcQ"
WATCH = f"https://www.youtube.com/watch?v={VID}"
CAPS = f"https://www.youtube.com/api/timedtext?v={VID}&fmt=json3"


def make_info(**extra) -> dict:
    info = {
        "id": VID,
        "title": "Never Gonna Give You Up",
        "channel": "Rick Astley",
        "duration": 213,
        "upload_date": "20091025",
        "view_count": 1_712_345_678,
        "like_count": 18_400_000,
        "comment_count": 2_412_000,
        "description": "The official video.",
        "language": "en",
        "live_status": "not_live",
        "subtitles": {},
        "automatic_captions": {
            "en-orig": [{"ext": "json3", "url": CAPS + "&lang=en"}],
            "en": [{"ext": "json3", "url": CAPS + "&lang=en"}],
            "fr": [{"ext": "json3", "url": CAPS + "&lang=en&tlang=fr"}],
        },
    }
    info.update(extra)
    return info


def json3(fragments: list[tuple[float, str]]) -> dict:
    return {"events": [{"tStartMs": int(s * 1000), "segs": [{"utf8": t}]} for s, t in fragments]
            + [{"tStartMs": 0, "aAppend": 1, "segs": [{"utf8": "\n"}]}]}


SPEECH = [(i * 3.0, f"sentence number {i} is here.") for i in range(60)]  # 3 minutes


@pytest.fixture
def stubs(monkeypatch):
    """Stub the two blocking upstreams (yt-dlp extraction, youtube-transcript-api)."""
    calls = {"info": 0, "api": 0, "stt": 0}
    state: dict = {"info": make_info(), "api": None, "stt": ("Hello there. " * 80, "nvidia/parakeet-tdt-0.6b-v3")}

    def extract(ident, proxy):
        calls["info"] += 1
        if isinstance(state["info"], BaseException):
            raise state["info"]
        return copy.deepcopy(state["info"])

    def api(ident, lang, proxy):
        calls["api"] += 1
        if isinstance(state["api"], BaseException):
            raise state["api"]
        return state["api"]

    async def transcribe(services, info, language, progress):
        calls["stt"] += 1
        return state["stt"]

    monkeypatch.setattr(youtube, "extract_info", extract)
    monkeypatch.setattr(youtube, "fetch_api_transcript", api)
    monkeypatch.setattr(youtube.stt, "transcribe", transcribe)
    return calls, state


def caption_server(fragments=SPEECH, status: int = 200):
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        if request.url.host == "www.youtube.com" and request.url.path == "/api/timedtext":
            return httpx.Response(status, json=json3(fragments)) if status == 200 else httpx.Response(status)
        return httpx.Response(404)

    return handler, hits


def blocked() -> Exception:
    from youtube_transcript_api import RequestBlocked

    return RequestBlocked(VID)


# ---- formatting ------------------------------------------------------------------------------

def test_clock():
    assert (clock(5), clock(65.9), clock(3725)) == ("0:05", "1:05", "1:02:05")


def test_fragments_merge_into_45_to_60_second_paragraphs():
    paras = paragraphs(SPEECH)
    starts = [p[0] for p in paras]
    assert starts[0] == 0 and len(paras) == 4
    assert all(45 <= b - a <= 60 for a, b in zip(starts, starts[1:]))
    assert all(p[1].endswith(".") for p in paras[:-1])  # broke at sentence ends
    text = render(paras, True)
    assert text.startswith("[0:00] sentence number 0 is here. sentence number 1")
    assert "\n\n[0:45] " in text
    plain = render(paras, False)
    assert "[" not in plain and plain.count("\n\n") == 3


def test_unpunctuated_captions_break_at_the_hard_limit():
    paras = paragraphs([(i * 2.0, f"word{i} more words") for i in range(100)])
    starts = [p[0] for p in paras]
    assert all(b - a == 60 for a, b in zip(starts, starts[1:]))


def test_noise_and_adjacent_and_rolling_duplicates_are_removed():
    frags = [
        (0, "[Music]"),
        (1, "♪ we're no strangers ♪"),
        (2, "we're no strangers"),  # adjacent duplicate
        (3, "hello there how are"),
        (4, "hello there how are you doing"),  # rolling: repeats the previous line
        (5, "you doing today"),  # repeats a 2-word tail but adds text: kept as is
        (6, "(Applause) no no"),
        (7, "no"),
        (8, ">> [laughs] [__] fine   then"),
        (9, "[upbeat music]"),
    ]
    (para,) = paragraphs(frags)
    assert para == [1, "we're no strangers hello there how are you doing you doing today no no no [__] fine then"]


def test_compact_paragraphs_beat_naive_per_line_timestamps():
    frags = [(i * 2.5, f"this is spoken caption line {i}") for i in range(400)]
    naive = "\n".join(f"[{s:.3f}s] {t}" for s, t in frags)  # v2 format
    compact = render(paragraphs(frags), True)
    assert len(compact) < 0.8 * len(naive)
    assert len(render(paragraphs(frags), False)) < len(compact)


def test_stt_text_becomes_untimed_600_char_paragraphs():
    text = " ".join(f"Sentence {i} has a few words in it." for i in range(150))
    paras = stt_paragraphs(text)
    assert len(paras) > 5 and all(600 <= len(p) < 700 for p in paras[:-1])
    assert " ".join(paras) == text
    run = stt_paragraphs("word " * 1000)  # no punctuation at all
    assert len(run) > 3 and all(len(p) <= 1200 for p in run)


def test_pick_track_prefers_manual_original_and_flags_translations():
    info = make_info(subtitles={"en-GB": [{"ext": "json3", "url": CAPS + "&m=1"}],
                                "live_chat": [{"ext": "json3", "url": "x"}]})
    assert pick_track(info, "")[1:] == ("captions", "en-GB", False)
    assert pick_track(info, "en")[1:] == ("captions", "en-GB", False)
    assert pick_track(info, "fr")[1:] == ("auto-captions", "fr", True)
    assert pick_track(info, "de") is None
    auto_only = make_info(language=None)
    assert pick_track(auto_only, "")[1:] == ("auto-captions", "en", False)  # from the -orig track
    manual_only = make_info(language=None, automatic_captions={}, subtitles={"es": [{"ext": "json3", "url": "u"}]})
    assert pick_track(manual_only, "")[1:] == ("captions", "es", False)


def test_transcript_api_picks_manual_original_then_translation():
    def tr(code, generated, translations=()):
        return SimpleNamespace(
            language_code=code, is_generated=generated,
            translation_languages=[SimpleNamespace(language_code=c) for c in translations],
            translate=lambda c: f"{code}->{c}",
        )

    listing = [tr("de", False, ["fr"]), tr("en", False), tr("en", True, ["fr"])]
    assert youtube._pick_api(listing, "")[0].language_code == "en"
    assert youtube._pick_api(listing, "")[0].is_generated is False
    assert youtube._pick_api(listing, "de")[0].language_code == "de"
    assert youtube._pick_api(listing, "fr") == ("de->fr", True)
    assert youtube._pick_api(listing, "ja") == (None, False)
    assert youtube._pick_api([tr("ja", False)], "")[0].language_code == "ja"


# ---- transcript flow -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fallback_order_api_blocked_then_json3_then_stt(stubs):
    calls, state = stubs
    state["api"] = blocked()
    handler, hits = caption_server()
    services = build_services(handler, stt_api_key="k")
    yt = YouTube(services)

    out = await yt.transcript("me", VID)
    head, body = out.split("\n\n", 1)
    size = len(render(paragraphs(SPEECH), True))
    assert head == f"Never Gonna Give You Up · {WATCH} · auto-captions · en · {size} chars"
    assert body.startswith("[0:00] sentence number 0")
    assert services.hosts.get(youtube.API_BLOCK) == "blocked"
    assert calls == {"info": 1, "api": 1, "stt": 0}

    # Another language: the blocked fast path is skipped; json3 reuses the cached info.
    out = await yt.transcript("me", VID, language="fr")
    assert " · auto-captions · fr · translated · " in out.split("\n", 1)[0]
    assert calls == {"info": 1, "api": 1, "stt": 0}
    assert "tlang=fr" in hits[-1]

    # No usable captions: STT fallback, untimed paragraphs.
    state["info"] = make_info(id="aaaaaaaaaaa", automatic_captions={})
    out = await yt.transcript("me", "aaaaaaaaaaa")
    head, body = out.split("\n\n", 1)
    assert " · stt · en · stt fallback · " in head and not body.startswith("[")
    assert calls["stt"] == 1 and calls["api"] == 1


@pytest.mark.asyncio
async def test_caption_network_error_still_falls_back_to_stt(stubs):
    calls, state = stubs
    state["api"] = RuntimeError("down")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    out = await YouTube(build_services(handler, stt_api_key="k")).transcript("me", VID)
    assert " · stt · en · stt fallback · " in out.split("\n", 1)[0] and calls["stt"] == 1


@pytest.mark.asyncio
async def test_api_fast_path_success_skips_extraction(stubs):
    calls, state = stubs
    state["api"] = {"source": "captions", "lang": "en", "note": "", "paras": paragraphs(SPEECH)}
    yt = YouTube(build_services())
    out = await yt.transcript("me", WATCH)
    assert out.startswith(f"{WATCH} · captions · en · ")  # no title: info was never extracted
    assert calls == {"info": 0, "api": 1, "stt": 0}


@pytest.mark.asyncio
async def test_transcript_cache_serves_timestamp_toggle_and_repeat(stubs):
    calls, state = stubs
    state["api"] = RuntimeError("down")
    handler, hits = caption_server()
    yt = YouTube(build_services(handler))
    first = await yt.transcript("me", VID)
    second = await yt.transcript("me", VID, timestamps=False)
    third = await yt.transcript("me", f"https://youtu.be/{VID}")
    assert calls == {"info": 1, "api": 1, "stt": 0} and len(hits) == 1
    assert "[0:00]" in first and "[0:" not in second and third == first
    assert yt.services.hosts.get(youtube.API_BLOCK) is None  # a plain failure is not a block


@pytest.mark.asyncio
async def test_long_transcript_pages_through_cursor(stubs):
    calls, state = stubs
    state["api"] = {"source": "captions", "lang": "en", "note": "",
                    "paras": paragraphs([(i * 3.0, f"line {i} of a long talk.") for i in range(600)])}
    services = build_services()
    out = await YouTube(services).transcript("me", VID, max_chars=500)
    head, body = out.split("\n\n", 1)
    assert "chars 0–" in head and "next: " in head and len(body) <= 500
    pieces, cursor = [body], head.rsplit("next: ", 1)[1]
    while cursor:
        chunk = services.pages.read("me", cursor, 2000)
        pieces.append(chunk.text.split("\n\n", 1)[1])
        cursor = chunk.cursor
    assert "".join(pieces) == render(state["api"]["paras"], True)


@pytest.mark.asyncio
async def test_no_fallback_paths_and_not_configured(stubs):
    calls, state = stubs
    state["api"] = RuntimeError("down")
    handler, _ = caption_server(status=404)
    yt = YouTube(build_services(handler, stt_api_key="k"))
    for kwargs, code in [
        (dict(reference="not a video"), INVALID_ARGUMENT),
        (dict(reference=VID, language="en us!"), INVALID_ARGUMENT),
        (dict(reference=VID, fallback_to_stt=False), UNAVAILABLE),
    ]:
        with pytest.raises(Fail) as exc:
            await yt.transcript("me", **kwargs)
        assert exc.value.code == code
    state["info"] = Fail(NOT_FOUND, "video is unavailable or private")
    with pytest.raises(Fail) as exc:
        await yt.transcript("me", "bbbbbbbbbbb")
    assert exc.value.code == NOT_FOUND
    assert calls["stt"] == 0

    no_key = YouTube(build_services(handler))
    with pytest.raises(Fail) as exc:
        await no_key.transcript("me", "ccccccccccc", source="stt")
    assert exc.value.code == NOT_CONFIGURED
    state["info"] = make_info()
    with pytest.raises(Fail) as exc:  # captions failed and STT is not configured: keep the real reason
        await no_key.transcript("me", VID)
    assert exc.value.code == UNAVAILABLE and "HTTP 404" in exc.value.message
    assert calls["stt"] == 0


@pytest.mark.asyncio
async def test_forced_stt_is_cached_and_never_tries_captions(stubs):
    calls, state = stubs
    yt = YouTube(build_services(stt_api_key="k"))
    a = await yt.transcript("me", VID, source="stt")
    b = await yt.transcript("me", VID, source="stt", timestamps=False)
    assert calls == {"info": 1, "api": 0, "stt": 1}
    assert a.startswith(f"Never Gonna Give You Up · {WATCH} · stt · en · ") and a == b


# ---- video -------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_video_with_api_key():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [{
            "snippet": {"title": "Never Gonna Give You Up", "channelTitle": "Rick Astley",
                        "publishedAt": "2009-10-25T06:57:33Z", "description": "Line one.\n" + "d" * 5000,
                        "liveBroadcastContent": "none"},
            "statistics": {"viewCount": "1712345678", "likeCount": "18400000", "commentCount": "2412000"},
            "contentDetails": {"duration": "PT3M33S"},
        }]})

    services = build_services(handler, youtube_api_key="yk")
    out = await YouTube(services).video("me", WATCH)
    title, line, _, body = out.split("\n", 3)
    assert title == "Never Gonna Give You Up"
    assert line == f"Rick Astley · 3:33 · 2009-10-25 · 1.7B views · 18.4M likes · 2.4M comments · {WATCH}"
    assert body.startswith("Line one.\nddd") and re.search(r"d \[\+3.5k chars: [\w-]{12}\]$", body)
    assert "dislike" not in out.lower() and "null" not in out
    query = parse_qs(urlparse(str(seen[0].url)).query)
    assert seen[0].url.path == "/youtube/v3/videos"
    assert query["part"] == ["snippet,statistics,contentDetails"] and "key" not in query
    assert seen[0].headers["x-goog-api-key"] == "yk"  # header, so the key never lands in URL logs
    await YouTube(services).video("me", VID)
    assert len(seen) == 1  # metadata cached


@pytest.mark.asyncio
async def test_video_without_api_key_uses_the_shared_extraction(stubs):
    calls, state = stubs
    state["info"] = make_info(like_count=None, comment_count=None, description="")
    out = await YouTube(build_services()).video("me", VID)
    assert out == f"Never Gonna Give You Up\nRick Astley · 3:33 · 2009-10-25 · 1.7B views · {WATCH}"
    state["info"] = make_info(id="ddddddddddd", duration=0, live_status="is_live")
    out = await YouTube(build_services()).video("me", "ddddddddddd")
    assert "Rick Astley · live · 2009-10-25" in out


@pytest.mark.asyncio
async def test_video_api_not_found():
    services = build_services(lambda r: httpx.Response(200, json={"items": []}), youtube_api_key="yk")
    with pytest.raises(Fail) as exc:
        await YouTube(services).video("me", VID)
    assert exc.value.code == NOT_FOUND


# ---- comments ----------------------------------------------------------------------------------

def thread(i: int, text: str = "", replies: int = 0) -> dict:
    when = (datetime.now(timezone.utc) - timedelta(days=3, hours=1)).isoformat().replace("+00:00", "Z")
    return {"snippet": {"totalReplyCount": replies, "topLevelComment": {"id": f"Ugc{i:05d}xyz", "snippet": {
        "authorDisplayName": f"@user{i}", "textDisplay": text or f"comment {i}\nsecond line",
        "likeCount": 1200 if i == 0 else i, "publishedAt": when}}}}


@pytest.mark.asyncio
async def test_comments_render_and_page_through_pending_then_upstream():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        seen.append(query)
        assert request.url.path == "/youtube/v3/commentThreads"
        if "pageToken" not in query:
            items = [thread(0, replies=3), thread(1, text="x" * 900, replies=1)] + [thread(i) for i in range(2, 20)]
            return httpx.Response(200, json={"items": items, "nextPageToken": "P2"})
        return httpx.Response(200, json={"items": [thread(i) for i in range(20, 25)]})

    services = build_services(handler, youtube_api_key="yk")
    yt = YouTube(services)
    out = await yt.comments("me", VID, limit=10, query="chorus")
    lines = out.split("\n")
    assert lines[0] == f"{VID} · relevance · 10 comments" and lines[1] == ""
    assert lines[2] == "- Ugc00000xyz @user0 1.2k↑ 3d: comment 0 second line (↳ 3 replies)"
    assert lines[3].startswith("- Ugc00001xyz @user1 1↑ 3d: xxx") and " [+" in lines[3]
    assert lines[3].endswith(" (↳ 1 reply)") and len(lines[3]) < 600
    assert lines[-1].startswith("more: ") and lines[-2] == ""
    assert seen[0] == {"part": "snippet", "maxResults": "20", "textFormat": "plainText", "videoId": VID,
                       "order": "relevance", "searchTerms": "chorus"}
    cursor = lines[-1][6:]

    out2 = await yt.comments("me", cursor=cursor)
    assert len(seen) == 1  # served from the pending rows
    assert out2.split("\n")[2].startswith("- Ugc00010xyz") and "more: " in out2
    replay = await yt.comments("me", cursor=cursor)  # cursors are replayable
    assert replay.split("\n")[:-1] == out2.split("\n")[:-1]

    out3 = await yt.comments("me", cursor=out2.rsplit("more: ", 1)[1], limit=10)
    assert seen[-1]["pageToken"] == "P2" and len(seen) == 2
    assert out3.startswith(f"{VID} · relevance · 5 comments") and "more:" not in out3


@pytest.mark.asyncio
async def test_comment_replies_use_parent_id():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = thread(7)["snippet"]["topLevelComment"]
        return httpx.Response(200, json={"items": [item]})

    out = await YouTube(build_services(handler, youtube_api_key="yk")).comments("me", parent_id="Ugc00000xyz")
    assert seen[0].url.path == "/youtube/v3/comments" and seen[0].url.params["parentId"] == "Ugc00000xyz"
    assert out.split("\n")[0] == "replies to Ugc00000xyz · 1 comments"
    assert out.split("\n")[2].startswith("- Ugc00007xyz @user7 7↑ 3d: comment 7") and "↳" not in out


@pytest.mark.parametrize(
    "status, reason, code",
    [(403, "commentsDisabled", UNAVAILABLE), (403, "quotaExceeded", RATE_LIMITED),
     (404, "videoNotFound", NOT_FOUND), (500, "backendError", "UPSTREAM_ERROR")],
)
@pytest.mark.asyncio
async def test_comment_errors_map_to_codes(status, reason, code):
    body = {"error": {"errors": [{"reason": reason}], "message": "secret detail"}}
    services = build_services(lambda r: httpx.Response(status, json=body), youtube_api_key="yk")
    with pytest.raises(Fail) as exc:
        await YouTube(services).comments("me", VID)
    assert exc.value.code == code and "secret" not in exc.value.message and "yk" not in exc.value.message


@pytest.mark.asyncio
async def test_comments_need_key_and_reference():
    with pytest.raises(Fail) as exc:
        await YouTube(build_services()).comments("me", VID)
    assert exc.value.code == NOT_CONFIGURED
    yt = YouTube(build_services(youtube_api_key="yk"))
    for kwargs in (dict(reference=""), dict(reference=VID, order="top"), dict(parent_id="bad id!")):
        with pytest.raises(Fail) as exc:
            await yt.comments("me", **kwargs)
        assert exc.value.code == INVALID_ARGUMENT


# ---- over MCP ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tools_return_plain_text_over_mcp(make_client, stubs):
    calls, state = stubs
    state["api"] = {"source": "captions", "lang": "en", "note": "", "paras": paragraphs(SPEECH)}
    async with make_client() as (client, _):
        video = await client.call_tool("fetch_youtube_content", {"reference": VID})
        transcript = await client.call_tool("fetch_youtube_transcript", {"reference": VID, "timestamps": False})
        comments = await client.call_tool("fetch_youtube_comments", {"reference": VID}, raise_on_error=False)
    assert video.structured_content is None and video.content[0].text.startswith("Never Gonna Give You Up\n")
    assert transcript.content[0].text.startswith("Never Gonna Give You Up · ")
    assert comments.is_error and comments.content[0].text.startswith("ERROR NOT_CONFIGURED: ")


# ---- live (pytest -m integration) ----------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_captions_and_metadata(request):
    if "integration" not in (request.config.getoption("markexpr") or ""):
        pytest.skip("live test; run with -m integration")
    from src.core.config import Settings
    from src.core.services import Services

    services = Services(Settings.from_env())
    try:
        yt = YouTube(services)
        out = await yt.transcript("anon", "jNQXAC9IVRw")
        assert "captions" in out.split("\n", 1)[0] and "elephants" in out
        assert (await yt.video("anon", "jNQXAC9IVRw")).startswith("Me at the zoo\n")
    finally:
        await services.aclose()
