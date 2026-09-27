"""Orchestration glue tying HTML parsing, OCR and output writing together.

- ``prepare_run`` (on a background thread, reporting progress through a
  callback) parses the chatlog, then ``run_ocr_batch`` OCRs the images the
  kept messages reference. ``RunContext`` holds a run's inputs.
- ``review_item.build_review_items`` turns the messages and OCR text into
  the review screen's items.
- ``finalize_run`` renders every message's final text (via
  ``review_item.lines_for_item``), runs the cleanup pass, adds a fresh
  BREAK marker, and writes the output file in one atomic replace, then
  records the run date and copies the added text to the clipboard.
"""

import datetime
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import pyperclip

from . import chatlog, cleanup, config, logging_config, ocr, state
from .review_item import ReviewItem, lines_for_item

logger = logging_config.get_logger(__name__)

# Matches the leading run of digits on a setup-screen approved-users line,
# e.g. "123456789 - Alice" -> "123456789". Everything after the digits is
# just a human-readable label and is ignored for filtering purposes.
_LEADING_DIGITS_RE = re.compile(r"^\s*(\d+)")


def parse_approved_user_ids(text: str) -> set[str]:
    """Parse the setup screen's multi-line approved-users field (one user
    per line, e.g. "123456789 - Alice") into the set of Discord user IDs to
    filter the chatlog by.

    Args:
        text: The field's contents.

    Returns:
        The user IDs.

    Raises:
        ValueError: Naming the first non-blank line that doesn't start with
            a numeric user ID.
    """
    ids: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _LEADING_DIGITS_RE.match(line)
        if not match:
            logger.warning("invalid approved-user line", extra=logging_config.extra(line=line))
            raise ValueError(
                f"Could not parse user line {line!r} - expected it to start with a numeric user ID."
            )
        ids.add(match.group(1))
    return ids


def parse_start_date(date_str: str) -> int:
    """Parse a 'YYYY-MM-DD[-HH-MM-SS]' string into a unix timestamp.

    The components are read as UTC, like the run dates finalize_run
    records and the message times chatlog.parse_message_groups reads, so
    neither this machine's nor the exporting device's timezone matters.

    Args:
        date_str: The setup screen's start date field.

    Returns:
        The unix timestamp.

    Raises:
        ValueError: With a readable message, if the date can't be parsed.
    """
    parts = date_str.strip().split("-")
    try:
        numeric_parts = tuple(int(p) for p in parts)
    except ValueError:
        logger.warning("invalid start date input", extra=logging_config.extra(date_str=date_str))
        raise ValueError(
            f"Could not parse date {date_str!r} - expected numbers separated by '-'."
        )

    if not 3 <= len(numeric_parts) <= 6:
        logger.warning("invalid start date input", extra=logging_config.extra(date_str=date_str))
        raise ValueError(
            f"Could not parse date {date_str!r} - expected 3 to 6 '-'-separated "
            "numbers (year-month-day[-hour-minute-second])."
        )

    try:
        date = datetime.datetime(*numeric_parts, tzinfo=datetime.timezone.utc)
    except ValueError as exc:
        logger.warning("invalid start date input", extra=logging_config.extra(date_str=date_str))
        raise ValueError(f"Could not parse date {date_str!r}: {exc}")

    timestamp = int(date.timestamp())
    logger.info("parsed start date", extra=logging_config.extra(date_str=date_str, timestamp=timestamp))
    return timestamp


@dataclass
class OcrBatchResult:
    """The outcome of run_ocr_batch.

    Attributes:
        file_info: {image_name: [paragraphs]} for every referenced image
            found in the image folder.
        missing_images: Referenced image names with no matching file in the
            image folder, in first-reference order.
    """

    file_info: dict[str, list[str]]
    missing_images: list[str] = field(default_factory=list)


def referenced_image_names(entries: Iterable[chatlog.MessageEntry]) -> list[str]:
    """Return every image name the kept messages reference, deduplicated.

    Args:
        entries: Parsed messages, as chatlog.parse_message_groups returns.

    Returns:
        Image names in first-reference order.
    """
    return list(dict.fromkeys(name for entry in entries for name in entry.image_names))


def _cache_entry_matches(entry: dict, fingerprint: dict[str, int]) -> bool:
    """Whether a cache entry was OCR'd from the file fingerprint describes.

    An entry without a fingerprint (carried over from the version 1 cache
    format) is trusted, rather than forcing a one-off re-OCR of everything.
    """
    if "size" not in entry or "mtime_ns" not in entry:
        return True
    return entry["size"] == fingerprint["size"] and entry["mtime_ns"] == fingerprint["mtime_ns"]


def run_ocr_batch(
    image_folder: str,
    image_names: Iterable[str],
    use_cache: bool,
    progress_callback: Optional[Callable[[float], None]] = None,
) -> OcrBatchResult:
    """OCR the given images, reusing cached results where possible.

    An image is re-OCR'd if it has no cache entry, its file's size or mtime
    changed since it was cached, or use_cache is False. New results are
    merged into the folder's cache, which is saved every
    config.OCR_CACHE_SAVE_EVERY images and once more at the end (including
    when OCR fails partway through), so interrupted work isn't lost. Cache
    entries for images not in image_names are kept.

    Args:
        image_folder: Folder the images live in.
        image_names: Names of the images to OCR, relative to image_folder.
        use_cache: False to re-OCR every image regardless of the cache.
        progress_callback: Called with the fraction (0-1) of OCR work done.

    Returns:
        The OCR text per image, plus any referenced images that don't exist.
    """
    names = list(dict.fromkeys(image_names))
    logger.info(
        "starting OCR batch",
        extra=logging_config.extra(
            image_folder=image_folder, image_count=len(names), use_cache=use_cache
        ),
    )

    folder = Path(image_folder)
    cache = state.load_cache(image_folder) or {}
    file_info: dict[str, list[str]] = {}
    missing: list[str] = []
    to_ocr: list[tuple[str, Path, dict[str, int]]] = []
    cache_dirty = False

    for name in names:
        path = folder / name
        if not path.is_file():
            missing.append(name)
            continue
        stat = path.stat()
        fingerprint = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        entry = cache.get(name)
        if use_cache and entry is not None and _cache_entry_matches(entry, fingerprint):
            file_info[name] = entry["paragraphs"]
            if "size" not in entry or "mtime_ns" not in entry:
                cache[name] = {"paragraphs": entry["paragraphs"], **fingerprint}
                cache_dirty = True
        else:
            to_ocr.append((name, path, fingerprint))

    reused = len(file_info)
    unsaved = 0
    try:
        for i, (name, path, fingerprint) in enumerate(to_ocr):
            paragraphs = ocr.split_into_paragraphs(ocr.transcribe_image(str(path)))
            file_info[name] = paragraphs
            cache[name] = {"paragraphs": paragraphs, **fingerprint}
            cache_dirty = True
            unsaved += 1
            if unsaved >= config.OCR_CACHE_SAVE_EVERY:
                state.save_cache(image_folder, cache)
                cache_dirty = False
                unsaved = 0
            if progress_callback:
                progress_callback((i + 1) / len(to_ocr))
    finally:
        if cache_dirty:
            state.save_cache(image_folder, cache)

    if progress_callback and not to_ocr:
        progress_callback(1.0)

    if missing:
        logger.warning(
            "referenced images not found in image folder",
            extra=logging_config.extra(image_folder=image_folder, missing_images=missing),
        )
    logger.info(
        "OCR batch complete",
        extra=logging_config.extra(
            referenced=len(names), reused_from_cache=reused, transcribed=len(to_ocr),
            missing=len(missing),
        ),
    )
    return OcrBatchResult(file_info=file_info, missing_images=missing)


@dataclass
class RunContext:
    """The inputs a single run (OCR -> review -> finalize) was started with.

    Attributes:
        html_path: The chatlog export.
        image_folder: The folder its images were exported to.
        output_path: The transcript file Finalize appends to.
        start_time: Unix time (seconds, UTC); earlier messages are skipped.
        approved_author_ids: The Discord user IDs to keep messages from, or
            None for every author.
        use_cache: False to re-OCR every image, ignoring the cache.
    """

    html_path: Path
    image_folder: Path
    output_path: Path
    start_time: int
    approved_author_ids: Optional[set[str]]
    use_cache: bool


class RunError(Exception):
    """A run failure whose message is ready to show the user as-is."""


def prepare_run(
    run: RunContext,
    progress_callback: Callable[[float], None],
    status_callback: Callable[[str], None],
) -> tuple[list[chatlog.MessageEntry], OcrBatchResult]:
    """Parse the chatlog, then OCR the images its kept messages reference.

    Runs on the GUI's worker thread, so the callbacks must not touch Tk
    directly; they're expected to marshal onto the Tk thread themselves.

    Args:
        run: The run's inputs.
        progress_callback: Receives the fraction of OCR work done.
        status_callback: Receives a short description of the current stage.

    Returns:
        The kept messages and the OCR results for their images.

    Raises:
        RunError: The chatlog couldn't be read or parsed.
    """
    try:
        html_text = run.html_path.read_text(encoding="utf8")
    except OSError as exc:
        raise RunError(f"Could not read HTML file: {exc}") from exc
    try:
        entries = chatlog.parse_message_groups(
            html_text, run.start_time, run.approved_author_ids
        )
    except ValueError as exc:
        # parse_message_groups raises a clear ValueError for a malformed
        # export (a missing or non-snowflake per-message data-message-id).
        logger.exception("chatlog parsing failed")
        raise RunError(str(exc)) from exc
    except Exception as exc:
        logger.exception("unexpected error while parsing chatlog")
        raise RunError(f"Unexpected error while reading the chatlog: {exc!r}") from exc

    status_callback("Running OCR on images...")
    ocr_result = run_ocr_batch(
        str(run.image_folder),
        referenced_image_names(entries),
        run.use_cache,
        progress_callback=progress_callback,
    )
    return entries, ocr_result


def render_items(items: list[ReviewItem], edited_texts: list[dict[str, Optional[str]]]) -> str:
    """Render every review item's final chunks, in order, as one string.

    Args:
        items: The review items, in transcript order.
        edited_texts: One role->text dict per item, matching
            ReviewFrame.collect_edited_texts. None (or a missing role) means
            "use that slot's default text".

    Returns:
        The concatenated text for this run. No padding is added between
        items - spacing is owned entirely by each item's spacer slots (see
        ReviewItem.slot_roles and lines_for_item).
    """
    return "".join(
        chunk for item, edited in zip(items, edited_texts, strict=True) for chunk in lines_for_item(item, edited)
    )


def check_output_path(output_path: Path) -> None:
    """Check finalize_run will be able to write to output_path.

    Run when Start is clicked, so a bad output path is reported before the
    review rather than at Finalize. finalize_run reads the existing file,
    then writes its backup and a temporary file beside it, so the folder
    must be writable too, not just the file.

    Args:
        output_path: The transcript file.

    Raises:
        ValueError: With a message ready to show the user.
    """
    folder = output_path.parent
    if not folder.is_dir():
        raise ValueError(f"The output file's folder doesn't exist: {folder}")
    if not os.access(folder, os.W_OK):
        raise ValueError(f"The output file's folder isn't writable: {folder}")
    if output_path.exists():
        if not output_path.is_file():
            raise ValueError(f"The output path isn't a file: {output_path}")
        if not os.access(output_path, os.R_OK | os.W_OK):
            raise ValueError(f"The output file isn't readable and writable: {output_path}")


def output_backup_path(output_path: Path) -> Path:
    """Where finalize_run keeps the output file's previous version.

    Args:
        output_path: The transcript file.

    Returns:
        The same path with ".bak" appended, e.g. "transcript.txt.bak".
    """
    return output_path.with_name(output_path.name + ".bak")


@dataclass
class FinalizeResult:
    """Outcome of a finalize_run call that got as far as writing the output.

    Attributes:
        just_added: The text added to the output file by this run (also
            what was copied to the clipboard, if that succeeded).
        copied_to_clipboard: Whether just_added made it onto the clipboard.
        warnings: Human-readable problems from steps that ran *after* the
            output file was written. The run still counts as finalized -
            retrying it would append the same text a second time.
    """

    just_added: str
    copied_to_clipboard: bool = False
    warnings: list[str] = field(default_factory=list)


def finalize_run(
    output_path: Path,
    html_file_path: Path,
    items: list[ReviewItem],
    edited_texts: list[dict[str, Optional[str]]],
) -> FinalizeResult:
    """Append this run's transcript to the output file, then do the
    post-run bookkeeping.

    The new output is built entirely in memory - existing content plus this
    run's rendered items, cleaned up, plus a fresh BREAK marker bookmarking
    the end of the run - and written with one atomic replace, after copying
    the previous version to output_backup_path(output_path). That write is
    the commit point: if anything before it fails, the output file is
    untouched and the run can safely be retried. Steps after it (recording
    the run date, copying to the clipboard) can't undo the write, so their
    failures are returned as warnings instead of raised.

    Args:
        output_path: The transcript file to append to. Created if missing.
        html_file_path: The chatlog export; its mtime becomes the recorded
            run-end date for that chatlog (its next run's default start date).
        items: The review items, in transcript order.
        edited_texts: One role->text dict per item (see render_items).

    Returns:
        The added text, plus any post-commit warnings.

    Raises:
        OSError: If the output or chatlog file can't be read, or the output
            can't be written. The output file is unchanged in that case.
    """
    logger.info(
        "finalizing run",
        extra=logging_config.extra(output_path=str(output_path), item_count=len(items)),
    )

    existing = output_path.read_text(encoding="utf8") if output_path.exists() else ""
    cleaned = cleanup.clean_transcript(existing, render_items(items, edited_texts))
    # Taken by position, not by splitting on BREAK_MARKER: text containing
    # the marker must not cut the copy short. A run that adds nothing ends
    # up no longer than the existing content (clean_transcript also trims
    # its trailing marker), and adds nothing.
    just_added = cleaned[len(existing):] if len(cleaned) > len(existing) else ""
    # Read before the commit point, so a missing/unreadable chatlog aborts
    # cleanly rather than leaving a written output with no recorded date.
    end_time = os.path.getmtime(html_file_path)

    if output_path.exists():
        # Copied rather than moved aside, so the output file itself never
        # disappears, even briefly. A failure here aborts before the commit
        # point, like any other pre-write failure.
        shutil.copy2(output_path, output_backup_path(output_path))
    state.atomic_write_text(output_path, f"{cleaned}\n\n\n{config.BREAK_MARKER}\n\n\n")
    logger.info(
        "output file written",
        extra=logging_config.extra(added_chars=len(just_added), added_lines=len(just_added.splitlines())),
    )

    result = FinalizeResult(just_added=just_added)

    end_date = datetime.datetime.fromtimestamp(end_time, tz=datetime.timezone.utc)
    try:
        state.append_run_date(html_file_path, end_date.strftime("%Y-%m-%d-%H-%M-%S"))
    except Exception as exc:
        logger.exception("could not record run date")
        result.warnings.append(
            f"Could not record this run's end date ({exc}) - the next run's "
            "start date won't be pre-filled correctly."
        )

    try:
        pyperclip.copy(just_added)
        result.copied_to_clipboard = True
    except Exception as exc:
        logger.exception("could not copy added text to clipboard")
        result.warnings.append(f"Could not copy the new text to the clipboard ({exc}).")

    logger.info("run finalized", extra=logging_config.extra(warning_count=len(result.warnings)))
    return result
