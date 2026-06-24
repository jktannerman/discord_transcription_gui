"""Review screen shown after the OCR batch completes.

An infinite-scroll, paginated listing of every approved message in order.
Every row has the same two-column shape: an immutable left column (the
message's own original text, its image(s), or both stacked text-above-
images - mirroring Discord's own layout - depending on what the message
has) paired with the matching editable text box(es) on the right, stacked
in the same text-above-images order: a copy of the message's own text
whenever it has any, one box per attached image pre-filled with that
image's OCR text, and a one-line "spacer" box (no left-column counterpart
at all) between/after each of those, holding the literal "\n" tokens that
control the blank-line gap on either side of it - independently editable,
so a message with a caption and multiple images gets all of those boxes.
See pipeline.ReviewItem.slot_roles for the exact ordering and
ARCHITECTURE.md's "Spacer slots" section for the full design. Copy/paste
and arbitrary edits are allowed in every text box; nothing is parsed or
restricted there. Nothing is written to disk until the Finalize button at
the bottom is clicked, which writes every message's final lines in one
pass.

Only a bounded window of rows is ever materialized as widgets at once,
rather than every message in the transcript - building hundreds of
full-size image rows and 30-line Text widgets up front is what made the
screen laggy, and that cost scaled with transcript length regardless of
any per-row image lazy-loading. This is the standard "virtualized/windowed
list" pattern (as used by react-window, Android RecyclerView, iOS
UITableView, etc.): _reconcile() recomputes which item-index range should
be materialized as a *pure function* of the canvas's current scroll
position and each item's row height (estimated via estimate_row_height
until a row is actually built and measured, then its real winfo_height()
- see self._row_heights), via compute_visible_range (virtualization.py),
and reconciles the actually-built rows to match. Crucially, this
recomputation is idempotent: calling _reconcile() twice with no scroll
movement in between always yields the same range and is a no-op the
second time.

That idempotency is the whole reason this design replaced an earlier one
that tracked window_start/window_end as mutable state and stepped it
forward/backward incrementally (_advance_forward/_advance_backward),
preserving scroll position across each step by reverse-engineering a
corrective fraction from how far a surviving "anchor" row moved on screen.
Near the start/end of the item list, that fraction could fall outside
[0,1]; Tk's yview_moveto silently clamped it, which deterministically left
the viewport positioned so the *opposite* paging direction's trigger
condition became true - and because every step also rescheduled the same
debounced refresh that re-ran the trigger check, this became a
self-sustaining oscillation between two windows that ran forever with no
further user input. Representing scroll position as an absolute pixel
offset (via self._row_heights) rather than a fraction of total height, and
recomputing the materialized range from scratch on every tick rather than
incrementally stepping it, makes that failure mode structurally
impossible rather than just less likely: there's no separate "should I
page forward" vs. "should I page backward" check that can disagree about
the resting state, because there's only one range computation.

This windowing core (_reconcile/_sync_materialized_rows/
_remeasure_built_rows/_offset_of/_ensure_materialized/_destroy_row) is
deliberately kept together in this one class rather than split further,
since it's exactly the state that disagreed with itself in the bug above -
splitting it across files/objects would relocate that risk, not remove it.
Building a single row's widgets (_build_row and friends) doesn't carry that
same risk - it's mixed in from row_building.py's RowBuildingMixin, the same
way image lazy-loading and keyboard navigation are self-contained enough to
live in image_loading.py and keyboard_nav.py. Pure layout math that doesn't
touch widget state lives in virtualization.py.

The materialized rows are still packed into a single child Frame
(self._scroll_frame) so Tk handles their relative stacking for free, but
that Frame is repositioned via canvas.coords() on every _reconcile to sit
at its materialized range's true offset within the full virtual document
- it is deliberately NOT left at canvas position (0, 0). The canvas's
scrollregion is likewise set explicitly from self._row_heights (the full
document height), not derived from this frame's own bbox (which would
only ever reflect the small materialized subset).

Edits made in a row are preserved in self._saved_texts before that row is
torn down, and restored if the row is rebuilt later. The currently focused
text box keeps focus across a reconcile if it's still in the new
materialized range; otherwise focus is simply lost, same as scrolling a
focused widget off-screen.

Images are loaded/decoded lazily within the materialized window, only for
rows within (or near) the visible viewport, and unloaded again once
scrolled away (see image_loading.py).

Keyboard shortcuts (see keyboard_nav.py), bound per text box (and globally
for Page Up/Down) since Tk's defaults either don't cover these or actively
conflict with them: Ctrl+Backspace deletes the previous word; Tab/Shift-Tab
move between text boxes in transcript order, materializing the target row
via _ensure_materialized if it isn't already, landing on the Finalize
button once there's no text box left to advance to; Page Up/Down scroll the
whole window rather than (Tk's default) scrolling within whichever Text
widget has focus; Ctrl+Z/Ctrl+Shift+Z undo/redo within a single text box,
using Tk's built-in per-widget undo stack - kept alive across that box's
row being torn down and rebuilt by replaying its recorded edit history onto
the fresh widget (see text_undo.py), though not across the app being
restarted.

_reconcile is debounced (see DEBOUNCE_MS): fast scrolling fires many
wheel/scrollbar events in quick succession, and running it synchronously
on every single one blocked the Tk event loop with back-to-back row
builds/image decodes, which both caused lag and produced "ghost" partial
images (a widget's image= being swapped again before Tk finished painting
the previous swap). Debouncing collapses a burst of events into a single
pass once scrolling actually pauses - this is safe specifically because
_reconcile is idempotent, so coalescing several scroll events into one
pass changes only timing, never the result.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Tuple

from .. import logging_config
from ..pipeline import ReviewItem
from . import theme
from .image_loading import ImageLoader
from .keyboard_nav import KeyboardNavMixin
from .layout_constants import ROW_PACK_PADY_PX
from .row_building import RowBuildingMixin
from .text_undo import UndoLog
from .virtualization import compute_visible_range, estimate_row_height

logger = logging_config.get_logger(__name__)
# High-frequency per-scroll-tick tracing (reconcile/debounce/remeasure/
# image-load/box-resize events, all via _log_event) goes to its own log
# file/logger rather than `logger` above - see logging_config.setup_logging
# for why this is split out of the main app.log.
trace_logger = logging_config.get_trace_logger()

# Row/text-box construction (sizing constants included) lives in
# row_building.py's RowBuildingMixin - see that module's docstring for why
# it's split out from this one.

# How many extra viewport-heights worth of rows to keep loaded above and
# below the visible area, so scrolling a little doesn't trigger a reload
# and neighboring messages are visible for spacing context. Used both for
# image load/unload and for how many rows beyond the viewport get
# materialized as widgets - see compute_visible_range.
SCROLL_BUFFER_VIEWPORTS = 1

# How long to wait, after the most recent scroll event, before actually
# reconciling (see ReviewFrame._reconcile). Keeps a fast multi-event scroll
# burst from triggering a row build/image decode on every single tick.
DEBOUNCE_MS = 80


class ReviewFrame(KeyboardNavMixin, RowBuildingMixin, ttk.Frame):
    def __init__(
        self,
        master: tk.Widget,
        items: List[ReviewItem],
        on_finalize: Callable[[List[Dict[str, Optional[str]]]], None],
        initial_saved_texts: Optional[List[Dict[str, Optional[str]]]] = None,
        initial_focus_slot: Optional[Tuple[int, str]] = None,
        initial_scroll_fraction: Optional[float] = None,
    ):
        super().__init__(master)
        logger.info(
            "building review screen",
            extra=logging_config.extra(
                item_count=len(items), resuming=initial_saved_texts is not None
            ),
        )
        self._items = items
        self._on_finalize = on_finalize
        self._initial_focus_slot = initial_focus_slot
        self._initial_scroll_fraction = initial_scroll_fraction
        # Flat, transcript-ordered list of every editable box this item
        # list has, as (item_index, role) pairs - one entry per
        # item.slot_roles (see pipeline.ReviewItem), in order: "message" (a
        # copy of the message's own text), "ocr{N}" (the Nth attached
        # image's OCR text), and a "spacer_*" box between/after each of
        # those, all stacked to match _build_row's layout. This is what
        # Tab/Shift-Tab navigate (see keyboard_nav.py) and what
        # get_focused_slot/the resume focus-restore path address a box by,
        # since a single item index is no longer enough to identify one.
        self._slots: List[Tuple[int, str]] = [
            (idx, role) for idx, item in enumerate(items) for role in item.slot_roles
        ]
        self._slot_positions: Dict[Tuple[int, str], int] = {
            slot: pos for pos, slot in enumerate(self._slots)
        }
        # Per-item row height (px), seeded with cheap estimates and
        # overwritten with the real winfo_height() once a row is built -
        # the source of truth for the full virtual document's layout, used
        # to compute which index range should be materialized and to set
        # the canvas's scrollregion (see _reconcile). Only items in
        # [_materialized_range[0], _materialized_range[1]] (inclusive)
        # currently have widgets; None until the first _reconcile call.
        self._row_heights: List[int] = [estimate_row_height(item) for item in items]
        self._materialized_range: Optional[Tuple[int, int]] = None
        self._row_frames: Dict[int, tk.Widget] = {}
        # All per-box bookkeeping below is keyed by (item_index, role) -
        # see self._slots - since a row can now have any number of
        # independent boxes (and matching immutable originals) rather than
        # at most two.
        self._text_widgets: Dict[Tuple[int, str], tk.Text] = {}
        self._text_containers: Dict[Tuple[int, str], tk.Widget] = {}
        self._images = ImageLoader()
        # Text captured from a box just before its row is torn down, so
        # edits survive a row being paged out and back in. Absence means
        # "never edited/visited" - fall back to the item's initial_*_text.
        # Seeded from a saved session's edits when resuming, rather than
        # starting blank.
        self._saved_texts: Dict[Tuple[int, str], str] = {}
        if initial_saved_texts is not None and len(initial_saved_texts) == len(items):
            for idx, edited in enumerate(initial_saved_texts):
                for role, text in edited.items():
                    if text is not None:
                        self._saved_texts[(idx, role)] = text
        # Cursor ("insert" mark) position captured alongside self._saved_texts
        # when a box's row is torn down, so paging a focused box's row out and
        # back in (e.g. a fast Page Up/Down burst that outruns the
        # virtualization buffer - see _destroy_row) restores the cursor to
        # where it was rather than resetting it to the box's start. Not
        # persisted across a session save/resume - only self._saved_texts is -
        # so a resumed box's cursor still starts at "1.0", same as before.
        self._saved_cursor: Dict[Tuple[int, str], str] = {}
        # One UndoLog per box, recording every insert/delete/undo/redo it's
        # had since first built this session (see text_undo.py) - replayed
        # onto a fresh widget when that box's row is rebuilt after being
        # paged out, so Ctrl+Z keeps reaching back through edits made before
        # the teardown rather than starting blank. Never seeded from a
        # resumed session - only self._saved_texts is - so undo history
        # genuinely doesn't persist across app launches, just within one.
        self._undo_logs: Dict[Tuple[int, str], UndoLog] = {}
        # detach() callback from text_undo.attach_undo_recording, one per
        # currently-built box - called in _destroy_row just before that
        # box's widget is destroyed, to release the Tcl command the
        # recording proxy installed.
        self._undo_detach: Dict[Tuple[int, str], Callable[[], None]] = {}
        # The slot whose box had focus at the moment its row was torn down
        # (see _destroy_row), restored once that row is rebuilt - see
        # _build_row. None means either nothing was focused when a row was
        # last destroyed, or that restore has already happened.
        self._refocus_slot: Optional[Tuple[int, str]] = None
        self._update_job: Optional[str] = None
        self._initial_position_job: Optional[str] = None
        # Monotonic counter stamped on every _log_event call, purely so log
        # lines can be ordered exactly even if two land in the same
        # millisecond - used to reconstruct the precise sequence of
        # scroll/page/focus events that leads into a pagination loop.
        self._event_seq = 0

        # Scrollbar + canvas fill the whole frame - the Finalize button used
        # to live in a row permanently packed below them, which cost every
        # screenful of review the same slice of vertical space whether or
        # not the button was ever relevant yet. It's built further below as
        # a place()'d overlay instead, shown only once scrolled to the very
        # end of the transcript (see _update_finalize_button_visibility),
        # so the canvas gets the full frame height the rest of the time.
        scrollbar = ttk.Scrollbar(self, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0, bg=theme.DARK_BG_ALT)
        canvas.pack(side="left", fill="both", expand=True)
        self._canvas = canvas

        def _on_scrollbar(*args):
            self._log_event("input_scrollbar", args=args)
            canvas.yview(*args)
            self._schedule_reconcile()

        scrollbar.configure(command=_on_scrollbar)
        canvas.configure(yscrollcommand=scrollbar.set)

        self._scroll_frame = ttk.Frame(canvas)
        # Repositioned (via canvas.coords) on every _reconcile to sit at the
        # materialized window's true offset within the full virtual
        # document - NOT left at (0, 0). The scrollregion is likewise set
        # explicitly from self._row_heights in _reconcile, not derived from
        # this frame's own (materialized-only) bbox - using the automatic
        # canvas.bbox("all")-on-<Configure> binding that used to be here
        # would clobber that explicit full-document scrollregion back down
        # to just the materialized subset on every row build/destroy.
        self._canvas_window = canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")

        def _on_canvas_configure(e):
            canvas.itemconfig(self._canvas_window, width=e.width)
            self._log_event("input_canvas_configure", width=e.width, height=e.height)
            self._schedule_reconcile()

        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(e):
            self._log_event("input_mousewheel", delta=e.delta, widget=str(e.widget))
            # e.widget is whichever widget the cursor is actually over when
            # the wheel event fires (bind_all dispatches using the real
            # target, not just focus) - so hovering a scrollable text box
            # scrolls *it* first, and only once it's scrolled as far as it
            # can go in that direction does the wheel fall through to
            # scrolling the whole review window, same as if the box weren't
            # there at all.
            if isinstance(e.widget, tk.Text) and self._scroll_text_widget(e.widget, e.delta):
                return
            canvas.yview_scroll(int(-e.delta / 120), "units")
            self._schedule_reconcile()

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        # Global fallback for Page Up/Down so they scroll the review window
        # even when focus is on the Finalize button rather than a text box
        # (each text box also gets its own binding in _build_row, which
        # takes precedence and overrides Tk's default Text page-scrolling).
        canvas.bind_all("<Prior>", self._on_page_up)
        canvas.bind_all("<Next>", self._on_page_down)
        # bind_all is global, so undo it when this frame goes away, otherwise
        # the next screen's scrolling would dispatch to this destroyed canvas
        def _on_destroy(e):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Prior>")
            canvas.unbind_all("<Next>")
            if self._update_job is not None:
                self.after_cancel(self._update_job)
                self._update_job = None
            if self._initial_position_job is not None:
                self.after_cancel(self._initial_position_job)
                self._initial_position_job = None

        self.bind("<Destroy>", _on_destroy)

        # Floating Finalize button - place()'d (not packed/gridded) so it
        # overlays the canvas rather than claiming a permanent slice of the
        # frame's own layout, and positioned relative to `self` (the static
        # window, not the scrolling document) so it stays pinned to the
        # bottom of the viewport regardless of scroll position. Hidden by
        # default; _update_finalize_button_visibility shows it only once
        # scrolled to the very end of the transcript - see that method.
        button_row = ttk.Frame(self, relief="raised", borderwidth=1, padding=(16, 8))
        self._finalize_button = ttk.Button(
            button_row, text="Finalize and write to file", command=self._on_finalize_clicked
        )
        self._finalize_button.pack()
        self._finalize_button.bind("<Shift-Tab>", self._on_shift_tab)
        self._finalize_button_row = button_row
        self._finalize_button_visible = False

        self.after_idle(self._apply_initial_position)

    def _apply_initial_position(self) -> None:
        """First-layout hook, run once via after_idle in place of a plain
        _reconcile call: restores a resumed session's focus/scroll position
        if one was given, otherwise just reconciles at the top like a fresh
        review screen. Focusing a row's text box already scrolls it into
        view (_focus_text_box), so the focus-slot case subsumes the
        scroll-fraction one - the latter is only used as a fallback when a
        session was saved with no box focused (e.g. focus was on the
        Finalize button), or when the saved slot is no longer valid (e.g.
        the chatlog changed between sessions and that exact (index, role)
        pair doesn't exist in this run's items).

        _ensure_materialized/_focus_text_box need a real canvas height to
        compute scroll offsets against, which isn't guaranteed yet on the
        very first after_idle tick (the plain _reconcile() this replaced
        tolerated that via its own early-return-on-unsized-canvas guard,
        relying on a later <Configure> event to retry) - so this polls
        briefly until the canvas actually has one, rather than risking a
        focus/scroll jump computed against a height of 0."""
        if self._canvas.winfo_height() <= 1:
            self._initial_position_job = self.after(20, self._apply_initial_position)
            return
        if self._initial_focus_slot is not None and self._initial_focus_slot in self._slot_positions:
            index, role = self._initial_focus_slot
            self._ensure_materialized(index)
            self._focus_text_box(index, role)
            return
        if self._initial_scroll_fraction is not None:
            self._canvas.yview_moveto(self._initial_scroll_fraction)
        self._reconcile()

    def _scroll_text_widget(self, text_widget: tk.Text, delta: int) -> bool:
        """Try to scroll an editable text box by one wheel notch in the
        direction of `delta`. Returns False (does nothing) if the box is
        already at its limit in that direction - e.g. a box with no
        scrollbar (content fits already) is always "at its limit" in both
        directions, so this is a no-op for it and the wheel event falls
        through to scrolling the whole review window, exactly as before
        this box-local-scroll feature existed."""
        first, last = text_widget.yview()
        scrolling_up = delta > 0
        at_limit = first <= 0.0 if scrolling_up else last >= 1.0
        if at_limit:
            return False
        text_widget.yview_scroll(int(-delta / 120), "units")
        return True

    def _log_event(self, event: str, **fields) -> None:
        """Log one step of scroll/page/focus/resize handling to the dedicated
        scroll-trace log (see logging_config.get_trace_logger), stamped with
        a sequence number plus the canvas's current window/scroll state, so
        a captured log can be replayed step-by-step to see exactly what
        triggered what during a pagination loop. Every other call site that
        used to log this category of event directly (image load/unload,
        text-box resize) now goes through this instead, so nothing in this
        category falls outside the seq/scroll-state correlation."""
        self._event_seq += 1
        top_frac, bottom_frac = self._canvas.yview()
        trace_logger.debug(
            event,
            extra=logging_config.extra(
                seq=self._event_seq,
                materialized_range=self._materialized_range,
                top_frac=round(top_frac, 4),
                bottom_frac=round(bottom_frac, 4),
                **fields,
            ),
        )

    def _destroy_row(self, index: int) -> None:
        """Tear down the row widget(s) for items[index], saving any edited
        text first so it can be restored if the row is paged back in - and,
        if one of its boxes currently has focus, its cursor position too
        (self._saved_cursor) plus the slot itself (self._refocus_slot), so
        _build_row can restore both once this row is rebuilt rather than
        just silently dropping focus the way scrolling a focused widget
        off-screen normally would."""
        row = self._row_frames.pop(index, None)
        if row is None:
            return
        focused = self.focus_get()
        for key in [k for k in self._text_widgets if k[0] == index]:
            text_widget = self._text_widgets.pop(key)
            self._saved_texts[key] = text_widget.get("1.0", "end-1c")
            self._saved_cursor[key] = text_widget.index("insert")
            if text_widget is focused:
                self._refocus_slot = key
            self._text_containers.pop(key, None)
            detach = self._undo_detach.pop(key, None)
            if detach is not None:
                detach()
        self._images.unregister_row(index)
        row.destroy()

    def _offset_of(self, index: int) -> int:
        """Pixel offset of items[index]'s top within the full virtual
        document, per self._row_heights."""
        return sum(self._row_heights[:index])

    def _ensure_materialized(self, index: int) -> None:
        """Make sure items[index]'s row is built, jumping the scroll
        position there first if it's nowhere near the current viewport
        (e.g. a far-away Tab target). Cheap and safe to call even when the
        row is already materialized, since _reconcile always recomputes
        the materialized range from scratch rather than incrementally
        stepping it - see the module docstring.

        The scrollregion has to be set here, before yview_moveto, rather
        than left to _reconcile - on the very first call (before any
        reconcile has ever run, e.g. restoring a resumed session's focus
        straight out of __init__), the canvas has no scrollregion at all
        yet, so yview_moveto(fraction) has nothing to scroll within and is
        silently a no-op: the view stays at the top, _reconcile then
        materializes from index 0 as usual, and the target index is never
        actually built - which is exactly the bug this fixes."""
        if index in self._row_frames:
            return
        total_height = sum(self._row_heights)
        if total_height > 0:
            canvas = self._canvas
            canvas.configure(scrollregion=(0, 0, max(canvas.winfo_width(), 1), total_height))
            canvas.yview_moveto(self._offset_of(index) / total_height)
        self._reconcile()

    def _sync_materialized_rows(
        self, old_range: Optional[Tuple[int, int]], new_range: Tuple[int, int]
    ) -> List[int]:
        """Destroy materialized rows outside new_range, build rows inside
        it that aren't materialized yet, and return the indices that were
        newly built this call. Falls back to a full destroy-then-rebuild
        when the new range doesn't overlap the old one at all (e.g. a
        far-away _ensure_materialized jump), since there's nothing to
        incrementally patch in that case."""
        new_first, new_last = new_range
        overlap = (
            old_range is not None
            and old_range[0] <= new_last
            and new_first <= old_range[1]
        )

        if not overlap:
            for idx in list(self._row_frames):
                self._destroy_row(idx)
            newly_built = list(range(new_first, new_last + 1))
            for idx in newly_built:
                self._build_row(idx)
            return newly_built

        old_first, old_last = old_range
        for idx in list(self._row_frames):
            if idx < new_first or idx > new_last:
                self._destroy_row(idx)

        newly_built: List[int] = []
        # Growth below the old window, appended in index order at the end
        # of the pack order - mirrors the old design's forward paging.
        for idx in range(old_last + 1, new_last + 1):
            self._build_row(idx)
            newly_built.append(idx)
        # Growth above the old window, inserted in descending index order
        # immediately before the current first materialized row - pack()
        # has no "insert at index" beyond before=/after= a sibling, so this
        # has to go back-to-front, mirroring the old design's backward
        # paging.
        if new_first < old_first:
            anchor = self._row_frames.get(old_first)
            for idx in range(old_first - 1, new_first - 1, -1):
                anchor = self._build_row(idx, before=anchor)
                newly_built.append(idx)
        return newly_built

    def _remeasure_built_rows(self, scroll_top: float, newly_built: List[int]) -> float:
        """Overwrite self._row_heights for newly-built rows with their real
        on-screen height, and return the exact pixel delta that the canvas's
        scroll offset must be corrected by (sum of real-minus-estimated
        height, for rows whose pre-correction offset sits above
        scroll_top) to keep the same content on screen. This delta is
        exact and bounded - derived from the same trusted height table used
        for the scrollregion - unlike the previous design's reverse-
        engineered "anchor row moved by N px" math, so applying it can
        never request an out-of-range scroll fraction (see module
        docstring).

        "Real on-screen height" is winfo_height() plus 2*ROW_PACK_PADY_PX,
        not winfo_height() alone - that constant's docstring (layout_
        constants.py) explains why: the vertical pack() gap outside a row's
        own Frame is real screen space winfo_height() can't see, and
        leaving it out here silently drifted self._row_heights (and
        everything keyboard_nav.py derives from it, like
        _scroll_box_into_view) away from the real screen position by a
        couple of px for every row scrolled past."""
        if not newly_built:
            return 0.0
        old_heights = list(self._row_heights)
        delta = 0.0
        for idx in newly_built:
            real = self._row_frames[idx].winfo_height() + 2 * ROW_PACK_PADY_PX
            old = old_heights[idx]
            if real and real != old:
                row_offset = sum(old_heights[:idx])
                above_scroll_top = row_offset < scroll_top
                if above_scroll_top:
                    delta += real - old
                self._row_heights[idx] = real
                self._log_event(
                    "remeasure_mismatch",
                    index=idx,
                    estimated_height=old,
                    real_height=real,
                    row_offset=row_offset,
                    scroll_top=scroll_top,
                    above_scroll_top=above_scroll_top,
                )
        return delta

    def _reconcile(self) -> None:
        """Recompute which item indices should be materialized from the
        canvas's current scroll position and self._row_heights, and
        reconcile the actually-built rows to match - building/destroying
        as needed, repositioning the materialized block, and fixing up the
        scrollregion. Pure-function-driven and idempotent: calling this
        twice with no scroll movement in between computes the same range
        and is a no-op the second time. See compute_visible_range and the
        module docstring for why that idempotency is what makes this
        immune to the oscillation bug a previous, stateful step-forward/
        step-backward design suffered from - there's no separate "should I
        page forward" vs. "should I page backward" check that can disagree
        about the resting state, because there's only one range
        computation, not two opposing ones racing each other."""
        canvas = self._canvas
        viewport_height = canvas.winfo_height()
        if viewport_height <= 1:
            return
        scroll_top = canvas.canvasy(0)
        buffer = viewport_height * SCROLL_BUFFER_VIEWPORTS

        first_idx, last_idx = compute_visible_range(
            self._row_heights, scroll_top, viewport_height, buffer
        )

        old_range = self._materialized_range
        newly_built = self._sync_materialized_rows(old_range, (first_idx, last_idx))
        self._materialized_range = (first_idx, last_idx)

        canvas.update_idletasks()
        delta = self._remeasure_built_rows(scroll_top, newly_built)

        total_height = sum(self._row_heights)
        corrected_scroll_top = scroll_top
        if delta and total_height > 0:
            corrected_scroll_top = max(0.0, min(scroll_top + delta, total_height))
            canvas.yview_moveto(corrected_scroll_top / total_height)

        canvas_width = max(canvas.winfo_width(), 1)
        canvas.configure(scrollregion=(0, 0, canvas_width, total_height))
        canvas.coords(self._canvas_window, 0, self._offset_of(first_idx))

        assert sorted(self._row_frames) == list(range(first_idx, last_idx + 1)), (
            sorted(self._row_frames), first_idx, last_idx,
        )

        self._update_visible_images()
        self._update_finalize_button_visibility()
        self._log_event(
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

    def _update_finalize_button_visibility(self) -> None:
        """Show the floating Finalize button only once the canvas is
        scrolled all the way to the end of the transcript, hiding it the
        rest of the time so it doesn't permanently eat space the way the
        old always-packed button row did. Driven by yview()'s bottom
        fraction rather than comparing pixel offsets directly - Tk clamps
        that fraction to exactly 1.0 once the view reaches the true end of
        the scrollregion (and a transcript short enough to fit on screen
        with no scrolling at all reports (0.0, 1.0) from the start, which
        correctly counts as "at the bottom" too - there's nothing to scroll
        past). place()/place_forget() rather than pack()/pack_forget() -
        this floats over the canvas instead of claiming its own slice of
        the frame, which is what lets the canvas keep the full frame height
        while the button is hidden."""
        _, bottom_frac = self._canvas.yview()
        at_bottom = bottom_frac >= 0.999
        if at_bottom and not self._finalize_button_visible:
            self._finalize_button_row.place(relx=0.5, rely=1.0, anchor="s", y=-10)
            self._finalize_button_visible = True
        elif not at_bottom and self._finalize_button_visible:
            self._finalize_button_row.place_forget()
            self._finalize_button_visible = False

    def _schedule_reconcile(self) -> None:
        """Coalesce a burst of scroll events into a single _reconcile pass,
        run shortly after the most recent event rather than on every one."""
        had_pending = self._update_job is not None
        if self._update_job is not None:
            self.after_cancel(self._update_job)
        self._log_event("debounce_scheduled", had_pending=had_pending)
        self._update_job = self.after(DEBOUNCE_MS, self._run_scheduled_reconcile)

    def _run_scheduled_reconcile(self) -> None:
        self._update_job = None
        self._log_event("debounce_fired")
        self._reconcile()

    def _update_visible_images(self) -> None:
        """Load images for rows within the (buffered) visible viewport and
        unload images for rows outside it. Uses self._row_heights/_offset_of
        (absolute canvas coordinates) rather than widget-relative geometry,
        which is relative to the repositioned _scroll_frame block (see
        _reconcile), not the canvas's coordinate space."""
        canvas = self._canvas
        canvas.update_idletasks()
        viewport_height = canvas.winfo_height()
        self._log_event("update_visible_images", viewport_height=viewport_height)
        if viewport_height <= 1:
            return

        buffer = viewport_height * SCROLL_BUFFER_VIEWPORTS
        visible_top = canvas.canvasy(0) - buffer
        visible_bottom = canvas.canvasy(viewport_height) + buffer

        self._images.update_visible(
            self._offset_of, self._row_heights, visible_top, visible_bottom,
            log_event=self._log_event,
        )

    def _get_box_text(self, index: int, role: str) -> Optional[str]:
        """Current text for one box - the materialized widget's live
        content if its row is currently built, else the last-saved text
        from a row that was paged out, else None (meaning "never touched",
        or this item has no box for this role at all - both are handled
        identically by callers, which fall back to the item's matching
        initial_*_text)."""
        widget = self._text_widgets.get((index, role))
        if widget is not None:
            return widget.get("1.0", "end-1c")
        return self._saved_texts.get((index, role))

    def collect_edited_texts(self) -> List[Dict[str, Optional[str]]]:
        """Current role->text mapping for every item, in transcript order -
        one entry per item.slot_roles (content and spacer roles alike) -
        see _get_box_text for what each value means. Used both for
        Finalize and for periodic session autosaving - the two need the
        same snapshot, just written to different places."""
        return [
            {role: self._get_box_text(idx, role) for role in item.slot_roles}
            for idx, item in enumerate(self._items)
        ]

    def get_focused_slot(self) -> Optional[Tuple[int, str]]:
        """(item_index, role) of the currently-focused text box, or None if
        no text box has focus (e.g. focus is on the Finalize button, or
        nothing in this frame at all) - used by autosave to remember where
        to restore focus to on resume."""
        return self._focused_slot()

    def get_scroll_top_fraction(self) -> float:
        """Canvas scroll position as a [0.0, 1.0] fraction - used as the
        autosaved fallback position when no text box is focused to restore
        focus to instead."""
        return self._canvas.yview()[0]

    def _on_finalize_clicked(self) -> None:
        logger.info("finalize button clicked on review screen")
        self._on_finalize(self.collect_edited_texts())
