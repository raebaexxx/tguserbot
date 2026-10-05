# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
semantic versioning for the plugin API surface (`userbot.plugin_api.__all__`).

## [Unreleased]

### Added

- `ai` plugin: Gemini chat with a persisted conversation, plus on-demand plugin
  generation. Streaming is throttled to one edit per `min_edit_interval` seconds,
  because Telegram counts message edits and the earlier edit-per-callback
  reporter in the tiktok plugin caused a real rate-limit incident.
- Plugin generation writes **only** to `<data_dir>/plugin-staging/`, which is not a
  plugin root, so nothing a model produces can run until the owner types
  `/ub plugin adopt <name>`. Adoption runs a static review and refuses a plugin
  that reaches for a shell, a socket, a native library, or the session file.
- `safety.py`: an AST review of plugin source. A denylist, not a sandbox, and it
  says so in the docstring.
- `installed_plugin_dir` joins `plugin_dir` as a plugin root, under the writable
  data directory. Writing adopted plugins into the shipped tree would work in
  development and fail in production, where `ProtectSystem=strict` makes it
  read-only.
- `PluginConfig.float_value`, for settings that are not whole numbers.
- `ModelRouter`: tries models in order and remembers the one that answered.
  A model's allowance is counted per model, so one configured model is not a
  fallback — when it runs out the only thing left to do is fail. That is not
  hypothetical: the configured default was being refused, and every question
  after it produced an error until a second model was tried.
- `model_fallbacks` in the `ai` and `sum` manifests. When a fallback answers, the
  plugin says so under the reply, and stays silent when the configured model was
  the one that worked. When every model is refused, the error names them.

### Fixed

- **`/ub tt` answered in the main thread even when asked inside a topic.** The
  video did not bypass the topic wrapper: it went through it, and the file path
  inside `topics.py` failed. `TelegramClient._file_to_media` returns
  `(file_handle, media, as_image)`, and the whole tuple was passed as the `media`
  field of `SendMediaRequest`, which cannot serialise it. The raise was caught by
  the fallback — a plain `event.respond(file=...)` — so the video arrived in the
  chat's main thread, which is the reported symptom one layer below where it was
  looked for. The media is now taken out of the tuple, and a `None` media is an
  error rather than something to send. The test double in `tests/test_topics.py`
  returned a bare media object, i.e. it agreed with the bug; it now returns the
  real three-tuple and serialises each request, and a second test builds the
  request with a real `TelegramClient` subclass to keep the shape honest.
- **A revoked Telegram session left the service looking healthy forever.** The
  watchdog ping was sent unconditionally, so if Telegram invalidated the auth key
  server-side — which Telethon cannot reconnect from — the process kept feeding
  `WATCHDOG=1` while answering nothing: never restarted, no error anywhere, and
  `systemctl status` green. The heartbeat now withholds the ping once the
  connection has been down for `DISCONNECT_GRACE` (180s, read against Telethon's
  own reconnection budget so a healthy reconnect is never cut short) and says so
  in the journal and in the unit's status text. systemd then restarts the process,
  and the start path reports the revoked session. The grace is not a restart
  threshold for blips: inside it the watchdog is still fed on purpose, which is
  the case the other test pins.
- **A spent quota was retried four times with backoff.** `429` was classified as
  retryable, so a condition that cannot recover was given four attempts and the
  real answer was delayed to arrive as an error. `QuotaExhausted` is now separate
  from a rate limit worth waiting out, which shares the status and the
  `RESOURCE_EXHAUSTED` code.
- **"Quota exhausted" was the wrong thing to tell the user.** The live refusal
  reads `You exceeded your current quota, please check your plan and billing
  details` and then, underneath, `Quota exceeded for metric:
  generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20
  ... Please retry in 56.601092868s`. That is twenty requests a minute, not a
  daily allowance. The headline is identical in both cases, so the retry hint is
  what decides: under `SWITCH_AFTER_SECONDS` (10s, a judgement call and documented
  as one) the plugin waits, above it it moves to the next model. The wait rides
  along on the exception so the reply can say "try again in a minute" rather than
  sending the reader to a billing page that will look normal.
- **`/ub ai` was broken by the change that was meant to fix it.** The plugins
  passed `model=` to the router, which binds `model` itself for each attempt, so
  every chat and every generation raised `TypeError: got multiple values for
  keyword argument 'model'`. The suite stayed green because the test double
  forwards straight to a fake client that accepts anything. Each role now owns a
  chain, and `tests/test_gemini_wiring.py` runs the real plugin against a real
  router over a mock transport, so the seam is no longer the one thing untested.
- **Attachment downloads in `sum` never worked.** `download_media` is a coroutine
  function in telethon; running it in a thread returned a coroutine, which `Path()`
  rejected, and the coroutine was then never awaited. Reported as a
  `TypeError` with a `RuntimeWarning` in the journal, and as a generic
  "ошибка" in Telegram.
- **Gemini failures were visible only to whoever was in the chat.** The reason was
  shown to the user and then discarded, so the journal showed a clean run and the
  next question about it had to be answered by asking for a screenshot. Both
  plugins now log the refusal with its reason. The router already logged the quota
  retries; this covers everything it passes straight through, such as a bad key.
- **`timeout_seconds` stopped being read.** It was in both manifests and honoured
  by the plugins, and building the client inside the router dropped it — leaving the
  setting in the file doing nothing and quietly putting `sum`, which allows 180
  seconds because it carries a transcript and media, back to the 120 second
  default. The router takes a timeout again, and `None` defers to the client's own
  default rather than keeping a second copy of the number to drift.
- **Every deploy produced a confusing traceback.** `tguserbotctl update` pulled
  while the service was live. The plugin watcher is on by default, so a pull looks
  like a plugin edit: the running process holds the old modules in memory while
  the new sources on disk import from them, and the reload fails with
  `Cannot load plugin sum: cannot import name 'ModelRouter'` on a tree that is not
  broken. The service is stopped before the pull now, which moves the burden onto
  the failure paths — `set -e` would otherwise exit with the bot stopped — so the
  pull and both installs are checked, and every failure restores the previous
  commit and starts the service. The deploy README also claimed the watcher
  "cannot hot-reload" under `ProtectSystem=strict`, which is not what happens: it
  cannot *write*, and it tries.

- **Streaming progress never appeared in production.** The ai plugin edited its
  placeholder with `event.edit_text`, and telethon's `Message` has no such
  method -- it is called `edit`. Every progress update raised `AttributeError`,
  the editor marked itself broken on the first token, and the final answer still
  arrived because that goes out as a new message, so nothing looked wrong from
  the outside. `userbot.messaging.edit_message` now knows both spellings and
  returns whether the edit worked, and both plugins use it.
- **`SupportsRespond` promised a method that does not exist.** The protocol
  declared `edit_text`, so a plugin could type-check against a fiction and fail
  at runtime. Trimmed to what the object actually has, with a pointer to the
  helper.
- **Every `/ub ...` command answered in the main thread, even when written inside
  a forum topic.** Telegram carries the topic on the message's reply header, and
  telethon 1.45 reads it and then drops it: both `send_message` and `send_file`
  build `InputReplyToMessage(reply_to)` from a single integer, so `top_msg_id`
  never reaches Telegram. There is no public telethon call that targets a topic,
  so `userbot.topics` builds the request itself and wraps both reply paths --
  commands in the dispatcher, and raw event handlers in `PluginContext`, which is
  how the `echo` plugin's "pong" was also landing in the wrong place. Anything
  beyond a plain text message is passed to telethon untouched, rather than partly
  handled and partly dropped.

  A fresh message in a topic turned out to be a second reading of the same
  header, not a missing one: Telegram sets `reply_to_top_id` only on a message
  that is itself a reply, and a message posted fresh in a topic points at the
  topic's root with `forum_topic` set instead. The first attempt read only
  `reply_to_top_id`, concluded the topic was unknowable, and shipped a fix that
  only covered replies. Both forms are read now, and the flag is what keeps an
  ordinary reply in a plain group from being mistaken for a topic. Which thread
  was chosen is logged on every reply, because the first report of this was
  diagnosed twice by guesswork before the real header was read off a live forum.
- **`/ub ai <question>` sent the placeholder and then nothing.** Every delivery
  path went through `placeholder.edit_text()` -- the progress updates, the final
  answer, and even the "the model returned no text" notice -- so when the
  placeholder turned out not to be editable, all of them failed together and the
  user got `…` and silence. The exceptions were swallowed at DEBUG, so the
  journal was empty and there was no trace of the failure anywhere. The answer is
  now sent as a new message, the operation that is known to work, and the
  placeholder is deleted afterwards; a failed edit is reported once at WARNING
  instead of being absorbed. Progress rendering is still attempted and still
  throttled, and stops retrying as soon as it is known to be broken.
- **`/ub tt <link>` reported "Не удалось скачать TikTok" with the video already
  downloaded.** `max_downloads: 1` in the plugin's own options made yt-dlp raise
  `MaxDownloadsReached` -- its normal way of saying "I have what you asked for,
  stop" -- and TikTok resolves through the playlist machinery, so that happened
  after a *single* video. Every exception from `extract_info` was treated as a
  failure. The option was also redundant: `noplaylist` is what actually prevents
  a playlist. Removed, and the exception is now handled as the control flow it
  is: the file scan decides whether it really succeeded.
- A failed TikTok download logged the class name and no message
  (`TikTok download failed: TikTokDownloadError`), which made the above
  undiagnosable from the server. The reason is now logged.
- A mistyped mode (`/ub ai flash ...`) is now answered as a question, with a
  one-line note about the mode that was probably meant. The suggestion only
  fires on a single-edit near-miss: at two, "list", "note", "code" and "more"
  would all be flagged, and commenting on an ordinary question is worse than
  answering it.
- A staged plugin path of `../x.py` or an absolute path is now rejected before
  anything is written, and the resolved location is re-checked against the
  staging root afterwards.

## [0.2.0] - 2026-09-27

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
- yt-dlp's `impersonate` option was passed as a string. yt-dlp only converts the
  string form in its CLI entry point, which this project does not use, so
  `YoutubeDL.__init__` raised `AssertionError` and **every** download failed.
  The option is now an `ImpersonateTarget`, and impersonation degrades to a
  warning when `curl-cffi` is missing rather than crashing.
- `PluginManager.shutdown()` was not authoritative: a `/ub plugin install` that
  was in flight could publish its runtime *after* shutdown had snapshotted the
  loaded names, leaving that plugin's commands and handlers live. Shutdown now
  sets a flag that refuses late loads and waits for in-flight work first.
- `gateway.connect()` no longer passes `catch_up` to `TelegramClient.connect()`,
  which takes no arguments in Telethon 1.45 and crashed the service on every
  start. Catch-up is a constructor option (`catch_up=True`, since Telethon
  defaults it to `False`) plus an explicit `client.catch_up()` once plugins have
  attached their handlers. A test now asserts every Telethon call site against
  the installed library's signatures, so a mocked test cannot hide this again.
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

- `sum` plugin: `/ub sum` summarises the last messages in the chat where the
  command was typed, including voice notes and video messages. The mime types
  were verified against the live model with real messages from the bot's own
  chats -- a voice note is OGG/Opus and declares `audio/ogg`, a "кружок" is MP4 --
  because a wrong mime type does not fail loudly, it just makes the model quietly
  omit the attachment. Media is **off by default**: a summary sends other people's
  messages to a third party. What did not fit the budget is listed in the reply
  rather than dropped.
- `userbot.gemini`: the Gemini client, moved out of the `ai` plugin so two plugins
  can share one implementation of the parts that are awkward (a routine 503, the
  `alt=sse` requirement, the `thoughtSignature` echo). `Part` now carries inline
  media as well as text.
- `userbot.messaging`: `edit_message` and `delete_message`, which work whether
  the object underneath is a telethon or a pyrogram message, and report failure
  instead of raising.
- Failure alerting: `OnFailure=` runs `deploy/alert.sh` once systemd gives up
  restarting, sending the journal excerpt to `TGUSERBOT_ALERT_CHAT`. Without a
  chat ID the hook exits quietly.
- Liveness supervision: the unit is `Type=notify` with `WatchdogSec=60`, pinged
  from the same tick as the heartbeat file, so a wedged process is restarted
  rather than sitting there looking healthy. Previously the heartbeat had a
  producer and no consumer.
- Structural types for the plugin boundary: `ctx.client`, `ctx.dispatcher` and
  `ctx.manager` are `Protocol`s, so mypy verifies plugin code instead of
  reporting success on `Any`.
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

- Development tooling moved up a major version: mypy 2.3, pytest 9.1,
  pytest-asyncio 1.4, pytest-cov 7.1. The pins were written when the project
  started and had not been moved, so four tools were a major behind. pytest-asyncio
  1.x drains the event loop at teardown, which surfaced two tests that leaked a
  background task swallowing cancellation forever; they now release the task
  instead of abandoning it. `pytest-timeout` stays on `<2.5` because 2.5.0 was
  yanked upstream for an accidental breaking change.
- `requirements.lock` now carries hashes, which means it no longer contains the
  editable `-e .` line (a local directory cannot be hashed). The install scripts
  do the two steps separately: `--require-hashes` for dependencies, then
  `--no-deps -e .` for the project.
- `uv.lock` is regenerated and a test fails if it drifts from `pyproject.toml`
  again; four dev dependencies had been added without updating it.
- Test coverage from 54% to 91%, with a hard floor of 85%. `git_source.py`, the
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
