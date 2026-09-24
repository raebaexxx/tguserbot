# tguserbot

A modular Telegram userbot built on Telethon and a lifecycle-aware plugin manager.

The project is designed for a single Telegram user account. It connects through
MTProto, not the Bot API. Plugins are local Python packages or explicitly trusted
Git repositories. Local plugins can be reloaded without restarting the process;
Git plugins are staged and activated manually.

## Status

The first implementation milestone contains:

- a single-account Telethon gateway;
- persistent SQLite session/runtime directories;
- a plugin manifest and lifecycle API;
- reliable enable/disable/reload/rollback operations;
- a local plugin watcher;
- an owner-only command dispatcher;
- `notes`, `status`, and `echo` demo plugins;
- tests and deployment templates.

## Requirements

- Python 3.12 or newer
- Telegram `api_id` and `api_hash` from <https://my.telegram.org/apps>
- Git for trusted Git plugin sources

## Local development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
```

Fill `TGUSERBOT_API_ID` and `TGUSERBOT_API_HASH` in `.env`. Never commit `.env`,
`.session`, or runtime data.

Perform the first interactive login:

```bash
.venv/bin/python -m userbot auth
```

Start the bot:

```bash
.venv/bin/python -m userbot run
```

The process uses one asyncio event loop. Do not run two clients against the same
session file.

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
```

The plugin class may implement `setup`, `migrate`, `start`, and `stop`. Use the
context methods instead of registering Telethon handlers directly:

```python
class Plugin:
    async def setup(self, ctx):
        ctx.register_command("example", self.handle)

    async def handle(self, command):
        await command.respond("It works")
```

See [plugin development](docs/plugin-development.md) for the lifecycle and
context API. The optional [TikTok plugin](docs/tiktok-plugin.md) downloads a
public video and removes its command message after sending.

## Management commands

Management commands work in any chat but are accepted only from the configured
owner IDs. The logged-in account is always added to the owner set.

```text
/ub help
/ub plugins
/ub plugin reload <name>
/ub plugin disable <name>
/ub plugin enable <name>
/ub plugin install <url> <ref> [subpath]
/ub plugin update <name> [ref]
```

Local changes under `plugins/` are detected automatically. A failed reload
leaves the previously active version running. Git sources are not fetched or
activated automatically; remote sources must be explicitly allowed through
`TGUSERBOT_GIT_ALLOWED_REPOS`. If a repository contains several plugin
folders, pass the plugin folder as the fourth argument, for example
`/ub plugin install https://github.com/raebaexxx/tguserbot.git main plugins/notes`.

## Server deployment

The server template is in `deploy/`. It uses a dedicated systemd service and
persistent directories under `/var/lib/tguserbot`; Docker is not required.
After installation, manage it with one command:

```bash
sudo tguserbotctl first-run
sudo tguserbotctl start
sudo tguserbotctl status
sudo tguserbotctl logs
```

The repository is public, so secrets and runtime data are intentionally ignored
by Git. Review `.gitignore` before adding any new generated files.

## Safety

Telegram monitors unofficial API clients. This project does not provide spam,
bulk unsolicited messaging, fake counters, ghost/read-status manipulation, or
Telegram data scraping for AI training. Use it only on an account and data you
are authorized to automate.
