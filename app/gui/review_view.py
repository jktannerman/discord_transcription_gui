"""Review screen shown after the OCR batch completes.

An infinite-scroll, paginated listing of every approved message in order.
Each row shows the message's image (if any) on the left, paired with one
freely-editable text box on the right pre-filled with that image's OCR
text - copy/paste and arbitrary edits are allowed, nothing is parsed or
restricted. Text-only messages are shown for context with no editable box.
Nothing is written to disk until the Finalize button at the bottom is
clicked, which writes every message's final lines in one pass.

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
_remeasure_built_rows/_offset_of/_ensure_materialized, plus _build_row/
_destroy_row) is deliberately kept together in this one class rather than
split further, since it's exactly the state that disagreed with itself in
the bug above - splitting it across files/objects would relocate that risk,
not remove it. Pure layout math that doesn't touch widget state lives in
virtualization.py; image lazy-loading and keyboard navigation are
self-contained enough to live in image_loading.py and keyboard_nav.py
respectively.

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
using Tk's built-in per-widget undo stack.

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
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable, Dict, List, Optional, Tuple

from .. import logging_config
from ..pipeline import ReviewItem
from . import theme
from .image_loading import THUMBNAIL_SIZE, ImageLoader
from .keyboard_nav import KeyboardNavMixin
from .virtualization import compute_visible_range, estimate_row_height

logger = logging_config.get_logger(__name__)

# Extra lines of headroom an editable text box is given beyond its current
# content when auto-sized (see ReviewFrame._size_text_container), so typing
# a little more doesn't immediately demand a resize/scrollbar.
TEXT_BOX_LEEWAY_LINES = 3

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


class ReviewFrame(KeyboardNavMixin, ttk.Frame):
    def __init__(
        self,
        master: tk.Widget,
        items: List[ReviewItem],
        on_finalize: Callable[[List[Optional[str]]], None],
    ):
        super().__init__(master)
        logger.info("building review screen", extra=logging_config.extra(item_count=len(items)))
        self._items = items
        self._on_finalize = on_finalize
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
        self._text_widgets: Dict[int, tk.Text] = {}
        self._text_containers: Dict[int, tk.Widget] = {}
        self._images = ImageLoader()
        # Real per-line pixel height for the text box font, used to size an
        # editable text box's container in px to fit its content - see
        # _size_text_container. Needs a live Tk instance, so it's measured
        # here rather than module-level.
        self._text_line_height_px = tkfont.Font(
            family=theme.TEXT_FONT_FAMILY, size=theme.TEXT_FONT_SIZE
        ).metrics("linespace")
        # Text captured from a row's widget just before it's torn down, so
        # edits survive a row being paged out and back in. None means
        # "never edited/visited" - fall back to item.initial_text.
        self._saved_texts: List[Optional[str]] = [None] * len(items)
        self._update_job: Optional[str] = None
        # Monotonic counter stamped on every _log_event call, purely so log
        # lines can be ordered exactly even if two land in the same
        # millisecond - used to reconstruct the precise sequence of
        # scroll/page/focus events that leads into a pagination loop.
        self._event_seq = 0

        # Pack the fixed-size widgets (button row, scrollbar) before the
        # expanding canvas - packing the expanding widget first starves the
        # others of space and squashes the scrollbar into a sliver.
        button_row = ttk.Frame(self)
        button_row.pack(side="bottom", fill="x", pady=8)
        self._finalize_button = ttk.Button(
            button_row, text="Finalize and write to file", command=self._on_finalize_clicked
        )
        self._finalize_button.pack(pady=6)
        self._finalize_button.bind("<Shift-Tab>", self._on_shift_tab)

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
            self._log_event("input_mousewheel", delta=e.delta)
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

        self.bind("<Destroy>", _on_destroy)

        self.after_idle(self._reconcile)

    def _log_event(self, event: str, **fields) -> None:
        """Log one step of scroll/page/focus handling at DEBUG, stamped with
        a sequence number plus the canvas's current window/scroll state, so
        a captured log can be replayed step-by-step to see exactly what
        triggered what during a pagination loop."""
        self._event_seq += 1
        top_frac, bottom_frac = self._canvas.yview()
        logger.debug(
            event,
            extra=logging_config.extra(
                seq=self._event_seq,
                materialized_range=self._materialized_range,
                top_frac=round(top_frac, 4),
                bottom_frac=round(bottom_frac, 4),
                **fields,
            ),
        )

    def _build_row(self, index: int, before: Optional[tk.Widget] = None) -> tk.Widget:
        """Materialize the row widget(s) for items[index] and register it in
        the window's bookkeeping dicts. If `before` is given, the row is
        inserted immediately above that widget instead of appended at the
        bottom (used when paging in rows above the current window)."""
        item = self._items[index]
        pack_kwargs = {"fill": "x", "pady": 4, "padx": 4}
        if before is not None:
            pack_kwargs["before"] = before

        row = ttk.Frame(self._scroll_frame, relief="groove", borderwidth=1, padding=6)
        row.pack(**pack_kwargs)
        self._row_frames[index] = row

        if item.image_path is None:
            preview = "\n".join(item.entry.text_lines).strip() or "(no text)"
            text_frame = ttk.Frame(row, style="MessageText.TFrame", padding=6)
            text_frame.pack(fill="x")
            ttk.Label(
                text_frame, text=preview, wraplength=900, justify="left",
                style="MessageText.TLabel",
            ).pack(anchor="w")
            return row

        left = ttk.Frame(row)
        left.pack(side="left", padx=6, fill="y")

        # Fixed-size placeholder so loading/unloading the image doesn't
        # change the row's layout (which would jump the scroll position).
        container = ttk.Frame(left, width=THUMBNAIL_SIZE[0], height=THUMBNAIL_SIZE[1])
        container.pack_propagate(False)
        container.pack()
        image_label = ttk.Label(container, text="(scroll to load image)", anchor="center")
        image_label.pack(fill="both", expand=True)

        self._images.register(index, item.image_path, image_label)

        if item.entry.text_lines:
            message_text = "\n".join(item.entry.text_lines).strip()
            if message_text:
                msg_frame = ttk.Frame(left, style="MessageText.TFrame", padding=6)
                msg_frame.pack(fill="x", pady=4)
                ttk.Label(
                    msg_frame, text=message_text, wraplength=THUMBNAIL_SIZE[0], justify="left",
                    style="MessageText.TLabel",
                ).pack(anchor="w")

        # Fixed-height container (same pack_propagate(False) trick as the
        # image placeholder above) so the text box's height is whatever
        # _size_text_container decides, rather than stretching to match the
        # image's height via fill="both" - that stretch is what previously
        # made every text box the same (often mostly-empty) height
        # regardless of how little text it held.
        text_container = ttk.Frame(row)
        text_container.pack(side="left", fill="x", expand=True, padx=6)
        text_container.pack_propagate(False)

        scrollbar = ttk.Scrollbar(text_container, orient="vertical")
        text_widget = tk.Text(
            text_container, wrap="word", relief="flat", undo=True,
            font=(theme.TEXT_FONT_FAMILY, theme.TEXT_FONT_SIZE),
            bg=theme.DARK_TEXT_BG, fg=theme.DARK_FG, insertbackground=theme.DARK_INSERT,
            selectbackground=theme.DARK_ACCENT, selectforeground="white",
            highlightthickness=1, highlightbackground=theme.DARK_BG_ALT,
            highlightcolor=theme.DARK_FOCUS_HIGHLIGHT,
        )
        # Set after construction (rather than passed as a kwarg) since the
        # callback needs to close over text_widget itself.
        text_widget.configure(
            yscrollcommand=lambda first, last, sb=scrollbar, t=text_widget: self._set_text_scrollbar(
                sb, t, first, last
            )
        )
        scrollbar.configure(command=text_widget.yview)
        # Scrollbar itself is packed/unpacked on demand by _set_text_scrollbar,
        # only while content actually overflows the box.
        text_widget.pack(side="left", fill="both", expand=True)

        saved = self._saved_texts[index]
        text_widget.insert("1.0", saved if saved is not None else (item.initial_text or ""))
        # The "insert" mark has right gravity, so inserting at "1.0" (where
        # it already sits on a fresh widget) leaves it at the *end* of the
        # new text rather than the start - then Tab-focusing this box later
        # would put the cursor (and the box's own auto-scroll-to-cursor) at
        # the bottom, with the start of the text scrolled out of view.
        text_widget.mark_set("insert", "1.0")
        text_widget.see("1.0")
        text_widget.edit_reset()  # don't let the initial insert be undoable
        text_widget.edit_modified(False)  # don't count that insert as a user edit

        self._size_text_container(text_container, text_widget)

        text_widget.bind("<Control-BackSpace>", self._delete_word_backward)
        text_widget.bind("<Tab>", self._on_tab)
        text_widget.bind("<Shift-Tab>", self._on_shift_tab)
        text_widget.bind("<Prior>", self._on_page_up)
        text_widget.bind("<Next>", self._on_page_down)
        text_widget.bind("<Control-z>", self._undo_text)
        text_widget.bind("<Control-Z>", self._redo_text)
        text_widget.bind(
            "<<Modified>>",
            lambda e, idx=index, c=text_container, t=text_widget: self._on_text_modified(idx, c, t),
        )
        self._text_widgets[index] = text_widget
        self._text_containers[index] = text_container
        return row

    def _max_text_box_height_px(self) -> int:
        """Cap an editable text box's height at roughly one screen's worth
        of pixels - the canvas viewport, falling back to the full screen if
        it hasn't been laid out yet - so a very long message scrolls inside
        its box (see _set_text_scrollbar) instead of growing taller than
        what's actually visible at once."""
        viewport = self._canvas.winfo_height()
        if viewport <= 1:
            viewport = self.winfo_screenheight()
        return viewport

    def _size_text_container(self, container: tk.Widget, text_widget: tk.Text) -> int:
        """Size an editable text box's container (px) to fit its current
        content plus TEXT_BOX_LEEWAY_LINES of headroom, never shrinking
        below the paired image's height (there's no benefit to a text box
        shorter than its image) and never growing past one screen's worth
        of height (_max_text_box_height_px) - a longer message gets an
        internal scrollbar instead. Returns the height applied."""
        container.update_idletasks()  # finalize the widget's real width before measuring wrap
        counted = text_widget.count("1.0", "end-1c", "displaylines")
        display_lines = counted[0] if counted else 1
        content_px = (display_lines + TEXT_BOX_LEEWAY_LINES) * self._text_line_height_px
        target_px = max(THUMBNAIL_SIZE[1], min(content_px, self._max_text_box_height_px()))
        container.configure(height=target_px)
        return target_px

    def _set_text_scrollbar(
        self, scrollbar: ttk.Scrollbar, text_widget: tk.Text, first: str, last: str
    ) -> None:
        """yscrollcommand for an editable text box: show its scrollbar only
        while content actually overflows the box. Most boxes are sized to
        fit their text exactly (see _size_text_container), so a
        permanently-visible empty scrollbar would be pure visual noise -
        this reveals one only once a message is long enough to hit the
        one-screen cap."""
        if float(first) <= 0.0 and float(last) >= 1.0:
            scrollbar.pack_forget()
        else:
            # before=text_widget: Tk allocates cavity space to packed slaves
            # in pack-call order, and the text box (packed first, with
            # fill="both"/expand=True) already claims the full cavity by the
            # time this fires - packing the scrollbar in afterwards with no
            # `before` would shrink it to a width-0 sliver, hidden but
            # "mapped", since it'd be last in that order with nothing left
            # to claim.
            scrollbar.pack(side="right", fill="y", before=text_widget)
        scrollbar.set(first, last)

    def _on_text_modified(self, index: int, container: tk.Widget, text_widget: tk.Text) -> None:
        """Bound to a text box's <<Modified>> event: re-run its sizing (see
        _size_text_container) as the user types, so the box grows to keep
        pace - up to the one-screen cap, beyond which _set_text_scrollbar
        takes over - and keep this row's recorded height in
        self._row_heights accurate so the canvas scrollregion doesn't drift
        out of sync. Mirrors what _remeasure_built_rows does for newly-built
        rows, just triggered immediately for the row being edited rather
        than waiting for the next scroll-driven _reconcile."""
        text_widget.edit_modified(False)
        self._size_text_container(container, text_widget)
        row = self._row_frames.get(index)
        if row is None:
            return
        row.update_idletasks()
        real_height = row.winfo_height()
        if real_height and real_height != self._row_heights[index]:
            self._row_heights[index] = real_height
            canvas = self._canvas
            canvas.configure(
                scrollregion=(0, 0, max(canvas.winfo_width(), 1), sum(self._row_heights))
            )

    def _destroy_row(self, index: int) -> None:
        """Tear down the row widget(s) for items[index], saving any edited
        text first so it can be restored if the row is paged back in."""
        row = self._row_frames.pop(index, None)
        if row is None:
            return
        text_widget = self._text_widgets.pop(index, None)
        if text_widget is not None:
            self._saved_texts[index] = text_widget.get("1.0", "end-1c")
        self._text_containers.pop(index, None)
        self._images.unregister(index)
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
        stepping it - see the module docstring."""
        if index in self._row_frames:
            return
        total_height = sum(self._row_heights)
        if total_height > 0:
            self._canvas.yview_moveto(self._offset_of(index) / total_height)
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
        winfo_height(), and return the exact pixel delta that the canvas's
        scroll offset must be corrected by (sum of real-minus-estimated
        height, for rows whose pre-correction offset sits above
        scroll_top) to keep the same content on screen. This delta is
        exact and bounded - derived from the same trusted height table used
        for the scrollregion - unlike the previous design's reverse-
        engineered "anchor row moved by N px" math, so applying it can
        never request an out-of-range scroll fraction (see module
        docstring)."""
        if not newly_built:
            return 0.0
        old_heights = list(self._row_heights)
        delta = 0.0
        for idx in newly_built:
            real = self._row_frames[idx].winfo_height()
            old = old_heights[idx]
            if real and real != old:
                row_offset = sum(old_heights[:idx])
                if row_offset < scroll_top:
                    delta += real - old
                self._row_heights[idx] = real
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
        if delta and total_height > 0:
            canvas.yview_moveto(max(0.0, min(scroll_top + delta, total_height)) / total_height)

        canvas_width = max(canvas.winfo_width(), 1)
        canvas.configure(scrollregion=(0, 0, canvas_width, total_height))
        canvas.coords(self._canvas_window, 0, self._offset_of(first_idx))

        assert sorted(self._row_frames) == list(range(first_idx, last_idx + 1)), (
            sorted(self._row_frames), first_idx, last_idx,
        )

        self._update_visible_images()
        self._log_event(
            "reconcile", first_idx=first_idx, last_idx=last_idx, total_height=total_height
        )

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

        self._images.update_visible(self._offset_of, self._row_heights, visible_top, visible_bottom)

    def _on_finalize_clicked(self) -> None:
        logger.info("finalize button clicked on review screen")
        edited_texts: List[Optional[str]] = []
        for idx, item in enumerate(self._items):
            if item.image_path is None:
                edited_texts.append(None)
                continue
            widget = self._text_widgets.get(idx)
            if widget is not None:
                edited_texts.append(widget.get("1.0", "end-1c"))
            elif self._saved_texts[idx] is not None:
                edited_texts.append(self._saved_texts[idx])
            else:
                edited_texts.append(item.initial_text or "")
        self._on_finalize(edited_texts)
