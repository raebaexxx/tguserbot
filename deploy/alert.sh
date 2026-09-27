#!/usr/bin/env bash
# Notify the owner that the service died.
#
# `Restart=on-failure` with StartLimitBurst=5 means systemd eventually gives up
# and the bot stays down; without this nobody finds out. Wired up via
# `OnFailure=` in the unit.
#
# Runs as the service user so it can reuse the session directory. The target is
# TGUSERBOT_ALERT_CHAT in /etc/tguserbot/userbot.env.
set -uo pipefail

SERVICE="${1:-tguserbot.service}"
PYTHON="/opt/tguserbot/.venv/bin/python"
JOURNAL_FILE="$(mktemp)"
trap 'rm -f "${JOURNAL_FILE}"' EXIT

# A missing excerpt must not stop the alert: the operator still needs to know
# the service is down. The unit grants the systemd-journal group so this
# normally succeeds.
if ! journalctl -u "${SERVICE}" --since "-10 min" --no-pager >"${JOURNAL_FILE}" 2>/dev/null; then
  echo "alert: cannot read the journal; sending the alert without it" >&2
  : >"${JOURNAL_FILE}"
fi

if [[ ! -r /etc/tguserbot/userbot.env ]]; then
  echo "alert: cannot read /etc/tguserbot/userbot.env" >&2
  exit 1
fi

if ! grep -q '^TGUSERBOT_ALERT_CHAT=.' /etc/tguserbot/userbot.env; then
  echo "alert: TGUSERBOT_ALERT_CHAT is not set; nothing to do" >&2
  exit 0
fi

if [[ ! -x "${PYTHON}" ]]; then
  echo "alert: ${PYTHON} is missing" >&2
  exit 1
fi

exec "${PYTHON}" - "${SERVICE}" "${JOURNAL_FILE}" <<'PYTHON'
import asyncio
import sys

from telethon import TelegramClient

SERVICE, JOURNAL = sys.argv[1], sys.argv[2]


def read_env(path: str = "/etc/tguserbot/userbot.env") -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return values


def excerpt(path: str, limit: int = 15) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = [line.rstrip() for line in handle.read().splitlines() if line.strip()]
    except OSError:
        return "  (journal unavailable)"
    tail = lines[-limit:]
    return "\n".join(f"  {line}" for line in tail) if tail else "  (journal empty)"


async def main() -> int:
    env = read_env()
    raw = env.get("TGUSERBOT_ALERT_CHAT", "").strip()
    if not raw:
        print("alert: TGUSERBOT_ALERT_CHAT is empty", file=sys.stderr)
        return 0
    try:
        chat = int(raw)
    except ValueError:
        print(f"alert: TGUSERBOT_ALERT_CHAT is not an id: {raw!r}", file=sys.stderr)
        return 1

    api_id = env.get("TGUSERBOT_API_ID", "0").strip()
    api_hash = env.get("TGUSERBOT_API_HASH", "").strip()
    data_dir = env.get("TGUSERBOT_DATA_DIR", "/var/lib/tguserbot").strip()
    try:
        api_id_int = int(api_id)
    except ValueError:
        print(f"alert: TGUSERBOT_API_ID is not an id: {api_id!r}", file=sys.stderr)
        return 1

    client = TelegramClient(
        f"{data_dir}/alert-session",
        api_id_int,
        api_hash,
        device_model="tguserbot-alert",
        app_version="0.2.0",
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("alert: the alert session is not authorized", file=sys.stderr)
            return 1
        await client.send_message(
            chat,
            f"⚠️ {SERVICE} failed and systemd has given up restarting it.\n\n"
            f"{excerpt(JOURNAL)}",
        )
    except Exception as exc:  # noqa: BLE001 - alerting must never raise
        print(f"alert: could not send: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return 0


sys.exit(asyncio.run(main()))
PYTHON
