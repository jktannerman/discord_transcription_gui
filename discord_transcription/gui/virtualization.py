"""Pure layout math for the review screen's virtualized row window - no Tk
dependency, so this is testable without a display. See review_view.py's
module docstring for why the windowing design (idempotent recomputation
from scratch, rather than incremental step-forward/step-backward) matters.
"""

import bisect
import textwrap
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..review_item import ReviewItem
from .image_loading import DEFAULT_IMAGE_COLUMN_WIDTH_PX, fitted_image_size, image_bounding_box
from .layout_constants import (
    GAP_BETWEEN_STACKED_PX,
    MIN_IMAGE_COLUMN_WIDTH_PX,
    MIN_TEXT_COLUMN_WIDTH_PX,
    ROW_FRAME_OVERHEAD_PX,
    ROW_HORIZONTAL_OVERHEAD_PX,
    ROW_PACK_PADY_PX,
    TEXT_BOX_MARGIN_PX,
)


@dataclass(frozen=True)
class TextMetrics:
    """Pixel sizes of the review screen's text, measured from the real
    font and widgets on the running display (see
    row_building.measure_text_metrics) rather than hardcoded, since they
    vary with the font Tk actually resolves and the display's DPI scaling.

    Attributes:
        char_width_px: Advance width of one character of the (monospace)
            text font.
        line_height_px: Height of one wrapped line in the original-text
            label.
        label_padding_px: The label's height beyond its lines' own height.
        spacer_box_height_px: Height a one-line spacer text box needs,
            including its internal padding, border and focus highlight.
    """

    char_width_px: int
    line_height_px: int
    label_padding_px: int
    spacer_box_height_px: int


# Fallback for callers with no display to measure against (unit tests):
# roughly Consolas 14 at 96 DPI.
DEFAULT_TEXT_METRICS = TextMetrics(
    char_width_px=10, line_height_px=22, label_padding_px=4, spacer_box_height_px=34,
)

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


def clamp_image_column_width(width_px: int, canvas_width_px: int) -> int:
    """Keep the image column within its limits for a given canvas width.

    The text column keeps at least MIN_TEXT_COLUMN_WIDTH_PX and the image
    column at least MIN_IMAGE_COLUMN_WIDTH_PX; if the canvas is too narrow
    for both, the image column's minimum wins.

    Args:
        width_px: The requested image column width.
        canvas_width_px: The review canvas's width.

    Returns:
        The width to use.
    """
    max_width = canvas_width_px - ROW_HORIZONTAL_OVERHEAD_PX - MIN_TEXT_COLUMN_WIDTH_PX
    return max(MIN_IMAGE_COLUMN_WIDTH_PX, min(width_px, max_width))


def image_column_width_for_fraction(fraction: float, canvas_width_px: int) -> int:
    """The image column width that keeps it `fraction` of the canvas width.

    Args:
        fraction: The image column's share of the canvas width.
        canvas_width_px: The review canvas's width.

    Returns:
        The width, clamped by clamp_image_column_width.
    """
    return clamp_image_column_width(round(fraction * canvas_width_px), canvas_width_px)


def _estimate_message_text_height(
    item: ReviewItem, metrics: TextMetrics, image_column_width_px: int = DEFAULT_IMAGE_COLUMN_WIDTH_PX
) -> int:
    # The original-text label wraps at the image column's width.
    text = item.initial_message_text or "(no text)"
    chars_per_line = max(1, image_column_width_px // max(1, metrics.char_width_px))
    line_count = wrapped_line_count(text, chars_per_line)
    return line_count * metrics.line_height_px + metrics.label_padding_px


def estimate_row_height(
    item: ReviewItem,
    max_text_box_height_px: Optional[int] = None,
    metrics: TextMetrics = DEFAULT_TEXT_METRICS,
    image_column_width_px: int = DEFAULT_IMAGE_COLUMN_WIDTH_PX,
) -> int:
    """Cheap, approximate height (px) for an item's row before it's ever
    been built as real widgets - good enough for scrollbar proportion and
    decoding which rows are near the viewport, not for actual layout. Once
    a row is materialized, ReviewFrame replaces this estimate with the
    row's real winfo_height().

    This hand-mirrors row_building.RowBuildingMixin._build_row's per-role
    sizing (same gap placement, same "content height + margin, capped at
    max_text_box_height_px" rule) rather than calling into it, since this
    module has to stay Tk-free to be unit-testable - so the two can't share
    code, only the ordering (item.slot_roles). If you change how a role's
    height/gap is computed in one of these, change it in the other too:
    drift between them is exactly what previously surfaced as a scroll-
    position jump once ReviewFrame._remeasure_built_rows corrected the
    estimate away after the row was actually built (see
    docs/ARCHITECTURE_REVIEW_SCREEN.md).

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
    metrics.spacer_box_height_px), unlike a content role whose right-column box is
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
    built and remeasured.

    `metrics` is the text font's measured pixel sizes (TextMetrics) -
    ReviewFrame passes ones measured on the running display; the default
    is only a rough stand-in for display-free unit tests.

    `image_column_width_px` is the left column's width: the original-text
    label wraps at it, and images are fitted to it (image_bounding_box)."""
    roles = item.slot_roles
    left = 0
    right = 0
    for position, role in enumerate(roles):
        gap = GAP_BETWEEN_STACKED_PX if position < len(roles) - 1 else 0

        if role == "message":
            label_h = _estimate_message_text_height(item, metrics, image_column_width_px)
            box_h = label_h + TEXT_BOX_MARGIN_PX
            if max_text_box_height_px is not None:
                box_h = min(box_h, max_text_box_height_px)
            left += label_h + gap
            right += box_h + gap
        elif role.startswith("ocr"):
            image_index = int(role[len("ocr"):])
            _, image_h = fitted_image_size(
                item.image_paths[image_index], image_bounding_box(image_column_width_px)
            )
            box_h = image_h + TEXT_BOX_MARGIN_PX
            if max_text_box_height_px is not None:
                box_h = min(box_h, max_text_box_height_px)
            left += image_h + gap
            right += box_h + gap
        else:
            right += metrics.spacer_box_height_px + gap

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
