"""Pure layout math for the review screen's virtualized row window - no Tk
dependency, so this is testable without a display. See review_view.py's
module docstring for why the windowing design (idempotent recomputation
from scratch, rather than incremental step-forward/step-backward) matters.
"""

import bisect
import textwrap
from typing import List, Tuple

from ..pipeline import ReviewItem
from .image_loading import THUMBNAIL_SIZE

# Estimated height (px) for an image row before it's ever been built and
# measured - only needs to be roughly right, since it only affects scrollbar
# proportion/buffering for rows the user hasn't scrolled near yet, and gets
# replaced with the real winfo_height() the moment the row is materialized.
_ESTIMATED_IMAGE_ROW_HEIGHT = THUMBNAIL_SIZE[1] + 220

# Used to turn a text-only row's character count into an estimated wrapped
# line count, matching _build_row's wraplength=900 for that row's label.
_TEXT_ROW_WRAPLENGTH = 900
_TEXT_ROW_CHARS_PER_LINE = _TEXT_ROW_WRAPLENGTH // 7  # ~7px/char at this font size
_TEXT_ROW_LINE_HEIGHT = 18
_TEXT_ROW_PADDING = 24


def wrapped_line_count(text: str, chars_per_line: int) -> int:
    """Number of soft-wrapped display lines `text` would occupy at
    `chars_per_line` - shared by estimate_row_height below and the
    editable text box's auto-sizing in review_view.py."""
    line_count = 0
    for line in text.splitlines() or [""]:
        line_count += len(textwrap.wrap(line, chars_per_line)) or 1
    return line_count


def estimate_row_height(item: ReviewItem) -> int:
    """Cheap, approximate height (px) for an item's row before it's ever
    been built as real widgets - good enough for scrollbar proportion and
    decoding which rows are near the viewport, not for actual layout. Once
    a row is materialized, ReviewFrame replaces this estimate with the
    row's real winfo_height()."""
    if item.image_path is not None:
        return _ESTIMATED_IMAGE_ROW_HEIGHT

    text = "\n".join(item.entry.text_lines).strip() or "(no text)"
    line_count = wrapped_line_count(text, _TEXT_ROW_CHARS_PER_LINE)
    return line_count * _TEXT_ROW_LINE_HEIGHT + _TEXT_ROW_PADDING


def compute_visible_range(
    heights: List[int], scroll_top: float, viewport_height: float, buffer: float
) -> Tuple[int, int]:
    """Pure function: given each item's row height (estimated or real) and
    the viewport's current position, return the inclusive [first_idx,
    last_idx] item-index range overlapping the viewport expanded by
    `buffer` on each side, clamped to the valid index range.

    Deliberately pure and independent of what's currently materialized -
    calling this twice with the same arguments always returns the same
    range. That idempotency is what makes ReviewFrame._reconcile immune to
    the oscillation bug a previous, stateful step-forward/step-backward
    design suffered from - see review_view.py's module docstring."""
    n = len(heights)
    if n == 0:
        return (0, -1)

    offsets = [0]
    for h in heights:
        offsets.append(offsets[-1] + h)
    total = offsets[-1]

    lo = max(0.0, min(scroll_top - buffer, total))
    hi = max(lo, min(scroll_top + viewport_height + buffer, total))

    first_idx = max(0, min(bisect.bisect_right(offsets, lo) - 1, n - 1))
    last_idx = max(first_idx, min(bisect.bisect_right(offsets, hi) - 1, n - 1))
    return (first_idx, last_idx)
