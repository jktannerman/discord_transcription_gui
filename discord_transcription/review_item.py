"""The review screen's central domain model: one ``ReviewItem`` per approved
message, plus the slot-role ordering and spacer-token parsing every other
review-screen module (row building, height estimation, keyboard navigation)
and finalize-time output writing all share.

Split out of ``pipeline.py`` - unlike that module's OCR-batch/output-writing
glue, this is the one shared concept several other modules depend on
directly (``ReviewItem.slot_roles`` in particular - see its own docstring),
the same way ``chatlog.py``/``ocr_corrections.py`` each got their own module
for *their* shared concepts.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config, logging_config, ocr_corrections
from .chatlog import MessageEntry

logger = logging_config.get_logger(__name__)


def _is_dice_command(entry: MessageEntry) -> bool:
    """Whether entry's own original text is a die-roll command (e.g.
    "%roll 2d6", "%draw 1 20") - see config.DICE_COMMAND_RE."""
    text = "\n".join(entry.text_lines).strip()
    return bool(text) and bool(config.DICE_COMMAND_RE.match(text))


def _spacer_default(empty_lines: int) -> str:
    """Default literal content for a spacer slot representing empty_lines
    blank lines - one more literal "\\n" token than that, since the gap
    also includes the newline that terminates whatever precedes it (see
    docs/ARCHITECTURE_SPACER_SLOTS.md)."""
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
    corrections: Optional[list[ocr_corrections.Correction]] = None,
) -> list[ReviewItem]:
    """Pair each approved message with its images (if any), its editable
    text box(es) (a copy of the message's own original text, unstripped,
    whenever it has any, and one box per attached image holding that
    image's joined OCR text - independently, so a message with both a
    caption and images gets all of them), and its spacer box(es) (see
    ReviewItem.slot_roles), pre-filled with the default blank-line counts
    documented in docs/ARCHITECTURE_SPACER_SLOTS.md. A die-roll
    command/result pair is detected from each message's own *original*
    text (not whatever the user later edits it to), so editing a message's
    transcribed text never changes its default spacing.

    Each image's joined OCR text additionally gets ocr_corrections.py's
    regex fixes applied here, before it's stored as initial_ocr_texts -
    the last point in the pipeline where text is still guaranteed to be
    "freshly OCR'd" rather than possibly user-edited (see that module's
    docstring for why that distinction matters). `corrections` defaults to
    loading discord_transcription/ocr_corrections.txt; only overridden by tests that want to
    check this step's wiring without depending on that file's actual
    (user-editable, expected-to-change) contents."""
    is_command = [_is_dice_command(entry) for entry in entries]
    if corrections is None:
        corrections = ocr_corrections.load_corrections()

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
            joined = "\n\n".join(stripped for para in paragraphs if (stripped := para.strip()))
            initial_ocr_texts.append(
                ocr_corrections.apply_corrections(joined, corrections, context=image_name)
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
    middle - see docs/ARCHITECTURE_SPACER_SLOTS.md). Any other
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
