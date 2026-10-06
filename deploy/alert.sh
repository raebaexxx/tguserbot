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
import contextlib
import fcntl
import os
import sys
import time

from telethon import TelegramClient

SERVICE, JOURNAL = sys.argv[1], sys.argv[2]

#: How long to wait for the dead process to be reaped and release the lock.
#: Matches ``LOCK_TIMEOUT`` in ``userbot.gateway`` -- the same single-writer rule,
#: because this is the same session.
LOCK_TIMEOUT = 5.0


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


@contextlib.contextmanager
def hold_session_lock(session_path: str):
    """Take the bot's session lock, or refuse to touch the session.

    A Telegram session is single-writer: two processes on one auth key corrupt it.
    ``OnFailure=`` can fire while the failed process is still being reaped, so the
    lock is taken the same way ``gateway.acquire_session_lock`` takes it -- by the
    same name, so the two cannot disagree about which file it is.

    Best-effort by design, because refusing to alert is worse than alerting: if the
    lock file cannot be created at all the alert still runs, and says so.
    """
    handle = None
    try:
        handle = open(f"{session_path}.lock", "a+b")  # noqa: SIM115 - released below
    except OSError as exc:
        print(f"alert: cannot open the session lock, continuing anyway: {exc}", file=sys.stderr)
        yield None
        return
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                print(
                    f"alert: another process still holds {session_path}.lock; "
                    "continuing anyway",
                    file=sys.stderr,
                )
                yield handle
                return
            time.sleep(0.2)
    try:
        yield handle
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            handle.close()


async def main() -> int:
    env = read_env()
    raw = env.get("TGUSERBOT_ALERT_CHAT", "").strip()
    if not raw:
        print("alert: TGUSERBOT_ALERT_CHAT is empty", file=sys.stderr)
        return 1
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

    # The bot's own session, not a separate one.
    #
    # This used to open ``<data_dir>/alert-session``, which nothing in this
    # repository ever authorizes: ``auth`` writes ``<data_dir>/session``. So
    # ``is_user_authorized()`` was False on every run and the alert exited 1 having
    # sent nothing -- the worst failure mode for an alert, since it looks like it
    # worked. Reusing the bot's session is safe here precisely because the service
    # has already failed, and it is the one credential known to be authorized.
    session_path = f"{data_dir}/session"
    if not os.path.exists(f"{session_path}.session"):
        print(
            f"alert: {session_path}.session does not exist; run `tguserbotctl auth` first",
            file=sys.stderr,
        )
        return 1

    with hold_session_lock(session_path):
        client = TelegramClient(
            session_path,
            api_id_int,
            api_hash,
            device_model="tguserbot-alert",
            app_version="0.2.0",
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                print("alert: the bot session is not authorized", file=sys.stderr)
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