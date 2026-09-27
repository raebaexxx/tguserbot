from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import time

from . import __version__
from .commands import (
    CommandContext,
    CommandDispatcher,
    PluginAction,
    parse_plugin_action,
)
from .config import Settings
from .gateway import TelegramGateway
from .git_source import GitSourceError
from .health import HealthService
from .loader import PluginLoadError
from .logging import setup_logging
from .manager import PluginManager
from .notify import SystemdNotifier
from .rate_limit import RateLimiter
from .storage import Storage
from .watcher import PluginWatcher

#: How often the watchdog timestamp is refreshed while the app is healthy.
HEARTBEAT_INTERVAL = 10.0

#: Longest a plugin command may run before it is reported as timed out.
DEFAULT_COMMAND_TIMEOUT = 120.0

#: Total budget for an orderly shutdown. Must stay below the unit's
#: TimeoutStopSec, otherwise systemd SIGKILLs the process mid-unload.
SHUTDOWN_TIMEOUT = 25.0


class UserbotApp:
    def __init__(self, settings: Settings):
        settings.validate()
        self.settings = settings
        self.logger = setup_logging(settings.log_dir, settings.log_level, json=settings.log_json)
        self.health = HealthService()
        self.health.heartbeat_path = settings.heartbeat_path
        self.storage = Storage(settings.database_path)
        self.rate_limiter = RateLimiter(min_interval=settings.min_request_interval)
        self.gateway = TelegramGateway(settings)
        self.dispatcher = CommandDispatcher(
            settings.owner_ids,
            command_timeout=settings.command_timeout or DEFAULT_COMMAND_TIMEOUT,
        )
        self.manager = PluginManager(
            settings=settings,
            client=self.gateway.client,
            storage=self.storage,
            dispatcher=self.dispatcher,
            rate_limiter=self.rate_limiter,
            health=self.health,
        )
        self.manager.set_shutdown_timeout(SHUTDOWN_TIMEOUT)
        self.watcher = PluginWatcher(
            self.manager,
            settings.plugin_dir,
            interval=settings.watch_interval,
            quiesce_timeout=SHUTDOWN_TIMEOUT,
        )
        self._stop_event = asyncio.Event()
        self._shutdown_lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self.notifier = SystemdNotifier()
        self._started = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self.settings.validate()
        await self.storage.initialize()
        try:
            self._warn_about_world_readable_secrets()
            me = await self.gateway.connect()
            if me is None or getattr(me, "id", None) is None:
                raise RuntimeError("Telegram did not return the authorized account")
            self.dispatcher.add_owner(int(me.id))
            self.health.set_telegram_state(connected=True, authorized=True)
            self.gateway.on_connection_state(self._on_connection_state)
            self.dispatcher.attach_client(self.gateway.client)
            self._register_core_commands()
            await self.manager.initialize_state()
            await self.manager.load_all_local()
            # Replay anything sent while we were down, now that the plugins'
            # handlers are attached.
            with contextlib.suppress(Exception):
                await self.gateway.catch_up()
            if self.settings.watch_enabled:
                await self.watcher.start()
            else:
                self.logger.info("plugin watcher disabled by configuration")
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
            self._connection_task = asyncio.create_task(
                self.gateway.monitor_connection(), name="connection-monitor"
            )
            self._started = True
            # Reset the start rate limiter first, then report readiness: a
            # Type=notify unit is only "started" once READY=1 arrives, and the
            # status text is what `systemctl status` will keep showing.
            self.notifier.reset()
            self.notifier.ready(
                f"connected as @{getattr(me, 'username', None) or getattr(me, 'id', '?')}"
            )
            self.logger.info(
                "Userbot started as @%s with %d active plugins",
                getattr(me, "username", None) or getattr(me, "id", "unknown"),
                self.manager.active_count(),
            )
        except BaseException:
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if not self._started and not self.storage.initialized:
                return
            self._started = False
            # Quiesce the watcher first: a reload interrupted mid-transaction is
            # what used to leave a live context that nothing tracked.
            with contextlib.suppress(Exception):
                await self.watcher.stop()
            self.notifier.stopping("unloading plugins")
            self._stop_heartbeat()
            self._stop_connection_monitor()
            with contextlib.suppress(Exception):
                await self.manager.shutdown()
            self.dispatcher.detach_client()
            with contextlib.suppress(Exception):
                await self.gateway.disconnect()
            with contextlib.suppress(Exception):
                await self.storage.close()
            self.health.set_telegram_state(connected=False, authorized=False)
            self.logger.info("Userbot stopped")

    async def run(self) -> None:
        await self.start()
        self._install_signal_handlers()
        try:
            await self._stop_event.wait()
        finally:
            await self.shutdown()

    def request_stop(self) -> None:
        self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self._stop_event.set)

    def _stop_heartbeat(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is None:
            return
        task.cancel()

    def _stop_connection_monitor(self) -> None:
        task, self._connection_task = self._connection_task, None
        if task is None:
            return
        task.cancel()

    def _on_connection_state(self, *, connected: bool) -> None:
        """Keep the reported Telegram state honest across network drops."""
        self.health.set_telegram_state(connected=connected, authorized=connected)
        if not connected:
            self.health.mark_error("Telegram connection lost")

    async def _heartbeat_loop(self) -> None:
        """Publish liveness: a timestamp file for monitoring, and sd_notify.

        The systemd unit uses ``Type=notify`` with ``WatchdogSec``, so a wedged
        process (a plugin deadlock, an event loop starved by a blocking call)
        gets restarted instead of sitting there looking fine. The ping comes
        from the same loop as the file so the two cannot disagree.
        """
        path = self.settings.heartbeat_path
        notify = SystemdNotifier()
        while True:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(int(time.time())), encoding="utf-8")
            except OSError as exc:
                self.logger.debug("heartbeat write failed: %s", exc)
            notify.ping()
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    def _warn_about_world_readable_secrets(self) -> None:
        env_file = self.settings.root_dir / ".env"
        if not env_file.is_file():
            return
        mode = env_file.stat().st_mode & 0o777
        if mode & 0o077:
            self.logger.warning(
                "%s is readable by other users (mode %o); run: chmod 600 %s",
                env_file,
                mode,
                env_file,
            )

    # -- core commands ------------------------------------------------------

    def _register_core_commands(self) -> None:
        async def help_command(command: CommandContext) -> None:
            await command.respond(self.dispatcher.help_text())

        async def version_command(command: CommandContext) -> None:
            await command.respond(
                f"tguserbot {__version__}\n"
                f"Схема БД: {await self.storage.schema_version()}\n"
                f"Python: {sys.version.split()[0]}"
            )

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
            await self._handle_plugin_command(command)

        self.dispatcher.register(
            "help",
            help_command,
            help_text="Показать команды",
            plugin_name="core",
        )
        self.dispatcher.register(
            "version",
            version_command,
            help_text="Показать версию",
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

    async def _handle_plugin_command(self, command: CommandContext) -> None:
        parsed = parse_plugin_action(command.args)
        if parsed is None:
            await command.respond(self._plugin_usage())
            return
        action = parsed.action
        try:
            if action == "list":
                await self._plugins_command(command)
            elif action == "reload":
                await self._reload_plugin(command, parsed.name)
            elif action == "enable":
                await self._enable_plugin(command, parsed.name)
            elif action == "disable":
                await self._disable_plugin(command, parsed.name)
            elif action == "install":
                await self._install_plugin(command, parsed)
            elif action == "update":
                await self._update_plugin(command, parsed)
        except PluginLoadError as exc:
            await command.respond(f"Ошибка: {exc}")
        except Exception as exc:
            self.logger.exception("plugin %s failed", action)
            await command.respond(f"Ошибка: {exc}")

    async def _enable_plugin(self, command: CommandContext, name: str | None) -> None:
        if not name:
            await command.respond("Использование: /ub plugin enable <name>")
            return
        await self.manager.enable(name)
        await command.respond(f"Плагин {name} включён.")

    async def _disable_plugin(self, command: CommandContext, name: str | None) -> None:
        if not name:
            await command.respond("Использование: /ub plugin disable <name>")
            return
        await self.manager.disable(name)
        await command.respond(f"Плагин {name} выключен.")

    async def _plugins_command(self, command: CommandContext) -> None:
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

    async def _reload_plugin(self, command: CommandContext, name: str | None) -> None:
        if not name:
            await command.respond("Использование: /ub plugin reload <name>")
            return
        runtime = self.manager.get_runtime(name)
        if runtime is None:
            if not self.manager.has_plugin(name):
                await command.respond(f"Плагин {name} не найден.")
                return
            if self.manager.is_disabled(name):
                await command.respond(f"Плагин {name} выключен. Сначала /ub plugin enable {name}.")
                return
            await self.manager.load_local(name, force=True)
            await command.respond(f"Плагин {name} перезагружен.")
            return
        if runtime.source == "git":
            await command.respond("Обновляю Git-плагин…")
            updated = await self.manager.update_git(name)
            await command.respond(
                f"Плагин {name} обновлён до commit {(updated.source_ref or '?')[:12]}."
            )
            return
        await self.manager.reload_local(name)
        await command.respond(f"Плагин {name} перезагружен.")

    async def _install_plugin(self, command: CommandContext, parsed: PluginAction) -> None:
        if parsed.ref is None or parsed.commit is None:
            await command.respond("Использование: /ub plugin install <url> <ref> [подпапка]")
            return
        try:
            runtime = await self.manager.install_git(
                parsed.ref, parsed.commit, subpath=parsed.subpath
            )
        except PluginLoadError:
            # Re-raise so the single top-level handler formats the reply; sending
            # a "loading…" message first would put the error in a second bubble.
            raise
        except GitSourceError as exc:
            await command.respond(f"Ошибка загрузки: {exc}")
            return
        await command.respond(
            f"Плагин {runtime.name} установлен из commit {(runtime.source_ref or 'unknown')[:12]}."
        )

    async def _update_plugin(self, command: CommandContext, parsed: PluginAction) -> None:
        if parsed.name is None:
            await command.respond("Использование: /ub plugin update <name> [ref]")
            return
        runtime = await self.manager.update_git(parsed.name, parsed.commit)
        await command.respond(
            f"Плагин {parsed.name} обновлён до commit {(runtime.source_ref or '?')[:12]}."
        )

    @staticmethod
    def _plugin_usage() -> str:
        return (
            "Использование:\n"
            "/ub plugin list\n"
            "/ub plugin reload <name>\n"
            "/ub plugin enable <name>\n"
            "/ub plugin disable <name>\n"
            "/ub plugin install <url> <ref> [подпапка]\n"
            "/ub plugin update <name> [ref]"
        )
