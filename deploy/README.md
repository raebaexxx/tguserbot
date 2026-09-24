# Server deployment

The server deployment intentionally uses systemd instead of Docker.

## Requirements

- Ubuntu/Debian-like systemd host
- Python 3.12+
- Git
- Outbound access to Telegram and GitHub

## Install

Clone this repository on the server or copy the checkout to `/opt/tguserbot`,
then run:

```bash
sudo bash deploy/install.sh
```

The installer creates:

- system user `tguserbot`;
- application directory `/opt/tguserbot`;
- configuration directory `/etc/tguserbot`;
- persistent data directory `/var/lib/tguserbot`;
- systemd unit `tguserbot.service`.

It does not create Telegram credentials and does not start the service.

## Configure and authenticate

Edit `/etc/tguserbot/userbot.env` as root. Put the `api_id` and `api_hash` there;
do not put them in Git or send them in chat. Then authenticate interactively:

```bash
sudo -u tguserbot /opt/tguserbot/.venv/bin/python -m userbot auth
```

After the first successful login, start the service:

```bash
systemctl start tguserbot
systemctl status tguserbot
journalctl -u tguserbot -f
```

The first start is intentionally read-only apart from the three demo commands.

## Updating

For core changes:

```bash
sudo -u tguserbot git -C /opt/tguserbot pull --ff-only
sudo -u tguserbot /opt/tguserbot/.venv/bin/python -m pytest
sudo systemctl restart tguserbot
```

Local plugin-only changes are detected by the watcher. Git plugin updates are
never automatic; use the owner-only `/ub plugin update <name>` command after
reviewing the commit.
