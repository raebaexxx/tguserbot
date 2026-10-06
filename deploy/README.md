# Server deployment

The server deployment intentionally uses systemd instead of Docker.

## Requirements

- Ubuntu/Debian-like systemd host
- Python 3.12+ with `venv` support (`apt-get install -y git python3 python3-venv`)
- `ffmpeg` only if you use a plugin that merges media formats
- Outbound access to Telegram and GitHub

The installer checks these up front and reports what is missing, rather than
failing somewhere inside `pip`.

## Install

```bash
sudo bash deploy/install.sh
```

The installer creates:

- system user `tguserbot`;
- application directory `/opt/tguserbot`;
- configuration directory `/etc/tguserbot`;
- persistent data directory `/var/lib/tguserbot`, including `logs/`;
- the systemd unit `tguserbot.service` and the `/usr/local/bin/tguserbotctl`
  symlink.

It does not create Telegram credentials. It is idempotent: re-running it updates
the code, keeps your existing `userbot.env`, and re-installs the unit.

Re-running it against a live service **stops the service first** and starts it
again afterwards, and only if it was running to begin with. The pull rewrites the
tree the running watcher reads from, so pulling underneath a live bot can leave it
reloading plugins out of a half-updated checkout. A bot that was stopped stays
stopped.

Ownership is deliberate: `/opt/tguserbot` stays root-owned and only `.venv`
belongs to the service user. An earlier version chowned the whole checkout,
which let a compromised bot rewrite its own code and break the next
`git pull --ff-only`.

## Configure and authenticate

Edit `/etc/tguserbot/userbot.env` as root and put the `api_id` and `api_hash`
there. Do not put them in Git or send them in chat.

```bash
sudoedit /etc/tguserbot/userbot.env
sudo chmod 640 /etc/tguserbot/userbot.env
```

Then install, authorize, and start everything with one command:

```bash
sudo tguserbotctl first-run
```

`first-run` installs, runs the interactive login, and starts the service. The
login runs under a pseudo-terminal so the phone number and 2FA password prompts
work; run it yourself with `sudo tguserbotctl auth` if you would rather see each
step.

For subsequent starts:

```bash
sudo tguserbotctl start
```

Useful commands:

```bash
sudo tguserbotctl status
sudo tguserbotctl logs
sudo tguserbotctl restart
sudo tguserbotctl update
sudo tguserbotctl health
```

## What runs on first start

`notes`, `status`, and `echo` are demo plugins and are loaded by default, so
they are present in production. `tiktok` is also loaded by default and will
download and upload files when a command is used. If you do not want the demo
commands in production, disable them in `userbot.env`:

```bash
TGUSERBOT_DISABLED_PLUGINS=echo,notes,tiktok
```

A plugin listed there cannot be re-enabled at runtime; remove it from the list
first and restart.

## Hot reload on a server

The watcher is **on by default** (`TGUSERBOT_WATCH` defaults to `1`) and it works
against the read-only `/opt/tguserbot/plugins` — it only reads. What does not work
is pulling underneath it: a `git pull` performed while the service is running looks
exactly like a plugin edit. The running process still holds the old modules in
memory while the new sources on disk import from them, and the reload fails on a
tree that is not broken:

```text
Cannot load plugin sum: cannot import name 'ModelRouter' from 'userbot.gemini'
```

`tguserbotctl update` stops the service before pulling, so this cannot happen
through the supported path. If you pull by hand, stop it yourself first.

To let the watcher actually reload, nothing is required — it already does, from a
read-only tree. `ProtectSystem=strict` makes `/opt/tguserbot/plugins` read-only,
and the watcher only reads: the scan takes file metadata and bytes, and the loader
compiles the sources in memory rather than importing through a bytecode cache. If
you would rather not run `git` as root to edit a plugin, point the plugin
directory somewhere writable and copy your plugins there:

```toml
TGUSERBOT_PLUGIN_DIR=/var/lib/tguserbot/plugins
```

`/var/lib/tguserbot` is already in the unit's `ReadWritePaths`. Alternatively set
`TGUSERBOT_WATCH=0` to switch the watcher off entirely.

Git plugin updates are never automatic; use the owner-only
`/ub plugin update <name>` command after reviewing the commit.

## Updating

```bash
sudo tguserbotctl update
```

This **stops the service**, pulls the latest code, reinstalls the locked
dependencies, starts it again, and waits for a health check. If the service does
not come up — or if the pull or the install fails part-way — it restores the
previous commit and starts the service on that, so a bad update does not leave a
crash loop or a stopped bot.

To inspect or change the configuration the bot actually sees:

```text
/ub version
/ub plugins
/ub status
```

## Service behaviour worth knowing

- `TimeoutStopSec=120`. The app bounds its own shutdown with a single shared
  deadline of 25 seconds and unloads plugins in parallel, so a slow plugin can
  no longer push the process past the limit and get `SIGKILL`ed mid-unload.
- `StartLimitBurst=5` per 300 seconds. Without it, a revoked session exits
  immediately and `Restart=on-failure` restarts it every five seconds forever.
- `MemoryMax=1G`. `PrivateTmp` means plugin downloads land in a tmpfs, so a
  large file is RAM, not disk.
- The session file is protected by a `flock`, so a second `userbot run` against
  the same data directory fails immediately instead of corrupting the auth key.
- `Type=notify` with `WatchdogSec=60`. The process pings systemd from the same
  tick that refreshes the heartbeat file under `/var/lib/tguserbot`, so a wedged
  plugin or a starved event loop is restarted within a minute instead of leaving
  a service that looks healthy and answers nothing.
- `OnFailure=` runs `deploy/alert.sh` once systemd has given up restarting. Set
  `TGUSERBOT_ALERT_CHAT` in `userbot.env` to a chat ID to be told; leave it
  empty and the hook exits quietly.

```bash
# enable alerting
sudoedit /etc/tguserbot/userbot.env   # TGUSERBOT_ALERT_CHAT=123456789
sudo systemctl daemon-reload
```

The alert sends through the bot's own session, taking the same `.lock` the
gateway takes. It used to open a separate `alert-session` file, which nothing ever
authorized — `tguserbotctl auth` writes `session` — so the hook exited non-zero
having sent nothing, which is the worst way for an alert to fail. Reusing the
session is safe because `OnFailure=` only fires once the service has already
failed, and the lock covers the brief window while that process is being reaped;
if the lock cannot be taken within 5s the alert proceeds anyway, since failing to
warn is worse than warning with the lock held.
