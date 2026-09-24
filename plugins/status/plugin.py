from __future__ import annotations

from userbot.commands import CommandContext
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext


class Plugin(BasePlugin):
    async def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        ctx.register_command("status", self.handle, help_text="Состояние userbot")

    async def handle(self, command: CommandContext) -> None:
        snapshot = self.ctx.health.snapshot(
            plugin_count=self.ctx.manager.active_count(),
            plugin_errors=await self.ctx.manager.error_count(),
        )
        lines = [
            f"Uptime: {snapshot['uptime_seconds']}s",
            f"Telegram: {'подключён' if snapshot['telegram_connected'] else 'не подключён'}",
            f"Авторизация: {'да' if snapshot['authorized'] else 'нет'}",
            f"Watcher: {'работает' if snapshot['watcher_running'] else 'остановлен'}",
            f"Плагины: {snapshot['plugin_count']} активных, {snapshot['plugin_errors']} с ошибками",
            f"Reload: {snapshot['reload_count']}",
        ]
        if snapshot.get("last_error"):
            lines.append(f"Последняя ошибка: {snapshot['last_error']}")
        await command.respond("\n".join(lines))
