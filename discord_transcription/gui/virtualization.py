"""Pure layout math for the review screen's virtualized rows.

No Tk dependency, so it's testable without a display. VirtualRows uses it
to estimate row heights before a row is built and to work out which rows
are near the viewport.
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
    """Pixel sizes of the review screen's text.

    Measured from the real font and widgets on the running display (see
    row_building.measure_text_metrics), since they vary with the font Tk
    resolves and the display's DPI scaling.

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


def wrapped_line_count(text: str, chars_per_line: int) -> int:
    """Number of soft-wrapped display lines `text` would occupy at
    `chars_per_line`."""
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
    """Estimate the height (px) of an item's row before it's built.

    Used for the scrollbar and for deciding which rows are near the
    viewport; VirtualRows replaces it with the real height once the row is
    built. A wrong estimate shows up as a scroll jump at that point, and
    one for a row that is never built shifts every later row's offset.

    This mirrors RowBuilder.fill_row's sizing by hand, since this module
    has to stay Tk-free; the two share only the role order
    (item.slot_roles) and layout_constants. Change both together.

    Each column (left: label and images; right: one box per slot) is
    summed separately, with GAP_BETWEEN_STACKED_PX after every element
    but the last, and the taller one is used. On top of that come the
    row's own chrome (ROW_FRAME_OVERHEAD_PX) and the pack gap outside it
    (2 * ROW_PACK_PADY_PX; see docs/ARCHITECTURE_ROW_GEOMETRY.md).

    Args:
        item: The row's item.
        max_text_box_height_px: The cap on a content box's height, as in
            RowBuilder.fixed_text_box_height; None for no cap. It applies
            to the right column only - the label or image beside a capped
            box keeps its full height.
        metrics: Measured text sizes; the default is a rough stand-in for
            unit tests without a display.
        image_column_width_px: The left column's width, which the label
            wraps at and images are fitted to.

    Returns:
        The estimated height in px.
    """
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
    """Rows overlapping the viewport, widened by `buffer` on each side.

    Depends only on its arguments, not on which rows are built, which is
    what keeps VirtualRows.reconcile idempotent.

    Args:
        heights: Each row's height (estimated or real).
        scroll_top: The viewport's top, in document coordinates.
        viewport_height: The viewport's height.
        buffer: Extra distance to include above and below.

    Returns:
        The inclusive (first_idx, last_idx) range, clamped to valid
        indexes; (0, -1) when there are no rows.
    """
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
