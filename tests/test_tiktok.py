from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

from userbot.loader import cleanup_loaded_plugin, load_plugin
from userbot.rate_limit import RateLimiter


class FakeContext:
    def __init__(self) -> None:
        self.logger = logging.getLogger("test.tiktok")
        self.rate_limiter = RateLimiter(min_interval=0)
        self.client = SimpleNamespace()
        self.commands: list[str] = []
        self.handlers: list[object] = []

    def register_command(self, name: str, callback, **kwargs) -> None:
        self.commands.append(name)

    def register_handler(self, callback, event) -> None:
        self.handlers.append((callback, event))

    def is_owner(self, sender_id: int | None) -> bool:
        return sender_id == 1


class FakeEvent:
    def __init__(self, text: str, sender_id: int = 1) -> None:
        self.raw_text = text
        self.sender_id = sender_id
        self.responses: list[str] = []
        self.files: list[str] = []
        self.edited: list[str] = []
        self.deleted = False

    async def respond(self, text: str | None = None, *, file: str | None = None, **kwargs) -> None:
        if file is not None:
            self.files.append(file)
        if text is not None:
            self.responses.append(text)

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edited.append(text)

    async def delete(self) -> None:
        self.deleted = True


def loaded_tiktok():
    loaded = load_plugin(Path("plugins") / "tiktok", "tiktok", 100)
    module = sys.modules[f"{loaded.module.__name__}.plugin"]
    return loaded, module


def test_tiktok_owner_downloads_and_deletes_command() -> None:
    async def scenario() -> None:
        loaded, _ = loaded_tiktok()
        plugin = loaded.instance
        context = FakeContext()
        plugin.ctx = context
        plugin.download_lock = asyncio.Lock()
        plugin.progress_lock = asyncio.Lock()

        def fake_download(url: str, temporary_dir: Path, progress_hook) -> Path:
            assert url == "https://vm.tiktok.com/example/"
            progress_hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 512,
                    "total_bytes": 1024,
                    "speed": 256,
                    "eta": 1,
                }
            )
            progress_hook({"status": "finished"})
            path = temporary_dir / "video.mp4"
            path.write_bytes(b"video")
            return path

        plugin._download_sync = fake_download
        event = FakeEvent("/ub tt https://vm.tiktok.com/example/")
        await plugin.handle_command(
            SimpleNamespace(event=event, args=event.raw_text.split(" ", 2)[2])
        )

        assert event.files
        assert event.deleted
        assert not await asyncio.to_thread(Path(event.files[0]).exists)
        assert not event.responses
        assert any("0%" in text for text in event.edited)
        assert any("Отправляю видео" in text for text in event.edited)
        cleanup_loaded_plugin(loaded)

    asyncio.run(scenario())


def test_tiktok_rejects_non_tiktok_url_and_leaves_error_status() -> None:
    async def scenario() -> None:
        loaded, _ = loaded_tiktok()
        plugin = loaded.instance
        plugin.ctx = FakeContext()
        plugin.download_lock = asyncio.Lock()
        plugin.progress_lock = asyncio.Lock()
        event = FakeEvent("/ub tt https://example.com/video")
        await plugin.handle_command(SimpleNamespace(event=event, args="https://example.com/video"))

        assert event.edited
        assert "tiktok.com" in event.edited[-1]
        assert not event.deleted
        cleanup_loaded_plugin(loaded)

    asyncio.run(scenario())


def test_tiktok_ignores_non_owner_direct_command() -> None:
    async def scenario() -> None:
        loaded, _ = loaded_tiktok()
        plugin = loaded.instance
        plugin.ctx = FakeContext()
        plugin.download_lock = asyncio.Lock()
        plugin.progress_lock = asyncio.Lock()
        event = FakeEvent("/tt https://vm.tiktok.com/example/", sender_id=2)
        await plugin.handle_direct_command(event)
        assert not event.deleted
        assert not event.responses
        cleanup_loaded_plugin(loaded)

    asyncio.run(scenario())
