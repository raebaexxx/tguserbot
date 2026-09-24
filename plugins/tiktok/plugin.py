from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from telethon import events

from userbot.commands import CommandContext
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext

MAX_FILE_SIZE = 50 * 1024 * 1024
ALLOWED_DOMAINS = ("tiktok.com", "tiktokv.com")
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
DIRECT_COMMAND_PATTERN = re.compile(
    r"^/tt(?:@[^\s]+)?(?:\s+([\s\S]*))?$",
    re.IGNORECASE,
)
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}


class TikTokDownloadError(RuntimeError):
    pass


class TikTokFileTooLarge(TikTokDownloadError):
    pass


@dataclass(slots=True)
class _ProgressState:
    percent: int = 0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed: float | None = None
    eta: str | None = None

    def update(
        self,
        percent: int,
        downloaded_bytes: int,
        total_bytes: int | None,
        speed: float | None,
        eta: str | None,
    ) -> None:
        self.percent = max(0, min(100, percent))
        self.downloaded_bytes = max(0, downloaded_bytes)
        self.total_bytes = total_bytes
        self.speed = speed
        self.eta = eta


class _YDLLogger:
    def __init__(self, logger: Any):
        self.logger = logger

    def debug(self, message: str) -> None:
        self.logger.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        self.logger.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        self.logger.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        self.logger.error("yt-dlp: %s", message)


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _is_tiktok_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    return any(
        normalized == domain or normalized.endswith(f".{domain}") for domain in ALLOWED_DOMAINS
    )


def _format_bytes(value: int | None) -> str:
    if not value:
        return "0 B"
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return "0 B"


def _format_progress(state: _ProgressState) -> str:
    lines = [f"⏳ Скачивание TikTok: {state.percent}%"]
    if state.total_bytes:
        lines.append(
            f"{_format_bytes(state.downloaded_bytes)} / {_format_bytes(state.total_bytes)}"
        )
    details: list[str] = []
    if state.speed is not None:
        details.append(f"{_format_bytes(int(state.speed))}/s")
    if state.eta and state.eta not in {"NA", "None"}:
        details.append(f"ETA {state.eta}")
    if details:
        lines.append(" • ".join(details))
    return "\n".join(lines)


def extract_tiktok_url(text: str) -> str:
    matches = URL_PATTERN.findall(text)
    if len(matches) != 1:
        raise TikTokDownloadError("Нужна ровно одна ссылка TikTok.")
    url = matches[0].rstrip(".,!?)]}")
    if len(url) > 2048:
        raise TikTokDownloadError("Ссылка слишком длинная.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise TikTokDownloadError("Некорректная ссылка.") from exc
    if parsed.scheme.lower() != "https":
        raise TikTokDownloadError("Поддерживаются только HTTPS-ссылки TikTok.")
    if parsed.username or parsed.password or port not in (None, 443):
        raise TikTokDownloadError("Некорректная ссылка TikTok.")
    if not _is_tiktok_host(parsed.hostname):
        raise TikTokDownloadError("Поддерживаются только ссылки tiktok.com.")
    return url


class Plugin(BasePlugin):
    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.download_lock: asyncio.Lock | None = None
        self.progress_lock: asyncio.Lock | None = None

    async def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.download_lock = asyncio.Lock()
        self.progress_lock = asyncio.Lock()
        ctx.register_command(
            "tt",
            self.handle_command,
            help_text="Скачать публичное TikTok-видео и удалить команду",
        )
        ctx.register_handler(
            self.handle_direct_command,
            events.NewMessage(pattern=r"(?i)^/tt(?:@[^\s]+)?(?:\s+|$)"),
        )

    async def handle_command(self, command: CommandContext) -> None:
        await self._process(command.event, command.args)

    async def handle_direct_command(self, event: Any) -> None:
        if self.ctx is None or not self.ctx.is_owner(getattr(event, "sender_id", None)):
            return
        raw_text = getattr(event, "raw_text", "") or ""
        match = DIRECT_COMMAND_PATTERN.match(raw_text)
        if match is None:
            return
        await self._process(event, match.group(1) or "")

    async def _process(self, event: Any, text: str) -> None:
        if self.ctx is None or self.download_lock is None or self.progress_lock is None:
            return
        if not self.ctx.is_owner(getattr(event, "sender_id", None)):
            return

        temporary_dir: Path | None = None
        progress_state = _ProgressState()
        progress_stop = asyncio.Event()
        progress_task: asyncio.Task[None] | None = None
        sent = False
        try:
            await self._set_status(event, "⏳ Скачивание TikTok: 0%")
            progress_task = asyncio.create_task(
                self._report_progress(event, progress_state, progress_stop),
                name="tiktok-progress",
            )
            try:
                url = extract_tiktok_url(text)
            except TikTokDownloadError as exc:
                await self._show_error(event, str(exc))
                return

            temporary_dir = Path(
                await asyncio.to_thread(
                    tempfile.mkdtemp,
                    prefix="tguserbot-tiktok-",
                )
            )
            loop = asyncio.get_running_loop()
            async with self.download_lock:
                video_path = await asyncio.to_thread(
                    self._download_sync,
                    url,
                    temporary_dir,
                    self._make_progress_hook(progress_state, loop),
                )
            if video_path.stat().st_size > MAX_FILE_SIZE:
                raise TikTokFileTooLarge

            await self._set_status(event, "✅ Скачано. Отправляю видео…")
            async with self.ctx.rate_limiter.slot("tiktok-upload"):
                await event.respond(file=str(video_path))
            sent = True
        except TikTokFileTooLarge:
            await self._show_error(event, "Видео слишком большое для этого плагина (лимит 50 МБ).")
        except Exception as exc:
            self.ctx.logger.warning("TikTok download failed: %s", type(exc).__name__)
            await self._show_error(
                event,
                "❌ Не удалось скачать TikTok. Проверьте ссылку или попробуйте позже.",
            )
        finally:
            progress_stop.set()
            if progress_task is not None:
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            if temporary_dir is not None:
                await asyncio.to_thread(_remove_tree, temporary_dir)
            if sent:
                await self._delete_command(event)

    def _make_progress_hook(
        self,
        state: _ProgressState,
        loop: asyncio.AbstractEventLoop,
    ) -> Any:
        def progress_hook(data: dict[str, Any]) -> None:
            try:
                status = data.get("status")
                if status == "downloading":
                    downloaded = int(data.get("downloaded_bytes") or 0)
                    total_value = data.get("total_bytes") or data.get("total_bytes_estimate")
                    total = int(total_value) if total_value else None
                    percent = int(downloaded * 100 / total) if total else 0
                    speed_value = data.get("speed")
                    speed = float(speed_value) if speed_value else None
                    eta_value = data.get("eta")
                    eta = str(eta_value) if eta_value not in (None, "NA") else None
                    values = (percent, downloaded, total, speed, eta)
                elif status == "finished":
                    values = (100, state.downloaded_bytes, state.total_bytes, None, None)
                else:
                    return
                try:
                    loop.call_soon_threadsafe(state.update, *values)
                except RuntimeError:
                    return
            except (TypeError, ValueError):
                return

        return progress_hook

    def _download_sync(
        self,
        url: str,
        temporary_dir: Path,
        progress_hook: Any,
    ) -> Path:
        os.environ.setdefault("YTDLP_NO_PLUGINS", "1")
        from yt_dlp import YoutubeDL

        options = {
            "outtmpl": str(temporary_dir / "video.%(ext)s"),
            "format": "best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "max_filesize": MAX_FILE_SIZE,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 20,
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "nocheckcertificate": False,
            "cachedir": False,
            "plugin_dirs": [],
            "restrictfilenames": True,
            "continuedl": False,
            "overwrites": True,
            "progress_hooks": [progress_hook],
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (compatible; tguserbot/0.1; +https://github.com/raebaexxx/tguserbot)",
            },
            "logger": _YDLLogger(
                self.ctx.logger if self.ctx else logging.getLogger("userbot.plugin.tiktok")
            ),
        }
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
        if not isinstance(info, dict):
            raise TikTokDownloadError("TikTok did not return video metadata.")
        if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
            raise TikTokDownloadError("Playlist links are not supported.")
        candidates = [
            path
            for path in temporary_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() in VIDEO_SUFFIXES
            and not path.name.endswith((".part", ".ytdl"))
        ]
        if not candidates:
            raise TikTokDownloadError("TikTok did not provide a video file.")
        return max(candidates, key=lambda path: path.stat().st_size)

    async def _report_progress(
        self,
        event: Any,
        state: _ProgressState,
        stop_event: asyncio.Event,
    ) -> None:
        last_edit = 0.0
        last_percent = -1
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            await asyncio.sleep(0.5)
            if stop_event.is_set():
                return
            now = loop.time()
            if state.percent == last_percent and state.percent < 100:
                continue
            if state.percent < 100 and now - last_edit < 2.0:
                continue
            await self._set_status(event, _format_progress(state))
            last_edit = now
            last_percent = state.percent

    async def _set_status(self, event: Any, text: str) -> bool:
        if self.progress_lock is None:
            return False
        async with self.progress_lock:
            edit = getattr(event, "edit_text", None)
            if not callable(edit):
                edit = getattr(event, "edit", None)
            if not callable(edit):
                return False
            try:
                result = edit(text)
                if inspect.isawaitable(result):
                    await result
                return True
            except Exception as exc:
                if self.ctx is not None:
                    self.ctx.logger.debug("TikTok status edit failed: %s", type(exc).__name__)
                return False

    async def _show_error(self, event: Any, text: str) -> None:
        if not await self._set_status(event, f"❌ {text}"):
            await self._respond(event, f"❌ {text}")

    async def _respond(self, event: Any, text: str) -> None:
        try:
            await event.respond(text)
        except Exception as exc:
            if self.ctx is not None:
                self.ctx.logger.warning("TikTok response failed: %s", type(exc).__name__)

    async def _delete_command(self, event: Any) -> None:
        delete = getattr(event, "delete", None)
        if not callable(delete):
            return
        try:
            result = delete()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            if self.ctx is not None:
                self.ctx.logger.warning("Could not delete TikTok command: %s", type(exc).__name__)
