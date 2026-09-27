"""The draggable divider between the review screen's image column and text
column.

Mixed into ReviewFrame, like row_building.py and keyboard_nav.py, since it
reaches into the frame's canvas, row bookkeeping and virtualization core.

The divider is a thin bar place()'d over the canvas at the column boundary -
each row is its own Frame, so no single paned widget could span them all.
Dragging it only moves the bar; releasing it applies the new width
(_set_image_column_width), which changes every row's height (images are
fitted to the column width, and the original-text label wraps at it), so it
goes through the same path a scroll does: every built row is torn down, all
row heights are re-estimated, and _reconcile rebuilds the rows around the
view. The per-box model (slot_state.py) keeps edits, cursor, undo history and
checkbox state across that rebuild, as it does for any scroll-driven one.

The column width is kept as a fraction of the canvas width, so it keeps its
proportion when the window is resized; ReviewFrame's caller persists that
fraction per chatlog (on_image_column_fraction_changed).
"""

import bisect
import time
import tkinter as tk
from typing import Optional, Tuple

from .. import logging_config
from . import theme
from .layout_constants import COLUMN_DIVIDER_WIDTH_PX, COLUMN_PADX_PX, IMAGE_COLUMN_LEFT_PX
from .virtualization import clamp_image_column_width, image_column_width_for_fraction

logger = logging_config.get_logger(__name__)

# How long to wait after the canvas's last width change (e.g. while the
# window is being resized) before re-laying out rows for the new width.
RESIZE_DEBOUNCE_MS = 150

# Divider color at rest and while hovered/dragged.
_DIVIDER_COLOR = "#4a4a4a"
_DIVIDER_ACTIVE_COLOR = theme.DARK_FOCUS_HIGHLIGHT

# (kind, key, screen_offset_px): the box (kind "box", key an (index, role)
# slot) or row (kind "row", key an item index) whose top edge should stay at
# screen_offset_px from the top of the viewport across a relayout.
ViewAnchor = Tuple[str, object, float]


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


class ColumnDividerMixin:
    def _build_column_divider(self) -> None:
        """Create the divider bar and its drag bindings. Placed by
        _position_column_divider once the canvas has a real width."""
        divider = tk.Frame(
            self, width=COLUMN_DIVIDER_WIDTH_PX, bg=_DIVIDER_COLOR,
            cursor="sb_h_double_arrow", highlightthickness=0, borderwidth=0,
        )
        divider.bind("<Enter>", lambda e: divider.configure(bg=_DIVIDER_ACTIVE_COLOR))
        divider.bind(
            "<Leave>",
            lambda e: None if self._divider_drag_width is not None else divider.configure(bg=_DIVIDER_COLOR),
        )
        divider.bind("<ButtonPress-1>", self._on_divider_press)
        divider.bind("<B1-Motion>", self._on_divider_drag)
        divider.bind("<ButtonRelease-1>", self._on_divider_release)
        self._column_divider = divider
        # The width the divider has been dragged to, while a drag is in
        # progress; None otherwise.
        self._divider_drag_width: Optional[int] = None
        self._resize_job: Optional[str] = None

    def _position_column_divider(self, image_column_width_px: Optional[int] = None) -> None:
        """Place the divider at the boundary for `image_column_width_px`
        (the current column width if omitted)."""
        width = self._image_column_width_px if image_column_width_px is None else image_column_width_px
        # Placed relative to the canvas, so its x is in canvas coordinates
        # (the same ones the rows are laid out in).
        self._column_divider.place(
            in_=self._canvas, x=divider_x_for_width(width), y=0,
            width=COLUMN_DIVIDER_WIDTH_PX, relheight=1.0,
        )

    def _canvas_width(self) -> int:
        return max(self._canvas.winfo_width(), 1)

    def _on_divider_press(self, event: tk.Event) -> str:
        self._divider_drag_width = self._image_column_width_px
        self._column_divider.configure(bg=_DIVIDER_ACTIVE_COLOR)
        self._log_event("divider_drag_start", width=self._image_column_width_px)
        return "break"

    def _on_divider_drag(self, event: tk.Event) -> str:
        """Move the divider with the pointer (clamped); rows are only
        re-laid out on release."""
        if self._divider_drag_width is None:
            return "break"
        pointer_x = event.x_root - self._canvas.winfo_rootx()
        width = clamp_image_column_width(width_for_divider_x(pointer_x), self._canvas_width())
        self._divider_drag_width = width
        self._position_column_divider(width)
        return "break"

    def _on_divider_release(self, event: tk.Event) -> str:
        """Apply the dragged-to width and report the new fraction."""
        width = self._divider_drag_width
        self._divider_drag_width = None
        self._column_divider.configure(bg=_DIVIDER_COLOR)
        if width is None:
            return "break"
        self._log_event("divider_drag_end", width=width)
        self._image_column_fraction = width / self._canvas_width()
        self._set_image_column_width(width)
        if self._on_image_column_fraction_changed is not None:
            self._on_image_column_fraction_changed(self._image_column_fraction)
        return "break"

    def _width_for_current_canvas(self) -> int:
        """The column width the stored fraction gives at the current canvas
        width - or, with no fraction yet, the current width clamped to it
        (and that becomes the fraction, so a later window resize keeps the
        proportion)."""
        canvas_width = self._canvas_width()
        if self._image_column_fraction is None:
            width = clamp_image_column_width(self._image_column_width_px, canvas_width)
            self._image_column_fraction = width / canvas_width
            return width
        return image_column_width_for_fraction(self._image_column_fraction, canvas_width)

    def _schedule_width_sync(self) -> None:
        """Debounced: re-lay out rows if the canvas width change (a window
        resize) means a different column width."""
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(RESIZE_DEBOUNCE_MS, self._run_width_sync)

    def _run_width_sync(self) -> None:
        self._resize_job = None
        if self._materialized_range is None:
            return  # _apply_initial_position hasn't run yet; it syncs itself
        self._set_image_column_width(self._width_for_current_canvas())

    def _capture_view_anchor(self) -> ViewAnchor:
        """What should stay in place on screen across a relayout: the
        focused box's top edge if it's in view, otherwise the top edge of
        the row at the top of the view."""
        canvas = self._canvas
        view_top = canvas.canvasy(0)
        view_bottom = canvas.canvasy(canvas.winfo_height())
        focused = self._focused_slot()
        if focused is not None:
            view = self._slot_views.get(focused)
            row = self._row_frames.get(focused[0])
            if view is not None and view.container is not None and row is not None:
                try:
                    box_top = self._box_document_top(focused[0], view.container, row)
                except tk.TclError:
                    box_top = None
                if box_top is not None and view_top <= box_top < view_bottom:
                    return ("box", focused, box_top - view_top)

        offsets = [0]
        for height in self._row_heights:
            offsets.append(offsets[-1] + height)
        index = max(0, min(bisect.bisect_right(offsets, view_top) - 1, len(self._row_heights) - 1))
        return ("row", index, offsets[index] - view_top)

    def _anchor_document_top(self, anchor: ViewAnchor) -> Optional[float]:
        """Where the anchor's top edge is now, in document coordinates -
        None if it's a box whose row isn't built."""
        kind, key, _ = anchor
        if kind == "row":
            return float(self._offset_of(key))
        index = key[0]
        view = self._slot_views.get(key)
        row = self._row_frames.get(index)
        if view is None or view.container is None or row is None:
            return float(self._offset_of(index))
        try:
            return self._box_document_top(index, view.container, row)
        except tk.TclError:
            return float(self._offset_of(index))

    def _scroll_to_anchor(self, anchor: ViewAnchor) -> None:
        """Scroll so the anchor sits at its captured screen offset again."""
        total_height = sum(self._row_heights)
        document_top = self._anchor_document_top(anchor)
        if total_height <= 0 or document_top is None:
            return
        canvas = self._canvas
        canvas.configure(scrollregion=(0, 0, self._canvas_width(), total_height))
        view_top = max(0.0, document_top - anchor[2])
        canvas.yview_moveto(view_top / total_height)

    def _set_image_column_width(self, width: int) -> None:
        """Re-lay out the review screen for a new image column width.

        Every row's height depends on the width, so this tears down every
        built row, re-estimates all row heights, then rebuilds around the
        view with the anchor (_capture_view_anchor) kept in place: first by
        its row's estimated position, then - once the rows are built and
        measured - by its real one.

        Args:
            width: The new image column width (already clamped).
        """
        if width == self._image_column_width_px:
            self._position_column_divider()
            return
        started = time.perf_counter()
        anchor = self._capture_view_anchor()
        old_width = self._image_column_width_px
        rows_before = len(self._row_frames)

        for index in list(self._row_frames):
            self._destroy_row(index)
        self._materialized_range = None
        self._image_column_width_px = width
        self._row_heights = self._estimate_row_heights()
        self._position_column_divider()

        # Rough position from the estimates, so _reconcile builds the right
        # rows; then the exact one from the built rows' real geometry.
        self._scroll_to_anchor(anchor)
        self._reconcile()
        self._canvas.update_idletasks()
        self._scroll_to_anchor(anchor)
        self._reconcile()

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        self._log_event(
            "image_column_relayout", old_width=old_width, new_width=width,
            anchor=[anchor[0], anchor[1], round(anchor[2], 1)], duration_ms=duration_ms,
        )
        logger.info(
            "image column resized",
            extra=logging_config.extra(
                old_width=old_width, new_width=width, rows_torn_down=rows_before,
                rows_built=len(self._row_frames), duration_ms=duration_ms,
            ),
        )
