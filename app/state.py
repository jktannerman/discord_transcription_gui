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

from . import config


def _ensure_data_dir() -> None:
    config.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)


def read_last_run_date() -> Optional[str]:
    """Return the most recent recorded end date, or None if none recorded yet."""
    if not config.RUN_DATE_FILE.exists():
        return None

    with open(config.RUN_DATE_FILE, "r", encoding="utf8") as f:
        dates = json.load(f)

    return dates[-1] if dates else None


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


def load_cache(folder_path: str) -> Optional[dict]:
    """Return the cached {image_name: [paragraphs]} dict for folder_path.

    Returns None if there is no cache, or the cache was written for a
    different image folder.
    """
    if not config.OCR_CACHE_FILE.exists():
        return None

    with open(config.OCR_CACHE_FILE, "r", encoding="utf8") as f:
        cache = json.load(f)

    if cache.get("folder") != str(Path(folder_path)):
        return None

    return cache.get("data")


def save_cache(folder_path: str, data: dict) -> None:
    """Persist the {image_name: [paragraphs]} dict for folder_path."""
    _ensure_data_dir()

    cache = {"folder": str(Path(folder_path)), "data": data}

    with open(config.OCR_CACHE_FILE, "w", encoding="utf8") as f:
        json.dump(cache, f, indent=2)
