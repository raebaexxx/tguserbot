"""Downloading and shrinking media before it leaves for a third-party API.

Two budgets are enforced before anything is written anywhere: the message's
stated size is checked against the configured cap, and the downloaded bytes are
checked again -- a server can lie about the size, and the cap exists because
the unit runs with ``PrivateTmp=true``, where /tmp is RAM, under
``MemoryMax=1G``.

ffmpeg does the video work: one audio track for the speech pass, and a handful
of evenly spread frames for the vision pass. Frames are duplicated-grain
deduplicated by hash, because six samples of a static scene are six identical
pictures and five wasted vision requests.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
from pathlib import Path
from typing import Any

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class MediaError(RuntimeError):
    """Media refused or undecodable, with a sentence for the user."""


class MediaTooLarge(MediaError):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"файл {size // (1024 * 1024)} МБ больше лимита {limit // (1024 * 1024)} МБ"
        )
        self.size = size
        self.limit = limit


def has_ffmpeg() -> bool:
    return shutil.which(FFMPEG) is not None and shutil.which(FFPROBE) is not None


def file_size(message: Any) -> int | None:
    info = getattr(message, "file", None)
    size = getattr(info, "size", None)
    try:
        return int(size) if size is not None else None
    except (TypeError, ValueError):
        return None


def file_mime(message: Any) -> str:
    info = getattr(message, "file", None)
    return str(getattr(info, "mime_type", None) or "")


def media_kind(message: Any) -> str | None:
    """What this message carries, in the words the plugin acts on."""
    if getattr(message, "voice", None) is not None:
        return "voice"
    if getattr(message, "video_note", None) is not None:
        return "video_note"
    if getattr(message, "video", None) is not None:
        return "video"
    if getattr(message, "photo", None) is not None:
        return "photo"
    if getattr(message, "audio", None) is not None:
        return "audio"
    if getattr(message, "document", None) is not None:
        mime = file_mime(message)
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("image/"):
            return "photo"
    return None


async def download(client: Any, message: Any, *, max_bytes: int) -> bytes:
    declared = file_size(message)
    if declared is not None and declared > max_bytes:
        raise MediaTooLarge(declared, max_bytes)
    buffer = io.BytesIO()
    result = await client.download_media(message, file=buffer)
    data = result.getvalue() if isinstance(result, io.BytesIO) else buffer.getvalue()
    if len(data) > max_bytes:
        raise MediaTooLarge(len(data), max_bytes)
    if not data:
        # The file is gone or too old for Telegram to serve it, and empty bytes
        # would go on to become an opaque ffmpeg failure three steps later.
        raise MediaError("Telegram не отдал файл (пустой ответ)")
    return data


def _run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, capture_output=True, check=False)


def duration_seconds(source: Path) -> float:
    result = _run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(source),
        ]
    )
    try:
        return float(result.stdout.decode("utf-8", "replace").strip())
    except ValueError:
        return 0.0


def extract_audio(source: Path, target_dir: Path) -> Path | None:
    """The speech track, mono 16 kHz, or ``None`` when there is no audio.

    A talking-head video and a silent timelapse differ only in this: the
    silent one still gets a summary, from its frames.
    """
    target = target_dir / "audio.mp3"
    result = _run(
        [
            FFMPEG,
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "32k",
            str(target),
        ]
    )
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        return None
    return target


def extract_frames(
    source: Path, target_dir: Path, *, count: int, width: int
) -> list[tuple[float, bytes]]:
    """Evenly spread frames as JPEG bytes, near-duplicates dropped.

    Timestamps are taken between samples (``i+1`` of ``count+1``), so the first
    frame is not the black leader and the last is not the logo card.
    """
    total = duration_seconds(source)
    if total <= 0:
        return []
    sample_count = max(1, min(count, 30))
    frames: list[tuple[float, bytes]] = []
    seen: set[bytes] = set()
    for index in range(sample_count):
        moment = total * (index + 1) / (sample_count + 1)
        target = target_dir / f"frame-{index:02d}.jpg"
        result = _run(
            [
                FFMPEG,
                "-y",
                "-ss",
                f"{moment:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale={max(32, width)}:-2",
                "-q:v",
                "5",
                str(target),
            ]
        )
        if result.returncode != 0 or not target.exists():
            continue
        data = target.read_bytes()
        digest = hashlib.sha256(data).digest()
        if digest in seen:
            continue
        seen.add(digest)
        frames.append((moment, data))
    return frames


__all__ = [
    "MediaError",
    "MediaTooLarge",
    "download",
    "duration_seconds",
    "extract_audio",
    "extract_frames",
    "file_mime",
    "file_size",
    "has_ffmpeg",
    "media_kind",
]
