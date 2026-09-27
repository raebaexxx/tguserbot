from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import time
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import AuthKeyUnregisteredError, SessionPasswordNeededError

from .config import Settings

logger = logging.getLogger("userbot.gateway")

#: Sidecar files Telethon's SQLite session can create next to the main file.
SESSION_SIDECARS = ("-journal", "-wal", "-shm")

#: How long to wait for the single-instance lock.
LOCK_TIMEOUT = 5.0


class GatewayError(RuntimeError):
    pass


class SessionLockedError(GatewayError):
    """Another process already holds this Telegram session."""


class SessionRevokedError(GatewayError):
    """The session was invalidated server-side and needs a fresh ``auth``."""


def _try_lock(lock_path: Path, timeout: float) -> tuple[Any, SessionLockedError | None]:
    """Blocking ``flock`` with a bounded retry; runs in a worker thread."""
    handle = open(lock_path, "a+b")  # noqa: SIM115 - ownership passes to the caller
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                return handle, SessionLockedError(
                    f"Another userbot process already uses {lock_path.name[: -len('.lock')]}."
                    " Stop it first; Telegram sessions are single-writer."
                )
            time.sleep(0.2)
            continue
        return handle, None


class TelegramGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Any = None
        self._lock_file: Any = None
        self._connection_state_hooks: list[Any] = []
        self.client = self._build_client()

    def _build_client(self) -> Any:
        # Telethon opens the SQLite session inside the constructor, so the data
        # directory has to exist first. Without this a fresh local checkout
        # failed with a bare "unable to open database file".
        try:
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GatewayError(
                f"Cannot create the data directory {self.settings.data_dir}: {exc}"
            ) from exc
        client = TelegramClient(
            str(self.settings.session_path),
            self.settings.api_id,
            self.settings.api_hash,
            # A high threshold turns a multi-hour FloodWait into a loop-wide
            # sleep that freezes every plugin. Keep it bounded and let the
            # dispatcher decide what to do with the error instead.
            flood_sleep_threshold=self.settings.flood_sleep_threshold,
            device_model=self.settings.device_model,
            app_version=self.settings.app_version,
        )
        return client

    # -- session file -------------------------------------------------------

    @property
    def session_file(self) -> Path:
        return Path(f"{self.settings.session_path}.session")

    def _secure_session_permissions(self) -> None:
        targets = [self.session_file]
        targets.extend(Path(f"{self.session_file}{suffix}") for suffix in SESSION_SIDECARS)
        for path in targets:
            with contextlib.suppress(FileNotFoundError, PermissionError, OSError):
                os.chmod(path, 0o600)

    async def acquire_session_lock(self) -> None:
        """Refuse to run two clients against the same session file.

        Telegram sessions are single-writer: a second process corrupts the
        auth key. This used to be a README warning; now it is enforced.
        """
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(f"{self.session_file}.lock")
        handle, error = await asyncio.to_thread(_try_lock, lock_path, LOCK_TIMEOUT)
        if error is not None:
            raise error
        self._lock_file = handle
        self._secure_session_permissions()
        logger.debug("acquired the session lock at %s", lock_path)

    def release_session_lock(self) -> None:
        handle, self._lock_file = self._lock_file, None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            handle.close()

    # -- connection state ---------------------------------------------------

    def bind_health(self, health: Any) -> None:
        connected = bool(self.client.is_connected())
        health.set_telegram_state(connected=connected, authorized=connected)

    def on_connection_state(self, hook: Any) -> None:
        self._connection_state_hooks.append(hook)

    async def monitor_connection(self, interval: float = 5.0) -> None:
        """Report connection transitions.

        Telethon exposes no connect/disconnect event, so this polls the public
        ``is_connected()``. Without it ``/ub status`` kept reporting
        "подключён" after the network dropped, because the flag was only ever
        written at startup.
        """
        previous: bool | None = None
        while True:
            connected = bool(self.client.is_connected())
            if connected != previous:
                previous = connected
                if connected:
                    logger.info("Telegram connection established")
                else:
                    logger.warning("Telegram connection lost")
                for hook in self._connection_state_hooks:
                    with contextlib.suppress(Exception):
                        hook(connected=connected)
            await asyncio.sleep(interval)

    # -- authentication -----------------------------------------------------

    async def authenticate(self) -> Any:
        """Perform the first interactive login and persist the session."""
        await self.acquire_session_lock()
        succeeded = False
        try:
            try:
                await self.client.start(phone=self.settings.phone)
            except SessionPasswordNeededError:
                # ``client.start`` already asked for the password when stdin is
                # a TTY; this is the non-interactive fallback.
                await self.client.sign_in(password=await self._ask_password())
            self._secure_session_permissions()
            me = await self.client.get_me()
            succeeded = True
            return me
        finally:
            if not succeeded:
                self.release_session_lock()

    async def _ask_password(self) -> str:
        import getpass

        return getpass.getpass("Двухэтапная проверка. Пароль от Telegram: ")

    async def connect(self) -> Any:
        """Connect without prompting; the session must already be authorized."""
        await self.acquire_session_lock()
        succeeded = False
        try:
            # ``catch_up=True`` so commands sent while the process was down are
            # still delivered; the default silently dropped them.
            await self.client.connect(catch_up=True)
            if not await self.client.is_user_authorized():
                raise GatewayError(
                    "Telegram session is not authorized; run `python -m userbot auth` first"
                )
            self._secure_session_permissions()
            me = await self.client.get_me()
            succeeded = True
            return me
        except AuthKeyUnregisteredError as exc:
            raise SessionRevokedError(
                "The Telegram session was invalidated server-side. "
                "Delete the .session file and run `python -m userbot auth` again."
            ) from exc
        finally:
            # Every failure path must hand the lock back, otherwise a stale
            # lock file blocks the next start until the process is killed.
            if not succeeded:
                self.release_session_lock()

    async def disconnect(self) -> None:
        if self.client.is_connected():
            with contextlib.suppress(Exception):
                await self.client.disconnect()
        self.release_session_lock()
        self._secure_session_permissions()

    async def wait_until_disconnected(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.disconnect()
