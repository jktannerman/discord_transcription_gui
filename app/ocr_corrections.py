"""User-editable regex find/replace rules for common Tesseract misreads -
the actual rules live in ``ocr_corrections.txt`` next to this module, not
in code, so they can be tuned without touching Python. Applied exactly
once, in ``review_item.build_review_items``, to each image's freshly-joined
OCR text before it becomes a review item's *initial* OCR-box content - by
that point in the pipeline there's no such thing yet as "the user's edit",
so nothing here needs to know or care which boxes a session has touched:
once a box has a saved edit, ``ReviewItem.initial_ocr_texts`` (this
module's only output) is never consulted again for it (see
``row_building.RowBuildingMixin._populate_text_box``'s ``self._saved_texts``
fallback) - corrections are seen by the user once, as a normal part of
that box's starting text, not silently reapplied over their own typing on
a later run.

File format (``ocr_corrections.txt``): entries separated by one or more
blank lines, each:
    <find regex, one line>
    <replacement, one line - \\1 etc. refer to the find regex's capture groups>
    # any number of comment lines (must start with "#")
A block consisting only of comment lines (e.g. the file's own header) is
a free-standing comment - skipped entirely, not treated as a malformed
entry missing its replacement line.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

from . import config, logging_config

logger = logging_config.get_logger(__name__)

_BLOCK_SEPARATOR_RE = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Correction:
    pattern: re.Pattern
    replacement: str
    comment: str


def load_corrections(path: Path = config.OCR_CORRECTIONS_FILE) -> List[Correction]:
    """Parse `path` into a list of Corrections, in file order (so a later
    entry can clean up after an earlier one, e.g. a broad fix followed by
    a narrower exception to it). A missing file just means no corrections
    run - a fresh checkout/install that hasn't created one yet behaves the
    same as an empty one, rather than erroring."""
    if not path.exists():
        return []

    text = path.read_text(encoding="utf8").strip()
    if not text:
        return []

    corrections: List[Correction] = []
    for block_num, block in enumerate(_BLOCK_SEPARATOR_RE.split(text), start=1):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines or all(line.startswith("#") for line in lines):
            continue  # comment-only block (e.g. the file's own header)
        if len(lines) < 2:
            raise ValueError(
                f"{path}: entry {block_num} has a find pattern ({lines[0]!r}) "
                "but no replacement line after it."
            )
        find, replacement, *comment_lines = lines
        bad_comment = next((line for line in comment_lines if not line.startswith("#")), None)
        if bad_comment is not None:
            raise ValueError(
                f"{path}: entry {block_num} has an extra line that isn't a "
                f"comment (comments must start with '#'): {bad_comment!r}"
            )
        try:
            pattern = re.compile(find)
        except re.error as exc:
            raise ValueError(f"{path}: entry {block_num}'s find pattern {find!r} is invalid: {exc}") from exc
        corrections.append(Correction(pattern, replacement, "\n".join(comment_lines)))

    logger.info(
        "loaded OCR corrections", extra=logging_config.extra(path=str(path), count=len(corrections))
    )
    return corrections


def apply_corrections(text: str, corrections: List[Correction]) -> str:
    """Run every correction over `text`, in order. Callers must only ever
    pass freshly-OCR'd text, never a box's current (possibly user-edited)
    content - see the module docstring for why build_review_items is the
    only place this is meant to be called from."""
    for correction in corrections:
        text = correction.pattern.sub(correction.replacement, text)
    return text
