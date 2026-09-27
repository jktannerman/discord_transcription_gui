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
See review_item.ReviewItem.slot_roles for the exact ordering and
docs/ARCHITECTURE_SPACER_SLOTS.md for the full design. Copy/paste
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
- it is deliberately NOT left at canvas position (0, 0). That repositioning
happens as soon as rows are destroyed or built, before anything can flush
the idle queue and repaint, so the screen never shows the block out of
place. The canvas's
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
from pathlib import Path
from tkinter import ttk
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .. import logging_config
from ..review_item import ReviewItem
from . import theme
from .image_context_menu import ImageContextMenuMixin
from .image_loading import ImageLoader
from .keyboard_nav import KeyboardNavMixin
from .layout_constants import ROW_PACK_PADY_PX
from .row_building import RowBuildingMixin
from .text_undo import UndoLog
from .virtualization import compute_visible_range, estimate_row_height
from .wheel import WHEEL_EVENT_SEQUENCES, wheel_delta

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


class ReviewFrame(KeyboardNavMixin, RowBuildingMixin, ImageContextMenuMixin, ttk.Frame):
    def __init__(
        self,
        master: tk.Widget,
        items: List[ReviewItem],
        on_finalize: Callable[[List[Dict[str, Optional[str]]]], None],
        html_path: Path,
        initial_saved_texts: Optional[List[Dict[str, Optional[str]]]] = None,
        initial_focus_slot: Optional[Tuple[int, str]] = None,
        initial_scroll_fraction: Optional[float] = None,
        initial_finalized_texts: Optional[List[Dict[str, Optional[str]]]] = None,
        initial_touched_slots: Optional[Iterable[Tuple[int, str]]] = None,
    ) -> None:
        super().__init__(master)
        logger.info(
            "building review screen",
            extra=logging_config.extra(
                item_count=len(items),
                resuming=initial_saved_texts is not None,
                has_finalized_texts=initial_finalized_texts is not None,
            ),
        )
        self._items = items
        self._on_finalize = on_finalize
        # This run's source chatlog export - used by image_context_menu.py's
        # "Open Chatlog at Message" action to build a file:// URI pointing
        # at this exact message's #chatlog__message-container-<id> anchor
        # (see that module for why that specific id, not some other one, is
        # the right anchor to use).
        self._html_path = html_path
        self._initial_focus_slot = initial_focus_slot
        self._initial_scroll_fraction = initial_scroll_fraction
        # Flat, transcript-ordered list of every editable box this item
        # list has, as (item_index, role) pairs - one entry per
        # item.slot_roles (see review_item.ReviewItem), in order: "message" (a
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
        # Finalized edits from a prior completed run - used as fallback for
        # slots not already covered by a session resume above. Session takes
        # priority (already in _saved_texts); finalized edits fill the rest,
        # so a fresh run pre-populates with the previously-finalized text
        # rather than raw OCR.
        if initial_finalized_texts is not None and len(initial_finalized_texts) == len(items):
            for idx, edited in enumerate(initial_finalized_texts):
                for role, text in edited.items():
                    if text is not None and (idx, role) not in self._saved_texts:
                        self._saved_texts[(idx, role)] = text
        # Slots the user has deliberately acted on this session - typed/
        # pasted/undone in, or clicked the OCR checkbox of (see
        # row_building.py's _on_text_modified/_on_ocr_checkbox_toggle).
        # Finalize only removes a box's stored finalized edit if its slot is
        # in here: a box that merely *looks* reverted, with no recorded
        # action behind it, keeps its stored edit - so a logic bug that
        # unticks a box or resets its text can't erase a finalized edit.
        # Persisted with the session (seeded from initial_touched_slots on
        # resume), since an untick made before closing the app still counts.
        self._touched_slots: Set[Tuple[int, str]] = set(initial_touched_slots or ())
        # Cursor ("insert" mark) position captured alongside self._saved_texts
        # when a box's row is torn down, so paging a focused box's row out and
        # back in (e.g. a fast Page Up/Down burst that outruns the
        # virtualization buffer - see _destroy_row) restores the cursor to
        # where it was rather than resetting it to the box's start. Not
        # persisted across a session save/resume - only self._saved_texts is -
        # so a resumed box's cursor still starts at "1.0", same as before.
        self._saved_cursor: Dict[Tuple[int, str], str] = {}
        # Per-OCR-box "edited vs. not" checkbox state (see row_building.py's
        # _build_editable_text_box/_on_ocr_checkbox_toggle) - keyed the same
        # way as every other per-box dict here, and never cleared by
        # _destroy_row, so it survives a row being torn down and rebuilt
        # with no extra teardown/rebuild plumbing. self._user_edited_texts
        # holds the last user-edited version of a box's text, kept distinct
        # from whatever it currently *displays* (the OCR default, while
        # unchecked). Seeded eagerly below - not lazily the first time a row
        # is built - since collect_edited_texts/autosave need every item's
        # checked-state regardless of whether its row has ever been
        # materialized this session.
        self._checkbox_checked: Dict[Tuple[int, str], bool] = {}
        self._user_edited_texts: Dict[Tuple[int, str], str] = {}
        for idx, item in enumerate(items):
            for image_index in range(len(item.image_paths)):
                key = (idx, f"ocr{image_index}")
                saved = self._saved_texts.get(key)
                default = item.initial_ocr_texts[image_index]
                checked = saved is not None and saved != default
                self._checkbox_checked[key] = checked
                if checked:
                    self._user_edited_texts[key] = saved
        # tk.BooleanVar backing each currently-built OCR box's checkbox -
        # only exists while that box's row is materialized, same as
        # self._text_widgets.
        self._checkbox_vars: Dict[Tuple[int, str], tk.BooleanVar] = {}
        # Keys whose next deferred <<Modified>> event(s) should NOT be
        # treated as "the user edited this OCR box" - set around a box's
        # own build-time insert/replay and around the checkbox's own
        # programmatic content swap, both of which fire <<Modified>> just
        # like a real edit (see row_building.py's _populate_text_box and
        # docs/ARCHITECTURE_REVIEW_SCREEN.md's "<<Modified>> fires on a
        # box's initial population" entry for why that event can't be trusted at face
        # value).
        self._suppress_ocr_auto_check: set = set()
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
        # after()-id of a pending debounced spellcheck pass for a currently-
        # built content box (see row_building.py's _schedule_spellcheck) -
        # only ever set for "message"/"ocr{N}" boxes, never a spacer box.
        # Cancelled in _destroy_row/_reclaim_widget_if_present so a timer
        # never fires against an already-destroyed widget.
        self._spellcheck_after_ids: Dict[Tuple[int, str], str] = {}
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
        # True while an image's right-click context menu (image_context_
        # menu.py) is open - checked by _on_mousewheel/_on_scrollbar below
        # and keyboard_nav.py's _on_page_up/_on_page_down, so scrolling
        # can't move rows (and this menu's target image) out from under an
        # open menu. Tk's own popup grab already keeps most scroll/keyboard
        # input from reaching this frame while the menu is up; this flag is
        # a belt-and-suspenders backstop against whatever platform-specific
        # grab gap (e.g. mouse wheel routing) isn't covered by that grab.
        self._scroll_frozen = False

        # Scrollbar + canvas fill the whole frame - the Finalize button used
        # to live in a row permanently packed below them, which cost every
        # screenful of review the same slice of vertical space whether or
        # not the button was ever relevant yet. It's built further below as
        # a place()'d overlay instead, shown only once scrolled to the very
        # end of the transcript (see _update_finalize_button_visibility),
        # so the canvas gets the full frame height the rest of the time.
        scrollbar = ttk.Scrollbar(self, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        # Stored on self (not just the local var above) so image_context_
        # menu.py's _show_image_context_menu can disable it - matching the
        # frozen wheel/Page Up/Down behavior below with a visible, actually-
        # inert scrollbar rather than a thumb that still drags but does
        # nothing - while a right-click menu is open.
        self._scrollbar = scrollbar

        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0, bg=theme.DARK_BG_ALT)
        canvas.pack(side="left", fill="both", expand=True)
        self._canvas = canvas

        # Per-item row height (px), seeded with cheap estimates and
        # overwritten with the real winfo_height() once a row is built -
        # the source of truth for the full virtual document's layout, used
        # to compute which index range should be materialized and to set
        # the canvas's scrollregion (see _reconcile). Only items in
        # [_materialized_range[0], _materialized_range[1]] (inclusive)
        # currently have widgets; None until the first _reconcile call.
        # Computed only now that self._canvas exists, so the estimate can
        # pass _max_text_box_height_px() the same content-box height cap
        # the real layout enforces (RowBuildingMixin._fixed_text_box_height)
        # - the canvas hasn't been given real geometry yet at this point in
        # __init__, so this falls back to that method's own
        # winfo_screenheight()-based guess, same as it would for any other
        # not-yet-laid-out call. Without this cap, a long message/OCR text's
        # estimated row height ran far past what its real, capped box would
        # ever be - see estimate_row_height's docstring.
        self._row_heights: List[int] = [
            estimate_row_height(item, max_text_box_height_px=self._max_text_box_height_px())
            for item in items
        ]

        def _on_scrollbar(*args: str) -> None:
            if self._scroll_frozen:
                return
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

        def _on_canvas_configure(e: tk.Event) -> None:
            canvas.itemconfig(self._canvas_window, width=e.width)
            self._log_event("input_canvas_configure", width=e.width, height=e.height)
            self._schedule_reconcile()

        canvas.bind("<Configure>", _on_canvas_configure)

        for sequence in WHEEL_EVENT_SEQUENCES:
            canvas.bind_all(sequence, self._on_mousewheel)
        # Global fallback for Page Up/Down so they scroll the review window
        # even when focus is on the Finalize button rather than a text box
        # (each text box also gets its own binding in _build_row, which
        # takes precedence and overrides Tk's default Text page-scrolling).
        canvas.bind_all("<Prior>", self._on_page_up)
        canvas.bind_all("<Next>", self._on_page_down)
        # bind_all is global, so undo it when this frame goes away, otherwise
        # the next screen's scrolling would dispatch to this destroyed canvas
        def _on_destroy(e: tk.Event) -> None:
            for sequence in WHEEL_EVENT_SEQUENCES:
                canvas.unbind_all(sequence)
            canvas.unbind_all("<Prior>")
            canvas.unbind_all("<Next>")
            if self._update_job is not None:
                self.after_cancel(self._update_job)
                self._update_job = None
            if self._initial_position_job is not None:
                self.after_cancel(self._initial_position_job)
                self._initial_position_job = None
            # Rows still materialized when the whole frame goes away (screen
            # switch, app close, or - in tests - the root window being torn
            # down) never go through _destroy_row, so any of their pending
            # debounced spellcheck timers (row_building.py's
            # _schedule_spellcheck) would otherwise fire after this frame's
            # widgets are gone. Left uncancelled, Tcl still queues and
            # attempts them - harmless individually (each just logs
            # "invalid command name" and no-ops), but real GUI tests run many
            # ReviewFrames back to back in the same process, and Tcl's timer
            # queue is shared across all of them (it's per-thread, not
            # per-interpreter) - enough leaked timers from earlier tests can
            # measurably delay a later test's own update()/update_idletasks()
            # calls, which _settle_pending_geometry's bounded retry counts
            # assume stay cheap (see its docstring). Cancelling explicitly
            # here, the same way self._update_job/self._initial_position_job
            # already are, keeps that assumption true.
            for after_id in self._spellcheck_after_ids.values():
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
            self._spellcheck_after_ids.clear()

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
        # <<PrevWindow>>, not <Shift-Tab>: on X11, Shift+Tab arrives as the
        # ISO_Left_Tab key, which <Shift-Tab> never matches - Tk's own
        # all-widget traversal (which doesn't scroll the canvas) handled it
        # instead. <<PrevWindow>> is Tk's own name for every platform's
        # "previous" key (Shift-Tab, ISO_Left_Tab, hpBackTab).
        self._finalize_button.bind("<<PrevWindow>>", self._on_shift_tab)
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

    def _on_mousewheel(self, event: tk.Event) -> str:
        """Handle one wheel/touchpad scroll event from anywhere on the review
        screen - bound globally for WHEEL_EVENT_SEQUENCES, and directly on
        every editable text box (row_building.py) so it replaces, rather
        than runs alongside, Tk's own Text scrolling for the same event.

        event.widget is whichever widget the cursor is over - so hovering a
        scrollable text box scrolls *it* first, and only once it's scrolled
        as far as it can go in that direction does the wheel fall through to
        scrolling the whole review window, as if the box weren't there.

        Returns:
            "break", so no other binding also handles this event.
        """
        if self._scroll_frozen:
            return "break"
        delta = wheel_delta(event)
        self._log_event(
            "input_mousewheel", delta=delta, num=event.num, state=event.state,
            widget=str(event.widget),
        )
        if not delta:
            return "break"
        if isinstance(event.widget, tk.Text) and self._scroll_text_widget(event.widget, delta):
            return "break"
        # At least one unit: macOS reports small deltas that would round to 0.
        units = int(-delta / 120) or (-1 if delta > 0 else 1)
        self._canvas.yview_scroll(units, "units")
        self._schedule_reconcile()
        return "break"

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
        text_widget.yview_scroll(int(-delta / 120) or (-1 if delta > 0 else 1), "units")
        return True

    def _log_event(self, event: str, **fields: object) -> None:
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
            text = text_widget.get("1.0", "end-1c")
            self._saved_texts[key] = text
            self._saved_cursor[key] = text_widget.index("insert")
            had_focus = text_widget is focused
            if had_focus:
                self._refocus_slot = key
            self._log_event(
                "box_teardown",
                key=key,
                widget=str(text_widget),
                had_focus=had_focus,
                **logging_config.text_fingerprint(text),
            )
            self._text_containers.pop(key, None)
            self._checkbox_vars.pop(key, None)
            detach = self._undo_detach.pop(key, None)
            if detach is not None:
                detach()
            pending_spellcheck = self._spellcheck_after_ids.pop(key, None)
            if pending_spellcheck is not None:
                try:
                    text_widget.after_cancel(pending_spellcheck)
                except tk.TclError:
                    pass
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

    def _try_build_row(self, index: int, before: Optional[tk.Widget] = None) -> Optional[tk.Widget]:
        """_build_row, but a single row's build failure doesn't propagate
        out of this Tk callback and abort the rest of _sync_materialized_
        rows's batch. row_building.py's _populate_text_box already
        recovers from the one concrete failure mode this hit in practice
        (a UndoLog op that can't be replayed - see
        INVESTIGATION_shift_tab_reconcile_lockup.md), but that recovery
        can only run for the box it happens in - this is a backstop for
        that fix missing something, or any other future per-row build
        failure, not a substitute for fixing what's actually raising.

        Before that recovery existed, an uncaught exception here (a) left
        this row's index missing from self._text_widgets while still
        listed in the static self._slots nav list (later KeyError on
        Tab/Shift-Tab), (b) aborted the rest of this batch, so every row
        after the failure in build order was silently never built either,
        and (c) never let _reconcile reach the self._materialized_range
        assignment that runs after this method returns - leaving it
        permanently stale relative to self._row_frames's real contents,
        corrupting every subsequent reconcile's idea of what's already
        built. Catching here instead means one bad row is missing (and
        retried on every future reconcile that wants it) rather than the
        whole screen wedging.

        Tears down whatever this attempt did manage to build via
        _destroy_row - which also captures into self._saved_texts/
        self._saved_cursor anything that succeeded before the failure -
        rather than leaving a half-built row Frame sitting in the packed
        order. Returns None on failure so callers can skip it: leave it
        out of newly_built, and (for the backward-growth loop) keep the
        existing anchor rather than advancing to a row that doesn't exist."""
        try:
            return self._build_row(index, before=before)
        except Exception:
            logger.error(
                "building this row raised an uncaught exception - tearing "
                "down whatever was built and skipping it for now, rather "
                "than aborting the rest of this reconcile's build batch",
                exc_info=True,
                extra=logging_config.extra(index=index),
            )
            if index in self._row_frames:
                self._destroy_row(index)
            return None

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
            newly_built = []
            for idx in range(new_first, new_last + 1):
                if self._try_build_row(idx) is not None:
                    newly_built.append(idx)
            return newly_built

        old_first, old_last = old_range
        for idx in list(self._row_frames):
            if idx < new_first or idx > new_last:
                self._destroy_row(idx)
        # Destroying rows above shifts everything left in the block up by
        # their height; move the block down to match right away, before
        # anything can repaint, instead of only at the end of _reconcile.
        if self._row_frames:
            self._canvas.coords(self._canvas_window, 0, self._offset_of(min(self._row_frames)))

        newly_built: List[int] = []
        # Growth below the old window, appended in index order at the end
        # of the pack order - mirrors the old design's forward paging.
        for idx in range(old_last + 1, new_last + 1):
            if self._try_build_row(idx) is not None:
                newly_built.append(idx)
        # Growth above the old window, inserted in descending index order
        # immediately before the current first materialized row - pack()
        # has no "insert at index" beyond before=/after= a sibling, so this
        # has to go back-to-front, mirroring the old design's backward
        # paging.
        if new_first < old_first:
            anchor = self._row_frames.get(old_first)
            for idx in range(old_first - 1, new_first - 1, -1):
                built = self._try_build_row(idx, before=anchor)
                if built is not None:
                    anchor = built
                    newly_built.append(idx)
        return newly_built

    # Bound on _settle_pending_geometry's retry loop - cheap (an idle-queue
    # flush each iteration) and only ever exercised on the rare reconcile
    # that needs more than one round, so a generous cap costs nothing in
    # the common case.
    _SETTLE_MAX_ATTEMPTS = 10

    def _settle_pending_geometry(self, newly_built: List[int]) -> None:
        """Get every row in `newly_built` a real winfo_height(), instead of
        trusting a single update_idletasks() call (just before this) to
        have been enough.

        A brand-new widget that has never been mapped to the screen
        reports winfo_height() == 1 (Tk's "no real geometry assigned yet"
        default) until the window system has actually mapped it - and
        confirmed experimentally (a standalone repro reproducing the real
        production scroll_trace.log symptom below), update_idletasks()
        alone *never* resolves that, no matter how many times it's called:
        idle-queue processing covers Tcl-level callbacks (including pack's
        own size negotiation), but a widget's first Map is an actual
        window-system event, which only update() (or the normal mainloop)
        pumps. The very first reconcile of a session can materialize an
        entire window's worth of rows - a deeply nested ttk.Frame tree,
        several levels deep, none of which have ever been mapped before -
        in one shot, which is exactly when this matters; every later
        reconcile measures correctly first try, because by then the canvas
        has already been mapped at least once. Left unguarded,
        _remeasure_built_rows took that bogus ~0 height as ground truth and
        permanently wrote 2*ROW_PACK_PADY_PX (9px) into self._row_heights
        for every such row - and since a row already in self._row_frames is
        never rebuilt (so never remeasured) just because a later reconcile
        runs, that corruption then threw self._offset_of off by hundreds of
        px for every row after it for the rest of the session, with no
        further chance to self-correct.

        Tries cheap update_idletasks() first (covers the ordinary case of a
        pack layout still settling, with no event-processing side effects),
        only escalating to update() - which processes *all* pending events,
        not just idle callbacks - if that wasn't enough. update() can call
        back into _reconcile itself (e.g. an already-scheduled debounced
        reconcile firing) before returning here; that's expected and safe,
        not reentrancy to guard against - see _reconcile's own docstring.
        Both stages bounded by _SETTLE_MAX_ATTEMPTS so a row that's for some
        other reason never going to get real geometry (shouldn't happen)
        can't hang the UI - _remeasure_built_rows's own winfo_height()<=1
        guard is the last-resort fallback if even update() doesn't settle
        it."""
        canvas = self._canvas

        def unsettled() -> List[int]:
            return [
                idx for idx in newly_built
                if idx in self._row_frames and self._row_frames[idx].winfo_height() <= 1
            ]

        for attempt in range(self._SETTLE_MAX_ATTEMPTS):
            remaining = unsettled()
            if not remaining:
                if attempt:
                    self._log_event("settle_pending_geometry_resolved", attempts=attempt)
                return
            canvas.update_idletasks()

        for attempt in range(self._SETTLE_MAX_ATTEMPTS):
            remaining = unsettled()
            if not remaining:
                self._log_event(
                    "settle_pending_geometry_resolved_via_full_update", attempts=attempt
                )
                return
            canvas.update()

        self._log_event(
            "settle_pending_geometry_gave_up",
            attempts=2 * self._SETTLE_MAX_ATTEMPTS,
            still_unsettled=remaining,
        )

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
        couple of px for every row scrolled past.

        Skips a row outright if its winfo_height() is still <=1 - Tk's
        "never been given real geometry" default, not a legitimately tiny
        row - rather than recording that as this row's real height. The
        caller (_reconcile, via _settle_pending_geometry) is expected to
        have already flushed Tk's idle queue until this stops happening,
        so this is a defense-in-depth fallback for if that bounded retry
        ever gives up, not the primary fix - see _settle_pending_geometry's
        docstring for why a single update_idletasks() isn't always enough.
        Leaving the row's previous estimate in self._row_heights in that
        case is strictly better than overwriting it with a near-zero
        value: the estimate, however imprecise, still degrades gracefully,
        while 2*ROW_PACK_PADY_PX would corrupt _offset_of for every row
        after it for the rest of the session."""
        if not newly_built:
            return 0.0
        old_heights = list(self._row_heights)
        delta = 0.0
        for idx in newly_built:
            real_winfo_height = self._row_frames[idx].winfo_height()
            if real_winfo_height <= 1:
                self._log_event(
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
        computation, not two opposing ones racing each other.

        Can end up reentrant with itself: _settle_pending_geometry can fall
        back to canvas.update() on the very first reconcile of a session,
        which - unlike update_idletasks() - drains *all* pending events,
        not just idle callbacks, including an already-scheduled debounced
        reconcile (from the canvas's very first <Configure>), which calls
        right back into this method before this outer call returns. That's
        fine, not a bug to guard against: a nested call computes the same
        first_idx/last_idx (scroll hasn't moved), so _sync_materialized_rows
        sees full overlap with the range this outer call already
        materialized and does no further building/destroying - in practice
        that nested call's own re-issued canvas.configure(scrollregion=...)/
        canvas.coords(...) is what actually finishes flushing the Map this
        outer call's canvas.update() was waiting on, confirmed by tracing
        actual call depth against a real production repro. An earlier
        version of this method added a reentrancy guard that made a nested
        call here a no-op, on the theory that it could rebuild/tear down
        rows out from under the outer call - that theory didn't hold up:
        guarding it back out reproduced the exact bug _settle_pending_
        geometry exists to fix, because the guard was blocking the one
        thing that actually resolved it."""
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
        # Position the block for its new first row (by estimated height, for
        # rows just built above) before update_idletasks repaints; the
        # remeasure below only corrects the remaining estimation error.
        canvas.coords(self._canvas_window, 0, self._offset_of(first_idx))

        canvas.update_idletasks()
        self._settle_pending_geometry(newly_built)
        delta = self._remeasure_built_rows(scroll_top, newly_built)

        total_height = sum(self._row_heights)
        corrected_scroll_top = scroll_top
        if delta and total_height > 0:
            corrected_scroll_top = max(0.0, min(scroll_top + delta, total_height))
            canvas.yview_moveto(corrected_scroll_top / total_height)

        canvas_width = max(canvas.winfo_width(), 1)
        canvas.configure(scrollregion=(0, 0, canvas_width, total_height))
        canvas.coords(self._canvas_window, 0, self._offset_of(first_idx))

        materialized = sorted(self._row_frames)
        expected = list(range(first_idx, last_idx + 1))
        if materialized != expected:
            # A real bug if it ever fires (this invariant is what
            # _sync_materialized_rows is supposed to guarantee), but crashing
            # the whole review screen over it would lose whatever wasn't
            # autosaved yet - log it and keep going with whatever's actually
            # built, same as this method's other defense-in-depth fallbacks
            # (_settle_pending_geometry, _remeasure_built_rows).
            logger.error(
                "materialized rows do not match computed range after reconcile",
                extra=logging_config.extra(
                    materialized=materialized, first_idx=first_idx, last_idx=last_idx,
                ),
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
        initial_*_text).

        An "ocr*" role is a special case: while its checkbox is unchecked,
        this always reports None - even though the box's live content is
        real text (the OCR default it's currently displaying) - since
        unchecked literally means "use the default", and a real edit
        sitting in self._user_edited_texts for if the box gets re-checked
        isn't the thing that should be written out or persisted to the
        session file while it's hidden behind that checkbox. This doesn't
        change what Finalize ever writes (None already falls back to the
        same default text in review_item.lines_for_item) - only what
        collect_edited_texts reports, which is what autosave/the session
        file's "edited_texts" field actually persist."""
        key = (index, role)
        if role.startswith("ocr") and not self._checkbox_checked.get(key, False):
            return None
        widget = self._text_widgets.get(key)
        text = widget.get("1.0", "end-1c") if widget is not None else self._saved_texts.get(key)
        # Text identical to the default isn't an edit - reporting it as one
        # (as every box used to be, once its row had been built even just
        # by scrolling past) stored copies of defaults as finalized edits,
        # which then pinned stale text over newer defaults on later runs.
        if text is not None and text == self._items[index].initial_text_for_role(role):
            return None
        return text

    def get_touched_slots(self) -> Set[Tuple[int, str]]:
        """The (item_index, role) slots the user has deliberately acted on
        this session (see self._touched_slots) - used by Finalize to decide
        which stored finalized edits may be removed, and by autosave so a
        resumed session keeps them."""
        return set(self._touched_slots)

    def collect_edited_texts(self) -> List[Dict[str, Optional[str]]]:
        """Current role->text mapping for every item, in transcript order -
        one entry per item.slot_roles (content and spacer roles alike) -
        see _get_box_text for what each value means (None: unchanged from
        the default, or an unchecked OCR box). Used both for
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
