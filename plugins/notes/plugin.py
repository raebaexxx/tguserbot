from __future__ import annotations

from userbot.commands import CommandContext
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext
from userbot.storage import Storage


class Plugin(BasePlugin):
    async def migrate(self, storage: Storage) -> None:
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
        await storage.execute("CREATE INDEX IF NOT EXISTS notes_chat_id_id ON notes(chat_id, id)")

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
            await command.respond(
                "Использование:\n/ub notes add <текст>\n/ub notes list\n/ub notes delete <id>"
            )
            return

        action = parts[0].lower()
        argument = parts[1].strip() if len(parts) == 2 else ""
        chat_id = str(getattr(command.event, "chat_id", "unknown"))

        if action == "add":
            if not argument:
                await command.respond("Текст заметки не указан.")
                return
            body = argument[:4000]
            note_id = await self.ctx.storage.execute(
                "INSERT INTO notes (chat_id, body) VALUES (?, ?)",
                (chat_id, body),
            )
            await command.respond(f"Заметка #{note_id} сохранена.")
            return

        if action == "list":
            rows = await self.ctx.storage.fetchall(
                "SELECT id, body, created_at FROM notes "
                "WHERE chat_id = ? ORDER BY id DESC LIMIT 20",
                (chat_id,),
            )
            if not rows:
                await command.respond("Заметок пока нет.")
                return
            lines = ["Последние заметки:"]
            for row in rows:
                body = str(row["body"]).replace("\n", " ")
                if len(body) > 120:
                    body = body[:117] + "..."
                lines.append(f"#{row['id']} — {body}")
            await command.respond("\n".join(lines))
            return

        if action == "delete":
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
            return

        await command.respond("Неизвестная подкоманда. Используйте /ub notes list")
