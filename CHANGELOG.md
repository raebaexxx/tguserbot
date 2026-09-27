# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
semantic versioning for the plugin API surface (`userbot.plugin_api.__all__`).

## [Unreleased]

### Fixed

Four defects reproduced before being fixed; each has a regression test.

- **Zombie plugin contexts after an interrupted reload.** A reload cancelled
  during shutdown left the manager pointing at the old, already-stopped
  generation while the dispatcher and the Telethon client still held the new
  one. The new context was never deactivated and survived shutdown as a live
  event handler. `CancelledError` is a `BaseException`, so the existing
  `except Exception` never saw it. Reload is now resumable, its rollback runs
  uninterruptibly, the watcher quiesces instead of cancelling, and
  `PluginContext.shutdown()` always deactivates even if a previous call was
  interrupted.
- **`notes delete` always reported success.** `Storage.execute` returned
  `cursor.lastrowid`, which SQLite leaves pointing at an unrelated earlier
  INSERT for DELETE and UPDATE. Split into `execute` (rows affected) and
  `execute_insert` (new rowid).
- **The rate limiter was global rather than per key.** The inter-key delay was
  awaited while holding the single bookkeeping lock, so three unrelated
  operations queued at 0.40s, 0.80s and 1.20s instead of starting together.
  The delay is now taken outside the lock, the slot is reserved so concurrent
  callers on one key queue properly, and the key map is LRU-bounded.
- **The TikTok progress reporter spammed `edit_message`.** The throttle was
  skipped once the percentage reached 100 and the reporter was only cancelled
  after the upload, producing roughly two edits per second for the whole upload
  and overwriting the final status. Telegram throttles edits, and the account is
  what gets limited.

Also fixed:

- A session lock is now enforced with `flock`; two processes against one session
  file used to be prevented only by a README warning.
- `connect` now uses `catch_up=True`. The default silently dropped commands sent
  while the process was down.
- `health` follows connect/disconnect transitions, so `/ub status` no longer
  reports "подключён" after the network drops.
- The `.env` parser no longer writes into `os.environ`, so a second settings
  file no longer silently reuses the first one's credentials.
- Git fetches are bounded by a timeout, allow-list comparison is canonical, only
  HTTPS and SSH are permitted with git's global configuration disabled, subpaths
  are containment-checked, refs are validated, and staged revisions plus
  abandoned clone directories are pruned. Pruning previously ordered revisions
  by commit SHA, which carries no ordering.
- A revoked session no longer leaves the session lock held, which made the next
  start fail with a bogus "another process" error.
- A fresh checkout with no data directory failed with a bare SQLite
  "unable to open database file", because Telethon opens the session inside the
  client constructor.
- `SIGINT` during startup exits 130 after a clean shutdown instead of exiting 0
  with live state.
- The local-plugin watcher compared file metadata before hashing, was not
  configurable, and had no debounce, so an editor's atomic save triggered
  several full reloads.
- A watcher tick failure is contained instead of killing the task.
- `/ub plugin disable <typo>` no longer creates a permanent phantom row, and
  `/ub plugin` reports real errors (`TGUSERBOT_GIT_ALLOWED_REPOS`, unknown
  plugin, not a Git plugin) instead of a generic message.
- `notes delete` on a note in another chat no longer claims success.
- An echoed argument longer than Telegram's limit is clamped.
- An operator override in `TGUSERBOT_DISABLED_PLUGINS` is reported when
  `/ub plugin enable` is refused, instead of silently reappearing disabled
  after a restart.

### Security

- Each plugin now gets its own SQLite file under `data/plugin-data/`. A plugin
  can no longer read or drop the core bookkeeping tables, and cannot collide
  with another plugin's table names. `ATTACH`, `PRAGMA`, `VACUUM`, and stacked
  statements are refused, which also closes the `ATTACH` path to the core
  database.
- The shipped unit no longer hands the service user write access to its own
  checkout.
- `git` runs with `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` disabled and
  `GIT_ALLOW_PROTOCOL=https:ssh`, so an operator's `url.*.insteadOf` rule cannot
  rewrite an allow-listed URL into an executable `ext::` helper.
- A world-readable `.env` is reported at startup.
- The process no longer mutates `os.environ` to configure yt-dlp.

### Deployment

- `TimeoutStopSec` 30 → 120, and shutdown is bounded by one shared 25-second
  deadline with parallel plugin unload. Four plugins at 10s of task cancellation
  plus 10s of `stop()` each is 80s, so systemd was `SIGKILL`ing the process
  mid-unload and `Plugin.stop()` never ran.
- `StartLimitBurst`/`StartLimitIntervalSec`: a revoked session exits instantly,
  and `Restart=on-failure` restarted it every five seconds forever.
- `MemoryMax`, `LimitNOFILE`, and further systemd hardening directives.
- The unit now states that `ProtectSystem=strict` makes the local-plugin watcher
  inert and documents the workaround; both READMEs previously promised automatic
  reload that could not happen.
- `install.sh` preflights `git`, `python3`, and `python3-venv`, honours
  `TGUSERBOT_APP_DIR`, and creates the log directory.
- `userbotctl` derives "system mode" from the same answer as `APP_DIR` (the two
  disagreed), reads the log path from the environment file instead of
  hardcoding `<app>/var/logs`, creates the log directory, runs `auth` under a
  pseudo-terminal, and health-checks after `update` with a rollback.

### Added

- Per-plugin configuration: `[config]` in `plugin.toml` for defaults and
  `<data_dir>/plugin-config.toml` for per-installation overrides, with typed
  accessors and a type check at load time.
- `/ub version`, reporting the running build and the storage schema version.
- `/ub plugin list`, `/ub health` in `userbotctl`, JSON log output, a heartbeat
  file, per-command timeouts, cooldowns, and duplicate-invocation coalescing.
- A stable public plugin API surface in `userbot/__init__.py`, plus `py.typed`.
- Storage schema versioning, replacing an inline `ALTER TABLE` with a
  `try`/`except`.
- An `AuthKeyUnregisteredError` path with a distinct exit code and an actionable
  message.
- CI: `mypy src plugins tests`, `ruff format --check`, a coverage gate, a
  shellcheck job, `pip-audit`, Python 3.14, and a concurrency group.
- An MIT `LICENSE`.

### Changed

- Test coverage from 54% to 90%, with a hard floor of 85%. `git_source.py`, the
  most security-relevant module, had no tests at all.
- Tests are hermetic: they build their own plugin trees instead of loading the
  repository's `plugins/`, and no longer depend on the working directory.
- `pytest-asyncio` replaces hand-rolled `asyncio.run` wrappers.
- yt-dlp browser impersonation is actually enabled, which is what `curl-cffi`
  was pinned for. The `YTDLP_NO_PLUGINS` hack and the mistyped `plugin_dirs`
  option were removed: neither had any effect on the library code path.

### Migration

Per-plugin data moved from the shared `userbot.sqlite3` into
`data/plugin-data/<name>/plugin.sqlite3`. A pre-existing `notes` table in the
core database is left untouched and no longer read; re-create the notes you
want to keep with `/ub notes add`. `plugin_state`, `plugin_migrations`, and
`plugin_kv` are migrated in place and the schema version is recorded in
`core_schema`.
