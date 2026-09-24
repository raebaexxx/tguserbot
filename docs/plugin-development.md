# Plugin development

A plugin is a Python package with a manifest and an entrypoint. The loader
compiles every Python file before importing it, then loads the package in a
unique namespace. A failed reload leaves the previous generation active.

## Minimal plugin

```text
plugins/example/
├── __init__.py
├── plugin.toml
└── plugin.py
```

`plugin.toml`:

```toml
name = "example"
version = "0.1.0"
api = "1"
entrypoint = "plugin:Plugin"
description = "Example"
schema_version = 1
```

`plugin.py`:

```python
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext


class Plugin(BasePlugin):
    async def setup(self, ctx: PluginContext):
        ctx.register_command("example", self.handle, help_text="Пример")

    async def handle(self, command):
        await command.respond("Привет")
```

## Lifecycle

- `migrate(storage)` runs once per `schema_version`.
- `setup(ctx)` registers commands and handlers.
- `start()` starts plugin work.
- `stop()` releases plugin resources.

Use `ctx.spawn(coro)` for background tasks. The plugin manager cancels those
tasks and removes registered handlers/commands during unload. Register raw
event handlers with `ctx.register_handler(callback, event)`, and use
`ctx.is_owner(sender_id)` for owner-only event handlers.

A plugin must not create a second Telegram client or reuse another account's
session. It receives the already configured client through `ctx.client`.

## Commands

Plugin commands are exposed under the reserved `/ub` or `.ub` prefix:

```text
/ub example
.ub example
```

The core dispatcher only accepts commands sent by the logged-in account or an
explicit owner ID from `TGUSERBOT_OWNER_IDS`.

## Git plugins

Git sources are trusted Python code and can access the Telegram client. Add the
exact repository URL to `TGUSERBOT_GIT_ALLOWED_REPOS`, review the commit, and
activate it manually. A repository with multiple plugins must specify its
subdirectory:

```text
/ub plugin install https://github.com/raebaexxx/tguserbot.git main plugins/example
```

Do not put API credentials, session strings, tokens, or personal data in a
plugin repository.
