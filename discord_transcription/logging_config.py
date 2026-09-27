"""Centralized logging setup.

Logs are single-line JSON records in a rotating file under
``config.APP_DATA_DIR``. Warnings and errors also go to the console. The
review screen's trace goes to a separate file (see get_trace_logger).
"""

import hashlib
import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from . import config

# The package's name - every module's __name__ is
# "discord_transcription.something", so this is always their common
# ancestor logger.
LOGGER_NAME = "discord_transcription"
# Not a child of LOGGER_NAME, so trace records don't also reach app.log.
TRACE_LOGGER_NAME = "scroll_trace"

_configured = False
# Stamped onto every log line (see JsonFormatter), so either log file can be
# filtered down to one run.
_run_id = ""


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": _run_id,
            "message": record.getMessage(),
        }

        extra_fields = getattr(record, "extra_fields", None)
        if extra_fields:
            payload.update(extra_fields)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, sort_keys=True, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the LOGGER_NAME logger tree, plus the separate
    TRACE_LOGGER_NAME tree (see get_trace_logger). Safe to call more than
    once - only the first call has any effect."""
    global _configured, _run_id
    if _configured:
        return
    _configured = True
    _run_id = _new_run_id()

    config.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

    formatter = JsonFormatter()

    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(config.CONSOLE_LOG_LEVEL)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False

    # A single scroll gesture can log dozens of trace events, so the trace
    # gets its own file and rotation budget (config.SCROLL_TRACE_*), and
    # never reaches the console.
    trace_logger = logging.getLogger(TRACE_LOGGER_NAME)
    trace_logger.propagate = False
    if not config.SCROLL_TRACE_ENABLED:
        trace_logger.disabled = True
        return
    trace_handler = logging.handlers.RotatingFileHandler(
        config.SCROLL_TRACE_LOG_FILE,
        maxBytes=config.SCROLL_TRACE_MAX_BYTES,
        backupCount=config.SCROLL_TRACE_BACKUP_COUNT,
        encoding="utf8",
    )
    trace_handler.setFormatter(formatter)
    trace_logger.setLevel(logging.DEBUG)
    trace_logger.addHandler(trace_handler)


def resolve_log_level() -> int:
    """The level setup_logging should use for LOG_FILE.

    Returns:
        The level named by the config.LOG_LEVEL_ENV_VAR environment
        variable if set, else config.LOG_LEVEL. An unrecognised name falls
        back to INFO rather than stopping the app from starting.
    """
    name = (os.environ.get(config.LOG_LEVEL_ENV_VAR) or config.LOG_LEVEL).strip().upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def get_trace_logger() -> logging.Logger:
    """The review screen's logger for high-frequency tracing (reconcile,
    debounce, remeasure, image load and text-box events), written to
    SCROLL_TRACE_LOG_FILE instead of LOG_FILE."""
    return logging.getLogger(TRACE_LOGGER_NAME)


def get_logger(name: str) -> logging.Logger:
    """Return a logger for a module under the discord_transcription package.

    Pass the module's ``__name__``, which is already a child of
    LOGGER_NAME. The exception is ``"__main__"`` (main.py run with
    ``python -m``), which would be outside the configured logger tree, so
    it gets LOGGER_NAME itself.

    Args:
        name: The calling module's ``__name__``.

    Returns:
        The logger to use.
    """
    if name == "__main__":
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(name)


def extra(**fields: Any) -> dict[str, dict[str, Any]]:
    """Build the 'extra' kwarg for structured fields, e.g.
    logger.info("ocr done", extra=extra(image=name, paragraphs=3))."""
    return {"extra_fields": fields}


def text_fingerprint(text: Optional[str]) -> dict[str, Any]:
    """A text's length plus a short hash, so log lines can show whether a
    box's content changed without logging the text itself. None gives
    ``{"len": None, "hash": None}``, distinct from an empty string."""
    if text is None:
        return {"len": None, "hash": None}
    return {"len": len(text), "hash": hashlib.md5(text.encode("utf8")).hexdigest()[:8]}
