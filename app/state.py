"""Persistent state for the transcription tool.

Replaces the original script's two external files
(``trans_v3_run_dates.txt`` and the pickled ``unedited_trans_file.txt``)
with JSON equivalents under ``config.APP_DATA_DIR``, so the cached OCR data
can be inspected/edited as plain text and isn't tied to pickle's
compatibility constraints.
"""

import json
from pathlib import Path
from typing import Optional

from . import config, logging_config

logger = logging_config.get_logger(__name__)


def _ensure_data_dir() -> None:
    config.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)


def read_last_run_date() -> Optional[str]:
    """Return the most recent recorded end date, or None if none recorded yet."""
    if not config.RUN_DATE_FILE.exists():
        logger.info("no run-date file found, no previous run date")
        return None

    with open(config.RUN_DATE_FILE, "r", encoding="utf8") as f:
        dates = json.load(f)

    last_date = dates[-1] if dates else None
    logger.info("read last run date", extra=logging_config.extra(last_date=last_date))
    return last_date


def append_run_date(date_str: str) -> None:
    """Append a new end date to the run-date log."""
    _ensure_data_dir()

    dates = []
    if config.RUN_DATE_FILE.exists():
        with open(config.RUN_DATE_FILE, "r", encoding="utf8") as f:
            dates = json.load(f)

    dates.append(date_str)

    with open(config.RUN_DATE_FILE, "w", encoding="utf8") as f:
        json.dump(dates, f, indent=2)

    logger.info("appended run date", extra=logging_config.extra(date=date_str))


def load_recent_paths(field: str) -> list:
    """Return the cached recent values for a setup-screen field (e.g.
    "html_path"), most-recently-used first. Empty list if none recorded yet."""
    if not config.RECENT_PATHS_FILE.exists():
        return []

    with open(config.RECENT_PATHS_FILE, "r", encoding="utf8") as f:
        data = json.load(f)

    return data.get(field, [])


def add_recent_path(field: str, value: str) -> None:
    """Record value as the most-recently-used entry for field, deduping
    against earlier entries and capping the history length."""
    if not value:
        return
    _ensure_data_dir()

    data = {}
    if config.RECENT_PATHS_FILE.exists():
        with open(config.RECENT_PATHS_FILE, "r", encoding="utf8") as f:
            data = json.load(f)

    paths = data.get(field, [])
    if value in paths:
        paths.remove(value)
    paths.insert(0, value)
    data[field] = paths[: config.MAX_RECENT_PATHS]

    with open(config.RECENT_PATHS_FILE, "w", encoding="utf8") as f:
        json.dump(data, f, indent=2)

    logger.info("recorded recent path", extra=logging_config.extra(field=field, value=value))


def read_approved_users_state() -> Optional[dict]:
    """Return {"text": str, "use_all_users": bool} as last saved from the
    setup screen, or None if it has never been saved (first run) - callers
    distinguish that from an intentionally-emptied field by checking for
    None rather than treating an empty/falsy result as "never saved"."""
    if not config.APPROVED_USERS_STATE_FILE.exists():
        return None

    with open(config.APPROVED_USERS_STATE_FILE, "r", encoding="utf8") as f:
        data = json.load(f)

    return {"text": data.get("text", ""), "use_all_users": data.get("use_all_users", False)}


def save_approved_users_state(text: str, use_all_users: bool) -> None:
    """Persist the setup screen's approved-users field verbatim, plus the
    "all users" toggle, so the next run can be pre-filled with exactly what
    was used this time."""
    _ensure_data_dir()

    with open(config.APPROVED_USERS_STATE_FILE, "w", encoding="utf8") as f:
        json.dump({"text": text, "use_all_users": use_all_users}, f, indent=2)

    logger.info(
        "saved approved-users setup state",
        extra=logging_config.extra(use_all_users=use_all_users, line_count=len(text.splitlines())),
    )


def load_cache(folder_path: str) -> Optional[dict]:
    """Return the cached {image_name: [paragraphs]} dict for folder_path.

    Returns None if there is no cache, or the cache was written for a
    different image folder.
    """
    if not config.OCR_CACHE_FILE.exists():
        logger.info("no ocr cache file found")
        return None

    with open(config.OCR_CACHE_FILE, "r", encoding="utf8") as f:
        cache = json.load(f)

    if cache.get("folder") != str(Path(folder_path)):
        logger.info(
            "ocr cache exists but is for a different folder",
            extra=logging_config.extra(
                requested_folder=str(Path(folder_path)), cached_folder=cache.get("folder")
            ),
        )
        return None

    data = cache.get("data")
    logger.info(
        "loaded ocr cache",
        extra=logging_config.extra(folder=str(Path(folder_path)), image_count=len(data or {})),
    )
    return data


def save_cache(folder_path: str, data: dict) -> None:
    """Persist the {image_name: [paragraphs]} dict for folder_path."""
    _ensure_data_dir()

    cache = {"folder": str(Path(folder_path)), "data": data}

    with open(config.OCR_CACHE_FILE, "w", encoding="utf8") as f:
        json.dump(cache, f, indent=2)

    logger.info(
        "saved ocr cache",
        extra=logging_config.extra(folder=str(Path(folder_path)), image_count=len(data)),
    )
