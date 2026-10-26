"""Speech-to-text for YouTube audio via an OpenAI-compatible endpoint (OpenRouter Parakeet).

Audio is downloaded in-process by yt-dlp from the already-extracted info dict (no second
extraction), never re-encoded, split with ``ffmpeg -c copy`` only when over the upload
limit, and transcribed with up to 4 concurrent requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import os
import re
import signal
import tempfile
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from .errors import (
    NOT_CONFIGURED,
    NOT_FOUND,
    RATE_LIMITED,
    TOO_LARGE,
    UNAVAILABLE,
    UNSUPPORTED_CONTENT,
    UPSTREAM_BLOCKED,
    UPSTREAM_ERROR,
    UPSTREAM_TIMEOUT,
    Fail,
    for_status,
)

T = TypeVar("T")
Progress = Callable[..., Awaitable[Any]] | None
log = logging.getLogger("web_mcp.stt")

AUDIO_FORMAT = "ba[acodec=opus]/ba"
AUDIO_SORT = ["lang", "+abr"]  # original-language track, smallest bitrate
MAX_UPLOAD = 24_000_000  # OpenRouter rejects requests over 25 MB
SEGMENT_SECONDS = 900
CONCURRENCY = 4
REQUEST_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
# nvidia/parakeet-tdt-0.6b-v3 languages; anything else goes to the fallback model.
PARAKEET = frozenset(
    "bg hr cs da nl en et fi fr de el hu it lv lt mt pl pt ro sk sl es sv ru uk".split()
)
_UPLOAD = {
    ".webm": ("audio.webm", "audio/webm"),
    ".m4a": ("audio.m4a", "audio/mp4"),
    ".mp4": ("audio.m4a", "audio/mp4"),
    ".opus": ("audio.ogg", "audio/ogg"),
    ".ogg": ("audio.ogg", "audio/ogg"),
    ".mp3": ("audio.mp3", "audio/mpeg"),
}


class _YdlLog:
    """Route yt-dlp output to logging instead of stdout/stderr."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    info = warning = debug

    def error(self, msg: str) -> None:
        log.info("yt-dlp: %s", msg)


def ydl_options(proxy: str | None) -> dict:
    """yt-dlp options shared by info extraction and audio download."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 10,
        "extractor_retries": 0,
        "js_runtimes": {"deno": {}},
        "logger": _YdlLog(),
    }
    if proxy:
        opts["proxy"] = proxy
    return opts


def ytdlp_fail(exc: BaseException, what: str) -> Fail:
    """Map a yt-dlp DownloadError to a safe Fail."""
    msg = str(exc).lower()
    if "requested format is not available" in msg or "no video formats" in msg:
        return Fail(UNAVAILABLE, "no downloadable audio for this video")
    if "confirm your age" in msg or "age-restricted" in msg or "inappropriate for some users" in msg:
        return Fail(UNAVAILABLE, "video is age-restricted")
    if "not a bot" in msg or "sign in to confirm" in msg:
        return Fail(UPSTREAM_BLOCKED, "YouTube is bot-checking this server")
    if "429" in msg or "too many requests" in msg:
        return Fail(RATE_LIMITED, "YouTube rate-limited this server")
    if "live event will begin" in msg or "premieres in" in msg:
        return Fail(UNAVAILABLE, "video has not started yet")
    if any(s in msg for s in ("private video", "video unavailable", "been removed", "not available", "does not exist")):
        return Fail(NOT_FOUND, "video is unavailable or private")
    if "timed out" in msg:
        return Fail(UPSTREAM_TIMEOUT, f"{what} timed out")
    return Fail(UPSTREAM_ERROR, f"{what} failed")


def audio_language(info: dict, language: str = "") -> str:
    """Base language code of the audio: the tool param, else yt-dlp's detected language."""
    code = language or info.get("language") or ""
    if not code:
        code = next((k[:-5] for k in info.get("automatic_captions") or {} if k.endswith("-orig")), "")
    return re.split(r"[-_+]", code.strip().lower())[0] if code else ""


def model_for(settings: Any, language: str) -> str:
    return settings.stt_model if not language or language in PARAKEET else settings.stt_fallback_model


async def report(progress: Progress, value: float, message: str) -> None:
    """Best-effort MCP progress notification; never fails the call."""
    if progress is None:
        return
    try:
        await progress(round(value, 1), total=100, message=message)
    except Exception:  # noqa: BLE001 - progress is advisory
        log.debug("progress report failed", exc_info=True)


async def _in_thread(fn: Callable[[], T], cancel: threading.Event, grace: float = 5.0) -> T:
    """Run ``fn`` in a worker; on task cancellation set ``cancel`` and give it time to stop."""
    future = asyncio.get_running_loop().run_in_executor(None, fn)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        cancel.set()
        await asyncio.wait({future}, timeout=grace)
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        raise


def download(info: dict, directory: Path, proxy: str | None, cancel: threading.Event,
             on_progress: Callable[[dict], None]) -> Path:
    """Download the original-language, smallest audio track as-is (blocking; run in a thread)."""
    import yt_dlp

    def hook(status: dict) -> None:
        if cancel.is_set():
            raise yt_dlp.utils.DownloadCancelled("cancelled")
        on_progress(status)

    opts = {
        **ydl_options(proxy),
        "format": AUDIO_FORMAT,
        "format_sort": AUDIO_SORT,
        "outtmpl": str(directory / "audio.%(ext)s"),
        "progress_hooks": [hook],
        "postprocessors": [],
        "fixup": "never",
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            audio_info = {k: v for k, v in info.items() if k not in ("subtitles", "automatic_captions")}
            ydl.process_ie_result(copy.deepcopy(audio_info), download=True)
    except yt_dlp.utils.DownloadCancelled:
        raise
    except yt_dlp.utils.DownloadError as exc:
        raise ytdlp_fail(exc, "audio download") from None
    files = [p for p in directory.iterdir() if p.is_file() and p.name.startswith("audio.")
             and p.suffix not in (".part", ".ytdl")]
    if not files:
        raise Fail(UNAVAILABLE, "no audio was downloaded")
    return files[0]


async def split(audio: Path, directory: Path) -> list[Path]:
    """Split into SEGMENT_SECONDS pieces without re-encoding; kills ffmpeg's group on cancel."""
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(audio),
        "-map", "0:a", "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-c", "copy",
        "-reset_timestamps", "1", str(directory / f"part%03d{audio.suffix}"),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
    except FileNotFoundError:
        raise Fail(NOT_CONFIGURED, "ffmpeg is required to transcribe long audio") from None
    try:
        _, err = await proc.communicate()
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
    if proc.returncode:
        log.info("ffmpeg segment failed: %s", err.decode(errors="replace")[-500:])
        raise Fail(UNSUPPORTED_CONTENT, "audio could not be split for transcription")
    parts = sorted(directory.glob(f"part*{audio.suffix}"))
    if not parts:
        raise Fail(UNSUPPORTED_CONTENT, "audio could not be split for transcription")
    return parts


async def _request(services: Any, path: Path, model: str, language: str) -> str:
    s = services.settings
    name, mime = _UPLOAD.get(path.suffix.lower(), ("audio" + path.suffix, "application/octet-stream"))
    # No response_format: OpenRouter rejects "text"; its default is JSON with a "text" field.
    data = {"model": model, **({"language": language} if language else {})}
    with path.open("rb") as handle:
        response = await services.net.client("api").post(
            f"{s.stt_endpoint}/audio/transcriptions",
            data=data,
            files={"file": (name, handle, mime)},
            headers={"Authorization": f"Bearer {s.stt_api_key}"},
            timeout=REQUEST_TIMEOUT,
        )
    if response.status_code == 413:
        raise Fail(TOO_LARGE, "audio segment is too large for speech-to-text")
    if response.status_code != 200:
        raise for_status(response.status_code, "speech-to-text")
    try:
        body = response.json()
    except ValueError:
        raise Fail(UPSTREAM_ERROR, "speech-to-text returned invalid JSON") from None
    usage = body.get("usage") or {}
    log.info("stt %s model=%s seconds=%s cost=%s", path.name, model, usage.get("seconds"), usage.get("cost"))
    return (body.get("text") or "").strip()


async def _transcribe_parts(services: Any, parts: list[Path], model: str, language: str,
                            progress: Progress) -> list[str]:
    texts = [""] * len(parts)
    done = 0
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(index: int, path: Path) -> None:
        nonlocal done
        async with gate:
            texts[index] = await _request(services, path, model, language)
        done += 1
        await report(progress, 50 + 50 * done / len(parts), f"transcribed {done}/{len(parts)}")

    try:
        async with asyncio.TaskGroup() as group:
            for index, path in enumerate(parts):
                group.create_task(one(index, path))
    except BaseExceptionGroup as grouped:
        raise grouped.exceptions[0] from None
    return texts


async def transcribe(services: Any, info: dict, language: str, progress: Progress) -> tuple[str, str]:
    """Transcribe a video's audio; returns (text, model)."""
    s = services.settings
    if not s.stt_api_key:
        raise Fail(NOT_CONFIGURED, "STT_API_KEY is required for speech-to-text")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise Fail(UNSUPPORTED_CONTENT, "live streams cannot be transcribed")
    if (info.get("duration") or 0) > s.stt_max_seconds:
        raise Fail(TOO_LARGE, f"video is longer than the {s.stt_max_seconds // 60}-minute speech-to-text limit")
    language = audio_language(info, language)
    model = model_for(s, language)
    loop = asyncio.get_running_loop()
    cancel = threading.Event()
    last = [-5]

    def on_progress(status: dict) -> None:
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        if status.get("status") != "downloading" or not total:
            return
        pct = int(100 * (status.get("downloaded_bytes") or 0) / total)
        if pct >= last[0] + 5:
            last[0] = pct
            asyncio.run_coroutine_threadsafe(report(progress, 0.4 * pct, f"downloading audio {pct}%"), loop)

    with tempfile.TemporaryDirectory(prefix="web_mcp_stt_", ignore_cleanup_errors=True) as root:
        directory = Path(root)
        await report(progress, 0, "downloading audio")
        audio = await _in_thread(
            lambda: download(info, directory, s.youtube_proxy_url, cancel, on_progress), cancel
        )
        if audio.stat().st_size <= MAX_UPLOAD:
            parts = [audio]
        else:
            await report(progress, 42, "splitting audio")
            parts = await split(audio, directory)
        await report(progress, 50, f"transcribing {len(parts)} part(s) with {model}")
        texts = await _transcribe_parts(services, parts, model, language, progress)
    text = " ".join(t for t in texts if t).strip()
    if not text:
        raise Fail(UNAVAILABLE, "speech-to-text returned no text")
    return text, model
