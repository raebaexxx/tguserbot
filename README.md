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
- a `sum` plugin: summarises the last messages in a chat, voice notes and
  video messages included (media is off by default — it leaves this machine);
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
| `TGUSERBOT_GIT_ALLOWED_REPOS` | empty | Repository URLs the Git plugin source may fetch. Compared after stripping the trailing slash and a `.git` suffix and lower-casing the *host*; the repository path's case is significant, so a GitHub entry must carry the case you will type. |

`owner_ids` is the whole authorisation surface of the bot, and the logged-in
account is always added to it. Anyone able to use that account can manage
plugins, so treat the session file as equivalent to the password.

## Summarising a chat

`/ub sum` summarises the last messages where you typed it, and `/ub sum 10 media`
includes voice notes and video messages. The formats were checked against the live
model with real messages from the bot's own chats rather than assumed: Telegram
voice notes are OGG/Opus and arrive as `audio/ogg`, video messages are MP4.

Media is **off by default**, and that default is the point: a summary sends other
people's messages to a third party. Turn it on per-plugin in
`plugin-config.toml` with `[sum] include_media = true`, and the reply then says how
many attachments were sent. Anything that did not fit the size budget is listed
under "Не учтено" rather than dropped — a summary that quietly ignores half a
conversation is worse than one that admits it. See `docs/sum-plugin.md`.

Both `sum` and `ai` list fallback models in `model_fallbacks`. The free tier's
daily allowance is counted per model, so when the configured one is exhausted the
plugins move to the next and say which model actually answered — instead of
spending four retries on a quota that resets at midnight.

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

**Editing a message: use `userbot.messaging.edit_message`, not `event.edit_text`.**
Telethon's `Message` has `edit` and no `edit_text`; Pyrogram's has `edit_text`
and no `edit`. Both are the obvious name to reach for, and the wrong one fails
quietly rather than loudly — a plugin that calls `edit_text` on a Telethon message
raises `AttributeError` on every call, so a streaming progress display simply
never appears while everything else still works. `edit_message` tries both, and
`delete_message` does the same for deletion.

**Replies follow the topic when Telegram says which one it is.** Telethon 1.45
reads the topic off the message's reply header and then drops it: `send_message`
and `send_file` both build `InputReplyToMessage(reply_to)` from a single
integer, so `top_msg_id` never reaches Telegram. `userbot.topics` builds the
request with the topic set, on both reply paths -- commands in the dispatcher,
raw event handlers in `PluginContext`. Plugins need do nothing, and
`event.underlying` reaches the real event.

Telegram marks a topic in two different ways, and only reading the first makes
a freshly posted message look like it has no topic. From a live forum:

```
id=40887 '/ub version'  forum_topic=True reply_to_msg_id=60   reply_to_top_id=None
id=40892 'да'            forum_topic=True reply_to_msg_id=40885 reply_to_top_id=60
```

`reply_to_top_id` is set only on a message that is itself a *reply*. A message
posted fresh in a topic points at the topic's **root** instead and flags itself
with `forum_topic`. So the topic is `reply_to_top_id` when present, and
`reply_to_msg_id` when `forum_topic` says so -- the flag is what keeps an
ordinary reply in a plain group from being mistaken for a topic.

Which thread was chosen is written to the journal on every reply, so this is
never a matter of guesswork:

```
INFO userbot.topics: replying in topic 60
INFO userbot.topics: replying in main thread (no topic on the message)
```

Calls using anything beyond plain text -- `silent`, buttons, `schedule` -- are
passed to telethon untouched, because a bot that quietly drops an argument is
worse than one that answers in the wrong thread.

`/ub plugin adopt <name>` installs a plugin that was generated into the staging
area (by `/ub ai new`, see below). Generation only ever writes to
`<data_dir>/plugin-staging/`, which is not a plugin root, so nothing it produced
can run until you type this command. Adoption runs a static review of the code
first and refuses anything that reaches for a shell, a socket, a native library,
or the session file — including through a renamed import (`import os as o`),
through `builtins`, through a submodule of a refused package, and through a
session path assembled from pieces. Anything else it flags is shown alongside the
result. The review is a denylist, not a sandbox — treat adopted code as trusted,
because you just approved it.

**Note on the systemd deployment:** the shipped unit sets `ProtectSystem=strict`,
which makes `/opt/tguserbot/plugins` read-only. Hot reload still works there: the
watcher only reads, and the loader compiles the sources in memory, so the tree does
not need to be writable. Point `TGUSERBOT_PLUGIN_DIR` somewhere else only if you
want to edit plugins without root.

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
