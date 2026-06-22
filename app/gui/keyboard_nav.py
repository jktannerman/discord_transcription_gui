"""Keyboard navigation and editing shortcuts for the review screen's text
boxes: Tab/Shift-Tab move between boxes in transcript order, Page Up/Down
scroll the whole window, Ctrl+Backspace deletes the previous word, and
Ctrl+Z/Ctrl+Shift+Z undo/redo within a box. Mixed into ReviewFrame rather
than taken as a standalone object, since every method here reaches into
ReviewFrame's row/widget bookkeeping (self._text_widgets, self._slots,
self._row_heights, self._ensure_materialized, self._canvas,
self._finalize_button) - threading all of that through as constructor
args would just relocate the coupling, not remove it.

"Transcript order" navigates self._slots, the flat (item_index, role) list
built once in ReviewFrame.__init__ - a row can now have a "message" box, an
"ocr" box, or both (message first, since text sits above image), so a
single item index is no longer enough to address one box.
"""

import re
import tkinter as tk
from typing import Optional, Tuple

_TRAILING_WORD_RE = re.compile(r"\S+\s*$")


class KeyboardNavMixin:
    def _delete_word_backward(self, event: tk.Event) -> str:
        """Ctrl+Backspace: delete the word before the cursor (plus any
        whitespace trailing it), or just merge with the previous line if
        the cursor is already at the start of a line."""
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

    def _undo_text(self, event: tk.Event) -> str:
        try:
            event.widget.edit_undo()
        except tk.TclError:
            pass  # nothing to undo
        return "break"

    def _redo_text(self, event: tk.Event) -> str:
        try:
            event.widget.edit_redo()
        except tk.TclError:
            pass  # nothing to redo
        return "break"

    def _on_page_up(self, event: Optional[tk.Event] = None) -> str:
        self._log_event("input_page_up")
        self._canvas.yview_scroll(-1, "pages")
        self._schedule_reconcile()
        return "break"

    def _on_page_down(self, event: Optional[tk.Event] = None) -> str:
        self._log_event("input_page_down")
        self._canvas.yview_scroll(1, "pages")
        self._schedule_reconcile()
        return "break"

    def _on_tab(self, event: tk.Event) -> str:
        return self._move_focus(1)

    def _on_shift_tab(self, event: tk.Event) -> str:
        return self._move_focus(-1)

    def _focused_slot(self) -> Optional[Tuple[int, str]]:
        focused = self.focus_get()
        if focused is None:
            return None
        for key, widget in self._text_widgets.items():
            if widget is focused:
                return key
        return None

    def _scroll_into_view(self, index: int) -> None:
        """Adjust the canvas's scroll position only as much as needed to
        bring items[index]'s row fully into the viewport, used after Tab
        moves focus somewhere not currently visible. Computed entirely
        from self._row_heights (the same authoritative source _reconcile
        uses for layout) rather than queried widget geometry: row.winfo_y()
        is relative to the repositioned _scroll_frame block (see
        _reconcile), not the canvas's absolute coordinate space, so it
        can't be compared directly against canvas.canvasy(0)."""
        canvas = self._canvas
        total_height = sum(self._row_heights)
        if total_height <= 0:
            return

        viewport_height = canvas.winfo_height()
        row_top = self._offset_of(index)
        row_bottom = row_top + self._row_heights[index]
        view_top = canvas.canvasy(0)
        view_bottom = canvas.canvasy(viewport_height)

        action = "none"
        if row_top < view_top:
            canvas.yview_moveto(row_top / total_height)
            action = "scroll_up"
        elif row_bottom > view_bottom:
            canvas.yview_moveto((row_bottom - viewport_height) / total_height)
            action = "scroll_down"

        self._log_event(
            "scroll_into_view",
            index=index,
            row_top=row_top,
            row_bottom=row_bottom,
            view_top=round(view_top, 1),
            view_bottom=round(view_bottom, 1),
            total_height=total_height,
            action=action,
        )

    def _focus_text_box(self, index: int, role: str) -> None:
        """Focus items[index]'s `role` text box, scroll its row into view
        on the review canvas, and make sure the box's own internal view
        shows its cursor - the box may have been built (or last left)
        scrolled to wherever its cursor happened to be, which isn't
        necessarily the start of its text."""
        self._log_event("focus_text_box", index=index, role=role)
        widget = self._text_widgets[(index, role)]
        widget.focus_set()
        widget.see("insert")
        self._scroll_into_view(index)

    def _move_focus(self, delta: int) -> str:
        """Move focus to the next/previous box in self._slots (or to/from
        the Finalize button at either end), materializing the target row
        first via _ensure_materialized if it isn't currently built."""
        slots = self._slots
        self._log_event(
            "move_focus_start",
            delta=delta,
            focus_is_finalize_button=self.focus_get() is self._finalize_button,
        )
        step = 1 if delta > 0 else -1

        if self.focus_get() is self._finalize_button:
            if delta < 0 and slots:
                self._goto_slot(slots[-1])
            return "break"

        current = self._focused_slot()
        if current is None:
            if not slots:
                return "break"
            pos = 0 if delta > 0 else len(slots) - 1
            self._goto_slot(slots[pos])
            return "break"

        pos = self._slot_positions[current] + step
        if pos < 0:
            # Already at the first box, nowhere to go.
            return "break"
        if pos >= len(slots):
            self._log_event("move_focus_to_finalize_button")
            self._finalize_button.focus_set()
            return "break"

        self._goto_slot(slots[pos])
        return "break"

    def _goto_slot(self, slot: Tuple[int, str]) -> None:
        index, role = slot
        self._ensure_materialized(index)
        self._focus_text_box(index, role)
