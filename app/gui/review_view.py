"""Review screen shown after the OCR batch completes.

An infinite-scroll, paginated listing of every approved message in order.
Every row has the same two-column shape: an immutable left column (the
message's own original text, its image, or both stacked text-above-image
- mirroring Discord's own layout - depending on what the message has)
paired with the matching editable text box(es) on the right, stacked in
the same text-above-image order: a copy of the message's own text
whenever it has any, and/or a box pre-filled with its image's OCR text
whenever it has an image - independently editable, so a message with both
gets both boxes. Copy/paste and arbitrary edits are allowed in every text
box; nothing is parsed or restricted. Nothing is written to disk until the
Finalize button at the bottom is clicked, which writes every message's
final lines in one pass.

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
from .image_loading import THUMBNAIL_SIZE, ImageLoader, fitted_image_size
from .keyboard_nav import KeyboardNavMixin
from .virtualization import compute_visible_range, estimate_row_height

logger = logging_config.get_logger(__name__)
# High-frequency per-scroll-tick tracing (reconcile/debounce/remeasure/
# image-load/box-resize events, all via _log_event) goes to its own log
# file/logger rather than `logger` above - see logging_config.setup_logging
# for why this is split out of the main app.log.
trace_logger = logging_config.get_trace_logger()

# Fixed height (in lines) for a "message"-role editable text box (a copy of
# the message's own text), regardless of how much text it actually holds -
# most messages here are short (often one line), and re-measuring a box's
# height from its wrapped line count on every keystroke (the old
# _size_text_container/_on_text_modified design) was the source of the
# review screen's worst scroll-position bugs: it made a row's true height
# unknowable until the box was built and typed in, which is exactly what
# _remeasure_built_rows had to keep correcting for (see ARCHITECTURE.md). A
# little taller than the common one-line case for comfortable editing;
# anything longer gets an internal scrollbar (_set_text_scrollbar) instead
# of growing the box.
TEXT_BOX_MIN_LINES = 3

# Extra headroom (px) added on top of the paired image's own fitted height
# when sizing an "ocr"-role box (a copy of an image's OCR text) - so the box
# isn't pixel-for-pixel identical to the image beside it. Like
# TEXT_BOX_MIN_LINES, this is a fixed amount rather than anything measured
# from the box's actual content.
TEXT_BOX_IMAGE_MARGIN_PX = 12

# Inner horizontal padding for an editable text box's own content (applied
# symmetrically by Tk's Text.padx), so wrapped/long lines don't run right up
# against the box's edge - it previously had none, which read as cramped
# against the right edge in particular since text there is ragged (wrapped
# at arbitrary word boundaries) rather than flush like the left edge.
TEXT_BOX_INNER_PADX = 6

# A maxed-out text box is capped at this fraction of the canvas viewport
# (see ReviewFrame._max_text_box_height_px), not the full viewport - a box
# that exactly fills the viewport leaves no margin, so tabbing to it rarely
# lands with it comfortably fully on-screen (Tk's "scroll just enough to
# reveal the target" positioning doesn't line it up pixel-perfectly with the
# viewport edge). Capping below 1.0 leaves room to spare instead.
TEXT_BOX_MAX_HEIGHT_FRACTION = 0.7

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
        on_finalize: Callable[[List[Tuple[Optional[str], Optional[str]]]], None],
        initial_saved_texts: Optional[List[Tuple[Optional[str], Optional[str]]]] = None,
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
        # list has, as (item_index, role) pairs - "role" is "message" (a
        # copy of the message's own text) or "ocr" (an image's OCR text).
        # A message gets a "message" slot whenever it has any text, an
        # "ocr" slot whenever it has an image, in that order - so a
        # message with both gets both, message slot first, matching the
        # text-above-image stacking in _build_row. This is what Tab/
        # Shift-Tab navigate (see keyboard_nav.py) and what
        # get_focused_slot/the resume focus-restore path address a box by,
        # since a single item index is no longer enough to identify one.
        self._slots: List[Tuple[int, str]] = []
        for idx, item in enumerate(items):
            if item.initial_message_text is not None:
                self._slots.append((idx, "message"))
            if item.image_path is not None:
                self._slots.append((idx, "ocr"))
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
        # see self._slots - since a row can now have up to two independent
        # boxes (and matching immutable originals) rather than at most one.
        self._text_widgets: Dict[Tuple[int, str], tk.Text] = {}
        self._text_containers: Dict[Tuple[int, str], tk.Widget] = {}
        self._images = ImageLoader()
        # Real per-line pixel height for the text box font, used to convert
        # TEXT_BOX_MIN_LINES into a fixed px height for a "message" box -
        # see _fixed_text_box_height. Needs a live Tk instance, so it's
        # measured here rather than module-level.
        self._text_line_height_px = tkfont.Font(
            family=theme.TEXT_FONT_FAMILY, size=theme.TEXT_FONT_SIZE
        ).metrics("linespace")
        # Text captured from a box just before its row is torn down, so
        # edits survive a row being paged out and back in. Absence means
        # "never edited/visited" - fall back to the item's initial_*_text.
        # Seeded from a saved session's edits when resuming, rather than
        # starting blank.
        self._saved_texts: Dict[Tuple[int, str], str] = {}
        if initial_saved_texts is not None and len(initial_saved_texts) == len(items):
            for idx, (message_text, ocr_text) in enumerate(initial_saved_texts):
                if message_text is not None:
                    self._saved_texts[(idx, "message")] = message_text
                if ocr_text is not None:
                    self._saved_texts[(idx, "ocr")] = ocr_text
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

    def _build_row(self, index: int, before: Optional[tk.Widget] = None) -> tk.Widget:
        """Materialize the row widget(s) for items[index] and register it in
        the window's bookkeeping dicts. If `before` is given, the row is
        inserted immediately above that widget instead of appended at the
        bottom (used when paging in rows above the current window).

        Every row has the same two-column shape, both columns stacked
        text-above-image (mirroring Discord's own layout) when a message
        has both: an immutable left column (the message's own original
        text, its image, or both) paired with the matching editable box(es)
        on the right - a copy of the message's own text whenever it has
        any, and/or an OCR text box whenever it has an image."""
        item = self._items[index]
        pack_kwargs = {"fill": "x", "pady": 4, "padx": 4}
        if before is not None:
            pack_kwargs["before"] = before

        row = ttk.Frame(self._scroll_frame, relief="groove", borderwidth=1, padding=6)
        row.pack(**pack_kwargs)
        self._row_frames[index] = row

        left = ttk.Frame(row)
        left.pack(side="left", padx=6, fill="y")
        right = ttk.Frame(row)
        right.pack(side="left", fill="x", expand=True, padx=6)

        has_message = item.initial_message_text is not None
        has_image = item.image_path is not None
        # When a row has both boxes, leave a gap below the top one so the
        # two stacked boxes (and their immutable counterparts) don't touch.
        gap_below_message = 6 if (has_message and has_image) else 0

        if has_message:
            self._build_immutable_message_label(left, item, pady_bottom=gap_below_message)

        image_h = 0
        if has_image:
            image_h = self._build_image_placeholder(left, item, index)

        if has_message:
            self._build_editable_text_box(
                right, index, "message", item.initial_message_text,
                pady_bottom=gap_below_message,
            )
        if has_image:
            self._build_editable_text_box(
                right, index, "ocr", item.initial_ocr_text or "", image_h=image_h,
            )

        return row

    def _build_immutable_message_label(
        self, parent: tk.Widget, item: ReviewItem, pady_bottom: int
    ) -> None:
        """Build the immutable, plain-styled label holding a message's own
        original text (entry.text_lines) - used for a text-only row's
        "original" column, and (stacked above the image) for an image
        row's caption too, now that both are edited the same way.

        Font matches the editable text boxes (theme.TEXT_FONT_FAMILY/SIZE)
        rather than the ttk default Label font, for visual consistency
        with the editable copy beside it; everything else about it (e.g.
        background) is left at the ttk default, matching the image
        column's own background, to stay visually distinct from that
        editable copy and from an editable box.

        The container's height isn't known until the label exists, so it's
        measured with pack_propagate left on (sizing naturally around the
        label, per its wraplength) before being pinned to a fixed
        width/height, same as before - this measure-once-at-build-time step
        is unrelated to (and much cheaper than) the per-keystroke remeasuring
        this change removes from its paired editable box (see
        _fixed_text_box_height): the label is never edited, so there's
        nothing to remeasure here after the initial build."""
        preview = "\n".join(item.entry.text_lines).strip() or "(no text)"
        container = ttk.Frame(parent)
        container.pack(pady=(0, pady_bottom))
        ttk.Label(
            container, text=preview, wraplength=THUMBNAIL_SIZE[0], justify="left",
            font=(theme.TEXT_FONT_FAMILY, theme.TEXT_FONT_SIZE),
        ).pack(anchor="w", fill="x")
        container.update_idletasks()
        container.configure(width=THUMBNAIL_SIZE[0], height=max(container.winfo_reqheight(), 1))
        container.pack_propagate(False)

    def _build_image_placeholder(self, parent: tk.Widget, item: ReviewItem, index: int) -> int:
        """Build the fixed-size image placeholder (actual pixels loaded
        lazily on scroll - see image_loading.py) and register it with
        self._images. Returns the image's on-screen height in px, used as
        its paired editable OCR box's height floor.

        Width is the global THUMBNAIL_SIZE[0] constant, same for every row,
        so images/text boxes still line up into two neat columns - only
        height is sized per image (to its actual aspect-preserving fit
        height, not the full bounding box) since most images here are
        landscape, and a box-shaped placeholder would letterbox them with
        large empty bands above/below the real photo. Fixed size (rather
        than left to the real loaded photo's size) so loading/unloading the
        image on scroll doesn't change the row's layout (which would jump
        the scroll position)."""
        _, image_h = fitted_image_size(item.image_path)
        container = ttk.Frame(parent, width=THUMBNAIL_SIZE[0], height=image_h)
        container.pack_propagate(False)
        container.pack()
        image_label = ttk.Label(container, text="(scroll to load image)", anchor="center")
        image_label.pack(fill="both", expand=True)
        self._images.register(index, item.image_path, image_label)
        return image_h

    def _build_editable_text_box(
        self,
        parent: tk.Widget,
        index: int,
        role: str,
        initial_text: str,
        pady_bottom: int = 0,
        image_h: int = 0,
    ) -> None:
        """Build one editable text box - role is "message" (a copy of the
        message's own text) or "ocr" (an image's OCR text) - and register
        it in the window's bookkeeping dicts, keyed by (index, role) since
        a row can now have one of these, the other, or both stacked
        text-above-image to match the left column (_build_row). image_h is
        the paired image's on-screen height (only meaningful, and only
        passed, for an "ocr" box - see _fixed_text_box_height)."""
        key = (index, role)
        # Fixed-height container (same pack_propagate(False) trick as the
        # left column's placeholders) so the text box's height is exactly
        # _fixed_text_box_height's verdict, computed once up front, rather
        # than stretching to fill whatever vertical space is left in
        # `parent` or being re-measured from content as the user types (see
        # _fixed_text_box_height's docstring for why the latter was removed).
        text_container = ttk.Frame(parent, height=self._fixed_text_box_height(role, image_h))
        text_container.pack(side="top", fill="x", pady=(0, pady_bottom))
        text_container.pack_propagate(False)

        scrollbar = ttk.Scrollbar(text_container, orient="vertical")
        text_widget = tk.Text(
            text_container, wrap="word", relief="flat", undo=True,
            font=(theme.TEXT_FONT_FAMILY, theme.TEXT_FONT_SIZE),
            bg=theme.DARK_TEXT_BG, fg=theme.DARK_FG, insertbackground=theme.DARK_INSERT,
            selectbackground=theme.DARK_ACCENT, selectforeground="white",
            highlightthickness=1, highlightbackground=theme.DARK_BG_ALT,
            highlightcolor=theme.DARK_FOCUS_HIGHLIGHT,
            padx=TEXT_BOX_INNER_PADX, pady=4,
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

        saved = self._saved_texts.get(key)
        text_widget.insert("1.0", saved if saved is not None else initial_text)
        # The "insert" mark has right gravity, so inserting at "1.0" (where
        # it already sits on a fresh widget) leaves it at the *end* of the
        # new text rather than the start - then Tab-focusing this box later
        # would put the cursor (and the box's own auto-scroll-to-cursor) at
        # the bottom, with the start of the text scrolled out of view.
        text_widget.mark_set("insert", "1.0")
        text_widget.see("1.0")
        text_widget.edit_reset()  # don't let the initial insert be undoable
        text_widget.edit_modified(False)  # don't count that insert as a user edit

        text_widget.bind("<Control-BackSpace>", self._delete_word_backward)
        text_widget.bind("<Tab>", self._on_tab)
        text_widget.bind("<Shift-Tab>", self._on_shift_tab)
        text_widget.bind("<Prior>", self._on_page_up)
        text_widget.bind("<Next>", self._on_page_down)
        text_widget.bind("<Control-z>", self._undo_text)
        text_widget.bind("<Control-Z>", self._redo_text)
        text_widget.bind(
            "<<Modified>>",
            lambda e, k=key, t=text_widget: self._on_text_modified(k, t),
        )
        self._text_widgets[key] = text_widget
        self._text_containers[key] = text_container

    def _max_text_box_height_px(self) -> int:
        """Cap an editable text box's height at TEXT_BOX_MAX_HEIGHT_FRACTION
        of the canvas viewport - falling back to the full screen if it
        hasn't been laid out yet - so a very long message scrolls inside
        its box (see _set_text_scrollbar) instead of growing taller than
        what comfortably fits on screen with some margin to spare."""
        viewport = self._canvas.winfo_height()
        if viewport <= 1:
            viewport = self.winfo_screenheight()
        return int(viewport * TEXT_BOX_MAX_HEIGHT_FRACTION)

    def _fixed_text_box_height(self, role: str, image_h: int) -> int:
        """A box's height (px), decided once at build time from a fixed
        rule rather than measured from its actual content: TEXT_BOX_MIN_LINES
        for a "message" box, or its paired image's own on-screen height plus
        TEXT_BOX_IMAGE_MARGIN_PX for an "ocr" box - either way capped at
        _max_text_box_height_px, with any overflow handled by the box's own
        internal scrollbar (_set_text_scrollbar) rather than the box growing.

        This replaces an earlier design (_size_text_container, removed) that
        measured the text's actual wrapped line count and resized the box to
        fit it, re-running on every keystroke (_on_text_modified). That made
        a row's true height unknowable until it was built and typed in -
        which is exactly the gap _remeasure_built_rows existed to correct,
        and the repeated source of this screen's scroll-position bugs (see
        ARCHITECTURE.md). Fixing height to something knowable upfront - the
        same way an image's height already was, via fitted_image_size's
        cheap header read - removes that correction's reason to exist
        instead of just estimating it more carefully."""
        max_px = self._max_text_box_height_px()
        if role == "ocr":
            target_px = image_h + TEXT_BOX_IMAGE_MARGIN_PX
        else:
            target_px = TEXT_BOX_MIN_LINES * self._text_line_height_px
        return max(1, min(target_px, max_px))

    def _set_text_scrollbar(
        self, scrollbar: ttk.Scrollbar, text_widget: tk.Text, first: str, last: str
    ) -> None:
        """yscrollcommand for an editable text box: show its scrollbar only
        while content actually overflows the box. Most boxes' fixed height
        (see _fixed_text_box_height) comfortably fits their typical content,
        so a permanently-visible empty scrollbar would be pure visual noise
        - this reveals one only once a message is long enough (or the box's
        fixed height short enough) that the content actually overflows."""
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

    def _on_text_modified(self, key: Tuple[int, str], text_widget: tk.Text) -> None:
        """Bound to a text box's <<Modified>> event. The box's own height is
        now fixed at build time (see _fixed_text_box_height) and never
        changes as the user types - overflow is handled entirely by the
        box's internal scrollbar (_set_text_scrollbar) - so there's nothing
        left to resize or remeasure here, just the modified-flag reset and
        keeping the edited row on screen.

        Scrolling the row back into view matters because focus alone
        doesn't keep a box on screen: the mouse wheel/scrollbar can move the
        viewport without touching focus at all, and Tk happily keeps
        delivering keystrokes to a focused-but-off-screen widget - typing is
        the easiest visible signal that the user is "at" this box and would
        want to see it, without needing a separate scroll-position watcher
        for an otherwise-rare case. Gated on the widget actually having
        focus, since <<Modified>> also fires for a freshly-built row's own
        initial text insert (see _build_editable_text_box) - that insert's
        own edit_modified(False) reset doesn't suppress it, because Tk
        queues <<Modified>> for the next idle tick rather than firing it
        synchronously, by which point this binding already exists. Without
        this guard, a row built only because it entered the virtualization
        buffer (not because the user scrolled it into view) would yank the
        canvas to reveal it anyway."""
        text_widget.edit_modified(False)
        if text_widget is self.focus_get():
            self._scroll_into_view(key[0])

    def _destroy_row(self, index: int) -> None:
        """Tear down the row widget(s) for items[index], saving any edited
        text first so it can be restored if the row is paged back in."""
        row = self._row_frames.pop(index, None)
        if row is None:
            return
        for role in ("message", "ocr"):
            key = (index, role)
            text_widget = self._text_widgets.pop(key, None)
            if text_widget is not None:
                self._saved_texts[key] = text_widget.get("1.0", "end-1c")
            self._text_containers.pop(key, None)
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

    def collect_edited_texts(self) -> List[Tuple[Optional[str], Optional[str]]]:
        """Current (edited_message_text, edited_ocr_text) pair for every
        item, in transcript order - see _get_box_text for what each value
        means. Used both for Finalize and for periodic session autosaving -
        the two need the same snapshot, just written to different
        places."""
        return [
            (self._get_box_text(idx, "message"), self._get_box_text(idx, "ocr"))
            for idx in range(len(self._items))
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
