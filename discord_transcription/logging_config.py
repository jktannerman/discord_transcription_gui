"""Centralized logging setup.

Logs are emitted as single-line JSON (per CLAUDE.md's logging convention) to
a rotating file under ``config.APP_DATA_DIR``, so a run can be replayed/
grepped afterwards without re-running the GUI. Warnings and errors also go
to the console.
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
# Deliberately NOT a child of LOGGER_NAME (e.g. "discord_transcription.scroll_trace") - it gets
# its own handlers/file (see setup_logging) and must not also propagate up
# into LOGGER_NAME's handlers, which would defeat the point of splitting it
# out from app.log in the first place.
TRACE_LOGGER_NAME = "scroll_trace"

_configured = False
# Stamped onto every log line (see JsonFormatter) so a multi-run log file -
# or LOG_FILE and SCROLL_TRACE_LOG_FILE side by side - can be filtered down
# to one run without having to re-derive line offsets by grepping for
# "application starting" each time.
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

    # Much higher-frequency than LOG_FILE (a single scroll gesture can fire
    # dozens of these), so it gets its own file/rotation budget rather than
    # competing with - and potentially evicting - LOG_FILE's lower-volume
    # lifecycle events. Not attached to the console handler: this volume of
    # output would drown out everything else printed there.
    #
    # The rotation budget and an on/off switch live in config
    # (SCROLL_TRACE_*).
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
    """The review screen's dedicated logger for high-frequency per-scroll-
    tick tracing (reconcile/debounce/remeasure/image-load/box-resize
    events) - routed to SCROLL_TRACE_LOG_FILE instead of LOG_FILE, see
    setup_logging."""
    return logging.getLogger(TRACE_LOGGER_NAME)


def get_logger(name: str) -> logging.Logger:
    """Return a logger for a module under the discord_transcription package.

    Pass the module's ``__name__`` - since every module already lives under
    the ``discord_transcription`` package, its dotted name is already a child of the
    ``LOGGER_NAME`` logger configured in setup_logging(), so no extra
    prefixing is needed (and would double it up).

    The one exception is main.py: if it's run directly with
    `python -m discord_transcription.main` (rather than via the installed console-script entry
    point, which imports it as a normal module), Python sets *that* one
    module's ``__name__`` to ``"__main__"`` rather than ``"discord_transcription.main"`` - a
    logger built from it would have no relation to the package's logger tree
    that setup_logging() attaches handlers to, and its records would
    silently vanish into the unconfigured root logger instead of reaching
    the file/console handlers. Fall back to LOGGER_NAME so it's still a
    child of the configured logger.
    """
    if name == "__main__":
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(name)


def extra(**fields: Any) -> dict[str, dict[str, Any]]:
    """Build the 'extra' kwarg for structured fields, e.g.
    logger.info("ocr done", extra=extra(image=name, paragraphs=3))."""
    return {"extra_fields": fields}


def text_fingerprint(text: Optional[str]) -> dict[str, Any]:
    """Compact, log-friendly stand-in for a text box's full content: its
    length plus a short hash, so two log lines can be compared for exact
    equality (e.g. "is this box's content the same before and after a row
    rebuild?") without dumping - and potentially truncating - the full text
    into every line. None is reported as ``{"len": None, "hash": None}``,
    distinct from an empty string (``{"len": 0, "hash": <hash of "">}``) -
    the two mean different things (never touched vs. cleared)."""
    if text is None:
        return {"len": None, "hash": None}
    return {"len": len(text), "hash": hashlib.md5(text.encode("utf8")).hexdigest()[:8]}
