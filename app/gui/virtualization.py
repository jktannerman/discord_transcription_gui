"""Pure layout math for the review screen's virtualized row window - no Tk
dependency, so this is testable without a display. See review_view.py's
module docstring for why the windowing design (idempotent recomputation
from scratch, rather than incremental step-forward/step-backward) matters.
"""

import bisect
import textwrap
from typing import List, Tuple

from ..pipeline import ReviewItem
from .image_loading import THUMBNAIL_SIZE, fitted_image_size

# Row overhead (px) added on top of an image's own fitted height to estimate
# its row's total height before it's ever been built and measured - row
# padding/border plus the OCR text box's minimum chrome. The image height
# itself comes from fitted_image_size's cheap header-only read (already used
# to size the real placeholder in review_view.py), not a flat per-row
# constant - most images here are landscape (width-, not height-,
# constrained against THUMBNAIL_SIZE), so a flat estimate sized for the
# worst-case portrait image overestimated most rows by 500+px. That
# overestimate got corrected away once a row was actually built (see
# ReviewFrame._remeasure_built_rows), but the correction itself shifts the
# scroll position to compensate - so a large, consistently-wrong estimate
# turned an ordinary scroll into a visible jump once the next batch of rows
# was measured.
_IMAGE_ROW_OVERHEAD = 220

# Used to turn a text-only row's character count into an estimated wrapped
# line count, matching _build_row's wraplength for that row's immutable-
# original label - THUMBNAIL_SIZE[0], the same fixed column width used for
# an image row's left column, since text-only rows now use the same
# two-column layout (immutable original on the left, editable copy on the
# right) rather than a single full-width label.
_TEXT_ROW_WRAPLENGTH = THUMBNAIL_SIZE[0]
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


def _estimate_message_text_height(item: ReviewItem) -> int:
    text = item.initial_message_text or "(no text)"
    line_count = wrapped_line_count(text, _TEXT_ROW_CHARS_PER_LINE)
    return line_count * _TEXT_ROW_LINE_HEIGHT + _TEXT_ROW_PADDING


def estimate_row_height(item: ReviewItem) -> int:
    """Cheap, approximate height (px) for an item's row before it's ever
    been built as real widgets - good enough for scrollbar proportion and
    decoding which rows are near the viewport, not for actual layout. Once
    a row is materialized, ReviewFrame replaces this estimate with the
    row's real winfo_height().

    A message with both a caption and an image (its message-text box
    stacked above its OCR/image box - see review_view.py) gets both
    estimates added together, since its row now needs room for both."""
    if item.image_path is None:
        return _estimate_message_text_height(item)

    _, image_h = fitted_image_size(item.image_path)
    height = image_h + _IMAGE_ROW_OVERHEAD
    if item.initial_message_text is not None:
        height += _estimate_message_text_height(item)
    return height


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
