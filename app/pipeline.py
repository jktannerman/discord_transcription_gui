"""Orchestration glue tying OCR, HTML parsing, and output writing together.

Split into pieces the GUI can drive explicitly:

- ``parse_start_date`` / ``run_ocr_batch`` run before any user interaction
  (the latter on a background thread, reporting progress via callback).
- ``build_review_items`` turns the approved messages + OCR'd paragraphs into
  a flat list the review screen displays all at once (image on one side,
  one freely-editable text box with that image's OCR text on the other).
  Nothing is written to disk until the user reviews everything and clicks
  Finalize.
- ``write_all_items`` writes every message's final lines (using whatever the
  user edited, or the original OCR text if they left it alone) once, in
  order, when Finalize is clicked.
- ``finalize_run`` performs the regex cleanup pass, records the new run
  date, and bookmarks the output file with a fresh BREAK marker.
"""

import datetime
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pyperclip

from . import cleanup, config, ocr, state
from .chatlog import MessageEntry


def parse_start_date(date_str: str) -> int:
    """Parse a 'YYYY-MM-DD[-HH-MM-SS]' string into a unix timestamp.

    Raises ValueError with a readable message on bad input, rather than the
    original script's unguarded ``int(x)`` crash.
    """
    parts = date_str.strip().split("-")
    try:
        numeric_parts = tuple(int(p) for p in parts)
    except ValueError:
        raise ValueError(
            f"Could not parse date {date_str!r} - expected numbers separated by '-'."
        )

    if not 3 <= len(numeric_parts) <= 6:
        raise ValueError(
            f"Could not parse date {date_str!r} - expected 3 to 6 '-'-separated "
            "numbers (year-month-day[-hour-minute-second])."
        )

    try:
        date = datetime.datetime(*numeric_parts)
    except ValueError as exc:
        raise ValueError(f"Could not parse date {date_str!r}: {exc}")

    return int(time.mktime(date.timetuple()))


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
    if use_cache:
        cached = state.load_cache(image_folder)
        if cached is not None:
            return cached

    file_info: dict[str, list[str]] = {}
    image_names = os.listdir(image_folder)
    total = len(image_names)

    for i, image_name in enumerate(image_names):
        if "." in image_name and not any(
            image_name.endswith(suffix) for suffix in config.SKIP_TYPES
        ):
            image_path = os.path.join(image_folder, image_name)
            creation_time = os.path.getctime(image_path)

            if creation_time >= start_time:
                raw_string = ocr.transcribe_image(image_path)
                file_info[image_name] = ocr.split_into_paragraphs(raw_string)

        if progress_callback and total:
            progress_callback((i + 1) / total)

    state.save_cache(image_folder, file_info)
    return file_info


def write_message_lines(output_path: Path, lines_to_write: list[str]) -> None:
    """Append one message's lines to the output file, matching the original
    padding/format (blank-line separators, literal '\\n' restored to real
    newlines in corrected text)."""
    with open(output_path, "a", encoding="utf8") as f:
        if lines_to_write:
            f.write("\n\n\n\n")

        for line in lines_to_write:
            f.write(line.replace("\\n", "\n"))

        if lines_to_write:
            f.write("\n\n")


@dataclass
class ReviewItem:
    """One row of the review screen: a message, optionally paired with an
    image and the editable text initialized from that image's OCR
    paragraphs (joined with blank lines)."""

    entry: MessageEntry
    image_path: Optional[Path]
    initial_text: Optional[str]


def build_review_items(
    entries: list[MessageEntry],
    file_info: dict[str, list[str]],
    image_folder: Path,
) -> list[ReviewItem]:
    """Pair each approved message with its image (if any) and that image's
    OCR text pre-joined into one editable block."""
    items: list[ReviewItem] = []
    for entry in entries:
        if entry.image_name is None:
            items.append(ReviewItem(entry=entry, image_path=None, initial_text=None))
            continue

        paragraphs = file_info.get(entry.image_name, [])
        initial_text = "\n\n".join(para.strip() for para in paragraphs)
        items.append(
            ReviewItem(
                entry=entry,
                image_path=image_folder / entry.image_name,
                initial_text=initial_text,
            )
        )
    return items


def lines_for_item(item: ReviewItem, edited_text: Optional[str] = None) -> list[str]:
    """Build the final lines to write for one review item, using edited_text
    in place of the original OCR text if the user changed it."""
    lines = [line + "\n" for line in item.entry.text_lines]

    if item.initial_text is not None:
        text = edited_text if edited_text is not None else item.initial_text
        if text:
            lines.append(text + "\n")

    return lines


def write_all_items(
    output_path: Path,
    items: list[ReviewItem],
    edited_texts: list[Optional[str]],
) -> None:
    """Write every review item's final lines, in order, in one pass."""
    for item, edited_text in zip(items, edited_texts):
        write_message_lines(output_path, lines_for_item(item, edited_text))


def finalize_run(output_path: Path, html_file_path: Path) -> str:
    """Clean up the output file, record the new run date, and bookmark it
    with a fresh BREAK marker. Returns the text added during this run
    (also copied to the clipboard)."""
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

    return just_added
