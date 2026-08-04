"""Orchestration glue tying OCR, HTML parsing, and output writing together.

Split into pieces the GUI can drive explicitly:

- ``parse_start_date`` / ``run_ocr_batch`` run before any user interaction
  (the latter on a background thread, reporting progress via callback).
- ``review_item.build_review_items`` turns the approved messages + OCR'd
  paragraphs into the flat list the review screen displays all at once -
  see that module for the ``ReviewItem``/spacer-slot domain model this glue
  hands off to and back from. Nothing is written to disk until the user
  reviews everything and clicks Finalize.
- ``write_all_items`` writes every message's final lines (via
  ``review_item.lines_for_item``, using whatever the user edited, or the
  original message/OCR text if they left it alone) once, in order, when
  Finalize is clicked.
- ``finalize_run`` performs the regex cleanup pass, records the new run
  date, and bookmarks the output file with a fresh BREAK marker.
"""

import datetime
import os
import re
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


def write_message_lines(output_path: Path, lines_to_write: list[str]) -> None:
    """Append one review item's lines to the output file verbatim - no
    padding is added here. Spacing between/within items is now entirely
    owned by that item's own spacer slots (see ReviewItem.slot_roles and
    lines_for_item), so each chunk in lines_to_write already carries
    whatever newlines its surrounding spacers decided on."""
    with open(output_path, "a", encoding="utf8") as f:
        for line in lines_to_write:
            f.write(line)

    logger.debug("wrote message lines", extra=logging_config.extra(line_count=len(lines_to_write)))


def write_all_items(
    output_path: Path,
    items: list[ReviewItem],
    edited_texts: list[dict[str, Optional[str]]],
) -> None:
    """Write every review item's final chunks, in order, in one pass.
    edited_texts is one role->text dict per item, matching
    ReviewFrame.collect_edited_texts."""
    logger.info("finalizing: writing all review items", extra=logging_config.extra(item_count=len(items)))
    edited_count = sum(1 for edited in edited_texts if any(v is not None for v in edited.values()))
    for item, edited in zip(items, edited_texts):
        write_message_lines(output_path, lines_for_item(item, edited))
    logger.info(
        "finished writing all review items",
        extra=logging_config.extra(item_count=len(items), edited_count=edited_count),
    )


def finalize_run(output_path: Path, html_file_path: Path) -> str:
    """Clean up the output file, record the new run date, and bookmark it
    with a fresh BREAK marker. Returns the text added during this run
    (also copied to the clipboard)."""
    logger.info("finalizing run", extra=logging_config.extra(output_path=str(output_path)))

    with open(output_path, "r", encoding="utf8") as f:
        raw = f.read()

    cleaned = cleanup.clean_transcript(raw)

    with open(output_path, "w", encoding="utf8") as f:
        f.write(cleaned)

    end_time = os.path.getmtime(html_file_path)
    end_date = datetime.datetime.fromtimestamp(end_time, tz=datetime.timezone.utc)
    state.append_run_date(end_date.strftime("%Y-%m-%d-%H-%M-%S"))

    # cleaned is already exactly what output_path now holds on disk (nothing
    # else writes to it between the write above and here) - re-reading it
    # back would just reproduce the same string from a second disk read.
    just_added = cleaned.split(config.BREAK_MARKER)[-1]
    pyperclip.copy(just_added)

    with open(output_path, "a", encoding="utf8") as f:
        f.write(f"\n\n\n{config.BREAK_MARKER}\n\n\n")

    logger.info(
        "run finalized",
        extra=logging_config.extra(added_chars=len(just_added), added_lines=len(just_added.splitlines())),
    )
    return just_added
