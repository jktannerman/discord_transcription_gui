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

# Mirrors review_view.ReviewFrame._fixed_text_box_height's two fixed-height
# rules (review_view.py can't be imported from here - it's the one that
# imports this Tk-free module - so these constants are kept in sync by hand
# rather than shared): a "message" box is a flat _MESSAGE_BOX_MIN_LINES
# tall regardless of its content, and an "ocr" box is its paired image's own
# height plus _IMAGE_BOX_MARGIN_PX. Getting this estimate close to the real
# eventual height matters more than usual now that the box's height is no
# longer just a floor under a content-driven size - it's the dominant term
# for an image row's total height, so a stale/wrong constant here (as
# _IMAGE_ROW_OVERHEAD, the flat fudge-factor this replaced, became once box
# auto-growth was removed) overestimates every such row by the same large,
# constant amount, which is exactly the kind of error that turns into a
# visible scroll jump once ReviewFrame._remeasure_built_rows corrects it
# away after the row is actually built.
_MESSAGE_BOX_MIN_LINES = 3
_IMAGE_BOX_MARGIN_PX = 12

# Extra vertical space (px) a row's own ttk.Frame(relief="groove",
# borderwidth=1, padding=6) adds on top of its tallest column - 6px padding
# top and bottom, plus a couple px for the groove border - see
# ReviewFrame._build_row.
_ROW_FRAME_OVERHEAD_PX = 14

# Gap (px) review_view.py's _build_row leaves between a row's stacked
# caption and image (and their paired editable boxes) when a message has
# both - applies to both columns, only when there's an image as well as a
# message.
_GAP_BELOW_MESSAGE_PX = 6


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

    Estimates both of the row's columns - the immutable left column (label
    and/or image, stacked) and the editable right column (message and/or
    ocr box, stacked the same way) - independently and takes the taller of
    the two, mirroring _build_row's actual side-by-side layout, then adds
    _ROW_FRAME_OVERHEAD_PX for the row's own padding/border. A message with
    both a caption and an image gets _GAP_BELOW_MESSAGE_PX added to
    whichever column total includes the caption element, same as the real
    layout."""
    has_message = item.initial_message_text is not None
    has_image = item.image_path is not None
    gap = _GAP_BELOW_MESSAGE_PX if (has_message and has_image) else 0

    left = 0
    right = 0
    if has_message:
        left += _estimate_message_text_height(item) + gap
        right += _MESSAGE_BOX_MIN_LINES * _TEXT_ROW_LINE_HEIGHT + gap
    if has_image:
        _, image_h = fitted_image_size(item.image_path)
        left += image_h
        right += image_h + _IMAGE_BOX_MARGIN_PX

    return max(left, right) + _ROW_FRAME_OVERHEAD_PX


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
