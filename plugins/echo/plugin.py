from __future__ import annotations

from telethon import events

from userbot.commands import CommandContext
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext


class Plugin(BasePlugin):
    async def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        ctx.register_command("echo", self.handle, help_text="Повторить текст")
        ctx.register_handler(
            self.ping,
            events.NewMessage(pattern=r"(?i)^/ubping$"),
        )

    async def handle(self, command: CommandContext) -> None:
        if command.args:
            await command.respond(command.args)
        else:
            await command.respond("Использование: /ub echo <текст>")

    async def ping(self, event: object) -> None:
        if not self.ctx.is_owner(getattr(event, "sender_id", None)):
            return
        await event.respond("pong")
