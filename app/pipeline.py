"""Orchestration glue tying OCR, HTML parsing, and output writing together.

Split into pieces the GUI can drive explicitly:

- ``parse_start_date`` / ``run_ocr_batch`` run before any user interaction
  (the latter on a background thread, reporting progress via callback).
- ``review_item.build_review_items`` turns the approved messages + OCR'd
  paragraphs into the flat list the review screen displays all at once -
  see that module for the ``ReviewItem``/spacer-slot domain model this glue
  hands off to and back from. Nothing is written to disk until the user
  reviews everything and clicks Finalize.
- ``finalize_run`` renders every message's final text (via
  ``review_item.lines_for_item``, using whatever the user edited, or the
  original message/OCR text if they left it alone), runs the regex cleanup
  pass, bookmarks the result with a fresh BREAK marker, and writes the
  output file in a single atomic replace - then records the new run date
  and copies the added text to the clipboard.
"""

import datetime
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pyperclip

from . import cleanup, config, logging_config, ocr, state
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

    Raises ValueError naming the offending line if a non-blank line doesn't
    start with a numeric user ID, rather than silently dropping it.
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

    The components are interpreted as UTC, not the local machine's
    timezone - matching how finalize_run records the date this field is
    normally pre-filled from (datetime.fromtimestamp(..., tz=utc)) and how
    chatlog.parse_message_groups now converts each message's own timestamp
    to a true UTC epoch via the export's declared timezone. Comparing two
    epoch seconds computed the same well-defined way is what makes the
    comparison correct regardless of what timezone either the exporting
    device or this machine happens to be in.

    Raises ValueError with a readable message on bad input, rather than the
    original script's unguarded ``int(x)`` crash.
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


def run_ocr_batch(
    image_folder: str,
    start_time: int,
    use_cache: bool,
    progress_callback: Optional[Callable[[float], None]] = None,
) -> dict[str, list[str]]:
    """Return {image_name: [paragraphs]} for every new-enough image.

    If use_cache is True and a matching cache exists for image_folder, the
    cache is returned directly and no OCR is run.
    """
    logger.info(
        "starting OCR batch",
        extra=logging_config.extra(image_folder=image_folder, start_time=start_time, use_cache=use_cache),
    )

    if use_cache:
        cached = state.load_cache(image_folder)
        if cached is not None:
            logger.info("using cached OCR data, skipping OCR batch")
            return cached

    file_info: dict[str, list[str]] = {}
    image_names = os.listdir(image_folder)
    total = len(image_names)
    skipped_type = 0
    skipped_old = 0

    for i, image_name in enumerate(image_names):
        if "." in image_name and not any(
            image_name.endswith(suffix) for suffix in config.SKIP_TYPES
        ):
            image_path = os.path.join(image_folder, image_name)
            creation_time = os.path.getctime(image_path)

            if creation_time >= start_time:
                raw_string = ocr.transcribe_image(image_path)
                file_info[image_name] = ocr.split_into_paragraphs(raw_string)
            else:
                skipped_old += 1
        else:
            skipped_type += 1

        if progress_callback and total:
            progress_callback((i + 1) / total)

    logger.info(
        "OCR batch complete",
        extra=logging_config.extra(
            total_files=total,
            transcribed=len(file_info),
            skipped_non_image_type=skipped_type,
            skipped_too_old=skipped_old,
        ),
    )

    state.save_cache(image_folder, file_info)
    return file_info


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
    the end of the run - and written with one atomic replace. That write is
    the commit point: if anything before it fails, the output file is
    untouched and the run can safely be retried. Steps after it (recording
    the run date, copying to the clipboard) can't undo the write, so their
    failures are returned as warnings instead of raised.

    Args:
        output_path: The transcript file to append to. Created if missing.
        html_file_path: The chatlog export; its mtime becomes the recorded
            run-end date (the next run's default start date).
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
    cleaned = cleanup.clean_transcript(existing + render_items(items, edited_texts))
    just_added = cleaned.split(config.BREAK_MARKER)[-1]
    # Read before the commit point, so a missing/unreadable chatlog aborts
    # cleanly rather than leaving a written output with no recorded date.
    end_time = os.path.getmtime(html_file_path)

    state.atomic_write_text(output_path, f"{cleaned}\n\n\n{config.BREAK_MARKER}\n\n\n")
    logger.info(
        "output file written",
        extra=logging_config.extra(added_chars=len(just_added), added_lines=len(just_added.splitlines())),
    )

    result = FinalizeResult(just_added=just_added)

    end_date = datetime.datetime.fromtimestamp(end_time, tz=datetime.timezone.utc)
    try:
        state.append_run_date(end_date.strftime("%Y-%m-%d-%H-%M-%S"))
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
