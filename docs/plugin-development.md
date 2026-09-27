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
description = "Пример"
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

Import from `userbot.plugin_api` (or the `userbot` package root). Everything
those modules export is covered by the project's semantic versioning; deeper
module paths are internal and may change.

## Manifest fields

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | `[a-z0-9][a-z0-9_-]{0,63}`, must equal the directory name |
| `version` | no | Free-form, defaults to `0.1.0` |
| `api` | no | Plugin API version, must be `"1"` |
| `entrypoint` | no | `module:attribute`, defaults to `plugin:Plugin` |
| `description` | no | Shown in documentation only |
| `schema_version` | no | Bump to re-run `migrate()`, defaults to `1` |
| `[config]` | no | Default settings, see below |

The entrypoint must be a class with an awaitable `setup(ctx)` plus the other
hooks. The manager checks this at load time and refuses the plugin with a clear
error rather than failing later with an `AttributeError`.

## Lifecycle

- `migrate(storage)` runs once per `schema_version`, against this plugin's own
  sandbox database. It must be idempotent: a migration that is interrupted is
  not recorded as applied and will run again.
- `setup(ctx)` registers commands and handlers. It runs before the plugin goes
  live, so nothing here is reachable by users yet.
- `start()` starts plugin work.
- `stop()` releases plugin resources. It has a wall-clock budget, so do not
  block it on long I/O.

Use `ctx.spawn(coro)` for background tasks. The plugin manager cancels those
tasks and removes registered handlers and commands during unload. Register raw
event handlers with `ctx.register_handler(callback, event)`, and use
`ctx.is_owner(sender_id)` for owner-only event handlers.

A plugin must not create a second Telegram client or reuse another account's
session. It receives the already configured client through `ctx.client`.

## Context

| Member | Purpose |
|---|---|
| `ctx.client` | The shared Telethon client |
| `ctx.storage` | This plugin's own SQLite database |
| `ctx.config` | Effective settings (manifest defaults + operator overrides) |
| `ctx.settings` | Global settings; read-only |
| `ctx.dispatcher` | Command registry |
| `ctx.rate_limiter` | `async with ctx.rate_limiter.slot("key"):` for spacing |
| `ctx.health` | Health snapshot |
| `ctx.manager` | Plugin manager, for cross-plugin operations |
| `ctx.logger` | Namespaced logger, already level-configured |
| `ctx.spawn(coro)` | Tracked background task, cancelled on unload |
| `ctx.register_command(...)` | Owner-only command under `/ub` |
| `ctx.register_handler(...)` | Raw Telethon event handler |
| `ctx.is_owner(sender_id)` | Owner check |

## Storage

`ctx.storage` is a `PluginStorage`: one SQLite file per plugin, under
`data/plugin-data/<name>/`. A plugin cannot reach the core bookkeeping tables
(`plugin_state`, `plugin_migrations`, `plugin_kv`) or another plugin's tables.

```python
await ctx.storage.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, body TEXT)")
rowid = await ctx.storage.execute_insert("INSERT INTO items (body) VALUES (?)", ("x",))
changed = await ctx.storage.execute("DELETE FROM items WHERE id = ?", (rowid,))
rows = await ctx.storage.fetchall("SELECT id, body FROM items ORDER BY id DESC LIMIT 20")
```

`execute` returns the number of affected rows and `execute_insert` returns the
new rowid. Do not read `lastrowid` for a DELETE: sqlite leaves it pointing at an
unrelated earlier INSERT, which previously made `notes delete` report success
for notes it had not touched.

`ATTACH`, `PRAGMA`, `VACUUM`, and stacked statements are refused, so a plugin
cannot attach the core database and drop tables from the outside.

For simple key/value state, skip the table entirely:

```python
await ctx.storage.set_value("cursor", last_id)
value = await ctx.storage.get_value("cursor")
```

## Configuration

Declare defaults in `plugin.toml`:

```toml
[config]
retries = 3
allowed_hosts = ["example.com"]
```

Override them per installation in `<data_dir>/plugin-config.toml`:

```toml
[example]
retries = 10
```

Read them with typed accessors, which report a mismatch as a configuration
error rather than letting a wrong type reach your logic:

```python
retries = ctx.config.int_value("retries", 3)
hosts = ctx.config.str_list("allowed_hosts", ("example.com",))
flag = ctx.config.bool_value("flag", False)
name = ctx.config.str_value("name", "default")
```

An override whose type does not match the default is refused when the plugin
loads. A malformed `plugin-config.toml` is logged and ignored, so a typo cannot
stop the bot from booting.

## Commands

Plugin commands are exposed under the reserved `/ub` or `.ub` prefix:

```text
/ub example
.ub example
```

The core dispatcher only accepts commands sent by the logged-in account or an
explicit owner ID from `TGUSERBOT_OWNER_IDS`. A command that runs longer than
`TGUSERBOT_COMMAND_TIMEOUT` is reported as timed out, and a repeat within the
cooldown is rejected, so a double tap does not run it twice.

A command name that collides with a core command (`help`, `version`, `plugins`,
`plugin`) or with another plugin's command is refused at load time.

The command must be the first thing in the message. `/ub` inside a quote or a
note body is not a command.

## Git plugins

Git sources are trusted Python code and can access the Telegram client. Add the
repository URL to `TGUSERBOT_GIT_ALLOWED_REPOS`, review the commit, and activate
it manually. Comparison ignores case and an optional `.git` suffix. A repository
with multiple plugins must specify its subdirectory:

```text
/ub plugin install https://github.com/raebaexxx/tguserbot.git main plugins/example
```

Fetching is a shallow clone of a single ref, with git's global and system
configuration disabled and only HTTPS and SSH transports permitted, so an
operator's `url.*.insteadOf` rule cannot rewrite an allow-listed URL into
something executable. Each plugin keeps its three most recently fetched
revisions, which makes `/ub plugin update <name> <commit>` the rollback path.

Do not put API credentials, session strings, tokens, or personal data in a
plugin repository.
