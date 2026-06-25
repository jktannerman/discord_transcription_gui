"""Pure layout math for the review screen's virtualized row window - no Tk
dependency, so this is testable without a display. See review_view.py's
module docstring for why the windowing design (idempotent recomputation
from scratch, rather than incremental step-forward/step-backward) matters.
"""

import bisect
import textwrap
from typing import List, Optional, Tuple

from ..pipeline import ReviewItem
from .image_loading import THUMBNAIL_SIZE, fitted_image_size
from .layout_constants import (
    GAP_BETWEEN_STACKED_PX,
    ROW_FRAME_OVERHEAD_PX,
    ROW_PACK_PADY_PX,
    SPACER_BOX_HEIGHT_PX,
    TEXT_BOX_MARGIN_PX,
)

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

# An editable box's height is its paired immutable element's own on-screen
# height (the label's estimate below, for a "message" box; the image's, for
# an "ocr" box) plus TEXT_BOX_MARGIN_PX - mirrors
# review_view.ReviewFrame._fixed_text_box_height's rule exactly, via the
# shared layout_constants module (review_view.py can't be imported from here
# - it's the one that imports this Tk-free module). Getting this estimate
# close to the real eventual height matters more than usual now that a box's
# height is no longer just a floor under a content-driven size - it's the
# dominant term in a row's total height, so a wrong constant here
# overestimates or underestimates every such row by the same large, constant
# amount, which is exactly the kind of error that turns into a visible
# scroll jump once ReviewFrame._remeasure_built_rows corrects it away after
# the row is actually built.


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


def estimate_row_height(item: ReviewItem, max_text_box_height_px: Optional[int] = None) -> int:
    """Cheap, approximate height (px) for an item's row before it's ever
    been built as real widgets - good enough for scrollbar proportion and
    decoding which rows are near the viewport, not for actual layout. Once
    a row is materialized, ReviewFrame replaces this estimate with the
    row's real winfo_height().

    Walks item.slot_roles - the same ordering _build_row uses - estimating
    both of the row's columns (the immutable left column: label and/or
    images; the editable right column: one box per slot, content or
    spacer) independently and takes the taller of the two, then adds
    ROW_FRAME_OVERHEAD_PX for the row's own padding/border, plus
    2*ROW_PACK_PADY_PX for the vertical pack() gap *outside* the row's own
    Frame (see that constant's docstring - winfo_height() can't see it,
    so it has to be added back by hand here and in
    ReviewFrame._remeasure_built_rows, or the document-space model this
    feeds drifts away from the real screen position one row at a time).
    Each stacked
    element but the last gets GAP_BETWEEN_STACKED_PX added to whichever
    column total includes it, same as the real layout. A spacer role has
    no left-column counterpart at all (only the right column gets
    SPACER_BOX_HEIGHT_PX), unlike a content role whose right-column box is
    always its left counterpart plus TEXT_BOX_MARGIN_PX - the max() is kept
    anyway so this stays correct regardless of how those two compare for
    any given item.

    `max_text_box_height_px` mirrors RowBuildingMixin._fixed_text_box_height's
    own cap on a content box's right-column height (TEXT_BOX_MAX_HEIGHT_
    FRACTION of the canvas) - omitted (None) means "don't cap," for callers
    that don't have a real viewport height to cap against yet. A long
    message/OCR text's real box stops growing past that cap (it gets an
    internal scrollbar instead), but its *left*-column counterpart (the
    label/image) doesn't shrink to match - so capping has to apply to the
    right column's running total alone, not to whichever of left/right
    happens to be larger overall. Skipping this cap left long-message rows
    overestimated by however far past the cap their uncapped guess ran -
    not a one-off glitch, since a never-built row (skipped by a far
    Tab/resume-focus jump or a scrollbar drag) keeps that overestimate
    baked into every later row's document-space offset until it's actually
    built and remeasured."""
    roles = item.slot_roles
    left = 0
    right = 0
    for position, role in enumerate(roles):
        gap = GAP_BETWEEN_STACKED_PX if position < len(roles) - 1 else 0

        if role == "message":
            label_h = _estimate_message_text_height(item)
            box_h = label_h + TEXT_BOX_MARGIN_PX
            if max_text_box_height_px is not None:
                box_h = min(box_h, max_text_box_height_px)
            left += label_h + gap
            right += box_h + gap
        elif role.startswith("ocr"):
            image_index = int(role[len("ocr"):])
            _, image_h = fitted_image_size(item.image_paths[image_index])
            box_h = image_h + TEXT_BOX_MARGIN_PX
            if max_text_box_height_px is not None:
                box_h = min(box_h, max_text_box_height_px)
            left += image_h + gap
            right += box_h + gap
        else:
            right += SPACER_BOX_HEIGHT_PX + gap

    return max(left, right) + ROW_FRAME_OVERHEAD_PX + 2 * ROW_PACK_PADY_PX


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
