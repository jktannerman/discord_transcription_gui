"""The review screen's windowing core: a scrollable canvas that only ever
has a bounded window of rows built as real widgets.

This is the standard "virtualized/windowed list" pattern (react-window,
Android RecyclerView, ...). reconcile() recomputes which row-index range
should be built as a *pure function* of the canvas's scroll position and
each row's height (estimated until a row is built and measured, then its
real height - see `heights`), via virtualization.compute_visible_range, and
builds/destroys rows to match. That recomputation is idempotent: calling
reconcile() twice with no scroll movement in between yields the same range
and is a no-op the second time, so there is no stored window state that
could drift or oscillate.

The built rows are packed into one child frame, which is repositioned with
canvas.coords() to sit at its first row's true offset within the full
virtual document - not left at (0, 0) - and the scrollregion is set
explicitly from `heights`, not from that frame's (partial) bbox. Nothing may
repaint while the block is out of place, so every teardown/build is
followed by a coords() call before the idle queue is flushed.

VirtualRows knows nothing about what a row contains. Its owner supplies:
- fill_row(index, row): build row `index`'s widgets inside `row`, a frame
  this class creates, packs and measures;
- on_row_destroying(index): called just before row `index` is destroyed;
- after_reconcile(): called at the end of every reconcile pass;
- on_width_change(): called when the canvas is resized.

reconcile() is normally debounced (schedule_reconcile): a fast scroll burst
fires many events, and building rows on every one blocked the event loop.
Coalescing is safe because reconcile() is idempotent.

A row's first-ever build can measure as winfo_height()==1: a widget has no
real geometry until the window system has mapped it, which only update()
(not update_idletasks()) pumps. reconcile() therefore settles new rows'
geometry before measuring them (_settle_pending_geometry), which can end up
calling reconcile() reentrantly via update() - expected and safe, since a
nested call computes the same range and builds nothing. See
docs/ARCHITECTURE_REVIEW_SCREEN.md.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from .. import logging_config
from . import theme
from .layout_constants import (
    ROW_FRAME_BORDERWIDTH_PX,
    ROW_FRAME_PADDING_PX,
    ROW_PACK_PADX_PX,
    ROW_PACK_PADY_PX,
)
from .virtualization import compute_visible_range

logger = logging_config.get_logger(__name__)
# High-frequency per-scroll-tick tracing goes to its own log file - see
# logging_config.setup_logging.
trace_logger = logging_config.get_trace_logger()

# How many extra viewport-heights worth of rows to keep built above and
# below the visible area, so scrolling a little doesn't trigger a rebuild.
# Also the margin images are loaded within (see ReviewFrame).
SCROLL_BUFFER_VIEWPORTS = 1

# How long to wait, after the most recent scroll event, before actually
# reconciling - see schedule_reconcile.
DEBOUNCE_MS = 80


class VirtualRows:
    """A canvas + scrollbar showing a long list of rows, only some built.

    Attributes:
        canvas: The scrolling canvas.
        scrollbar: Its scrollbar.
        heights: Every row's height (px) in the virtual document, including
            the pack gap outside its frame - estimated until the row is
            built, then measured. The owner sets the initial estimates.
        row_frames: {index: row frame} for the rows currently built.
        materialized_range: Inclusive (first, last) indices built by the
            last reconcile, or None before the first one.
        frozen: While True, the scrollbar is disabled and the owner's
            scroll-input handlers should ignore input (see set_frozen).
    """

    # Bound on _settle_pending_geometry's retry loops.
    _SETTLE_MAX_ATTEMPTS = 10

    def __init__(
        self,
        parent: tk.Widget,
        fill_row: Callable[[int, tk.Widget], None],
        on_row_destroying: Callable[[int], None],
        after_reconcile: Callable[[], None],
        on_width_change: Callable[[], None],
    ) -> None:
        """Create the scrollbar and canvas, packed into `parent`.

        Args:
            parent: The widget to pack into (it should use pack for these).
            fill_row: Builds row `index`'s contents inside the given frame.
            on_row_destroying: Called with a row's index just before the row
                is destroyed.
            after_reconcile: Called at the end of every reconcile pass.
            on_width_change: Called when the canvas's size changes.
        """
        self._fill_row = fill_row
        self._on_row_destroying = on_row_destroying
        self._after_reconcile = after_reconcile
        self._on_width_change = on_width_change

        self.heights: list[int] = []
        self.row_frames: dict[int, tk.Widget] = {}
        self.materialized_range: Optional[tuple[int, int]] = None
        self.frozen = False
        self._update_job: Optional[str] = None
        # Stamped on every trace line so they can be ordered exactly.
        self._event_seq = 0

        self.scrollbar = ttk.Scrollbar(parent, orient="vertical")
        self.scrollbar.pack(side="right", fill="y")
        self.canvas = tk.Canvas(parent, borderwidth=0, highlightthickness=0, bg=theme.DARK_BG_ALT)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.configure(command=self._on_scrollbar)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self._scroll_frame = ttk.Frame(self.canvas)
        self._canvas_window = self.canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Destroy>", self._on_destroy)

    # -- input -------------------------------------------------------------

    def _on_scrollbar(self, *args: str) -> None:
        if self.frozen:
            return
        self.log_event("input_scrollbar", args=args)
        self.canvas.yview(*args)
        self.schedule_reconcile()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfig(self._canvas_window, width=event.width)
        self.log_event("input_canvas_configure", width=event.width, height=event.height)
        self.schedule_reconcile()
        self._on_width_change()

    def _on_destroy(self, event: tk.Event) -> None:
        if self._update_job is not None:
            self.canvas.after_cancel(self._update_job)
            self._update_job = None

    def set_frozen(self, frozen: bool) -> None:
        """Freeze or unfreeze scrolling (e.g. while a popup menu is open).

        Disables the scrollbar itself; wheel and key handlers check `frozen`.

        Args:
            frozen: True to freeze.
        """
        self.frozen = frozen
        self.scrollbar.state(["disabled"] if frozen else ["!disabled"])

    def scroll_by(self, amount: int, what: str) -> None:
        """Scroll the canvas and schedule a reconcile.

        Args:
            amount: How far, negative for up.
            what: "units" or "pages", as for Canvas.yview_scroll.
        """
        self.canvas.yview_scroll(amount, what)
        self.schedule_reconcile()

    # -- geometry queries ----------------------------------------------------

    def offset_of(self, index: int) -> int:
        """Pixel offset of row `index`'s slot within the virtual document."""
        return sum(self.heights[:index])

    def total_height(self) -> int:
        return sum(self.heights)

    def canvas_width(self) -> int:
        return max(self.canvas.winfo_width(), 1)

    def viewport_height(self) -> int:
        return self.canvas.winfo_height()

    def view_top(self) -> float:
        """Document y at the top edge of the viewport."""
        return self.canvas.canvasy(0)

    def view_bottom(self) -> float:
        """Document y at the bottom edge of the viewport."""
        return self.canvas.canvasy(self.canvas.winfo_height())

    def at_bottom(self) -> bool:
        """Whether the view reaches the end of the document (also true when
        everything fits on screen)."""
        return self.canvas.yview()[1] >= 0.999

    def scroll_top_fraction(self) -> float:
        return self.canvas.yview()[0]

    # -- moving the view -------------------------------------------------------

    def set_scrollregion(self) -> None:
        """Set the scrollregion to the full virtual document.

        yview_moveto is silently ignored while there is no scrollregion, so
        anything that moves the view before the first reconcile sets it first.
        """
        self.canvas.configure(scrollregion=(0, 0, self.canvas_width(), self.total_height()))

    def move_view_to(self, document_y: float) -> None:
        """Put document_y (clamped at 0) at the top of the viewport."""
        total_height = self.total_height()
        if total_height > 0:
            self.canvas.yview_moveto(max(document_y, 0) / total_height)

    def ensure_materialized(self, index: int) -> None:
        """Make sure row `index` is built, jumping the view there first if it
        isn't. Cheap and safe when it's already built, since reconcile
        always recomputes the range from scratch."""
        if index in self.row_frames:
            return
        if self.total_height() > 0:
            self.set_scrollregion()
            self.move_view_to(self.offset_of(index))
        self.reconcile()

    # -- tracing -----------------------------------------------------------------

    def log_event(self, event: str, **fields: object) -> None:
        """Log one step of scroll/page/focus/resize handling to the scroll
        trace, stamped with a sequence number and the current window/scroll
        state, so a captured trace can be replayed step by step."""
        self._event_seq += 1
        top_frac, bottom_frac = self.canvas.yview()
        trace_logger.debug(
            event,
            extra=logging_config.extra(
                seq=self._event_seq,
                materialized_range=self.materialized_range,
                top_frac=round(top_frac, 4),
                bottom_frac=round(bottom_frac, 4),
                **fields,
            ),
        )

    # -- building and destroying rows -----------------------------------------

    def build_row(self, index: int, before: Optional[tk.Widget] = None) -> tk.Widget:
        """Build row `index` and register it.

        Args:
            index: The row to build.
            before: If given, the row is packed immediately above this
                widget instead of at the bottom (used when growing upward).

        Returns:
            The row's frame.
        """
        pack_kwargs: dict = {"fill": "x", "pady": ROW_PACK_PADY_PX, "padx": ROW_PACK_PADX_PX}
        if before is not None:
            pack_kwargs["before"] = before
        row = ttk.Frame(
            self._scroll_frame, relief="groove",
            borderwidth=ROW_FRAME_BORDERWIDTH_PX, padding=ROW_FRAME_PADDING_PX,
        )
        row.pack(**pack_kwargs)
        self.row_frames[index] = row
        self._fill_row(index, row)
        return row

    def destroy_row(self, index: int) -> None:
        """Tear down row `index`, if built, after telling the owner."""
        row = self.row_frames.pop(index, None)
        if row is None:
            return
        self._on_row_destroying(index)
        row.destroy()

    def destroy_all(self) -> None:
        """Tear down every built row and forget the materialized range."""
        for index in list(self.row_frames):
            self.destroy_row(index)
        self.materialized_range = None

    def _try_build_row(self, index: int, before: Optional[tk.Widget] = None) -> Optional[tk.Widget]:
        """build_row, but one row's failure doesn't abort the rest of a
        reconcile's batch.

        Left uncaught, a failure would leave the rest of the batch unbuilt
        and materialized_range out of step with row_frames. Instead whatever
        was built is torn down and the row is skipped (a later reconcile
        retries it). A backstop: the logged error still needs fixing.

        Returns:
            The row's frame, or None if building it raised.
        """
        try:
            return self.build_row(index, before=before)
        except Exception:
            logger.error(
                "building this row raised an uncaught exception - tearing "
                "down whatever was built and skipping it for now, rather "
                "than aborting the rest of this reconcile's build batch",
                exc_info=True,
                extra=logging_config.extra(index=index),
            )
            if index in self.row_frames:
                self.destroy_row(index)
            return None

    def _sync_materialized_rows(
        self, old_range: Optional[tuple[int, int]], new_range: tuple[int, int]
    ) -> list[int]:
        """Destroy built rows outside new_range, build the ones inside it
        that aren't built yet, and return the indices newly built. Falls
        back to a full destroy-then-rebuild when the ranges don't overlap
        (e.g. a far-away ensure_materialized jump)."""
        new_first, new_last = new_range
        overlap = (
            old_range is not None
            and old_range[0] <= new_last
            and new_first <= old_range[1]
        )

        if not overlap:
            for idx in list(self.row_frames):
                self.destroy_row(idx)
            newly_built = []
            for idx in range(new_first, new_last + 1):
                if self._try_build_row(idx) is not None:
                    newly_built.append(idx)
            return newly_built

        old_first, old_last = old_range
        for idx in list(self.row_frames):
            if idx < new_first or idx > new_last:
                self.destroy_row(idx)
        # Destroying rows above shifts everything left in the block up by
        # their height; move the block down to match right away, before
        # anything can repaint.
        if self.row_frames:
            self.canvas.coords(self._canvas_window, 0, self.offset_of(min(self.row_frames)))

        newly_built: list[int] = []
        # Growth below the old window, appended in index order.
        for idx in range(old_last + 1, new_last + 1):
            if self._try_build_row(idx) is not None:
                newly_built.append(idx)
        # Growth above the old window, inserted back-to-front immediately
        # before the current first row - pack() can only insert relative to
        # a sibling.
        if new_first < old_first:
            anchor = self.row_frames.get(old_first)
            for idx in range(old_first - 1, new_first - 1, -1):
                built = self._try_build_row(idx, before=anchor)
                if built is not None:
                    anchor = built
                    newly_built.append(idx)
        return newly_built

    def _settle_pending_geometry(self, newly_built: list[int]) -> None:
        """Get every newly built row a real winfo_height().

        A never-mapped widget reports winfo_height() == 1 until the window
        system maps it. update_idletasks() alone never resolves that (it
        only runs idle callbacks); update() does, since it processes all
        pending events. So this tries update_idletasks() first (enough for
        a pack layout still settling), then update(), each a bounded number
        of times. update() can re-enter reconcile() (e.g. a pending
        debounced reconcile firing); that nested call builds nothing and is
        what finishes flushing the Map, so it's deliberately not guarded
        against. _remeasure_built_rows skips any row still unsettled.
        """
        canvas = self.canvas

        def unsettled() -> list[int]:
            return [
                idx for idx in newly_built
                if idx in self.row_frames and self.row_frames[idx].winfo_height() <= 1
            ]

        for attempt in range(self._SETTLE_MAX_ATTEMPTS):
            remaining = unsettled()
            if not remaining:
                if attempt:
                    self.log_event("settle_pending_geometry_resolved", attempts=attempt)
                return
            canvas.update_idletasks()

        for attempt in range(self._SETTLE_MAX_ATTEMPTS):
            remaining = unsettled()
            if not remaining:
                self.log_event(
                    "settle_pending_geometry_resolved_via_full_update", attempts=attempt
                )
                return
            canvas.update()

        self.log_event(
            "settle_pending_geometry_gave_up",
            attempts=2 * self._SETTLE_MAX_ATTEMPTS,
            still_unsettled=remaining,
        )

    def _remeasure_built_rows(self, scroll_top: float, newly_built: list[int]) -> float:
        """Replace newly built rows' estimated heights with their real ones,
        and return how far the scroll offset must move to keep the same
        content on screen (the sum of real-minus-estimated heights of rows
        above scroll_top).

        A row's real height is winfo_height() plus 2*ROW_PACK_PADY_PX: the
        pack gap outside its frame is real screen space winfo_height()
        can't see (see docs/ARCHITECTURE_ROW_GEOMETRY.md). A row still
        reporting winfo_height() <= 1 has no real geometry yet and keeps its
        estimate - recording ~0 would corrupt every later row's offset.
        """
        if not newly_built:
            return 0.0
        old_heights = list(self.heights)
        delta = 0.0
        for idx in newly_built:
            real_winfo_height = self.row_frames[idx].winfo_height()
            if real_winfo_height <= 1:
                self.log_event(
                    "remeasure_skipped_unsettled_geometry",
                    index=idx,
                    estimated_height=old_heights[idx],
                )
                continue
            real = real_winfo_height + 2 * ROW_PACK_PADY_PX
            old = old_heights[idx]
            if real and real != old:
                row_offset = sum(old_heights[:idx])
                above_scroll_top = row_offset < scroll_top
                if above_scroll_top:
                    delta += real - old
                self.heights[idx] = real
                self.log_event(
                    "remeasure_mismatch",
                    index=idx,
                    estimated_height=old,
                    real_height=real,
                    row_offset=row_offset,
                    scroll_top=scroll_top,
                    above_scroll_top=above_scroll_top,
                )
        return delta

    def reconcile(self) -> None:
        """Recompute which rows should be built from the scroll position and
        `heights`, build/destroy rows to match, reposition the built block,
        correct the scroll offset for measured heights, and fix up the
        scrollregion. Idempotent: a second call with no scroll movement is
        a no-op (see the module docstring)."""
        canvas = self.canvas
        viewport_height = canvas.winfo_height()
        if viewport_height <= 1:
            return
        scroll_top = canvas.canvasy(0)
        buffer = viewport_height * SCROLL_BUFFER_VIEWPORTS

        first_idx, last_idx = compute_visible_range(
            self.heights, scroll_top, viewport_height, buffer
        )

        old_range = self.materialized_range
        newly_built = self._sync_materialized_rows(old_range, (first_idx, last_idx))
        self.materialized_range = (first_idx, last_idx)
        # Position the block for its new first row (by estimated height, for
        # rows just built above) before update_idletasks repaints; the
        # remeasure below only corrects the remaining estimation error.
        canvas.coords(self._canvas_window, 0, self.offset_of(first_idx))

        canvas.update_idletasks()
        self._settle_pending_geometry(newly_built)
        delta = self._remeasure_built_rows(scroll_top, newly_built)

        total_height = self.total_height()
        corrected_scroll_top = scroll_top
        if delta and total_height > 0:
            corrected_scroll_top = max(0.0, min(scroll_top + delta, total_height))
            canvas.yview_moveto(corrected_scroll_top / total_height)

        self.set_scrollregion()
        canvas.coords(self._canvas_window, 0, self.offset_of(first_idx))

        materialized = sorted(self.row_frames)
        expected = list(range(first_idx, last_idx + 1))
        if materialized != expected:
            # A real bug if it ever fires, but crashing the review screen
            # would lose whatever wasn't autosaved yet - log and carry on.
            logger.error(
                "materialized rows do not match computed range after reconcile",
                extra=logging_config.extra(
                    materialized=materialized, first_idx=first_idx, last_idx=last_idx,
                ),
            )

        self._after_reconcile()
        self.log_event(
            "reconcile",
            old_range=old_range,
            first_idx=first_idx,
            last_idx=last_idx,
            total_height=total_height,
            newly_built=newly_built,
            scroll_top_before=scroll_top,
            delta=delta,
            scroll_top_after=corrected_scroll_top,
        )

    def schedule_reconcile(self) -> None:
        """Coalesce a burst of scroll events into a single reconcile pass,
        run DEBOUNCE_MS after the most recent one."""
        had_pending = self._update_job is not None
        if self._update_job is not None:
            self.canvas.after_cancel(self._update_job)
        self.log_event("debounce_scheduled", had_pending=had_pending)
        self._update_job = self.canvas.after(DEBOUNCE_MS, self._run_scheduled_reconcile)

    def _run_scheduled_reconcile(self) -> None:
        self._update_job = None
        self.log_event("debounce_fired")
        self.reconcile()
