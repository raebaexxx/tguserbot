from __future__ import annotations

import asyncio
import inspect
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from telethon import events

from userbot.commands import CommandContext
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext

#: Refuse anything larger; checked during the download, not only afterwards.
#: Overridable via ``[tiktok] max_file_mib`` in plugin-config.toml.
DEFAULT_MAX_FILE_MIB = 50

#: Hosts accepted. Overridable via ``[tiktok] allowed_domains``.
DEFAULT_ALLOWED_DOMAINS = ("tiktok.com", "tiktokv.com")

URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
DIRECT_COMMAND_PATTERN = re.compile(
    r"^/tt(?:@[^\s]+)?(?:\s+([\s\S]*))?$",
    re.IGNORECASE,
)
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}

#: Longest URL we will hand to yt-dlp.
MAX_URL_LENGTH = 2048

#: Minimum seconds between progress edits of the command message. Telegram
#: throttles repeated edits aggressively, and the account is what gets limited.
PROGRESS_MIN_INTERVAL = 3.0

#: How often the reporter wakes up to consider an edit.
PROGRESS_TICK = 0.5

#: yt-dlp retries/timeout budget for one download.
SOCKET_TIMEOUT = 20
RETRIES = 3

#: TikTok rejects requests that do not look like a browser. ``curl-cffi`` is a
#: hard dependency for exactly this; without impersonation the extractor is
#: routinely served a challenge page instead of the video.
IMPERSONATE_TARGET = "chrome"

USER_AGENT = "Mozilla/5.0 (compatible; tguserbot/0.1; +https://github.com/raebaexxx/tguserbot)"


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
    """Route yt-dlp chatter into the plugin logger instead of stdout."""

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


def _is_tiktok_host(hostname: str | None, allowed: tuple[str, ...]) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in allowed)


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


def extract_tiktok_url(
    text: str,
    allowed_domains: tuple[str, ...] = DEFAULT_ALLOWED_DOMAINS,
) -> str:
    matches = URL_PATTERN.findall(text)
    if len(matches) != 1:
        raise TikTokDownloadError("Нужна ровно одна ссылка TikTok.")
    url = matches[0].rstrip(".,!?)]}")
    if len(url) > MAX_URL_LENGTH:
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
    if not _is_tiktok_host(parsed.hostname, allowed_domains):
        raise TikTokDownloadError("Поддерживаются только ссылки tiktok.com.")
    return url


def _impersonate_target() -> Any:
    """Resolve the impersonation target, or ``None`` when it is unavailable.

    yt-dlp accepts an ``ImpersonateTarget`` here, not the string form: the
    string-to-enum conversion lives in its CLI entry point, which this plugin
    does not use. Passing a plain string reaches ``is_supported_target`` and
    raises ``AssertionError`` from inside ``YoutubeDL.__init__`` -- so every
    download would fail, not just impersonation.
    """
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except ImportError:
        return None
    try:
        return ImpersonateTarget.from_str(IMPERSONATE_TARGET)
    except Exception:
        return None


class Plugin(BasePlugin):
    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.download_lock: asyncio.Lock | None = None
        self.progress_lock: asyncio.Lock | None = None
        self.max_file_size: int = DEFAULT_MAX_FILE_MIB * 1024 * 1024
        self.allowed_domains: tuple[str, ...] = DEFAULT_ALLOWED_DOMAINS
        self._temporary_dir: Path = Path(".")

    async def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.download_lock = asyncio.Lock()
        self.progress_lock = asyncio.Lock()
        self.max_file_size = (
            ctx.config.int_value("max_file_mib", DEFAULT_MAX_FILE_MIB) * 1024 * 1024
        )
        self.allowed_domains = tuple(
            domain.lower()
            for domain in ctx.config.str_list("allowed_domains", DEFAULT_ALLOWED_DOMAINS)
        )
        if not self.allowed_domains:
            raise ValueError("tiktok: allowed_domains must not be empty")
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

        try:
            url = extract_tiktok_url(text, self.allowed_domains)
        except TikTokDownloadError as exc:
            await self._show_error(event, str(exc))
            return

        await self._set_status(event, "⏳ Скачивание TikTok: 0%")
        temporary_dir: Path | None = None
        reporter: asyncio.Task[None] | None = None
        sent = False
        try:
            temporary_dir = Path(
                await asyncio.to_thread(tempfile.mkdtemp, prefix="tguserbot-tiktok-")
            )
            loop = asyncio.get_running_loop()
            state = _ProgressState()
            progress = asyncio.Event()
            reporter = asyncio.create_task(
                self._report_progress(event, state, progress),
                name="tiktok-progress",
            )
            async with self.download_lock:
                try:
                    video_path = await asyncio.to_thread(
                        self._download_sync,
                        url,
                        temporary_dir,
                        self._make_progress_hook(state, loop, progress),
                    )
                except TikTokFileTooLarge:
                    raise
                except Exception as exc:
                    raise TikTokDownloadError(str(exc)) from exc
            # Stop the reporter *before* uploading: leaving it running made it
            # overwrite the final status and edit the message ~2x/second for the
            # whole upload, which is exactly how Telegram edit throttling starts.
            progress.set()
            await self._await_reporter(reporter)
            reporter = None

            if video_path.stat().st_size > self.max_file_size:
                raise TikTokFileTooLarge

            await self._set_status(event, "✅ Скачано. Отправляю видео…")
            async with self.ctx.rate_limiter.slot("tiktok-upload"):
                await event.respond(file=str(video_path))
            sent = True
        except TikTokFileTooLarge:
            await self._show_error(event, "Видео слишком большое для этого плагина (лимит 50 МБ).")
        except TikTokDownloadError as exc:
            if self.ctx is not None:
                # The message, not just the class name. "TikTok download failed:
                # TikTokDownloadError" says that a download failed and nothing
                # about why, which made the live failure undiagnosable from the
                # server -- the same mistake the ai plugin had just been fixed for.
                self.ctx.logger.warning("TikTok download failed: %s", exc)
            await self._show_error(
                event,
                "❌ Не удалось скачать TikTok. Проверьте ссылку или попробуйте позже.",
            )
        except Exception:
            if self.ctx is not None:
                self.ctx.logger.exception("TikTok download failed")
            await self._show_error(
                event,
                "❌ Не удалось скачать TikTok. Проверьте ссылку или попробуйте позже.",
            )
        finally:
            if reporter is not None:
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
            if temporary_dir is not None:
                await asyncio.to_thread(_remove_tree, temporary_dir)
            if sent:
                await self._delete_command(event)

    async def _await_reporter(self, reporter: asyncio.Task[None]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(reporter), timeout=PROGRESS_TICK * 4)
        except (TimeoutError, asyncio.CancelledError):
            reporter.cancel()
        finally:
            await asyncio.gather(reporter, return_exceptions=True)

    def _make_progress_hook(
        self,
        state: _ProgressState,
        loop: asyncio.AbstractEventLoop,
        stop: asyncio.Event,
    ) -> Any:
        def progress_hook(data: dict[str, Any]) -> None:
            if stop.is_set():
                return
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
                    # Abort while the file is still growing: a post-download size
                    # check only runs after the disk is already full.
                    if downloaded > self.max_file_size:
                        stop.set()
                        raise TikTokFileTooLarge
                    values = (percent, downloaded, total, speed, eta)
                elif status == "finished":
                    values = (100, state.downloaded_bytes, state.total_bytes, None, None)
                else:
                    return
            except (TypeError, ValueError):
                return
            try:
                loop.call_soon_threadsafe(state.update, *values)
            except RuntimeError:
                return

        return progress_hook

    def build_options(self, progress_hook: Any) -> dict[str, Any]:
        """Build the yt-dlp option mapping.

        Split out from :meth:`_download_sync` so the mapping can be validated in
        tests without touching the network. yt-dlp silently accepts unknown keys
        and stores them in ``params``, so a typo is invisible until a download
        misbehaves; ``tests/test_tiktok.py`` therefore checks the keys against
        yt-dlp's real option set and constructs a real ``YoutubeDL`` with them.
        """
        # Imported here so a broken yt-dlp install cannot stop the whole userbot
        # from booting. Note that yt-dlp's own plugin scanner is never invoked on
        # the library path (load_all_plugins() is CLI-only), so no
        # YTDLP_NO_PLUGINS / plugin_dirs juggling is needed or wanted -- setting
        # process-wide env from a worker thread was a side effect on every other
        # plugin.
        from yt_dlp import YoutubeDL  # noqa: F401  (validated by the caller)

        options: dict[str, Any] = {
            "outtmpl": str(self._temporary_dir / "video.%(ext)s"),
            "format": "best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            # No "max_downloads": 1. It looked like a safety limit, but yt-dlp
            # raises MaxDownloadsReached once the count is hit -- and TikTok
            # resolves through the playlist machinery, so that happened after a
            # *single* video and the plugin reported a failure with the file
            # already on disk. nplaylist is what actually prevents a playlist.
            "max_filesize": self.max_file_size,
            "retries": RETRIES,
            "fragment_retries": RETRIES,
            "socket_timeout": SOCKET_TIMEOUT,
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "nocheckcertificate": False,
            "cachedir": False,
            "restrictfilenames": True,
            "continuedl": False,
            "overwrites": True,
            "progress_hooks": [progress_hook],
            "http_headers": {"User-Agent": USER_AGENT},
            "logger": _YDLLogger(
                self.ctx.logger if self.ctx else logging.getLogger("userbot.plugin.tiktok")
            ),
        }
        target = _impersonate_target()
        if target is not None:
            options["impersonate"] = target
        elif self.ctx is not None:
            self.ctx.logger.warning(
                "yt-dlp impersonation is unavailable; TikTok downloads may fail. Install curl-cffi."
            )
        return options

    def _download_sync(
        self,
        url: str,
        temporary_dir: Path,
        progress_hook: Any,
    ) -> Path:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import MaxDownloadsReached

        self._temporary_dir = temporary_dir
        options = self.build_options(progress_hook)
        # yt-dlp types its options as a private TypedDict, so a plain dict of
        # runtime values cannot be passed without a cast.
        with YoutubeDL(cast(Any, options)) as downloader:
            try:
                info = downloader.extract_info(url, download=True)
            except MaxDownloadsReached:
                # yt-dlp's way of saying "I have what you asked for, stop". It
                # is control flow, not a failure: the file it wanted is already
                # written. The scan below decides whether that is true, so a
                # genuine failure with no file still reports as one.
                info = None
        if isinstance(info, dict):
            if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
                raise TikTokDownloadError("Playlist links are not supported.")
        elif info is not None:
            raise TikTokDownloadError("TikTok did not return video metadata.")
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
        """Edit the command message, but never faster than PROGRESS_MIN_INTERVAL.

        The throttle used to be skipped once the percentage hit 100, so a
        finished download spammed ~2 edits/second for as long as the upload took.
        """
        loop = asyncio.get_running_loop()
        last_edit = 0.0
        last_text = ""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=PROGRESS_TICK)
                return
            except TimeoutError:
                pass
            now = loop.time()
            if now - last_edit < PROGRESS_MIN_INTERVAL:
                continue
            text = _format_progress(state)
            if text == last_text:
                continue
            if not await self._set_status(event, text):
                # Editing is failing (deleted message, flood wait); stop trying
                # rather than hammering the API for the rest of the download.
                return
            last_edit = now
            last_text = text

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
