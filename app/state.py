"""Persistent state for the transcription tool.

Replaces the original script's two external files
(``trans_v3_run_dates.txt`` and the pickled ``unedited_trans_file.txt``)
with JSON equivalents under ``config.APP_DATA_DIR``, so the cached OCR data
can be inspected/edited as plain text and isn't tied to pickle's
compatibility constraints.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, logging_config

logger = logging_config.get_logger(__name__)


def _ensure_data_dir() -> None:
    config.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str, backup_path: Optional[Path] = None) -> None:
    """Replace path's contents with text without ever leaving a truncated or
    partial file in its place.

    Writes to a temp file in the same directory, fsyncs it so the bytes are
    actually on disk, then os.replaces it into path. os.replace is atomic on
    Windows/POSIX, so a crash at any point leaves either the old file or the
    new one intact - never a half-written one.

    Args:
        path: The file to write. Its directory must already exist.
        text: The full new contents.
        backup_path: If given, whatever currently occupies path is first
            rotated there, so even a bad *new* write (not just a crash
            mid-write) leaves the previous version recoverable.

    Raises:
        OSError: If writing fails. path is left as it was.
    """
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=path.stem + "_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

        if backup_path is not None and path.exists():
            os.replace(path, backup_path)
        os.replace(tmp_path, path)
    except OSError:
        logger.error("failed to write %s", path, exc_info=True)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data) -> None:
    """Write data to path as JSON via atomic_write_text, rotating the
    previous version to a .bak sibling (read back by
    _read_json_with_backup if the primary file is ever missing/corrupt)."""
    _ensure_data_dir()
    atomic_write_text(path, json.dumps(data, indent=2), backup_path=path.with_suffix(".bak"))


def _read_json_with_backup(path: Path):
    """Read JSON from path, falling back to its .bak sibling (written by
    _atomic_write_json) if path is missing or unreadable - covers both a
    corrupt primary file and the narrow window where path has been rotated
    out but the replacement hasn't landed yet. Returns None if neither file
    is present/readable."""
    backup_path = path.with_suffix(".bak")

    if path.exists():
        try:
            with open(path, "r", encoding="utf8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("%s unreadable, trying backup", path, exc_info=True)

    if backup_path.exists():
        try:
            with open(backup_path, "r", encoding="utf8") as f:
                data = json.load(f)
            logger.warning("loaded %s from backup", path)
            return data
        except (json.JSONDecodeError, OSError):
            logger.warning("backup for %s unreadable too", path, exc_info=True)

    return None


def read_last_run_date() -> Optional[str]:
    """Return the most recent recorded end date, or None if none recorded yet."""
    dates = _read_json_with_backup(config.RUN_DATE_FILE)
    if dates is None:
        logger.info("no run-date file found, no previous run date")
        return None

    last_date = dates[-1] if dates else None
    logger.info("read last run date", extra=logging_config.extra(last_date=last_date))
    return last_date


def append_run_date(date_str: str) -> None:
    """Append a new end date to the run-date log."""
    dates = _read_json_with_backup(config.RUN_DATE_FILE) or []
    dates.append(date_str)
    _atomic_write_json(config.RUN_DATE_FILE, dates)

    logger.info("appended run date", extra=logging_config.extra(date=date_str))


def load_recent_paths(field: str) -> list:
    """Return the cached recent values for a setup-screen field (e.g.
    "html_path"), most-recently-used first. Empty list if none recorded yet."""
    data = _read_json_with_backup(config.RECENT_PATHS_FILE)
    if data is None:
        return []

    return data.get(field, [])


def add_recent_path(field: str, value: str) -> None:
    """Record value as the most-recently-used entry for field, deduping
    against earlier entries and capping the history length."""
    if not value:
        return

    data = _read_json_with_backup(config.RECENT_PATHS_FILE) or {}

    paths = data.get(field, [])
    if value in paths:
        paths.remove(value)
    paths.insert(0, value)
    data[field] = paths[: config.MAX_RECENT_PATHS]

    _atomic_write_json(config.RECENT_PATHS_FILE, data)

    logger.info("recorded recent path", extra=logging_config.extra(field=field, value=value))


def read_approved_users_state() -> Optional[dict]:
    """Return {"text": str, "use_all_users": bool} as last saved from the
    setup screen, or None if it has never been saved (first run) - callers
    distinguish that from an intentionally-emptied field by checking for
    None rather than treating an empty/falsy result as "never saved"."""
    data = _read_json_with_backup(config.APPROVED_USERS_STATE_FILE)
    if data is None:
        return None

    return {"text": data.get("text", ""), "use_all_users": data.get("use_all_users", False)}


def save_approved_users_state(text: str, use_all_users: bool) -> None:
    """Persist the setup screen's approved-users field verbatim, plus the
    "all users" toggle, so the next run can be pre-filled with exactly what
    was used this time."""
    _atomic_write_json(config.APPROVED_USERS_STATE_FILE, {"text": text, "use_all_users": use_all_users})

    logger.info(
        "saved approved-users setup state",
        extra=logging_config.extra(use_all_users=use_all_users, line_count=len(text.splitlines())),
    )


def load_session(html_path: str) -> Optional[dict]:
    """Return the saved in-progress review session for html_path, or None if
    there isn't one for that specific chatlog (no prior run for it, or its
    last run finished/was finalized normally). Sessions saved for other
    chatlogs, if any, are unaffected either way."""
    sessions = _read_json_with_backup(config.SESSIONS_FILE)
    if not isinstance(sessions, dict):
        logger.info("no sessions file found")
        return None

    session = sessions.get(str(Path(html_path)))
    if session is None:
        return None

    logger.info(
        "loaded saved session",
        extra=logging_config.extra(html_path=html_path, output_path=session.get("output_path")),
    )
    return session


def save_session(html_path: str, session: dict) -> None:
    """Persist the in-progress review session (run inputs, per-item edits,
    focus/scroll position) for html_path, overwriting only that chatlog's
    previously saved session - sessions saved for other chatlogs are kept
    alongside it indefinitely, so two different chatlogs can each be
    partially transcribed and resumed independently. This is the highest-
    value target for crash safety in the whole app: it's autosaved every
    few seconds while reviewing a transcript that may represent hours of
    OCR + correction work, and the app can be closed (or crash) at any
    instant mid-write. _atomic_write_json's fsync + rename means that never
    corrupts the file in place, and the .bak rotation means even a write
    that completes but encodes a bad/incomplete in-memory session still
    leaves the previous-known-good sessions recoverable on the next
    resume-prompt rather than discarding all progress outright."""
    sessions = _read_json_with_backup(config.SESSIONS_FILE)
    if not isinstance(sessions, dict):
        sessions = {}
    sessions[str(Path(html_path))] = session
    _atomic_write_json(config.SESSIONS_FILE, sessions)

    logger.debug(
        "saved session",
        extra=logging_config.extra(
            html_path=html_path,
            output_path=session.get("output_path"),
            item_count=len(session.get("edited_texts") or []),
        ),
    )


def clear_session(html_path: str) -> None:
    """Remove the saved in-progress session for html_path only - called
    once that chatlog's run is finalized, since there's nothing left to
    resume for it. Sessions saved for other chatlogs are left in place.
    The just-removed session is archived first (see
    archive_session_backup) - finalizing (or declining to resume a pending
    one) is itself the end of a session, the same as the cases handled at
    the load_session call site in main_window.py."""
    sessions = _read_json_with_backup(config.SESSIONS_FILE)
    if not isinstance(sessions, dict):
        return

    key = str(Path(html_path))
    if key not in sessions:
        return

    archive_session_backup(html_path, sessions[key])

    del sessions[key]
    _atomic_write_json(config.SESSIONS_FILE, sessions)

    logger.info("cleared saved session", extra=logging_config.extra(html_path=html_path))


def archive_session_backup(html_path: str, session: dict) -> None:
    """Record `session` (html_path's saved session, as it looked right
    before it stopped being the live in-progress one) into its rotating
    end-of-session backup history - kept separately from SESSIONS_FILE so
    these survive being overwritten by whatever the *next* session
    autosaves. Keeps only the config.SESSION_BACKUP_COUNT most recent
    entries per html_path, most-recent-first.

    Deliberately a no-op if `session` is identical to the most recently
    archived entry for this html_path: load_session's caller and
    clear_session can both end up archiving the exact same still-unedited
    session for the same chatlog in a single call sequence (e.g. a pending
    session that's loaded then immediately declined) - without this check
    that would burn a backup slot on a duplicate instead of an actually
    distinct prior session."""
    backups = _read_json_with_backup(config.SESSION_BACKUPS_FILE)
    if not isinstance(backups, dict):
        backups = {}

    key = str(Path(html_path))
    history = backups.get(key, [])
    if history and history[0] == session:
        return

    history = [session] + history
    backups[key] = history[: config.SESSION_BACKUP_COUNT]
    _atomic_write_json(config.SESSION_BACKUPS_FILE, backups)

    logger.info(
        "archived end-of-session backup",
        extra=logging_config.extra(html_path=html_path, backup_count=len(backups[key])),
    )


def load_session_backups(html_path: str) -> list:
    """Return html_path's end-of-session backup history, most-recent-first
    (up to config.SESSION_BACKUP_COUNT entries) - empty list if none have
    ever been archived for it. Backups for other chatlogs, if any, don't
    affect this lookup either way."""
    backups = _read_json_with_backup(config.SESSION_BACKUPS_FILE)
    if not isinstance(backups, dict):
        return []

    return backups.get(str(Path(html_path)), [])


def load_finalized_edits(html_path: str) -> Optional[dict]:
    """Return the stored finalized edits for html_path as
    {message_id: {role: text}}, or None if there are no stored edits for
    that specific chatlog. Finalized edits for other chatlogs, if any, are
    unaffected."""
    all_finalized = _read_json_with_backup(config.FINALIZED_EDITS_FILE)
    if not isinstance(all_finalized, dict):
        return None

    data = all_finalized.get(str(Path(html_path)))
    if data is None:
        logger.info(
            "no finalized edits for this chatlog",
            extra=logging_config.extra(html_path=html_path),
        )
        return None

    logger.info(
        "loaded finalized edits",
        extra=logging_config.extra(html_path=html_path, message_count=len(data)),
    )
    return data


def save_finalized_edits(html_path: str, updates_by_message: dict) -> None:
    """Apply one Finalize's changes to the stored finalized edits for html_path.

    Roles absent from updates_by_message are left exactly as stored, so a
    box the user didn't deliberately change keeps its prior finalized edit.
    Before any stored edit is replaced by different text or removed, the
    old version is appended to config.FINALIZED_EDITS_HISTORY_FILE, and
    that history is written first - if it can't be, nothing is changed.
    Finalized edits for other chatlogs are kept alongside this one
    indefinitely.

    Args:
        html_path: The chatlog these edits belong to.
        updates_by_message: {message_id: {role: text_or_None}}. A string
            stores that text for the role; None removes any stored edit for
            it (the user deliberately reverted the box to its default).
    """
    all_finalized = _read_json_with_backup(config.FINALIZED_EDITS_FILE)
    if not isinstance(all_finalized, dict):
        all_finalized = {}

    key = str(Path(html_path))
    existing = all_finalized.get(key, {})
    replaced_at = datetime.now(timezone.utc).isoformat()
    history_entries = []
    for message_id, role_texts in updates_by_message.items():
        per_message = existing.get(message_id, {})
        for role, text in role_texts.items():
            old_text = per_message.get(role)
            if old_text is not None and old_text != text:
                history_entries.append({
                    "html_path": key,
                    "message_id": message_id,
                    "role": role,
                    "old_text": old_text,
                    "new_text": text,
                    "replaced_at": replaced_at,
                })
            if text is None:
                per_message.pop(role, None)
            else:
                per_message[role] = text
        if per_message:
            existing[message_id] = per_message
        else:
            existing.pop(message_id, None)
    all_finalized[key] = existing

    if history_entries:
        _append_finalized_edits_history(history_entries)
    _atomic_write_json(config.FINALIZED_EDITS_FILE, all_finalized)

    stored_count = sum(len(roles) for roles in existing.values())
    logger.info(
        "saved finalized edits",
        extra=logging_config.extra(
            html_path=html_path,
            message_count=len(existing),
            stored_role_count=stored_count,
            replaced_or_removed_count=len(history_entries),
        ),
    )


def _append_finalized_edits_history(entries: list) -> None:
    """Append entries to the finalized-edit history file (see
    config.FINALIZED_EDITS_HISTORY_FILE). Never removes anything."""
    history = _read_json_with_backup(config.FINALIZED_EDITS_HISTORY_FILE)
    if not isinstance(history, list):
        history = []
    history.extend(entries)
    _atomic_write_json(config.FINALIZED_EDITS_HISTORY_FILE, history)
    logger.info(
        "archived replaced/removed finalized edits",
        extra=logging_config.extra(count=len(entries), history_size=len(history)),
    )


def load_finalized_edits_history(html_path: Optional[str] = None) -> list:
    """Return the finalized-edit history, oldest first.

    Args:
        html_path: If given, only entries for this chatlog are returned.

    Returns:
        A list of {html_path, message_id, role, old_text, new_text,
        replaced_at} dicts - empty if nothing has ever been replaced.
    """
    history = _read_json_with_backup(config.FINALIZED_EDITS_HISTORY_FILE)
    if not isinstance(history, list):
        return []
    if html_path is None:
        return history
    key = str(Path(html_path))
    return [entry for entry in history if entry.get("html_path") == key]


def load_cache(folder_path: str) -> Optional[dict]:
    """Return the cached {image_name: [paragraphs]} dict for folder_path.

    Returns None if there is no cache, or no cache was ever saved for that
    specific image folder - caches for other folders, if any, don't affect
    this lookup either way.
    """
    cache = _read_json_with_backup(config.OCR_CACHE_FILE)
    if not isinstance(cache, dict):
        logger.info("no ocr cache file found")
        return None

    data = cache.get(str(Path(folder_path)))
    if data is None:
        logger.info(
            "no ocr cache for this folder",
            extra=logging_config.extra(requested_folder=str(Path(folder_path))),
        )
        return None

    logger.info(
        "loaded ocr cache",
        extra=logging_config.extra(folder=str(Path(folder_path)), image_count=len(data)),
    )
    return data


def save_cache(folder_path: str, data: dict) -> None:
    """Persist the {image_name: [paragraphs]} dict for folder_path,
    overwriting only that folder's previously cached entry - caches for
    other folders are kept alongside it indefinitely, so OCR'ing a second
    chatlog's images never forces a first chatlog's cache to be redone.
    This is the other high-value target alongside sessions - it represents
    however long the Tesseract pass over the whole image folder took, and
    losing it forces redoing OCR from scratch on the next run - so it goes
    through the same atomic write + backup rotation as sessions."""
    cache = _read_json_with_backup(config.OCR_CACHE_FILE)
    if not isinstance(cache, dict):
        cache = {}
    cache[str(Path(folder_path))] = data
    _atomic_write_json(config.OCR_CACHE_FILE, cache)

    logger.info(
        "saved ocr cache",
        extra=logging_config.extra(folder=str(Path(folder_path)), image_count=len(data)),
    )
