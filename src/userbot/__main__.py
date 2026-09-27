from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

from .app import UserbotApp
from .config import Settings
from .gateway import SessionLockedError, SessionRevokedError, TelegramGateway
from .logging import setup_logging


async def _authenticate(settings: Settings) -> None:
    setup_logging(settings.log_dir, settings.log_level, json=settings.log_json)
    settings.validate()
    gateway = TelegramGateway(settings)
    try:
        me = await gateway.authenticate()
        identifier = getattr(me, "username", None) or getattr(me, "id", "unknown")
        print(f"Авторизация успешна: {identifier}")
    finally:
        with contextlib.suppress(Exception):
            await gateway.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="userbot",
        description="Модульный Telegram-юзербот с менеджером плагинов",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "auth"),
        default="run",
        help="run — запустить юзербот; auth — интерактивная авторизация",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="корень проекта (по умолчанию $TGUSERBOT_ROOT или текущий каталог)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="путь к файлу с переменными окружения (по умолчанию <root>/.env)",
    )
    return parser


async def _run(settings: Settings) -> int:
    app = UserbotApp(settings)
    try:
        await app.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        # The app installs its own SIGINT/SIGTERM handlers, so this only fires
        # when the interrupt lands outside the running loop (e.g. during
        # startup). Shut down properly instead of exiting 0 with live state.
        app.request_stop()
        await app.shutdown()
        return 130
    except SessionLockedError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 4
    except SessionRevokedError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 5
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        settings = Settings.from_env(args.root, env_file=args.env_file)
        settings.validate()
        if args.command == "auth":
            asyncio.run(_authenticate(settings))
            return 0
        return asyncio.run(_run(settings))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
