"""Focus and keyboard navigation on the review screen: Tab/Shift-Tab move
between boxes in transcript order, Up/Down keep the cursor's box on screen,
and a focused box keeps (or gets back) focus as its row is torn down and
rebuilt.

"Transcript order" is the flat list of slots, (item_index, role), built
once from every item's slot_roles - a row can have several boxes, so an
item index alone doesn't address one.

Also home to bind_select_all, the one app-wide shortcut (Ctrl+A), set up
once at startup for every screen's text fields.
"""

import tkinter as tk
from typing import Callable, Optional

from .. import logging_config
from .layout_constants import ROW_PACK_PADY_PX
from .slot_boxes import Slot, SlotBoxes
from .virtual_rows import VirtualRows

logger = logging_config.get_logger(__name__)

# Widget classes whose Ctrl+A should select all their text (see
# bind_select_all) - every text-entry widget the app uses.
_SELECT_ALL_CLASSES = ("Text", "Entry", "TEntry", "TCombobox")
# Ctrl+A, Ctrl+Shift+A, and Ctrl+A with Caps Lock on.
_SELECT_ALL_SEQUENCES = ("<Control-a>", "<Control-A>", "<Control-Lock-A>")


def _select_all(event: tk.Event) -> str:
    """Select all of the focused widget's text, via its class's own
    <<SelectAll>> handler.

    Args:
        event: The Ctrl+A key event.

    Returns:
        "break", so the key's default X11 binding (move to line start)
        doesn't also run.
    """
    event.widget.event_generate("<<SelectAll>>")
    return "break"


def bind_select_all(root: tk.Misc) -> None:
    """Make Ctrl+A select all in every text field, on every platform.

    Tk maps Ctrl+A to select-all only on Windows. On X11 it's an
    Emacs-style "move to line start" (<<LineStart>>), with select-all on
    Ctrl+/ instead. A class binding on the physical key takes priority over
    that virtual event, so this overrides it for the whole app. Tk also
    binds <<LineStart>> to Control-Lock-A specifically, so Caps Lock needs
    its own binding here, or it would still go to line start.

    Args:
        root: Any widget in the app (class bindings are app-wide).
    """
    for widget_class in _SELECT_ALL_CLASSES:
        for sequence in _SELECT_ALL_SEQUENCES:
            root.bind_class(widget_class, sequence, _select_all)


class FocusNavigator:
    """Moves focus between the review screen's boxes and keeps the focused
    box on screen.

    Attributes:
        slots: Every box's slot, in transcript order.
        refocus_slot: The slot whose box had focus when its row was torn
            down, to be refocused once the row is rebuilt; None otherwise.
        last_focused_slot: The box that most recently had focus, kept even
            after the app stops being the active window (when Tk reports no
            focus at all); None if focus last went to the Finalize button.
    """

    def __init__(
        self,
        focus_owner: tk.Misc,
        slots: list[Slot],
        boxes: SlotBoxes,
        rows: VirtualRows,
        finalize_button: tk.Widget,
        on_view_moved: Callable[[], None],
    ) -> None:
        """
        Args:
            focus_owner: Any widget of the app, used to ask Tk what has focus.
            slots: Every box's slot, in transcript order.
            boxes: The boxes being navigated.
            rows: The rows they live in.
            finalize_button: Where Tab lands after the last box.
            on_view_moved: Called after this moves the view.
        """
        self._focus_owner = focus_owner
        self.slots = slots
        self._slot_positions = {slot: pos for pos, slot in enumerate(slots)}
        self._boxes = boxes
        self._rows = rows
        self._finalize_button = finalize_button
        self._on_view_moved = on_view_moved
        self.refocus_slot: Optional[Slot] = None
        self.last_focused_slot: Optional[Slot] = None

    # -- what has focus --------------------------------------------------------

    def focused_slot(self) -> Optional[Slot]:
        """The slot of the box Tk says has focus right now, if any."""
        return self._boxes.key_for_widget(self._focus_owner.focus_get())

    def note_focused_slot(self, slot: Optional[Slot]) -> None:
        """Record the box that just got focus (None: the Finalize button)."""
        self.last_focused_slot = slot

    def slot_to_restore(self) -> Optional[Slot]:
        """The box to restore focus to on resume: the focused one if Tk
        reports one, otherwise the last one that had focus - Tk reports no
        focus while the app isn't the active window, which on some desktops
        includes the moment the window is closing.

        Returns:
            A slot, or None if no box has had focus yet, or focus last went
            to the Finalize button.
        """
        current = self.focused_slot()
        if current is not None:
            return current
        if self._focus_owner.focus_get() is self._finalize_button:
            return None
        return self.last_focused_slot

    # -- keeping focus across a row rebuild ------------------------------------

    def restore_focus_after_build(self, index: int) -> None:
        """If row `index`'s box lost focus when the row was torn down, give
        it back now that the row is rebuilt.

        Deferred to the next idle tick: this runs mid-reconcile, and
        focusing now would have its scroll-into-view clobbered by the
        reconcile's own scroll correction. Skipped if something else took
        focus meanwhile (another box, or the Finalize button) - not keyed on
        focus_get() being None, since destroying a focused widget hands
        focus to an ancestor rather than clearing it.
        """
        if self.refocus_slot is None or self.refocus_slot[0] != index:
            return
        slot = self.refocus_slot
        self.refocus_slot = None
        self._focus_owner.after_idle(
            lambda s=slot: self.focus_text_box(*s)
            if self.focused_slot() is None and self._focus_owner.focus_get() is not self._finalize_button
            else None
        )

    # -- Tab / Shift-Tab ---------------------------------------------------------

    def on_tab(self, event: tk.Event) -> str:
        return self.move_focus(1)

    def on_shift_tab(self, event: tk.Event) -> str:
        return self.move_focus(-1)

    def move_focus(self, delta: int) -> str:
        """Move focus to the next/previous box (or to/from the Finalize
        button at either end), building the target row first if needed."""
        slots = self.slots
        self._rows.log_event(
            "move_focus_start",
            delta=delta,
            focus_is_finalize_button=self._focus_owner.focus_get() is self._finalize_button,
        )
        step = 1 if delta > 0 else -1

        if self._focus_owner.focus_get() is self._finalize_button:
            if delta < 0 and slots:
                self.goto_slot(slots[-1])
            return "break"

        current = self.focused_slot()
        if current is None:
            if not slots:
                return "break"
            pos = 0 if delta > 0 else len(slots) - 1
            self.goto_slot(slots[pos])
            return "break"

        pos = self._slot_positions[current] + step
        if pos < 0:
            # Already at the first box, nowhere to go.
            return "break"
        if pos >= len(slots):
            self._rows.log_event("move_focus_to_finalize_button")
            self._finalize_button.focus_set()
            return "break"

        self.goto_slot(slots[pos])
        return "break"

    def goto_slot(self, slot: Slot) -> None:
        """Focus `slot` for Tab/Shift-Tab, aligning its box to the top of
        the review window."""
        index, role = slot
        self._rows.ensure_materialized(index)
        self.focus_text_box(index, role, align_top=True)

    def focus_text_box(self, index: int, role: str, align_top: bool = False) -> None:
        """Focus a box, scroll it into view, and make its own internal view
        show its cursor.

        Args:
            index: The box's item index.
            role: The box's role.
            align_top: Passed through to scroll_box_into_view.
        """
        self._rows.log_event("focus_text_box", index=index, role=role)
        view = self._boxes.views.get((index, role))
        if view is None:
            # Normally impossible (goto_slot builds the row first), but a
            # row whose build failed leaves exactly this gap - log rather
            # than raise on every Tab aimed at it.
            logger.warning(
                "no live widget for this slot - its row's build likely "
                "failed; nothing to focus",
                extra=logging_config.extra(index=index, role=role),
            )
            return
        view.text_widget.focus_set()
        # Recorded directly as well as via <FocusIn>: Tk only delivers
        # FocusIn once the app is the active window, which it may not be yet
        # (e.g. restoring a resumed session's focus at startup).
        self.note_focused_slot((index, role))
        view.text_widget.see("insert")
        self.scroll_box_into_view((index, role), align_top=align_top)

    # -- keeping a box on screen ---------------------------------------------------

    def box_document_top(self, index: int, container: tk.Widget, row: tk.Widget) -> float:
        """Document-space y of a box's top edge.

        offset_of(index) is where row `index`'s pack slot starts; the row
        frame's own top is ROW_PACK_PADY_PX below that; and the box's offset
        within its row comes from real screen positions, which are
        unaffected by the built block being repositioned.

        Raises:
            tk.TclError: If a widget involved was destroyed mid-check.
        """
        return (
            self._rows.offset_of(index) + ROW_PACK_PADY_PX
            + (container.winfo_rooty() - row.winfo_rooty())
        )

    def box_top(self, key: Slot) -> Optional[float]:
        """Document-space y of a built box's top edge, or None if its row
        isn't built (or was destroyed mid-check)."""
        view = self._boxes.views.get(key)
        row = self._rows.row_frames.get(key[0])
        if view is None or view.container is None or row is None:
            return None
        try:
            return self.box_document_top(key[0], view.container, row)
        except tk.TclError:
            return None

    def scroll_box_into_view(self, key: Slot, align_top: bool = False) -> None:
        """Scroll only as much as needed to bring `key`'s own box - not just
        its row, which can be taller than the viewport - fully into view.

        With `align_top` (Tab/Shift-Tab), the box's top edge is scrolled to
        the top of the viewport every time, even if the box was already
        fully visible, so the next box always starts at the same place.
        Near the end of the document it ends up as high as it can go.
        """
        index, _ = key
        view = self._boxes.views.get(key)
        row = self._rows.row_frames.get(index)
        if view is None or view.container is None or row is None:
            return
        container = view.container
        canvas = self._rows.canvas
        total_height = self._rows.total_height()
        viewport_height = canvas.winfo_height()
        if total_height <= 0 or viewport_height <= 1:
            return

        try:
            box_top = self.box_document_top(index, container, row)
        except tk.TclError:
            return
        box_bottom = box_top + container.winfo_height()
        view_top = canvas.canvasy(0)
        view_bottom = canvas.canvasy(viewport_height)

        action = "none"
        if align_top:
            if abs(box_top - view_top) >= 1:
                self._rows.move_view_to(box_top)
                action = "align_top"
        elif box_top < view_top:
            self._rows.move_view_to(box_top)
            action = "scroll_up"
        elif box_bottom > view_bottom:
            self._rows.move_view_to(box_bottom - viewport_height)
            action = "scroll_down"

        # Cross-check the document-space model against real screen pixels:
        # a nonzero discrepancy means `heights` has drifted from the real
        # layout (see docs/ARCHITECTURE_ROW_GEOMETRY.md).
        real_offset_px = container.winfo_rooty() - canvas.winfo_rooty()
        model_offset_px = box_top - view_top
        self._rows.log_event(
            "scroll_box_into_view",
            key=key,
            box_top=round(box_top, 1),
            box_bottom=round(box_bottom, 1),
            view_top=round(view_top, 1),
            view_bottom=round(view_bottom, 1),
            total_height=total_height,
            action=action,
            recorded_row_height=self._rows.heights[index] if index < len(self._rows.heights) else None,
            real_row_winfo_height=row.winfo_height(),
            real_offset_px=real_offset_px,
            model_offset_px=round(model_offset_px, 1),
            model_real_discrepancy_px=round(model_offset_px - real_offset_px, 1),
        )
        if action == "align_top":
            # Aligning can move the view further than a minimal scroll, so
            # rows below the target may not be built yet.
            self._rows.schedule_reconcile()
        self._on_view_moved()

    def on_vertical_arrow(self, event: tk.Event, key: Slot) -> None:
        """Up/Down: Tk's default binding still moves the cursor (not
        "break"); one idle tick later, once it has, make sure the cursor
        didn't leave the viewport."""
        widget = event.widget
        self._focus_owner.after_idle(lambda: self.keep_cursor_in_viewport(key, widget))

    def keep_cursor_in_viewport(self, key: Slot, widget: tk.Text) -> None:
        """If the cursor ended up above/below the viewport, scroll just
        enough to bring it back - aligning the box's top/bottom edge (not
        just the cursor's line) with the viewport's, so as much of the box
        as fits is shown."""
        index, _ = key
        view = self._boxes.views.get(key)
        if view is None or widget is not view.text_widget:
            return  # row was torn down/rebuilt before this idle tick ran
        container = view.container
        row = self._rows.row_frames.get(index)
        if row is None:
            return
        try:
            bbox = widget.bbox("insert")
            if bbox is None:
                return
            container_top = self.box_document_top(index, container, row)
            cursor_top = container_top + (widget.winfo_rooty() - container.winfo_rooty()) + bbox[1]
        except tk.TclError:
            return
        container_bottom = container_top + container.winfo_height()
        cursor_bottom = cursor_top + bbox[3]

        canvas = self._rows.canvas
        viewport_height = canvas.winfo_height()
        if viewport_height <= 1 or self._rows.total_height() <= 0:
            return
        view_top = canvas.canvasy(0)
        view_bottom = canvas.canvasy(viewport_height)

        action = "none"
        if cursor_top < view_top:
            self._rows.move_view_to(container_top)
            action = "scroll_up_to_box_top"
        elif cursor_bottom > view_bottom:
            self._rows.move_view_to(container_bottom - viewport_height)
            action = "scroll_down_to_box_bottom"

        self._rows.log_event(
            "arrow_scroll_into_view",
            key=key,
            cursor_top=round(cursor_top, 1),
            cursor_bottom=round(cursor_bottom, 1),
            view_top=round(view_top, 1),
            view_bottom=round(view_bottom, 1),
            action=action,
        )
        if action != "none":
            self._rows.schedule_reconcile()
            self._on_view_moved()
