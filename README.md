# tguserbot

A modular Telegram userbot built on Telethon and a lifecycle-aware plugin manager.

The project is designed for a single Telegram user account. It connects through
MTProto, not the Bot API. Plugins are local Python packages or explicitly trusted
Git repositories. Local plugins can be reloaded without restarting the process;
Git plugins are staged and activated manually.

## Status

- a single-account Telethon gateway with an enforced single-writer session lock;
- persistent SQLite storage, with each plugin confined to its own database file;
- a plugin manifest, lifecycle API, and per-plugin configuration;
- reliable enable/disable/reload/rollback operations;
- a local plugin watcher that quiesces instead of interrupting a reload;
- an owner-only command dispatcher with per-command timeouts and cooldowns;
- `notes`, `status`, and `echo` demo plugins, plus a `tiktok` downloader;
- an `ai` plugin: Gemini chat, and plugin generation behind a two-step review;
- tests, CI gates, and deployment templates.

## Requirements

- Python 3.12 or newer (tested on 3.12, 3.13, and 3.14)
- Telegram `api_id` and `api_hash` from <https://my.telegram.org/apps>
- Git for trusted Git plugin sources
- `ffmpeg` only if you use a plugin that merges media formats

## Local development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
```

Fill `TGUSERBOT_API_ID` and `TGUSERBOT_API_HASH` in `.env`, then restrict it:

```bash
chmod 600 .env
```

The process warns at startup if `.env` is readable by other users. Never commit
`.env`, `.session`, or runtime data.

Perform the first interactive login:

```bash
.venv/bin/python -m userbot auth
```

Start the bot:

```bash
.venv/bin/python -m userbot run
```

The process uses one asyncio event loop. A `flock` on the session file prevents a
second process from opening the same session, so a duplicate `run` fails fast
with a clear message instead of corrupting the auth key.

## Configuration

All settings come from the environment; `.env` is read for convenience and real
environment variables always win. See `.env.example` for the full list with
comments. The ones worth knowing:

| Variable | Default | Purpose |
|---|---|---|
| `TGUSERBOT_OWNER_IDS` | empty | Extra Telegram IDs allowed to run `/ub`. The logged-in account is always an owner. |
| `TGUSERBOT_DISABLED_PLUGINS` | empty | Plugins that must never load. A plugin listed here cannot be re-enabled at runtime; remove it from the list first. |
| `TGUSERBOT_WATCH` | `1` | Set to `0` to disable the local-plugin watcher. |
| `TGUSERBOT_WATCH_INTERVAL` | `2.0` | Seconds between directory scans. The scan reads file metadata and only re-hashes a plugin whose size or mtime changed. |
| `TGUSERBOT_COMMAND_TIMEOUT` | `120` | Seconds a `/ub` command may run before it is reported as timed out. |
| `TGUSERBOT_LOG_JSON` | `0` | Set to `1` for one JSON object per log line. |
| `TGUSERBOT_ALERT_CHAT` | empty | Chat ID to notify when the service dies and systemd stops restarting it. |
| `TGUSERBOT_GEMINI_API_KEY` | empty | API key for the `ai` plugin. Comma-separate several to rotate through them. |
| `TGUSERBOT_GIT_ALLOWED_REPOS` | empty | Exact repository URLs accepted by the Git plugin source. |

`owner_ids` is the whole authorisation surface of the bot, and the logged-in
account is always added to it. Anyone able to use that account can manage
plugins, so treat the session file as equivalent to the password.

## Plugin layout

A plugin is a directory with `plugin.toml`, `__init__.py`, and `plugin.py`:

```text
plugins/example/
├── __init__.py
├── plugin.toml
└── plugin.py
```

A minimal manifest:

```toml
name = "example"
version = "0.1.0"
api = "1"
entrypoint = "plugin:Plugin"
description = "Example plugin"
schema_version = 1
```

The plugin class may implement `migrate`, `setup`, `start`, and `stop`. Import
the API from its stable surface and use the context methods instead of
registering Telethon handlers directly:

```python
from userbot.plugin_api import Plugin as BasePlugin
from userbot.plugin_api import PluginContext


class Plugin(BasePlugin):
    async def setup(self, ctx: PluginContext):
        ctx.register_command("example", self.handle)

    async def handle(self, command):
        await command.respond("It works")
```

A plugin gets its own SQLite file under `data/plugin-data/<name>/`, reachable as
`ctx.storage`. It cannot see or damage the core bookkeeping tables; `ATTACH`,
`PRAGMA`, and stacked statements are refused.

Settings live in two places. `[config]` in `plugin.toml` holds the shipped
defaults, and `data/plugin-config.toml` overrides them per installation:

```toml
[example]
retries = 3
```

Read them through `ctx.config` (`int_value`, `bool_value`, `str_value`,
`str_list`, `require`); a mistyped override is reported at load time.

See [plugin development](docs/plugin-development.md) for the full lifecycle and
context API. The optional [TikTok plugin](docs/tiktok-plugin.md) downloads a
public video and removes its command message after sending.

## Management commands

Management commands work in any chat but are accepted only from the configured
owner IDs. The logged-in account is always added to the owner set.

```text
/ub help
/ub version
/ub plugins
/ub plugin list
/ub plugin reload <name>
/ub plugin disable <name>
/ub plugin enable <name>
/ub plugin install <url> <ref> [subpath]
/ub plugin update <name> [ref]
/ub plugin adopt <name>
```

`/ub version` reports the running build and the storage schema version, which is
the quickest way to confirm which code a server is actually running.

Local changes under `plugins/` are picked up by the watcher, which compares file
size and mtime before re-hashing and only reloads when the bytes actually
changed. A failed reload leaves the previously active version running. Git
sources are never fetched or activated automatically; remote sources must be
allow-listed through `TGUSERBOT_GIT_ALLOWED_REPOS` and installed explicitly. If a
repository contains several plugin folders, pass the plugin folder as the fourth
argument, for example
`/ub plugin install https://github.com/raebaexxx/tguserbot.git main plugins/notes`.

Fetching a Git plugin is a no-op unless the URL is allow-listed; comparison
ignores case and an optional `.git` suffix. Each plugin keeps the three most
recently fetched revisions, so `/ub plugin update <name> <commit>` is also the
rollback path.

Replies stay in the forum topic they were asked in. Telegram carries the thread
on the message's reply header, and telethon 1.45 reads it and then drops it --
`send_message` and `send_file` both build `InputReplyToMessage(reply_to)` from a
single integer, so `top_msg_id` never reaches Telegram and every reply would
land in the main thread. `userbot.topics` builds the request with the topic set,
and both reply paths are wrapped: commands in the dispatcher, and raw event
handlers in `PluginContext`. Plugins need do nothing; `event.underlying` reaches
the real event for code that needs it. Calls using anything beyond plain text --
`silent`, buttons, `schedule` -- are passed to telethon untouched, because a
bot that quietly drops an argument is worse than one that answers in the wrong
thread.

`/ub plugin adopt <name>` installs a plugin that was generated into the staging
area (by `/ub ai new`, see below). Generation only ever writes to
`<data_dir>/plugin-staging/`, which is not a plugin root, so nothing it produced
can run until you type this command. Adoption runs a static review of the code
first and refuses anything that reaches for a shell, a socket, a native library,
or the session file; anything else it flags is shown alongside the result. The
review is a denylist, not a sandbox — treat adopted code as trusted, because you
just approved it.

**Note on the systemd deployment:** the shipped unit sets `ProtectSystem=strict`,
which makes `/opt/tguserbot/plugins` read-only, so the watcher cannot fire there.
Point `TGUSERBOT_PLUGIN_DIR` at a writable path such as
`/var/lib/tguserbot/plugins` to enable hot reload on a server.

## Server deployment

The server template is in `deploy/`. It uses a dedicated systemd service and
persistent directories under `/var/lib/tguserbot`; Docker is not required.

```bash
sudo bash deploy/install.sh
sudo tguserbotctl first-run
sudo tguserbotctl status
sudo tguserbotctl logs
```

`tguserbotctl update` pulls the latest code, reinstalls the locked
dependencies, restarts, health-checks the result, and rolls back to the previous
commit if the service does not come up.

The repository is public, so secrets and runtime data are intentionally ignored
by Git. Review `.gitignore` before adding any new generated files.

## Development checks

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src plugins tests
.venv/bin/pytest
```

`pytest` enforces a coverage floor of 85% (currently 90%).

## Safety

Telegram monitors unofficial API clients. This project does not provide spam,
bulk unsolicited messaging, fake counters, ghost/read-status manipulation, or
Telegram data scraping for AI training. Use it only on an account and data you
are authorized to automate.

Git plugins are trusted code, not a sandbox: an installed plugin runs in this
process with access to the Telegram client and its data. Review a commit before
installing it, and allow-list only repositories you control.
