from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .app import UserbotApp
from .config import Settings
from .gateway import TelegramGateway
from .logging import setup_logging


async def _authenticate(settings: Settings) -> None:
    setup_logging(settings.log_dir, settings.log_level)
    settings.validate()
    gateway = TelegramGateway(settings)
    try:
        me = await gateway.authenticate()
        identifier = getattr(me, "username", None) or getattr(me, "id", "unknown")
        print(f"Авторизация успешна: {identifier}")
    finally:
        await gateway.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(prog="userbot")
    parser.add_argument("command", nargs="?", choices=("run", "auth"), default="run")
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args()
    settings = Settings.from_env(args.root)
    if args.command == "auth":
        try:
            asyncio.run(_authenticate(settings))
        except Exception as exc:
            print(f"Ошибка авторизации: {exc}")
            return 1
        return 0
    app: UserbotApp | None = None
    try:
        app = UserbotApp(settings)
        asyncio.run(app.run())
    except KeyboardInterrupt:
        if app is not None:
            app.request_stop()
    except Exception as exc:
        if app is not None:
            app.logger.exception("Fatal userbot error")
        print(f"Ошибка запуска: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
