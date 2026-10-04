"""Structured logging setup.

Log records are emitted in a compact key=value format so they stay greppable in
a terminal and machine-parsable in a log pipeline, e.g.::

    [DISCOVERY] candidates=142 accepted=8 rejected=134
    [ALERT] universe=123456 level=HOT
    [ALERT] skipped universe=123456 reason=cooldown

Secrets (Discord token, database password, cookies) are never passed to the
logging subsystem; :func:`redact` exists as a defensive helper for the rare case
where a URL with embedded credentials must be printed.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

from scoutforge.config.settings import Settings, get_settings

_RESERVED = frozenset(
    {
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
)

_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|cookie|authorization|api[_-]?key|credential)",
    re.IGNORECASE,
)
_URL_CREDENTIALS_RE = re.compile(r"://([^:/@\s]+):([^@/\s]+)@")

#: Keys that must never be written to logs even if a caller passes them.
BLOCKED_KEYS = frozenset(
    {
        "discord_token",
        "token",
        "password",
        "secret",
        "cookie",
        "cookies",
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
    }
)


def redact(value: str) -> str:
    """Strip embedded credentials from URLs and mask long opaque secrets."""
    value = _URL_CREDENTIALS_RE.sub(r"://\1:***@", value)
    if len(value) > 24 and " " not in value and re.fullmatch(r"[A-Za-z0-9._\-]+", value):
        return f"{value[:4]}***{value[-2:]}"
    return value


def _sanitize_extra(extra: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in extra.items():
        if key in _RESERVED:
            continue
        if key.lower() in BLOCKED_KEYS or _SECRET_KEY_RE.search(key):
            clean[key] = "***"
        elif isinstance(value, str):
            clean[key] = redact(value)
        elif isinstance(value, (int, float, bool, type(None), list, tuple, dict)):
            clean[key] = value
        else:
            clean[key] = str(value)
    return clean


class KeyValueFormatter(logging.Formatter):
    """``[LEVEL] logger key=value key=value`` formatter."""

    def __init__(self, use_json: bool = False) -> None:
        super().__init__()
        self.use_json = use_json

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        extra = _sanitize_extra(record.__dict__)
        extra.pop("levelname", None)
        exc = ""
        if record.exc_info:
            exc = "\n" + self.formatException(record.exc_info)

        if self.use_json:
            payload: dict[str, Any] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
                **extra,
            }
            return json.dumps(payload, default=str) + exc

        if extra:
            rendered = " ".join(f"{k}={v}" for k, v in sorted(extra.items()))
            return f"[{record.levelname}] {record.name}: {message} {rendered}{exc}"
        return f"[{record.levelname}] {record.name}: {message}{exc}"


def setup_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """Configure the root logger.

    Args:
        settings: Optional settings override. Defaults to the process singleton.
        force: Replace existing handlers even if logging was already configured.
    """
    settings = settings or get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    if root.handlers and not force:
        root.setLevel(level)
        return

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(KeyValueFormatter(use_json=settings.log_json))
    root.addHandler(handler)
    root.setLevel(level)

    # Third-party libraries are noisy at DEBUG/INFO.
    logging.getLogger("discord").setLevel(max(level, logging.WARNING))
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.database.echo else logging.WARNING
    )


def get_logger(name: str) -> logging.Logger:
    """Return a module logger."""
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, tag: str, **fields: Any) -> None:
    """Emit a tagged structured event.

    Example::

        log_event(log, logging.INFO, "DISCOVERY", candidates=142, accepted=8)
    """
    logger.log(level, "[%s]", tag, extra=fields)
