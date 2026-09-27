"""The review screen's editable text boxes: their state, their widgets, and
everything that happens when one is edited.

Every box is addressed by its slot, (item_index, role) - see
review_item.ReviewItem.slot_roles. Its lasting state is a SlotState
(slot_state.py) that exists from the moment the screen is built; its
widgets are a SlotView (slot_view.py) that exists only while its row is
built (see virtual_rows.py). A rebuilt box is simply filled from its
SlotState, so edits, cursor, undo history and the OCR checkbox survive a
row being torn down and rebuilt.

Keeping the two in step:
- Widget to model: any difference between a widget and its SlotState is a
  user edit (sync_from_widget) - recorded in the undo history, marking the
  slot touched, and ticking an "ocr" box's checkbox. It runs on every
  <<Modified>> event, and before anything reads or replaces a box's text,
  since <<Modified>> arrives on a later idle tick than the edit itself.
- Model to widget: the app's own writes (undo/redo, the checkbox) go
  through set_text, which updates the SlotState first, so the resulting
  <<Modified>> isn't mistaken for a user edit.

Also here: the OCR checkbox, spellcheck tagging, and undo/redo (using the
box's own EditHistory, not Tk's, which is turned off on these widgets).
"""

import re
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable, Optional

from .. import logging_config, spellcheck
from ..review_item import ReviewItem
from . import theme
from .edit_history import cursor_after_change
from .slot_state import SlotState
from .slot_view import SlotView

logger = logging_config.get_logger(__name__)

Slot = tuple[int, str]

# Inner horizontal padding for an editable text box's own content, so
# wrapped lines don't run right up against the box's edge.
TEXT_BOX_INNER_PADX = 6

# How long to wait, after the most recent keystroke, before running a
# spellcheck pass on a box.
SPELLCHECK_DEBOUNCE_MS = 300

# Tag marking a misspelled word's range - only ever configured on
# "message"/"ocr{N}" boxes, never on a spacer box.
SPELLCHECK_TAG = "misspelled"

# Shift modifier bit in a key event's state.
_SHIFT_MASK = 0x1

_TRAILING_WORD_RE = re.compile(r"\S+\s*$")


def make_spacer_text_widget(parent: tk.Widget) -> tk.Text:
    """Create (but don't pack) a spacer slot's one-line text box.

    Shared with row_building.measure_text_metrics, so the box that's
    measured is exactly the one that's built.
    """
    return tk.Text(
        parent, height=1, wrap="none", relief="flat", undo=False,
        **theme.dark_text_kwargs(),
        padx=TEXT_BOX_INNER_PADX, pady=4,
    )


def delete_word_backward(event: tk.Event) -> str:
    """Ctrl+Backspace: delete the word before the cursor (plus any
    whitespace trailing it), or just merge with the previous line if the
    cursor is already at the start of a line."""
    widget = event.widget
    line_start = widget.index("insert linestart")
    text_before = widget.get(line_start, "insert")
    if not text_before:
        if widget.index("insert") != "1.0":
            widget.delete("insert -1c", "insert")
        return "break"

    match = _TRAILING_WORD_RE.search(text_before)
    delete_from = f"{line_start}+{match.start()}c" if match else line_start
    widget.delete(delete_from, "insert")
    return "break"


def initial_slot_states(
    items: list[ReviewItem],
    initial_saved_texts: Optional[list[dict[str, Optional[str]]]],
    initial_finalized_texts: Optional[list[dict[str, Optional[str]]]],
) -> dict[Slot, SlotState]:
    """Build one SlotState per editable box, seeded with any saved text.

    A resumed session's edit wins over a previously-finalized one;
    finalized edits fill in the boxes the session doesn't cover. An "ocr"
    box starts checked exactly when its seeded text differs from its OCR
    default.

    Args:
        items: The review items, in transcript order.
        initial_saved_texts: A resumed session's per-item role->text edits,
            or None. Ignored unless it has one entry per item.
        initial_finalized_texts: Previously-finalized per-item role->text
            edits, or None. Same length rule.

    Returns:
        A SlotState for every (item_index, role) slot.
    """
    seeded: dict[Slot, str] = {}
    for source in (initial_saved_texts, initial_finalized_texts):
        if source is None or len(source) != len(items):
            continue
        for idx, edited in enumerate(source):
            for role, text in edited.items():
                if text is not None:
                    seeded.setdefault((idx, role), text)

    states: dict[Slot, SlotState] = {}
    for idx, item in enumerate(items):
        for role in item.slot_roles:
            key = (idx, role)
            default = item.initial_text_for_role(role)
            text = seeded.get(key, default)
            state = SlotState(default=default, text=text)
            if role.startswith("ocr") and text != default:
                state.checked = True
                state.user_edit = text
            states[key] = state
    return states


def _configure_spellcheck_tag(text_widget: tk.Text) -> None:
    """Configure a box's "misspelled" tag: a red underline via the tag's
    "underlinefg" option (Tk 8.6.6+), falling back to a plain underline on
    an older Tk rather than losing the box over a cosmetic feature."""
    try:
        text_widget.tag_configure(
            SPELLCHECK_TAG, underline=True, underlinefg=theme.SPELLCHECK_UNDERLINE
        )
    except tk.TclError:
        logger.warning(
            "this Tk version doesn't support underlinefg - falling back "
            "to a plain (uncolored) underline for misspelled words",
        )
        text_widget.tag_configure(SPELLCHECK_TAG, underline=True)


def _set_text_scrollbar(
    scrollbar: ttk.Scrollbar, before_widget: tk.Widget, first: str, last: str
) -> None:
    """yscrollcommand for a content box: show its scrollbar only while the
    content overflows the box.

    before_widget is whatever sits immediately left of where the scrollbar
    goes (the text widget, or an "ocr" box's checkbox column). Packing the
    scrollbar before it puts it at the true right edge: slaves packed
    earlier already claim the whole cavity, so a scrollbar packed after them
    would get a zero-width sliver.
    """
    if float(first) <= 0.0 and float(last) >= 1.0:
        scrollbar.pack_forget()
    else:
        scrollbar.pack(side="right", fill="y", before=before_widget)
    scrollbar.set(first, last)


class SlotBoxes:
    """Every editable box's state, and the widgets of those currently built.

    Attributes:
        states: {slot: SlotState} for every box in the transcript.
        views: {slot: SlotView} for the boxes whose row is built.
        clock: Time source for EditHistory's pause rule (tests freeze it).
    """

    def __init__(
        self,
        items: list[ReviewItem],
        log_event: Callable[..., None],
        bind_navigation: Callable[[tk.Text, Slot], None],
        on_focused_box_edited: Callable[[Slot], None],
        initial_saved_texts: Optional[list[dict[str, Optional[str]]]] = None,
        initial_finalized_texts: Optional[list[dict[str, Optional[str]]]] = None,
        initial_touched_slots: Optional[Iterable[Slot]] = None,
    ) -> None:
        """Build the SlotStates for every box.

        Args:
            items: The review items, in transcript order.
            log_event: The scroll-trace logger (VirtualRows.log_event).
            bind_navigation: Binds the review screen's navigation and
                scrolling keys (Tab, Page Up/Down, the wheel, ...) on a
                newly built box.
            on_focused_box_edited: Called when the box that has focus is
                edited, e.g. to scroll it back into view.
            initial_saved_texts: See initial_slot_states.
            initial_finalized_texts: See initial_slot_states.
            initial_touched_slots: Slots a resumed session had already
                touched (an untick made before closing still counts).
        """
        self._items = items
        self._log_event = log_event
        self._bind_navigation = bind_navigation
        self._on_focused_box_edited = on_focused_box_edited
        self.states = initial_slot_states(items, initial_saved_texts, initial_finalized_texts)
        for key in initial_touched_slots or ():
            if key in self.states:
                self.states[key].touched = True
        self.views: dict[Slot, SlotView] = {}
        self.clock: Callable[[], float] = time.monotonic

    # -- building and releasing widgets -----------------------------------------

    def build_content_box(
        self, parent: tk.Widget, key: Slot, height: int, pady_bottom: int = 0
    ) -> None:
        """Build a "message" or "ocr{N}" box, packed into `parent`.

        The box has a fixed height (a container with pack_propagate off);
        longer content scrolls inside it, with a scrollbar shown only while
        it overflows. An "ocr" box also gets its checkbox, in a column at
        its top-right.

        Args:
            parent: The row's right-hand column.
            key: The box's slot.
            height: The box's height (px).
            pady_bottom: Gap left below it.
        """
        self._reclaim_if_present(key)
        text_container = ttk.Frame(parent, height=height)
        text_container.pack(side="top", fill="x", pady=(0, pady_bottom))
        text_container.pack_propagate(False)

        scrollbar = ttk.Scrollbar(text_container, orient="vertical")
        text_widget = tk.Text(
            text_container, wrap="word", relief="flat", undo=False,
            **theme.dark_text_kwargs(),
            padx=TEXT_BOX_INNER_PADX, pady=4,
        )
        _configure_spellcheck_tag(text_widget)

        # Packed before text_widget, so it claims its slice of the right
        # edge before text_widget's expand=True takes the rest.
        checkbox_column: Optional[tk.Widget] = None
        checked_var: Optional[tk.BooleanVar] = None
        if key[1].startswith("ocr"):
            # ttk (styled in theme.py) so the checked indicator matches the
            # setup screen's checkboxes; the column's style matches the text
            # background so there's no visible seam.
            checkbox_column = ttk.Frame(text_container, style="OcrCheckboxColumn.TFrame")
            checkbox_column.pack(side="right", fill="y")
            checked_var = tk.BooleanVar(value=self.states[key].checked)
            checkbox = ttk.Checkbutton(
                checkbox_column, variable=checked_var, takefocus=0,
                style="OcrCheckbox.TCheckbutton",
                command=lambda k=key: self.on_ocr_checkbox_toggle(k),
            )
            checkbox.pack(side="top")

        before_widget = checkbox_column if checkbox_column is not None else text_widget
        text_widget.configure(
            yscrollcommand=lambda first, last, sb=scrollbar, b=before_widget: (
                _set_text_scrollbar(sb, b, first, last)
            )
        )
        scrollbar.configure(command=text_widget.yview)
        text_widget.pack(side="left", fill="both", expand=True)

        self._populate(key, text_widget)
        self.views[key] = SlotView(text_widget, text_container, checkbox_var=checked_var)
        self.schedule_spellcheck(key)

    def build_spacer_box(
        self, parent: tk.Widget, key: Slot, height: int, pady_bottom: int = 0
    ) -> None:
        """Build a spacer box: one text line tall, no scrollbar, no
        spellcheck - it only ever holds a few literal "\\n" tokens.

        Args:
            parent: The row's right-hand column.
            key: The box's slot.
            height: The box's height (px) - one line of the text font.
            pady_bottom: Gap left below it.
        """
        self._reclaim_if_present(key)
        text_container = ttk.Frame(parent, height=height)
        text_container.pack(side="top", fill="x", pady=(0, pady_bottom))
        text_container.pack_propagate(False)

        text_widget = make_spacer_text_widget(text_container)
        text_widget.pack(side="left", fill="both", expand=True)

        self._populate(key, text_widget)
        self.views[key] = SlotView(text_widget, text_container)

    def _populate(self, key: Slot, text_widget: tk.Text) -> None:
        """Fill a freshly built box from its SlotState and bind its keys.

        Args:
            key: The box's slot.
            text_widget: The new, empty Text widget for it.
        """
        state = self.states[key]
        text_widget.insert("1.0", state.text)
        # The "insert" mark has right gravity, so the insert above left it
        # at the end; put it back where it was ("1.0" for a box never
        # visited). Tk clamps an index past the end rather than raising.
        text_widget.mark_set("insert", state.cursor)
        text_widget.see("insert")
        text_widget.edit_modified(False)
        self._log_event(
            "box_build",
            key=key,
            widget=str(text_widget),
            **logging_config.text_fingerprint(state.text),
        )

        text_widget.bind("<Control-BackSpace>", delete_word_backward)
        # Both cases go to one handler that checks Shift itself: the
        # keysym's case alone would make Caps Lock swap undo and redo.
        text_widget.bind("<Control-z>", self.on_undo_key)
        text_widget.bind("<Control-Z>", self.on_undo_key)
        text_widget.bind(
            "<<Modified>>",
            lambda e, k=key, t=text_widget: self._on_text_modified(k, t),
        )
        self._bind_navigation(text_widget, key)

    def _reclaim_if_present(self, key: Slot) -> None:
        """Tear down a live widget already registered for `key`, if any.

        Should be impossible - a row is only built when it isn't already -
        but a bookkeeping bug could get here anyway (see
        archive/INVESTIGATION_shift_tab_reconcile_lockup.md). Brings the
        SlotState up to date first, so nothing typed is lost, then destroys
        the old container so no widget is leaked.
        """
        if key not in self.views:
            return
        logger.warning(
            "building a box for a key that already has a live widget - "
            "reclaiming its content before replacing it, rather than "
            "silently orphaning it",
            extra=logging_config.extra(key=key, old_widget=str(self.views[key].text_widget)),
        )
        self._release_view(key).container.destroy()

    def _release_view(self, key: Slot) -> SlotView:
        """Unregister a box's widgets, saving what its SlotState needs first
        (text and cursor) and cancelling any pending spellcheck. Doesn't
        destroy anything."""
        view = self.views.pop(key)
        view.cancel_spellcheck()
        try:
            self.sync_from_widget(key, view.text_widget)
            self.states[key].cursor = view.text_widget.index("insert")
        except tk.TclError:
            logger.error(
                "could not read back this box's widget - whatever it held "
                "since the last sync is lost",
                exc_info=True,
                extra=logging_config.extra(key=key),
            )
        return view

    def release_row(self, index: int, focused: Optional[tk.Misc]) -> Optional[Slot]:
        """Unregister every box of a row that's about to be destroyed.

        Args:
            index: The row's item index.
            focused: The widget that has focus right now, if any.

        Returns:
            The slot whose box had focus, if one of this row's did.
        """
        had_focus_key = None
        for key in [k for k in self.views if k[0] == index]:
            view = self._release_view(key)
            had_focus = view.text_widget is focused
            if had_focus:
                had_focus_key = key
            self._log_event(
                "box_teardown",
                key=key,
                widget=str(view.text_widget),
                had_focus=had_focus,
                **logging_config.text_fingerprint(self.states[key].text),
            )
        return had_focus_key

    def cancel_all_spellchecks(self) -> None:
        """Cancel every built box's pending spellcheck (the screen is going
        away without its rows being torn down one by one)."""
        for view in self.views.values():
            view.cancel_spellcheck()

    def key_for_widget(self, widget: Optional[tk.Misc]) -> Optional[Slot]:
        """The slot of the built box backed by `widget`, if any."""
        if widget is None:
            return None
        for key, view in self.views.items():
            if view.text_widget is widget:
                return key
        return None

    # -- keeping model and widget in step --------------------------------------

    def sync_from_widget(self, key: Slot, text_widget: tk.Text) -> bool:
        """Bring a box's SlotState up to date with its live widget.

        Any difference is a user edit made in the widget (typing, paste,
        Ctrl+Backspace, a middle-click paste, or a test's direct insert): it
        is recorded in the undo history, marks the slot touched, and ticks
        an "ocr" box's checkbox.

        Args:
            key: The box's slot.
            text_widget: The live widget currently backing that box.

        Returns:
            True if the widget held an edit the SlotState didn't have yet.
        """
        state = self.states[key]
        text = text_widget.get("1.0", "end-1c")
        if text == state.text:
            return False
        state.history.record(state.text, text, self.clock())
        state.text = text
        state.touched = True
        if key[1].startswith("ocr"):
            self._on_ocr_box_user_edit(key, text)
        return True

    def set_text(self, key: Slot, text: str, cursor: str = "1.0") -> None:
        """Replace a box's text as the app, not as a user edit.

        Updates the SlotState first, so the <<Modified>> event this write
        causes isn't taken for a user edit, then the widget if it's built.
        Callers record the change in the undo history themselves, if it
        belongs there.

        Args:
            key: The box's slot.
            text: The new text.
            cursor: Tk index to put the cursor at afterwards.
        """
        state = self.states[key]
        state.text = text
        state.cursor = cursor
        view = self.views.get(key)
        if view is None:
            return
        text_widget = view.text_widget
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", text)
        text_widget.mark_set("insert", cursor)
        text_widget.see("insert")

    def _on_text_modified(self, key: Slot, text_widget: tk.Text) -> None:
        """Bound to a box's <<Modified>> event: record any user edit,
        re-run the spellcheck, and - if the box has focus - tell the owner
        (which scrolls it back into view: the wheel/scrollbar can move the
        view without moving focus, and keystrokes still reach an off-screen
        focused box). The focus check also keeps a freshly built box's own
        initial insert, which fires this event too, from moving the view."""
        had_focus = text_widget is text_widget.focus_get()
        changed = self.sync_from_widget(key, text_widget)
        self._log_event(
            "box_modified",
            key=key,
            had_focus=had_focus,
            changed=changed,
            **logging_config.text_fingerprint(self.states[key].text),
        )
        text_widget.edit_modified(False)
        if not key[1].startswith("spacer"):
            self.schedule_spellcheck(key)
        if had_focus:
            self._on_focused_box_edited(key)

    # -- the OCR checkbox ---------------------------------------------------------

    def _on_ocr_box_user_edit(self, key: Slot, text: str) -> None:
        """An "ocr" box was edited by the user, so its checkbox ticks itself.

        This is the "any change checks the box" rule: unlike undo/redo
        (which compare the result to the OCR default), it doesn't untick
        even if the new text happens to match the default again.
        """
        state = self.states[key]
        state.checked = True
        state.user_edit = text
        view = self.views.get(key)
        var = view.checkbox_var if view is not None else None
        if var is not None and not var.get():
            var.set(True)

    def on_ocr_checkbox_toggle(self, key: Slot) -> None:
        """Command callback for an "ocr" box's checkbox.

        By the time this runs, Tk has already flipped the checkbox's
        variable. Unchecking shows the OCR default without discarding the
        edit (kept in SlotState.user_edit); checking shows that edit again,
        or leaves the default in place if there never was one. The swap is
        one undo step of its own.
        """
        view = self.views[key]
        var = view.checkbox_var
        checked = var.get()
        # Read before syncing: an edit not yet synced would tick the box
        # again via _on_ocr_box_user_edit, undoing the click.
        self.sync_from_widget(key, view.text_widget)
        var.set(checked)
        state = self.states[key]
        state.checked = checked
        state.touched = True
        text_to_show = state.user_edit if checked and state.user_edit is not None else state.default

        state.history.record(state.text, text_to_show, self.clock(), standalone=True)
        self.set_text(key, text_to_show)

        self._log_event(
            "ocr_checkbox_toggled",
            key=key,
            checked=checked,
            **logging_config.text_fingerprint(text_to_show),
        )

    def _resync_ocr_checkbox_after_undo(self, key: Slot) -> None:
        """Re-derive an "ocr" box's checkbox after an undo/redo: checked
        exactly when the text differs from the OCR default (the rule used to
        seed it), rather than typing's "any change checks the box". So
        undoing a box's only edit unticks it, and undoing an untick brings
        back both the edit and the tick."""
        if not key[1].startswith("ocr"):
            return
        state = self.states[key]
        state.checked = state.text != state.default
        if state.checked:
            state.user_edit = state.text
        view = self.views.get(key)
        if view is not None and view.checkbox_var is not None:
            view.checkbox_var.set(state.checked)

    # -- undo/redo ---------------------------------------------------------------

    def on_undo_key(self, event: tk.Event) -> str:
        """Ctrl+Z undoes and Ctrl+Shift+Z redoes - decided by the Shift
        modifier rather than the letter's case, so Caps Lock doesn't swap
        them."""
        shift_held = isinstance(event.state, int) and bool(event.state & _SHIFT_MASK)
        return self.redo_text(event) if shift_held else self.undo_text(event)

    def undo_text(self, event: tk.Event) -> str:
        """Undo one step in the event's box."""
        return self._step_history(event.widget, redo=False)

    def redo_text(self, event: tk.Event) -> str:
        """Redo one step in the event's box."""
        return self._step_history(event.widget, redo=True)

    def _step_history(self, widget: tk.Text, redo: bool) -> str:
        """Move one step back (undo) or forward (redo) in a box's history.
        The cursor lands at the change, and an "ocr" box's checkbox is
        re-derived from the result.

        Returns:
            "break", so Tk's own Text bindings don't also handle the key.
        """
        action = "redo" if redo else "undo"
        key = self.key_for_widget(widget)
        if key is None:
            return "break"
        self.sync_from_widget(key, widget)
        state = self.states[key]
        before = state.text
        target = state.history.redo(before) if redo else state.history.undo(before)
        if target is None:
            logger.info(
                f"{action} pressed, nothing to {action}",
                extra=logging_config.extra(key=key),
            )
            return "break"
        self.set_text(key, target, cursor=f"1.0+{cursor_after_change(before, target)}c")
        state.touched = True
        self._resync_ocr_checkbox_after_undo(key)
        logger.info(
            f"{action} applied",
            extra=logging_config.extra(
                key=key,
                undo_depth=state.history.undo_depth,
                redo_depth=state.history.redo_depth,
                **logging_config.text_fingerprint(target),
            ),
        )
        return "break"

    # -- spellcheck ----------------------------------------------------------------

    def schedule_spellcheck(self, key: Slot) -> None:
        """Debounce a spellcheck pass on a built box: cancel any pending one
        and schedule a fresh one SPELLCHECK_DEBOUNCE_MS from now. Also run
        right after a box is (re)built, since tags don't survive the old
        widget being destroyed."""
        view = self.views.get(key)
        if view is None:
            return
        view.cancel_spellcheck()
        view.spellcheck_after_id = view.text_widget.after(
            SPELLCHECK_DEBOUNCE_MS,
            lambda k=key, t=view.text_widget: self.run_spellcheck(k, t),
        )

    def run_spellcheck(self, key: Slot, text_widget: tk.Text) -> None:
        """(Re)tag a box's misspelled words. Guarded against the widget
        having been destroyed since the pass was scheduled."""
        view = self.views.get(key)
        if view is not None and view.text_widget is text_widget:
            view.spellcheck_after_id = None
        try:
            content = text_widget.get("1.0", "end-1c")
        except tk.TclError:
            return
        try:
            spans = spellcheck.find_misspelled_spans(content)
            text_widget.tag_remove(SPELLCHECK_TAG, "1.0", "end")
            for start, end in spans:
                text_widget.tag_add(SPELLCHECK_TAG, f"1.0+{start}c", f"1.0+{end}c")
        except tk.TclError:
            logger.warning(
                "spellcheck tagging failed on a box that was torn down mid-pass",
                extra=logging_config.extra(key=key),
            )

    # -- reporting edits -----------------------------------------------------------

    def reported_text(self, key: Slot) -> Optional[str]:
        """The edit to report for one box, for Finalize and autosave.

        An "ocr" box whose checkbox is unchecked reports None even though it
        shows real text: unchecked means "use the OCR default", and an edit
        hidden behind the checkbox isn't written out or saved.

        Returns:
            The box's current text, or None if it equals the default (or is
            an unchecked "ocr" box).
        """
        view = self.views.get(key)
        if view is not None:
            self.sync_from_widget(key, view.text_widget)
        state = self.states[key]
        if key[1].startswith("ocr") and not state.checked:
            return None
        # Text identical to the default isn't an edit - storing it as one
        # would pin stale text over newer defaults on later runs.
        if state.text == state.default:
            return None
        return state.text

    def collect_edited_texts(self) -> list[dict[str, Optional[str]]]:
        """One role->text dict per item, in transcript order - see
        reported_text for what each value means."""
        return [
            {role: self.reported_text((idx, role)) for role in item.slot_roles}
            for idx, item in enumerate(self._items)
        ]

    def touched_slots(self) -> set[Slot]:
        """The slots the user has deliberately acted on this session (see
        SlotState.touched)."""
        return {key for key, state in self.states.items() if state.touched}
