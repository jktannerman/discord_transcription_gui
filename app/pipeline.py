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
from dataclasses import dataclass, field
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
    """Append one review item's lines to the output file verbatim - no
    padding is added here. Spacing between/within items is now entirely
    owned by that item's own spacer slots (see ReviewItem.slot_roles and
    lines_for_item), so each chunk in lines_to_write already carries
    whatever newlines its surrounding spacers decided on."""
    with open(output_path, "a", encoding="utf8") as f:
        for line in lines_to_write:
            f.write(line)

    logger.debug("wrote message lines", extra=logging_config.extra(line_count=len(lines_to_write)))


def _is_dice_command(entry: MessageEntry) -> bool:
    """Whether entry's own original text is a die-roll command (e.g.
    "%roll 2d6", "%draw 1 20") - see config.DICE_COMMAND_RE."""
    text = "\n".join(entry.text_lines).strip()
    return bool(text) and bool(config.DICE_COMMAND_RE.match(text))


def _spacer_default(empty_lines: int) -> str:
    """Default literal content for a spacer slot representing empty_lines
    blank lines - one more literal "\\n" token than that, since the gap
    also includes the newline that terminates whatever precedes it (see
    ARCHITECTURE.md's "Spacer slots" section)."""
    return "\\n" * (empty_lines + 1)


@dataclass
class ReviewItem:
    """One row of the review screen: a message, paired with an editable
    text box for its own message text (``initial_message_text``, whenever
    it has any), one independently-editable OCR text box per attached
    image, in attachment order (``image_paths``/``initial_ocr_texts`` are
    parallel lists, possibly empty for a text-only message), and a spacer
    text box between every adjacent pair of those plus a final one before
    the next message (``initial_spacer_texts``, keyed by role - see
    slot_roles). A message with a caption and N images gets one message
    box, N OCR boxes, and N+1 spacer boxes - see build_review_items."""

    entry: MessageEntry
    image_paths: list[Path]
    initial_message_text: Optional[str]
    initial_ocr_texts: list[str]
    initial_spacer_texts: dict[str, str] = field(default_factory=dict)

    @property
    def message_id(self) -> str:
        return self.entry.message_id

    @property
    def slot_roles(self) -> list[str]:
        """Every editable box this item has, in transcript order: a
        "message" slot whenever it has one, a "spacer_msg_img" slot if it
        has both a message and at least one image, then for each image an
        "ocr{i}" slot followed by a "spacer_img{i}" slot (omitted after the
        last image), and always a trailing "spacer_end" slot - the gap
        before the next message. This single ordering is shared by row
        building, height estimation, keyboard navigation, and output
        writing, so they can't drift apart from each other."""
        has_message = self.initial_message_text is not None
        image_count = len(self.image_paths)
        roles: list[str] = []
        if has_message:
            roles.append("message")
        if has_message and image_count:
            roles.append("spacer_msg_img")
        for i in range(image_count):
            roles.append(f"ocr{i}")
            if i < image_count - 1:
                roles.append(f"spacer_img{i}")
        roles.append("spacer_end")
        return roles

    def initial_text_for_role(self, role: str) -> str:
        """The default text for one of this item's slots - used as the
        fallback whenever a live/saved edit for that role is absent."""
        if role == "message":
            return self.initial_message_text or ""
        if role.startswith("ocr"):
            return self.initial_ocr_texts[int(role[len("ocr"):])]
        return self.initial_spacer_texts[role]


def build_review_items(
    entries: list[MessageEntry],
    file_info: dict[str, list[str]],
    image_folder: Path,
) -> list[ReviewItem]:
    """Pair each approved message with its images (if any), its editable
    text box(es) (a copy of the message's own original text, unstripped,
    whenever it has any, and one box per attached image holding that
    image's joined OCR text - independently, so a message with both a
    caption and images gets all of them), and its spacer box(es) (see
    ReviewItem.slot_roles), pre-filled with the default blank-line counts
    documented in ARCHITECTURE.md's "Spacer slots" section. A die-roll
    command/result pair is detected from each message's own *original*
    text (not whatever the user later edits it to), so editing a message's
    transcribed text never changes its default spacing."""
    is_command = [_is_dice_command(entry) for entry in entries]

    items: list[ReviewItem] = []
    for i, entry in enumerate(entries):
        if entry.image_names:
            # No message box at all for an image-only message with no
            # caption - nothing there to edit.
            initial_message_text = "\n".join(entry.text_lines) if entry.text_lines else None
        else:
            # A text-only message always gets a message box, even if its
            # text is empty, so it still gets a row at all.
            initial_message_text = "\n".join(entry.text_lines)

        initial_ocr_texts = []
        for image_name in entry.image_names:
            paragraphs = file_info.get(image_name, [])
            # Tesseract output routinely ends with a blank line, which
            # split_into_paragraphs turns into a trailing empty-after-strip
            # paragraph - join naively and that becomes a literal "\n\n"
            # tail on the OCR text, which then carries through to the final
            # output unless the user happens to manually trim it. Dropping
            # empty paragraphs (wherever they fall, not just at the end)
            # avoids that without changing how real paragraph breaks are
            # rendered.
            initial_ocr_texts.append(
                "\n\n".join(stripped for para in paragraphs if (stripped := para.strip()))
            )

        image_count = len(entry.image_names)
        spacer_texts: dict[str, str] = {}
        if initial_message_text is not None and image_count:
            spacer_texts["spacer_msg_img"] = _spacer_default(config.EMPTY_LINES_TEXT_TO_IMAGE)
        for image_index in range(image_count - 1):
            spacer_texts[f"spacer_img{image_index}"] = _spacer_default(config.EMPTY_LINES_BETWEEN_IMAGES)

        if is_command[i]:
            end_empty_lines = config.EMPTY_LINES_DICE_COMMAND_TO_RESULT
        elif i > 0 and is_command[i - 1]:
            next_is_command = i + 1 < len(entries) and is_command[i + 1]
            end_empty_lines = (
                config.EMPTY_LINES_RESULT_TO_NEXT_COMMAND if next_is_command else config.EMPTY_LINES_NORMAL
            )
        else:
            end_empty_lines = config.EMPTY_LINES_NORMAL
        spacer_texts["spacer_end"] = _spacer_default(end_empty_lines)

        items.append(
            ReviewItem(
                entry=entry,
                image_paths=[image_folder / name for name in entry.image_names],
                initial_message_text=initial_message_text,
                initial_ocr_texts=initial_ocr_texts,
                initial_spacer_texts=spacer_texts,
            )
        )

    image_items = sum(1 for item in items if item.image_paths)
    total_images = sum(len(item.image_paths) for item in items)
    logger.info(
        "built review items",
        extra=logging_config.extra(
            total_items=len(items), image_items=image_items, total_images=total_images
        ),
    )
    return items


_SPACER_TOKEN_RE = re.compile(r"\\n")


def _count_spacer_tokens(raw: Optional[str]) -> int:
    """Number of literal "\\n" tokens (backslash followed by "n") in a
    spacer box's text, after discarding every *real* newline/carriage-
    return character anywhere in it (start, end, or mixed through the
    middle - see ARCHITECTURE.md's "Spacer slots" section). Any other
    stray character in the box is ignored, never written."""
    if not raw:
        return 0
    without_real_newlines = raw.replace("\r", "").replace("\n", "")
    return len(_SPACER_TOKEN_RE.findall(without_real_newlines))


def lines_for_item(item: ReviewItem, edited: Optional[dict[str, Optional[str]]] = None) -> list[str]:
    """Build the final chunks to write for one review item, walking
    item.slot_roles in order and using edited[role] in place of the
    corresponding default text whenever the user touched that box (None,
    or the role missing from edited, means "use the default"). A content
    role ("message"/"ocrN") contributes its text with only *trailing* real
    newlines stripped, and no newline forced onto the end - the spacer
    role that always immediately follows it supplies that terminator, plus
    however many blank lines the user left in that spacer. A spacer role
    contributes that many literal newline characters instead of its own
    text verbatim - see _count_spacer_tokens."""
    edited = edited or {}
    chunks: list[str] = []

    for role in item.slot_roles:
        text = edited.get(role)
        if text is None:
            text = item.initial_text_for_role(role)

        if role.startswith("spacer"):
            count = _count_spacer_tokens(text)
            if count:
                chunks.append("\n" * count)
        else:
            content = text.rstrip("\r\n") if text else text
            if content:
                chunks.append(content)

    return chunks


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
