"""Orchestration glue tying OCR, HTML parsing, and output writing together.

Split into pieces the GUI can drive explicitly:

- ``parse_start_date`` / ``run_ocr_batch`` run before any user interaction
  (the latter on a background thread, reporting progress via callback).
- ``build_review_items`` turns the approved messages + OCR'd paragraphs into
  a flat list the review screen displays all at once: each item gets an
  editable text box for its own message text (whenever it has any) and a
  separate one for its image's OCR text (whenever it has an image) - a
  message with both gets both boxes, independently editable, mirroring
  Discord's own text-above-image layout. Nothing is written to disk until
  the user reviews everything and clicks Finalize.
- ``write_all_items`` writes every message's final lines (using whatever the
  user edited, or the original message/OCR text if they left it alone)
  once, in order, when Finalize is clicked.
- ``finalize_run`` performs the regex cleanup pass, records the new run
  date, and bookmarks the output file with a fresh BREAK marker.
"""

import datetime
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pyperclip

from . import cleanup, config, logging_config, ocr, state
from .chatlog import MessageEntry

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
    """Append one message's lines to the output file, matching the original
    padding/format (blank-line separators)."""
    with open(output_path, "a", encoding="utf8") as f:
        if lines_to_write:
            f.write("\n\n\n\n")

        for line in lines_to_write:
            f.write(line)

        if lines_to_write:
            f.write("\n\n")

    logger.debug("wrote message lines", extra=logging_config.extra(line_count=len(lines_to_write)))


@dataclass
class ReviewItem:
    """One row of the review screen: a message, paired with up to two
    independently-editable text boxes - one initialized from the message's
    own original text (``initial_message_text``), one from its image's OCR
    paragraphs joined with blank lines (``initial_ocr_text``). Either can
    be None, but never both: ``initial_message_text`` is None only for an
    image message with no caption (nothing to edit there), and
    ``initial_ocr_text`` is None only for a text-only message (no image to
    OCR) - see build_review_items. A message with both a caption and an
    image gets both boxes."""

    entry: MessageEntry
    image_path: Optional[Path]
    initial_message_text: Optional[str]
    initial_ocr_text: Optional[str]

    @property
    def message_id(self) -> str:
        return self.entry.message_id


def build_review_items(
    entries: list[MessageEntry],
    file_info: dict[str, list[str]],
    image_folder: Path,
) -> list[ReviewItem]:
    """Pair each approved message with its image (if any) and its editable
    text box(es): a copy of the message's own original text (unstripped, so
    deliberate spacing carries over) whenever it has any, and/or that
    image's joined OCR text whenever it has an image - independently, so a
    message with both gets both."""
    items: list[ReviewItem] = []
    for entry in entries:
        if entry.image_name is None:
            items.append(
                ReviewItem(
                    entry=entry,
                    image_path=None,
                    initial_message_text="\n".join(entry.text_lines),
                    initial_ocr_text=None,
                )
            )
            continue

        paragraphs = file_info.get(entry.image_name, [])
        # Tesseract output routinely ends with a blank line, which
        # split_into_paragraphs turns into a trailing empty-after-strip
        # paragraph - join naively and that becomes a literal "\n\n" tail on
        # initial_ocr_text, which then carries through to the final output
        # unless the user happens to manually trim it. Dropping empty
        # paragraphs (wherever they fall, not just at the end) avoids that
        # without changing how real paragraph breaks are rendered.
        initial_ocr_text = "\n\n".join(
            stripped for para in paragraphs if (stripped := para.strip())
        )
        initial_message_text = "\n".join(entry.text_lines) if entry.text_lines else None
        items.append(
            ReviewItem(
                entry=entry,
                image_path=image_folder / entry.image_name,
                initial_message_text=initial_message_text,
                initial_ocr_text=initial_ocr_text,
            )
        )

    image_items = sum(1 for item in items if item.image_path is not None)
    logger.info(
        "built review items",
        extra=logging_config.extra(total_items=len(items), image_items=image_items),
    )
    return items


def lines_for_item(
    item: ReviewItem,
    edited_message_text: Optional[str] = None,
    edited_ocr_text: Optional[str] = None,
) -> list[str]:
    """Build the final lines to write for one review item, using each
    edited_* value in place of the corresponding original text if the user
    changed it. The message-text block (if this item has one) is written
    first, then the OCR block (if it has one) - text above image, mirroring
    both Discord's own layout and the review screen's box order."""
    lines: list[str] = []

    if item.initial_message_text is not None:
        text = edited_message_text if edited_message_text is not None else item.initial_message_text
        if text:
            lines.extend(line + "\n" for line in text.split("\n"))

    if item.image_path is not None:
        ocr_text = edited_ocr_text if edited_ocr_text is not None else item.initial_ocr_text
        if ocr_text:
            lines.append(ocr_text + "\n")

    return lines


def write_all_items(
    output_path: Path,
    items: list[ReviewItem],
    edited_texts: list[tuple[Optional[str], Optional[str]]],
) -> None:
    """Write every review item's final lines, in order, in one pass.
    edited_texts is one (edited_message_text, edited_ocr_text) pair per
    item, matching ReviewFrame.collect_edited_texts."""
    logger.info("finalizing: writing all review items", extra=logging_config.extra(item_count=len(items)))
    edited_count = sum(1 for m, o in edited_texts if m is not None or o is not None)
    for item, (edited_message_text, edited_ocr_text) in zip(items, edited_texts):
        write_message_lines(output_path, lines_for_item(item, edited_message_text, edited_ocr_text))
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

    with open(output_path, "r", encoding="utf8") as f:
        raw = f.read()

    just_added = raw.split(config.BREAK_MARKER)[-1]
    pyperclip.copy(just_added)

    with open(output_path, "a", encoding="utf8") as f:
        f.write(f"\n\n\n{config.BREAK_MARKER}\n\n\n")

    logger.info(
        "run finalized",
        extra=logging_config.extra(added_chars=len(just_added), added_lines=len(just_added.splitlines())),
    )
    return just_added
