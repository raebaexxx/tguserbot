from __future__ import annotations

from userbot.commands import CommandContext
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext
from userbot.storage import PluginStorage

USAGE = "Использование:\n/ub notes add <текст>\n/ub notes list\n/ub notes delete <id>"

#: How many notes a single ``list`` returns.
PAGE_SIZE = 20

#: Telegram rejects longer text messages, so cap what we store.
MAX_BODY_LENGTH = 4000

#: How much of a note body ``list`` echoes back.
PREVIEW_LENGTH = 120


class Plugin(BasePlugin):
    async def migrate(self, storage: PluginStorage) -> None:
        await storage.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await storage.execute(
            "CREATE INDEX IF NOT EXISTS notes_chat_id_id ON notes(chat_id, id)"
        )

    async def setup(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        ctx.register_command(
            "notes",
            self.handle,
            help_text="add/list/delete заметки для текущего чата",
        )

    async def handle(self, command: CommandContext) -> None:
        parts = command.args.strip().split(maxsplit=1)
        if not parts:
            await command.respond(USAGE)
            return

        action = parts[0].lower()
        argument = parts[1].strip() if len(parts) == 2 else ""
        chat_id = str(getattr(command.event, "chat_id", "unknown"))

        if action == "add":
            await self._add(command, chat_id, argument)
            return
        if action == "list":
            await self._list(command, chat_id)
            return
        if action == "delete":
            await self._delete(command, chat_id, argument)
            return
        await command.respond("Неизвестная подкоманда. Используйте /ub notes list")

    async def _add(self, command: CommandContext, chat_id: str, argument: str) -> None:
        if not argument:
            await command.respond("Текст заметки не указан.")
            return
        body = argument[:MAX_BODY_LENGTH]
        note_id = await self.ctx.storage.execute_insert(
            "INSERT INTO notes (chat_id, body) VALUES (?, ?)",
            (chat_id, body),
        )
        await command.respond(f"Заметка #{note_id} сохранена.")

    async def _list(self, command: CommandContext, chat_id: str) -> None:
        rows = await self.ctx.storage.fetchall(
            "SELECT id, body, created_at FROM notes "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, PAGE_SIZE),
        )
        if not rows:
            await command.respond("Заметок пока нет.")
            return
        lines = ["Последние заметки:"]
        for row in rows:
            body = " ".join(str(row["body"]).split())
            if len(body) > PREVIEW_LENGTH:
                body = body[: PREVIEW_LENGTH - 3] + "..."
            lines.append(f"#{row['id']} — {body}")
        await command.respond("\n".join(lines))

    async def _delete(self, command: CommandContext, chat_id: str, argument: str) -> None:
        if not argument.isdigit():
            await command.respond("ID заметки должен быть числом.")
            return
        deleted = await self.ctx.storage.execute(
            "DELETE FROM notes WHERE id = ? AND chat_id = ?",
            (int(argument), chat_id),
        )
        if deleted:
            await command.respond(f"Заметка #{argument} удалена.")
        else:
            await command.respond("Заметка не найдена в этом чате.")
