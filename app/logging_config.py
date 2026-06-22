"""Centralized logging setup.

Logs are emitted as single-line JSON (per CLAUDE.md's logging convention) to
both a rotating file under ``config.APP_DATA_DIR`` and the console, so a run
can be replayed/grepped afterwards without re-running the GUI.
"""

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from typing import Any

from . import config

LOGGER_NAME = "gui_transcription"

_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        extra_fields = getattr(record, "extra_fields", None)
        if extra_fields:
            payload.update(extra_fields)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, sort_keys=True, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the 'gui_transcription' logger tree. Safe to call more than
    once - only the first call has any effect."""
    global _configured
    if _configured:
        return
    _configured = True

    config.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

    formatter = JsonFormatter()

    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a logger for a module under the 'gui_transcription' package.

    Pass the module's ``__name__`` - since every module already lives under
    the ``gui_transcription`` package, its dotted name is already a child of
    the ``LOGGER_NAME`` logger configured in setup_logging(), so no extra
    prefixing is needed (and would double it up).
    """
    return logging.getLogger(name)


def extra(**fields: Any) -> dict[str, dict[str, Any]]:
    """Build the 'extra' kwarg for structured fields, e.g.
    logger.info("ocr done", extra=extra(image=name, paragraphs=3))."""
    return {"extra_fields": fields}
