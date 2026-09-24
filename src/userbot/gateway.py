from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from telethon import TelegramClient

from .config import Settings


class TelegramGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Any = TelegramClient(
            str(settings.session_path),
            settings.api_id,
            settings.api_hash,
            flood_sleep_threshold=settings.flood_sleep_threshold,
            device_model=settings.device_model,
            app_version=settings.app_version,
        )

    @property
    def session_file(self) -> Path:
        return Path(f"{self.settings.session_path}.session")

    def _secure_session_permissions(self) -> None:
        try:
            os.chmod(self.session_file, 0o600)
        except FileNotFoundError:
            return

    async def authenticate(self) -> Any:
        """Perform the first interactive login and persist the session."""
        await self.client.start(phone=self.settings.phone)
        self._secure_session_permissions()
        me = await self.client.get_me()
        return me

    async def connect(self) -> Any:
        """Connect without prompting; the session must already be authorized."""
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telegram session is not authorized; run `python -m userbot auth` first"
            )
        self._secure_session_permissions()
        return await self.client.get_me()

    async def disconnect(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()
        self._secure_session_permissions()
