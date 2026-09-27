"""The draggable divider between the review screen's image column and text
column.

The divider is a thin bar place()'d over the canvas at the column boundary -
each row is its own Frame, so no single paned widget could span them all.
Dragging it only moves the bar; releasing it applies the new width
(set_width), which changes every row's height (images are fitted to the
column width, and the original-text label wraps at it), so every built row
is torn down, all row heights are re-estimated, and the rows are rebuilt
around the view. The per-box model (slot_state.py) keeps edits, cursor,
undo history and checkbox state across that rebuild, as it does for any
scroll-driven one.

The column width is kept as a fraction of the canvas width, so it keeps its
proportion when the window is resized; ReviewFrame's caller persists that
fraction per chatlog (on_fraction_changed).
"""

import bisect
import time
import tkinter as tk
from typing import Callable, Optional

from .. import logging_config
from . import theme
from .layout_constants import COLUMN_DIVIDER_WIDTH_PX, COLUMN_PADX_PX, IMAGE_COLUMN_LEFT_PX
from .row_building import RowBuilder
from .slot_boxes import Slot
from .virtual_rows import VirtualRows
from .virtualization import clamp_image_column_width, image_column_width_for_fraction

logger = logging_config.get_logger(__name__)

# How long to wait after the canvas's last width change (e.g. while the
# window is being resized) before re-laying out rows for the new width.
RESIZE_DEBOUNCE_MS = 150

# Divider color at rest and while hovered/dragged.
_DIVIDER_COLOR = "#4a4a4a"
_DIVIDER_ACTIVE_COLOR = theme.DARK_FOCUS_HIGHLIGHT

# (kind, key, screen_offset_px): the box (kind "box", key a slot) or row
# (kind "row", key an item index) whose top edge should stay at
# screen_offset_px from the top of the viewport across a relayout.
ViewAnchor = tuple[str, object, float]


def divider_x_for_width(image_column_width_px: int) -> int:
    """The divider's left x (px, canvas coordinates) for a column width.

    Centered in the gap between the image column's right edge and the text
    column's left edge.

    Args:
        image_column_width_px: The image column's width.

    Returns:
        The x to place() the divider at.
    """
    gap_center = IMAGE_COLUMN_LEFT_PX + image_column_width_px + COLUMN_PADX_PX
    return gap_center - COLUMN_DIVIDER_WIDTH_PX // 2


def width_for_divider_x(divider_center_x: float) -> int:
    """The image column width whose divider would be centered at a given x.

    Args:
        divider_center_x: The x (px, canvas coordinates) of the pointer.

    Returns:
        The (unclamped) image column width.
    """
    return round(divider_center_x - IMAGE_COLUMN_LEFT_PX - COLUMN_PADX_PX)


class ColumnDivider:
    """The divider bar, and re-laying out the rows when it moves.

    Attributes:
        widget: The divider bar itself.
        fraction: The image column's share of the canvas width, or None
            until the canvas first has a real width.
        drag_width: The width dragged to while a drag is in progress; None
            otherwise.
    """

    def __init__(
        self,
        parent: tk.Widget,
        rows: VirtualRows,
        builder: RowBuilder,
        focused_slot: Callable[[], Optional[Slot]],
        box_top: Callable[[Slot], Optional[float]],
        initial_fraction: Optional[float] = None,
        on_fraction_changed: Optional[Callable[[float], None]] = None,
    ) -> None:
        """Create the divider bar (placed later, by position()).

        Args:
            parent: The widget the bar is a child of (placed over the canvas).
            rows: The rows to re-lay out.
            builder: Holds the column width, and re-estimates row heights.
            focused_slot: The box that has focus, if any.
            box_top: A built box's document-space top, or None.
            initial_fraction: A saved fraction to start from, if any.
            on_fraction_changed: Called with the new fraction after a drag.
        """
        self._rows = rows
        self._builder = builder
        self._focused_slot = focused_slot
        self._box_top = box_top
        self._on_fraction_changed = on_fraction_changed
        self.fraction: Optional[float] = None
        if initial_fraction is not None and 0.0 < initial_fraction < 1.0:
            self.fraction = initial_fraction
        self.drag_width: Optional[int] = None
        self._resize_job: Optional[str] = None

        widget = tk.Frame(
            parent, width=COLUMN_DIVIDER_WIDTH_PX, bg=_DIVIDER_COLOR,
            cursor="sb_h_double_arrow", highlightthickness=0, borderwidth=0,
        )
        widget.bind("<Enter>", lambda e: widget.configure(bg=_DIVIDER_ACTIVE_COLOR))
        widget.bind(
            "<Leave>",
            lambda e: None if self.drag_width is not None else widget.configure(bg=_DIVIDER_COLOR),
        )
        widget.bind("<ButtonPress-1>", self.on_press)
        widget.bind("<B1-Motion>", self.on_drag)
        widget.bind("<ButtonRelease-1>", self.on_release)
        self.widget = widget

    def position(self, image_column_width_px: Optional[int] = None) -> None:
        """Place the divider at the boundary for `image_column_width_px`
        (the current column width if omitted)."""
        width = self._builder.image_column_width_px if image_column_width_px is None else image_column_width_px
        # Placed relative to the canvas, so its x is in canvas coordinates
        # (the same ones the rows are laid out in).
        self.widget.place(
            in_=self._rows.canvas, x=divider_x_for_width(width), y=0,
            width=COLUMN_DIVIDER_WIDTH_PX, relheight=1.0,
        )

    def cancel_pending(self) -> None:
        """Cancel a pending debounced width sync (the screen is going away)."""
        if self._resize_job is not None:
            self.widget.after_cancel(self._resize_job)
            self._resize_job = None

    # -- dragging --------------------------------------------------------------

    def on_press(self, event: tk.Event) -> str:
        self.drag_width = self._builder.image_column_width_px
        self.widget.configure(bg=_DIVIDER_ACTIVE_COLOR)
        self._rows.log_event("divider_drag_start", width=self._builder.image_column_width_px)
        return "break"

    def on_drag(self, event: tk.Event) -> str:
        """Move the divider with the pointer (clamped); rows are only
        re-laid out on release."""
        if self.drag_width is None:
            return "break"
        pointer_x = event.x_root - self._rows.canvas.winfo_rootx()
        width = clamp_image_column_width(width_for_divider_x(pointer_x), self._rows.canvas_width())
        self.drag_width = width
        self.position(width)
        return "break"

    def on_release(self, event: tk.Event) -> str:
        """Apply the dragged-to width and report the new fraction."""
        width = self.drag_width
        self.drag_width = None
        self.widget.configure(bg=_DIVIDER_COLOR)
        if width is None:
            return "break"
        self._rows.log_event("divider_drag_end", width=width)
        self.fraction = width / self._rows.canvas_width()
        self.set_width(width)
        if self._on_fraction_changed is not None:
            self._on_fraction_changed(self.fraction)
        return "break"

    # -- window resizes ----------------------------------------------------------

    def width_for_current_canvas(self) -> int:
        """The column width the stored fraction gives at the current canvas
        width - or, with no fraction yet, the current width clamped to it
        (and that becomes the fraction, so a later window resize keeps the
        proportion)."""
        canvas_width = self._rows.canvas_width()
        if self.fraction is None:
            width = clamp_image_column_width(self._builder.image_column_width_px, canvas_width)
            self.fraction = width / canvas_width
            return width
        return image_column_width_for_fraction(self.fraction, canvas_width)

    def schedule_width_sync(self) -> None:
        """Debounced: re-lay out rows if the canvas width change (a window
        resize) means a different column width."""
        if self._resize_job is not None:
            self.widget.after_cancel(self._resize_job)
        self._resize_job = self.widget.after(RESIZE_DEBOUNCE_MS, self.run_width_sync)

    def run_width_sync(self) -> None:
        self._resize_job = None
        if self._rows.materialized_range is None:
            return  # the initial layout hasn't run yet; it syncs itself
        self.set_width(self.width_for_current_canvas())

    # -- re-laying out, keeping the view anchored ---------------------------------

    def capture_view_anchor(self) -> ViewAnchor:
        """What should stay in place on screen across a relayout: the
        focused box's top edge if it's in view, otherwise the top edge of
        the row at the top of the view."""
        view_top = self._rows.view_top()
        view_bottom = self._rows.view_bottom()
        focused = self._focused_slot()
        if focused is not None:
            box_top = self._box_top(focused)
            if box_top is not None and view_top <= box_top < view_bottom:
                return ("box", focused, box_top - view_top)

        offsets = [0]
        for height in self._rows.heights:
            offsets.append(offsets[-1] + height)
        index = max(0, min(bisect.bisect_right(offsets, view_top) - 1, len(self._rows.heights) - 1))
        return ("row", index, offsets[index] - view_top)

    def _anchor_document_top(self, anchor: ViewAnchor) -> float:
        """Where the anchor's top edge is now, in document coordinates (a
        box whose row isn't built falls back to its row's top)."""
        kind, key, _ = anchor
        if kind == "row":
            return float(self._rows.offset_of(key))
        box_top = self._box_top(key)
        return box_top if box_top is not None else float(self._rows.offset_of(key[0]))

    def _scroll_to_anchor(self, anchor: ViewAnchor) -> None:
        """Scroll so the anchor sits at its captured screen offset again."""
        if self._rows.total_height() <= 0:
            return
        self._rows.set_scrollregion()
        self._rows.move_view_to(self._anchor_document_top(anchor) - anchor[2])

    def set_width(self, width: int) -> None:
        """Re-lay out the review screen for a new image column width.

        Every row's height depends on the width, so this tears down every
        built row, re-estimates all row heights, then rebuilds around the
        view with the anchor (capture_view_anchor) kept in place: first by
        its row's estimated position, then - once the rows are built and
        measured - by its real one.

        Args:
            width: The new image column width (already clamped).
        """
        if width == self._builder.image_column_width_px:
            self.position()
            return
        started = time.perf_counter()
        anchor = self.capture_view_anchor()
        old_width = self._builder.image_column_width_px
        rows_before = len(self._rows.row_frames)

        self._rows.destroy_all()
        self._builder.image_column_width_px = width
        self._rows.heights = self._builder.estimate_heights()
        self.position()

        # Rough position from the estimates, so reconcile builds the right
        # rows; then the exact one from the built rows' real geometry.
        self._scroll_to_anchor(anchor)
        self._rows.reconcile()
        self._rows.canvas.update_idletasks()
        self._scroll_to_anchor(anchor)
        self._rows.reconcile()

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        self._rows.log_event(
            "image_column_relayout", old_width=old_width, new_width=width,
            anchor=[anchor[0], anchor[1], round(anchor[2], 1)], duration_ms=duration_ms,
        )
        logger.info(
            "image column resized",
            extra=logging_config.extra(
                old_width=old_width, new_width=width, rows_torn_down=rows_before,
                rows_built=len(self._rows.row_frames), duration_ms=duration_ms,
            ),
        )
