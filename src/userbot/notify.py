from __future__ import annotations

import logging
import os

logger = logging.getLogger("userbot.notify")

#: The file the heartbeat timestamp is written to, and its format: a Unix time
#: on a single line, so any monitoring tool can read it.
NOTIFY_SOCKET_ENV = "NOTIFY_SOCKET"
WATCHDOG_PID_ENV = "WATCHDOG_PID"


class SystemdNotifier:
    """Sends ``READY=1`` and watchdog pings over the systemd notify socket.

    A no-op outside systemd, and a no-op when the unit does not set
    ``WatchdogSec`` (``WATCHDOG_PID`` is only exported in that case). Every
    failure is swallowed: liveness reporting must never be the reason the bot
    goes down.
    """

    def __init__(self) -> None:
        self._socket = os.environ.get(NOTIFY_SOCKET_ENV)
        self._watchdog = bool(os.environ.get(WATCHDOG_PID_ENV))
        self._ready_sent = False
        self.pings = 0

    @property
    def watchdog_enabled(self) -> bool:
        return self._watchdog and self._socket is not None

    def _send(self, message: str) -> bool:
        if not self._socket:
            return False
        import socket

        address = self._socket
        try:
            if address.startswith("@"):
                # Abstract namespace: the leading NUL is the address marker.
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
                sock.connect("\0" + address[1:])
            else:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
                sock.connect(address)
            try:
                sock.sendall(message.encode("utf-8"))
            finally:
                sock.close()
        except (OSError, ValueError) as exc:
            logger.debug("sd_notify failed: %s", exc)
            return False
        return True

    def ready(self, status: str | None = None) -> bool:
        """Tell systemd the service is up. ``Type=notify`` waits for this."""
        if not self._socket:
            return False
        self._ready_sent = True
        message = "READY=1"
        if status:
            message += f"\nSTATUS={status}"
        return self._send(message)

    def stopping(self, status: str | None = None) -> bool:
        if not self._socket:
            return False
        message = "STOPPING=1"
        if status:
            message += f"\nSTATUS={status}"
        return self._send(message)

    def status(self, status: str) -> bool:
        """Replace the status text ``systemctl status`` shows. Nothing else.

        Used to explain a watchdog that has stopped being fed, which otherwise
        looks like an unexplained restart in the journal.
        """
        if not self._socket:
            return False
        return self._send(f"STATUS={status}")

    def ping(self) -> bool:
        """Keep the watchdog fed. No-op when no watchdog is configured."""
        if not self.watchdog_enabled:
            return False
        self.pings += 1
        return self._send("WATCHDOG=1")

    def reset(self) -> bool:
        """Ask systemd to reset the start rate limiter after a clean start.

        Deliberately carries no ``STATUS``: ``systemctl status`` keeps the last
        status text it was handed, so a "recovered" message here would leave the
        service looking like it is still recovering long after it is healthy. The
        caller sends its real status in the following :meth:`ready`.
        """
        if not self._socket:
            return False
        return self._send("RESET=1")
