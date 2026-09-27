"""Tests for a TikTok download that yt-dlp signals with an exception.

The live failure: ``/ub tt <link>`` reported "Не удалось скачать TikTok" while
yt-dlp had in fact written the file. ``MaxDownloadsReached`` is yt-dlp's normal
way of saying "I have what you asked for, stop" -- it is control flow, not an
error -- and the plugin treated every exception from ``extract_info`` as a
failure.

It was triggered by ``"max_downloads": 1`` in the plugin's own options. TikTok
resolves through the playlist machinery, so after the first entry yt-dlp hit its
own limit and raised. The option was also redundant: ``nplaylist: True`` is what
actually prevents playlist downloads.

The production log could not help, because the plugin logged
``TikTokDownloadError`` with the class name and no message -- the same mistake
the ai plugin had just been fixed for.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeEvent, shipped_module


@pytest.fixture(scope="module")
def tiktok_module() -> Any:
    loaded, module = shipped_module("tiktok")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


def make_plugin(module: Any) -> Any:
    import asyncio

    plugin = module.Plugin()
    plugin.ctx = None
    plugin.download_lock = asyncio.Lock()
    plugin.progress_lock = asyncio.Lock()
    return plugin


def ydl_that_raises(module: Any, exception: BaseException) -> Any:
    """A YoutubeDL stand-in that raises the way the real one did."""

    class FakeYDL:
        def __init__(self, options: dict[str, Any]) -> None:
            pass

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            raise exception

    return FakeYDL


# --- the option that caused it ---------------------------------------------


def test_max_downloads_is_not_set(tiktok_module: Any) -> None:
    """Regression: "max_downloads": 1 made yt-dlp raise on a single video.

    Redundant with nplaylist -- which is what actually stops a playlist -- and
    actively harmful, because TikTok resolves through the playlist machinery.
    """

    plugin = make_plugin(tiktok_module)
    options = plugin.build_options(lambda _d: None)
    assert "max_downloads" not in options, (
        "max_downloads makes yt-dlp raise MaxDownloadsReached after one video"
    )
    # The option that does the job is still there.
    assert options["noplaylist"] is True


def test_no_yt_dlp_option_can_stop_early_on_its_own(tiktok_module: Any) -> None:
    """Any option that makes yt-dlp stop is a hard failure here."""
    plugin = make_plugin(tiktok_module)
    options = plugin.build_options(lambda _d: None)
    for key in ("max_downloads", "playlistend", "playlist_items", "break_on_existing"):
        assert key not in options, f"{key} can make yt-dlp stop before finishing"


# --- MaxDownloadsReached is not a failure ----------------------------------


def test_a_reached_download_limit_still_returns_the_file(
    tiktok_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live failure, reproduced.

    yt-dlp wrote video.mp4 and then raised MaxDownloadsReached to say it was
    done. The plugin reported a download failure and the user got an error
    instead of the video.
    """
    import yt_dlp
    from yt_dlp.utils import MaxDownloadsReached

    plugin = make_plugin(tiktok_module)
    monkeypatch.setattr(
        yt_dlp, "YoutubeDL", ydl_that_raises(tiktok_module, MaxDownloadsReached("limit"))
    )
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        (target / "video.mp4").write_bytes(b"data" * 64)
        result = plugin._download_sync("https://vt.tiktok.com/x/", target, lambda _d: None)
        assert result == target / "video.mp4", (
            f"the file was there but the download was reported as failed: {result!r}"
        )


def test_a_reached_limit_with_no_file_still_fails(
    tiktok_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Treating the exception as success must not invent a result."""
    import yt_dlp
    from yt_dlp.utils import MaxDownloadsReached

    plugin = make_plugin(tiktok_module)
    monkeypatch.setattr(
        yt_dlp, "YoutubeDL", ydl_that_raises(tiktok_module, MaxDownloadsReached("limit"))
    )
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        with pytest.raises(tiktok_module.TikTokDownloadError):
            plugin._download_sync("https://vt.tiktok.com/x/", target, lambda _d: None)


def test_a_reached_limit_with_only_a_part_file_still_fails(
    tiktok_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .part file is not a video, and must not be shipped as one."""
    import yt_dlp
    from yt_dlp.utils import MaxDownloadsReached

    plugin = make_plugin(tiktok_module)
    monkeypatch.setattr(
        yt_dlp, "YoutubeDL", ydl_that_raises(tiktok_module, MaxDownloadsReached("limit"))
    )
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        (target / "video.mp4.part").write_bytes(b"x" * 32)
        with pytest.raises(tiktok_module.TikTokDownloadError):
            plugin._download_sync("https://vt.tiktok.com/x/", target, lambda _d: None)


def test_a_real_error_is_still_an_error(
    tiktok_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the control-flow exception is forgiven."""
    import yt_dlp

    plugin = make_plugin(tiktok_module)
    monkeypatch.setattr(yt_dlp, "YoutubeDL", ydl_that_raises(tiktok_module, RuntimeError("boom")))
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        (target / "video.mp4").write_bytes(b"data")
        with pytest.raises(RuntimeError, match="boom"):
            plugin._download_sync("https://vt.tiktok.com/x/", target, lambda _d: None)


# --- the log has to say what went wrong ------------------------------------


async def test_a_download_failure_logs_the_reason(tiktok_module: Any) -> None:
    """The production log read "TikTok download failed: TikTokDownloadError".

    The class name, with no message. It said a download had failed and nothing
    about why, which is the same mistake the ai plugin was just fixed for.
    """
    import logging

    plugin = make_plugin(tiktok_module)
    recorded: list[logging.LogRecord] = []

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            recorded.append(record)

    logger = logging.getLogger("test.tiktok.failure")
    logger.addHandler(Handler())
    logger.setLevel(logging.WARNING)

    class Ctx:
        def __init__(self) -> None:
            self.logger = logger

        @staticmethod
        def is_owner(sender_id: Any) -> bool:
            return sender_id == 1

    plugin.ctx = Ctx()

    def failing_download(*args: Any, **kwargs: Any) -> Any:
        raise tiktok_module.TikTokDownloadError("TikTok недоступен")

    plugin._download_sync = failing_download
    event = FakeEvent("/ub tt https://vt.tiktok.com/x", sender_id=1)

    from types import SimpleNamespace

    await plugin.handle_command(SimpleNamespace(event=event, args="https://vt.tiktok.com/x"))

    failures = [record for record in recorded if "TikTok download failed" in record.getMessage()]
    assert failures, "a failed download must be logged"
    text = " ".join(record.getMessage() for record in failures)
    assert "недоступен" in text, f"the reason was not logged: {text!r}"
