from __future__ import annotations

import asyncio
import signal

from .commands import CommandContext, CommandDispatcher
from .config import Settings
from .gateway import TelegramGateway
from .health import HealthService
from .logging import setup_logging
from .manager import PluginManager
from .rate_limit import RateLimiter
from .storage import Storage
from .watcher import PluginWatcher


class UserbotApp:
    def __init__(self, settings: Settings):
        settings.validate()
        self.settings = settings
        self.logger = setup_logging(settings.log_dir, settings.log_level)
        self.health = HealthService()
        self.storage = Storage(settings.database_path)
        self.rate_limiter = RateLimiter(
            min_interval=settings.min_request_interval,
        )
        self.gateway = TelegramGateway(settings)
        self.dispatcher = CommandDispatcher(settings.owner_ids)
        self.manager = PluginManager(
            settings=settings,
            client=self.gateway.client,
            storage=self.storage,
            dispatcher=self.dispatcher,
            rate_limiter=self.rate_limiter,
            health=self.health,
        )
        self.watcher = PluginWatcher(self.manager, settings.plugin_dir)
        self._stop_event = asyncio.Event()
        self._shutdown_lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self.settings.validate()
        await self.storage.initialize()
        try:
            me = await self.gateway.connect()
            if me is None or getattr(me, "id", None) is None:
                raise RuntimeError("Telegram did not return the authorized account")
            self.dispatcher.add_owner(int(me.id))
            self.health.telegram_connected = True
            self.health.authorized = True
            self.dispatcher.attach_client(self.gateway.client)
            self._register_core_commands()
            await self.manager.initialize_state()
            await self.manager.load_all_local()
            await self.watcher.start()
            self._started = True
            self.logger.info(
                "Userbot started as @%s with %d active plugins",
                getattr(me, "username", None) or getattr(me, "id", "unknown"),
                self.manager.active_count(),
            )
        except Exception:
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if not self._started and not self.storage.initialized:
                return
            self._started = False
            await self.watcher.stop()
            await self.manager.shutdown()
            self.dispatcher.detach_client()
            await self.gateway.disconnect()
            await self.storage.close()
            self.health.telegram_connected = False
            self.health.authorized = False
            self.logger.info("Userbot stopped")

    async def run(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await self._stop_event.wait()
        finally:
            await self.shutdown()

    def request_stop(self) -> None:
        self._stop_event.set()

    def _register_core_commands(self) -> None:
        async def help_command(command: CommandContext) -> None:
            await command.respond(self.dispatcher.help_text())

        async def plugins_command(command: CommandContext) -> None:
            plugins = await self.manager.list_plugins()
            if not plugins:
                await command.respond("Активных плагинов нет.")
                return
            lines = ["Плагины:"]
            for plugin in plugins:
                suffix = f" — {plugin['error']}" if plugin.get("error") else ""
                lines.append(
                    f"{plugin['name']}: {plugin['status']} "
                    f"({plugin.get('version') or '?'}, {plugin.get('source')}){suffix}"
                )
            await command.respond("\n".join(lines[:50]))

        async def plugin_command(command: CommandContext) -> None:
            parts = command.args.strip().split(maxsplit=3)
            if not parts:
                await command.respond(
                    "Использование: /ub plugin <reload|enable|disable|install|update> ..."
                )
                return
            action = parts[0].lower()
            if action == "list":
                await plugins_command(command)
                return
            if action == "reload" and len(parts) >= 2:
                name = parts[1]
                runtime = self.manager._runtimes.get(name)
                if runtime is None:
                    await command.respond(f"Плагин {name} не найден.")
                    return
                if runtime.source == "git":
                    await command.respond("Обновляю Git-плагин…")
                    await self.manager.update_git(name)
                else:
                    await self.manager.reload_local(name)
                await command.respond(f"Плагин {name} перезагружен.")
                return
            if action == "enable" and len(parts) >= 2:
                name = parts[1]
                await self.manager.enable(name)
                await command.respond(f"Плагин {name} включён.")
                return
            if action == "disable" and len(parts) >= 2:
                name = parts[1]
                await self.manager.disable(name)
                await command.respond(f"Плагин {name} выключен.")
                return
            if action == "install" and len(parts) >= 3:
                url, git_ref = parts[1], parts[2]
                subpath = parts[3] if len(parts) == 4 else None
                runtime = await self.manager.install_git(url, git_ref, subpath=subpath)
                await command.respond(
                    f"Плагин {runtime.name} установлен из commit "
                    f"{(runtime.source_ref or 'unknown')[:12]}."
                )
                return
            if action == "update" and len(parts) >= 2:
                name = parts[1]
                update_ref = parts[2] if len(parts) >= 3 else None
                runtime = await self.manager.update_git(name, update_ref)
                await command.respond(
                    f"Плагин {name} обновлён до commit {(runtime.source_ref or 'unknown')[:12]}."
                )
                return
            await command.respond("Неверная подкоманда. Используйте /ub help.")

        self.dispatcher.register(
            "help",
            help_command,
            help_text="Показать команды",
            plugin_name="core",
        )
        self.dispatcher.register(
            "plugins",
            plugins_command,
            help_text="Показать состояние плагинов",
            plugin_name="core",
        )
        self.dispatcher.register(
            "plugin",
            plugin_command,
            help_text="Управление плагинами",
            plugin_name="core",
        )
