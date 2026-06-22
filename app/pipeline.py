"""Orchestration glue tying OCR, HTML parsing, and output writing together.

Split into pieces the GUI can drive explicitly:

- ``parse_start_date`` / ``run_ocr_batch`` run before any user interaction
  (the latter on a background thread, reporting progress via callback).
- ``RunController`` walks the approved messages one at a time; for messages
  with an attached image, the GUI hands its paragraphs to a
  ``ParagraphCorrectionController`` for the one-paragraph-at-a-time
  correction screen, then calls ``submit_current`` with the resulting lines.
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


class ParagraphCorrectionController:
    """Walks one image's OCR paragraphs, mirroring the original script's
    bbb/ccc/ddd/eee/fff/ggg suffix-code loop via explicit method calls
    instead of parsed text suffixes."""

    def __init__(self, paragraphs: list[str]):
        self._paragraphs = paragraphs
        self._index = 0
        self._lines: list[str] = []
        self._accept_all_remaining = False
        self._stopped = False

    @property
    def done(self) -> bool:
        return self._stopped or self._index >= len(self._paragraphs)

    @property
    def current_paragraph(self) -> Optional[str]:
        return None if self.done else self._paragraphs[self._index]

    @property
    def progress(self) -> tuple[int, int]:
        return self._index, len(self._paragraphs)

    @property
    def lines(self) -> list[str]:
        return list(self._lines)

    def accept(self, text: Optional[str] = None) -> None:
        """Accept the current paragraph (Enter / ddd-without-suffix semantics)."""
        if self.done:
            return
        para = self._paragraphs[self._index]
        self._lines.append(text if text is not None else para + "\\n\\n")
        self._index += 1
        self._fill_remaining_if_flagged()

    def go_back(self) -> None:
        """bbb: undo the last accepted paragraph and re-show it."""
        if self._index == 0:
            return
        self._lines.pop()
        self._index -= 1

    def accept_all_remaining(self, text: Optional[str] = None) -> None:
        """ddd: accept the current paragraph, then auto-accept all that follow."""
        self._accept_all_remaining = True
        self.accept(text)

    def skip_rest(self) -> None:
        """eee: drop the current and all following paragraphs."""
        self._stopped = True

    def skip_this_only(self) -> None:
        """fff: drop only the current paragraph, continue to the next."""
        if not self.done:
            self._index += 1

    def accept_and_stop(self, text: Optional[str] = None) -> None:
        """ggg: accept the current paragraph, then stop processing further ones."""
        if self.done:
            return
        para = self._paragraphs[self._index]
        self._lines.append(text if text is not None else para + "\\n\\n")
        self._stopped = True

    def _fill_remaining_if_flagged(self) -> None:
        if not self._accept_all_remaining:
            return
        while not self.done:
            para = self._paragraphs[self._index]
            self._lines.append(para + "\\n\\n")
            self._index += 1


@dataclass
class RunController:
    """Walks the approved message list, writing each one's lines once it
    (and any attached image's paragraphs) has been resolved."""

    entries: list[MessageEntry]
    file_info: dict[str, list[str]]
    output_path: Path
    _entry_index: int = 0

    @property
    def done(self) -> bool:
        return self._entry_index >= len(self.entries)

    def current_entry(self) -> Optional[MessageEntry]:
        return None if self.done else self.entries[self._entry_index]

    def paragraphs_for_current(self) -> list[str]:
        entry = self.current_entry()
        if entry is None or entry.image_name is None:
            return []
        return self.file_info.get(entry.image_name, [])

    def submit_current(self, image_lines: list[str]) -> None:
        """Write the current message (text lines + corrected image lines)
        and advance to the next one."""
        entry = self.entries[self._entry_index]
        lines_to_write = [line + "\n" for line in entry.text_lines] + image_lines
        write_message_lines(self.output_path, lines_to_write)
        self._entry_index += 1


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
