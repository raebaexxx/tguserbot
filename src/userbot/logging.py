from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

LOGGER_NAME = "userbot"

#: Loggers that share this module's handlers.
#:
#: ``telethon`` is here because it logs under ``telethon.*`` while the ``userbot``
#: logger carries ``propagate=False`` and ``root`` is never configured, so
#: everything Telethon wrote below WARNING was formatted by nobody and dropped by
#: ``lastResort``. Those are precisely the lines that explain a bot that is alive,
#: connected and no longer updating: "Cannot get difference since Telegram is
#: having issues", "Got difference for account updates", "Reconnecting to new data
#: center", the connection lifecycle. A two-day outage in which the journal held
#: nothing at all was a direct consequence of them going nowhere.
TELETHON_LOGGER_NAME = "telethon"
ATTACHED_LOGGERS = (LOGGER_NAME, TELETHON_LOGGER_NAME)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log shippers and ``journalctl -o cat``."""

    _RESERVED = {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    *,
    json: bool = False,
) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    resolved = getattr(logging, str(level).upper(), None)
    resolved_level = resolved if isinstance(resolved, int) else logging.INFO
    logger.setLevel(resolved_level)
    logger.propagate = False

    # The same handlers go on both loggers, so the two are cleared together: the
    # stream and the file are shared objects, and leaving a closed one attached to
    # ``telethon`` would keep writing every one of its lines into a file this call
    # just abandoned.
    attached = [logging.getLogger(name) for name in ATTACHED_LOGGERS]
    for target in attached:
        target.setLevel(resolved_level)
        target.propagate = False
        for handler in list(target.handlers):
            target.removeHandler(handler)
            handler.close()

    if json:
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    handlers[0].setFormatter(formatter)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "userbot.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    except OSError:
        logger.warning("cannot open the log file in %s; continuing with stderr only", log_dir)
    else:
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    for handler in handlers:
        for target in attached:
            target.addHandler(handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)
