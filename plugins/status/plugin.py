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
        idle = snapshot.get("updates_idle_seconds")
        seen = snapshot.get("updates_seen", 0)
        # Silence is normal for an idle bot, so this states a fact and does not
        # editorialise. The pair is what separates "quiet" from "not being fed":
        # a bot with a rising "last update" and a stalled pts has stopped being
        # updated even though it looks connected.
        if idle is None:
            updates = f"Апдейты: не получено ни одного ({seen})"
        else:
            updates = f"Апдейты: последний {idle:.0f}s назад, всего {seen}"
        pts = snapshot.get("session_pts")
        state_age = snapshot.get("session_update_age_seconds")
        if pts is None:
            updates += "\nSession pts: неизвестен"
        elif state_age is None:
            updates += f"\nSession pts: {pts}"
        else:
            updates += f"\nSession pts: {pts} (состояние {state_age:.0f}s назад)"

        lines = [
            f"Uptime: {snapshot['uptime_seconds']}s",
            f"Telegram: {'подключён' if snapshot['telegram_connected'] else 'не подключён'}",
            f"Авторизация: {'да' if snapshot['authorized'] else 'нет'}",
            updates,
            f"Watcher: {'работает' if snapshot['watcher_running'] else 'остановлен'}",
            f"Плагины: {snapshot['plugin_count']} активных, {snapshot['plugin_errors']} с ошибками",
            f"Reload: {snapshot['reload_count']}",
        ]
        if snapshot.get("last_error"):
            lines.append(f"Последняя ошибка: {snapshot['last_error']}")
        for entry in snapshot.get("plugin_error_details", []):
            lines.append(f"Ошибка {entry['plugin']} ({entry['age_seconds']}s): {entry['message']}")
        await command.respond("\n".join(lines))
