"""Speech-to-text: OpenRouter request shape, model routing, segmenting, cancellation, errors."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
from pathlib import Path

import httpx
import pytest

from src.core import stt
from src.core.config import Settings
from src.core.errors import NOT_CONFIGURED, TOO_LARGE, UNAVAILABLE, UNSUPPORTED_CONTENT, UPSTREAM_BLOCKED, Fail
from src.core.services import Services
from tests.conftest import build_services

PARAKEET = "nvidia/parakeet-tdt-0.6b-v3"
WHISPER = "openai/whisper-large-v3-turbo"
URL = "https://openrouter.ai/api/v1/audio/transcriptions"


def fake_download(ext: str = ".webm", size: int = 64, calls: list | None = None):
    def download(info, directory: Path, proxy, cancel, on_progress):
        if calls is not None:
            calls.append(info)
        on_progress({"status": "downloading", "downloaded_bytes": size // 2, "total_bytes": size})
        path = directory / f"audio{ext}"
        path.write_bytes(b"x" * size)
        return path

    return download


def ok(text: str = "Hello world.") -> httpx.Response:
    return httpx.Response(200, json={"text": text, "usage": {"seconds": 12, "cost": 0.0001}})


@pytest.mark.asyncio
async def test_request_shape_asks_for_segments_and_uses_native_filename(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(" Hello there. ")

    monkeypatch.setattr(stt, "download", fake_download(".webm"))
    services = build_services(handler, stt_api_key="sk-test")
    text, model, cues = await stt.transcribe(services, {"language": "en", "duration": 60}, "", None)
    assert (text, model, cues) == ("Hello there.", PARAKEET, [])
    (request,) = seen
    body = request.content
    assert str(request.url) == URL and request.method == "POST"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert b'name="model"\r\n\r\n' + PARAKEET.encode() in body
    assert b'name="language"\r\n\r\nen' in body
    assert b'filename="audio.webm"' in body and b"Content-Type: audio/webm" in body
    assert b'name="response_format"\r\n\r\nverbose_json' in body and b"\r\n\r\ntext\r\n" not in body


@pytest.mark.parametrize(
    "info_lang, param, model, sent",
    [
        ("de", "", PARAKEET, "de"),
        ("ja", "", WHISPER, "ja"),
        ("", "", PARAKEET, None),  # unknown language → Parakeet, no language field
        ("ja", "pt-BR", PARAKEET, "pt"),  # tool param wins, reduced to the base code
        ("zh-Hans", "", WHISPER, "zh"),
    ],
)
@pytest.mark.asyncio
async def test_model_routing_by_language(monkeypatch, info_lang, param, model, sent):
    bodies: list[bytes] = []
    monkeypatch.setattr(stt, "download", fake_download(".m4a"))
    services = build_services(lambda r: bodies.append(r.content) or ok(), stt_api_key="k")
    _, used, _ = await stt.transcribe(services, {"language": info_lang} if info_lang else {}, param, None)
    assert used == model
    assert b'filename="audio.m4a"' in bodies[0]
    if sent:
        assert b'name="language"\r\n\r\n' + sent.encode() + b"\r\n" in bodies[0]
    else:
        assert b'name="language"' not in bodies[0]


def test_language_from_original_caption_track_and_upload_names():
    assert stt.audio_language({"automatic_captions": {"fr": [], "es-orig": []}}) == "es"
    assert stt.audio_language({}, "") == ""
    assert stt._UPLOAD[".opus"][0] == "audio.ogg"
    assert stt._UPLOAD[".webm"][0] == "audio.webm"


@pytest.mark.asyncio
async def test_small_file_is_one_request_large_file_is_split_and_joined_in_order(monkeypatch):
    active = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        if b"PART" not in request.content:
            return ok("whole")
        return ok("t" + request.content.split(b"PART")[1][:1].decode())

    splits: list[Path] = []

    async def fake_split(audio: Path, directory: Path) -> list[Path]:
        splits.append(audio)
        parts = []
        for i in range(6):
            part = directory / f"part{i:03d}{audio.suffix}"
            part.write_bytes(b"PART" + str(i).encode())
            parts.append(part)
        return parts

    monkeypatch.setattr(stt, "split", fake_split)
    monkeypatch.setattr(stt, "MAX_UPLOAD", 100)
    services = build_services(handler, stt_api_key="k")

    monkeypatch.setattr(stt, "download", fake_download(size=100))
    text, _, _ = await stt.transcribe(services, {}, "en", None)
    assert (splits, text) == ([], "whole")  # at the limit: one request, no split
    monkeypatch.setattr(stt, "download", fake_download(size=101))
    text, _, _ = await stt.transcribe(services, {}, "en", None)
    assert len(splits) == 1
    assert text == "t0 t1 t2 t3 t4 t5"
    assert peak <= stt.CONCURRENCY


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.asyncio
async def test_split_uses_ffmpeg_stream_copy(monkeypatch, tmp_path):
    source = tmp_path / "audio.webm"
    made = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-c:a", "libopus", "-b:a", "32k", str(source)],
        capture_output=True,
    )
    if made.returncode:
        pytest.skip("ffmpeg lacks libopus")
    monkeypatch.setattr(stt, "SEGMENT_SECONDS", 5)
    out = tmp_path / "out"
    out.mkdir()
    parts = await stt.split(source, out)
    assert [p.name for p in parts] == ["part000.webm", "part001.webm", "part002.webm"]
    assert all(p.stat().st_size > 0 for p in parts)


@pytest.mark.asyncio
async def test_cancelling_the_task_sets_the_download_cancel_event(monkeypatch):
    started, finished = threading.Event(), threading.Event()
    events: list[threading.Event] = []

    def blocking_download(info, directory, proxy, cancel, on_progress):
        events.append(cancel)
        started.set()
        cancel.wait(5)  # the real progress hook raises DownloadCancelled here
        finished.set()
        raise RuntimeError("cancelled")

    monkeypatch.setattr(stt, "download", blocking_download)
    services = build_services(stt_api_key="k")
    task = asyncio.create_task(stt.transcribe(services, {}, "", None))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events[0].is_set() and finished.is_set()


@pytest.mark.asyncio
async def test_guards_run_before_any_download(monkeypatch):
    calls: list = []
    monkeypatch.setattr(stt, "download", fake_download(calls=calls))
    with pytest.raises(Fail) as exc:
        await stt.transcribe(build_services(), {}, "", None)
    assert exc.value.code == NOT_CONFIGURED
    services = build_services(stt_api_key="k", stt_max_seconds=600)
    with pytest.raises(Fail) as exc:
        await stt.transcribe(services, {"is_live": True}, "", None)
    assert exc.value.code == UNSUPPORTED_CONTENT
    with pytest.raises(Fail) as exc:
        await stt.transcribe(services, {"duration": 601}, "", None)
    assert exc.value.code == TOO_LARGE
    assert calls == []


@pytest.mark.parametrize(
    "response, code",
    [
        (httpx.Response(200, json={"text": "  "}), UNAVAILABLE),
        (httpx.Response(401, json={"error": {"message": "User not found."}}), UPSTREAM_BLOCKED),
        (httpx.Response(413), TOO_LARGE),
    ],
)
@pytest.mark.asyncio
async def test_upstream_failures_map_to_codes(monkeypatch, response, code):
    monkeypatch.setattr(stt, "download", fake_download())
    with pytest.raises(Fail) as exc:
        await stt.transcribe(build_services(lambda r: response, stt_api_key="k"), {}, "", None)
    assert exc.value.code == code
    assert "User not found" not in exc.value.message


@pytest.mark.asyncio
async def test_progress_reports_download_and_transcription(monkeypatch):
    reports: list[tuple] = []

    async def progress(value, total=None, message=None):
        reports.append((value, total, message))

    monkeypatch.setattr(stt, "download", fake_download())
    await stt.transcribe(build_services(lambda r: ok(), stt_api_key="k"), {}, "", progress)
    await asyncio.sleep(0)  # let the thread-scheduled download report run
    messages = [m for _, _, m in reports]
    assert "downloading audio 50%" in messages
    assert any(m.startswith("transcribing 1 part(s)") for m in messages)
    assert any(v == 100 for v, _, _ in reports)
    assert all(t == 100 for _, t, _ in reports)


def test_ytdlp_errors_map_to_safe_codes():
    assert stt.ytdlp_fail(Exception("ERROR: [youtube] x: Video unavailable"), "x").code == "NOT_FOUND"
    assert stt.ytdlp_fail(Exception("Sign in to confirm you're not a bot"), "x").code == "UPSTREAM_BLOCKED"
    assert stt.ytdlp_fail(Exception("Sign in to confirm your age"), "x").code == "UNAVAILABLE"
    assert stt.ytdlp_fail(Exception("Requested format is not available"), "x").code == "UNAVAILABLE"
    assert stt.ytdlp_fail(Exception("boom"), "audio download").message == "audio download failed"


# ---- live (pytest -m integration) ----------------------------------------------------------

def _live(request) -> None:
    if "integration" not in (request.config.getoption("markexpr") or ""):
        pytest.skip("live test; run with -m integration")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_openrouter_parakeet_transcribes_a_short_video(request):
    _live(request)
    from src.core.youtube import YouTube

    settings = Settings.from_env()
    if not settings.stt_api_key:
        pytest.skip("STT_API_KEY / OPENROUTER_API_KEY not set")
    services = Services(settings)
    try:
        try:
            probe = await services.net.client("api").get(
                f"{settings.stt_endpoint}/key", headers={"Authorization": f"Bearer {settings.stt_api_key}"}
            )
        except httpx.HTTPError:
            pytest.skip("STT endpoint unreachable")
        if probe.status_code != 200:
            pytest.skip(f"STT key rejected (HTTP {probe.status_code})")
        out = await YouTube(services).transcript("anon", "jNQXAC9IVRw", source="stt")
        head, _, body = out.partition("\n\n")
        assert " · stt" in head and "elephant" in body.lower()
    finally:
        await services.aclose()


@pytest.mark.asyncio
async def test_segments_become_timed_cues_offset_across_parts(monkeypatch):
    replies = iter([
        {"text": "One. Two.", "duration": 900.0, "segments": [{"start": 0.5, "text": " One."}, {"start": 30.0, "text": "Two."}]},
        {"text": "Three.", "duration": 120.0, "segments": [{"start": 4.0, "text": "Three."}]},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(replies))

    async def two_parts(audio, directory):
        parts = [directory / "part0.webm", directory / "part1.webm"]
        for part in parts:
            part.write_bytes(b"x")
        return parts

    monkeypatch.setattr(stt, "download", fake_download(".webm", size=stt.MAX_UPLOAD + 1))
    monkeypatch.setattr(stt, "split", two_parts)
    monkeypatch.setattr(stt, "CONCURRENCY", 1)  # deterministic reply order
    services = build_services(handler, stt_api_key="k")
    text, _, cues = await stt.transcribe(services, {"language": "en"}, "", None)
    assert text == "One. Two. Three."
    assert cues == [(0.5, "One."), (30.0, "Two."), (904.0, "Three.")]


@pytest.mark.asyncio
async def test_provider_without_verbose_json_gets_one_plain_retry(monkeypatch):
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if b"verbose_json" in request.content:
            return httpx.Response(400, json={"error": "unsupported response_format"})
        return ok("Plain text only.")

    monkeypatch.setattr(stt, "download", fake_download(".webm"))
    text, _, cues = await stt.transcribe(build_services(handler, stt_api_key="k"), {"language": "en"}, "", None)
    assert (text, cues, len(bodies)) == ("Plain text only.", [], 2)
    assert b"response_format" not in bodies[1]
