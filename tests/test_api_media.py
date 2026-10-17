"""Tests for the media module: classification, budgets, and ffmpeg work.

The ffmpeg branch runs real ffmpeg on a generated two-second video (skipped
when ffmpeg is absent), and the ``_run``-mocked tests pin the degenerate paths:
a file with no audio stream, and six samples of a static scene collapsing into
one frame instead of six identical vision requests.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import shipped_submodule


@pytest.fixture(scope="module")
def media() -> Any:
    loaded, module = shipped_submodule("api", "_media")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


def message(**fields: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "voice": None,
        "video": None,
        "video_note": None,
        "photo": None,
        "audio": None,
        "document": None,
        "file": SimpleNamespace(size=None, mime_type=None),
    }
    defaults.update(fields)
    if "file" in fields and isinstance(fields["file"], dict):
        defaults["file"] = SimpleNamespace(**fields["file"])
    return SimpleNamespace(**defaults)


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"voice": object()}, "voice"),
        ({"video_note": object()}, "video_note"),
        ({"video": object()}, "video"),
        ({"photo": object()}, "photo"),
        ({"audio": object()}, "audio"),
        ({"document": object(), "file": {"mime_type": "audio/mpeg"}}, "audio"),
        ({"document": object(), "file": {"mime_type": "video/mp4"}}, "video"),
        ({"document": object(), "file": {"mime_type": "image/webp"}}, "photo"),
        ({"document": object(), "file": {"mime_type": "text/plain"}}, None),
        ({}, None),
    ],
)
def test_media_kind_classifies_messages(media: Any, fields: dict[str, Any], expected: Any) -> None:
    assert media.media_kind(message(**fields)) == expected


def test_file_size_and_mime_read_the_file_info(media: Any) -> None:
    item = message(file={"size": 1234, "mime_type": "audio/ogg"})
    assert media.file_size(item) == 1234
    assert media.file_mime(item) == "audio/ogg"
    assert media.file_size(message(file={"size": "broken"})) is None


# --- download ---------------------------------------------------------------


class BufferClient:
    async def download_media(self, _message: Any, file: Any) -> Any:
        file.write(self.payload)
        return file

    def __init__(self, payload: bytes) -> None:
        self.payload = payload


async def test_download_returns_the_bytes(media: Any) -> None:
    data = await media.download(BufferClient(b"abc"), message(), max_bytes=10)
    assert data == b"abc"


async def test_a_declared_oversize_file_is_refused_before_download(media: Any) -> None:
    item = message(file={"size": 100})
    with pytest.raises(media.MediaTooLarge):
        await media.download(BufferClient(b""), item, max_bytes=10)


async def test_an_actual_oversize_file_is_refused_after_download(media: Any) -> None:
    with pytest.raises(media.MediaTooLarge):
        await media.download(BufferClient(b"0123456789"), message(), max_bytes=5)


async def test_an_empty_download_is_an_error(media: Any) -> None:
    with pytest.raises(media.MediaError):
        await media.download(BufferClient(b""), message(), max_bytes=10)


# --- ffmpeg, mocked ---------------------------------------------------------


def test_duration_seconds_parses_ffprobe(media: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        media, "_run", lambda argv: subprocess.CompletedProcess(argv, 0, b"12.5\n", b"")
    )
    assert media.duration_seconds(Path("x.mp4")) == 12.5


def test_duration_seconds_is_zero_when_ffprobe_fails(
    media: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        media, "_run", lambda argv: subprocess.CompletedProcess(argv, 1, b"", b"nope")
    )
    assert media.duration_seconds(Path("x.mp4")) == 0.0


def test_extract_audio_returns_none_when_there_is_no_stream(
    media: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        media, "_run", lambda argv: subprocess.CompletedProcess(argv, 1, b"", b"no audio")
    )
    assert media.extract_audio(tmp_path / "video.mp4", tmp_path) is None


def test_extract_frames_dedupes_identical_samples(
    media: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        if "-show_entries" in argv:
            return subprocess.CompletedProcess(argv, 0, b"10.0\n", b"")
        Path(argv[-1]).write_bytes(b"same-pixels")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(media, "_run", fake_run)
    frames = media.extract_frames(Path("v.mp4"), tmp_path, count=6, width=64)
    # six samples, one distinct picture: one frame, and the moment it came from.
    assert len(frames) == 1
    assert frames[0][0] == pytest.approx(10.0 * 1 / 7)


def test_extract_frames_keeps_distinct_samples(
    media: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = {"n": 0}

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        if "-show_entries" in argv:
            return subprocess.CompletedProcess(argv, 0, b"9.0\n", b"")
        counter["n"] += 1
        Path(argv[-1]).write_bytes(f"pixels-{counter['n']}".encode())
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(media, "_run", fake_run)
    frames = media.extract_frames(Path("v.mp4"), tmp_path, count=3, width=64)
    assert [round(moment, 2) for moment, _ in frames] == [2.25, 4.5, 6.75]


def test_extract_frames_needs_a_duration(
    media: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media, "_run", lambda argv: subprocess.CompletedProcess(argv, 0, b"", b""))
    assert media.extract_frames(Path("v.mp4"), tmp_path, count=3, width=64) == []


# --- ffmpeg, for real -------------------------------------------------------

FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg and ffprobe are not installed")
def test_a_generated_video_round_trips_through_ffmpeg(media: Any, tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=160x120:rate=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        capture_output=True,
        check=True,
    )
    assert media.has_ffmpeg()
    assert media.duration_seconds(video) == pytest.approx(2.0, abs=0.5)

    audio = media.extract_audio(video, tmp_path)
    assert audio is not None and audio.stat().st_size > 0

    frames = media.extract_frames(video, tmp_path, count=3, width=64)
    assert len(frames) == 3  # the test pattern changes every frame
    assert all(data[:2] == b"\xff\xd8" for _moment, data in frames)  # JPEG magic
