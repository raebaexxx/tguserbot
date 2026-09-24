from __future__ import annotations

import asyncio
import inspect
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from telethon import events
from yt_dlp import YoutubeDL

from userbot.commands import CommandContext
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext

MAX_FILE_SIZE = 50 * 1024 * 1024
ALLOWED_DOMAINS = ("tiktok.com", "tiktokv.com")
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
DIRECT_COMMAND_PATTERN = re.compile(
    r"^/tiktok(?:@[^\s]+)?(?:\s+([\s\S]*))?$",
    re.IGNORECASE,
)
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}


class TikTokDownloadError(RuntimeError):
    pass


class TikTokFileTooLarge(TikTokDownloadError):
    pass


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

    async def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.download_lock = asyncio.Lock()
        ctx.register_command(
            "tiktok",
            self.handle_command,
            help_text="Скачать публичное TikTok-видео и удалить команду",
        )
        ctx.register_handler(
            self.handle_direct_command,
            events.NewMessage(pattern=r"(?i)^/tiktok(?:@[^\s]+)?(?:\s+|$)"),
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
        if self.ctx is None or self.download_lock is None:
            return
        if not self.ctx.is_owner(getattr(event, "sender_id", None)):
            return

        temporary_dir: Path | None = None
        try:
            try:
                url = extract_tiktok_url(text)
            except TikTokDownloadError as exc:
                await self._respond(event, str(exc))
                return

            temporary_dir = Path(
                await asyncio.to_thread(
                    tempfile.mkdtemp,
                    prefix="tguserbot-tiktok-",
                )
            )
            async with self.download_lock:
                video_path = await asyncio.to_thread(self._download_sync, url, temporary_dir)
            if video_path.stat().st_size > MAX_FILE_SIZE:
                raise TikTokFileTooLarge

            async with self.ctx.rate_limiter.slot("tiktok-upload"):
                await event.respond(file=str(video_path))
        except TikTokFileTooLarge:
            await self._respond(event, "Видео слишком большое для этого плагина (лимит 50 МБ).")
        except Exception as exc:
            self.ctx.logger.warning("TikTok download failed: %s", type(exc).__name__)
            await self._respond(
                event,
                "Не удалось скачать TikTok. Проверьте ссылку или попробуйте позже.",
            )
        finally:
            if temporary_dir is not None:
                await asyncio.to_thread(_remove_tree, temporary_dir)
            await self._delete_command(event)

    def _download_sync(self, url: str, temporary_dir: Path) -> Path:
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
