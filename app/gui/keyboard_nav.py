"""Keyboard navigation and editing shortcuts for the review screen's text
boxes: Tab/Shift-Tab move between boxes in transcript order, Page Up/Down
scroll the whole window, Up/Down keep the cursor's box on-screen (scrolling
the review window, not just the box's own internal view, if the cursor
would otherwise go offscreen - see _keep_cursor_in_viewport), Ctrl+Backspace
deletes the previous word, and Ctrl+Z/Ctrl+Shift+Z undo/redo within a box.
Mixed into ReviewFrame rather
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
        else:
            self._record_undo_marker(event.widget, "undo")
        return "break"

    def _redo_text(self, event: tk.Event) -> str:
        try:
            event.widget.edit_redo()
        except tk.TclError:
            pass  # nothing to redo
        else:
            self._record_undo_marker(event.widget, "redo")
        return "break"

    def _record_undo_marker(self, widget: tk.Text, name: str) -> None:
        """Append an "undo"/"redo" marker to `widget`'s UndoLog (see
        text_undo.py), so that if this box's row is later torn down and
        rebuilt, replaying its log reproduces this undo/redo too - not just
        the insert/delete calls either side of it. Without this, a box torn
        down right after an undo would replay back to the *un-undone* text,
        since edit_undo()/edit_redo() act on Tk's internal undo stack
        directly rather than via the widget's Tcl "insert"/"delete"
        subcommands that text_undo.py's recording proxy observes."""
        for key, candidate in self._text_widgets.items():
            if candidate is widget:
                log = self._undo_logs.get(key)
                if log is not None:
                    log.ops.append((name, ()))
                return

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

    def _scroll_box_into_view(self, key: Tuple[int, str]) -> None:
        """Adjust the canvas's scroll position only as much as needed to
        bring `key`'s own box - not just its row - fully into the
        viewport. Used after Tab/Shift-Tab moves focus somewhere not fully
        visible, and by _on_text_modified to keep a focused box onscreen
        while typing.

        A row can stack more than one box (a message's text box, one OCR
        box per attached image, and a spacer box between/after each of
        those - see _build_row) and can end up taller than the viewport
        itself, so checking only the row's outer bounds - this method's
        predecessor, which this replaced - could report a row as "already
        fully visible" while the specific box a caller actually cares about
        was still only partially onscreen, or even entirely covered: e.g.
        the row's bottom-most box sitting just past the viewport edge while
        the row's top-most box (also within the same row, so sharing the
        same row-level bounds) was fully visible. Box bounds are computed
        the same way _keep_cursor_in_viewport's are forced to: row.winfo_y()
        is relative to the repositioned _scroll_frame block (see
        _reconcile), not the canvas's absolute coordinate space, so
        self._offset_of(index) (the row's own document-space offset) is
        combined with a winfo_rooty() delta for the box's offset *within*
        that row, which isn't affected by that repositioning."""
        index, role = key
        container = self._text_containers.get(key)
        row = self._row_frames.get(index)
        if container is None or row is None:
            return
        canvas = self._canvas
        total_height = sum(self._row_heights)
        viewport_height = canvas.winfo_height()
        if total_height <= 0 or viewport_height <= 1:
            return

        try:
            box_top = self._offset_of(index) + (container.winfo_rooty() - row.winfo_rooty())
        except tk.TclError:
            return  # a widget along the way was destroyed mid-check
        box_bottom = box_top + container.winfo_height()
        view_top = canvas.canvasy(0)
        view_bottom = canvas.canvasy(viewport_height)

        action = "none"
        if box_top < view_top:
            canvas.yview_moveto(max(box_top, 0) / total_height)
            action = "scroll_up"
        elif box_bottom > view_bottom:
            canvas.yview_moveto(max(box_bottom - viewport_height, 0) / total_height)
            action = "scroll_down"

        self._log_event(
            "scroll_box_into_view",
            key=key,
            box_top=round(box_top, 1),
            box_bottom=round(box_bottom, 1),
            view_top=round(view_top, 1),
            view_bottom=round(view_bottom, 1),
            total_height=total_height,
            action=action,
        )
        self._update_finalize_button_visibility()

    def _on_vertical_arrow(self, event: tk.Event, key: Tuple[int, str]) -> None:
        """Up/Down aren't bound to "break" - Tk's default Text binding still
        moves the cursor (and keeps it visible within the box's own internal
        scroll, via the box's own .see("insert")) exactly as it always has.
        What that default binding doesn't do is keep the box's *container*
        within the canvas viewport - a box can scroll its cursor internally
        while sitting partially or fully off the top/bottom edge of the
        review window. Checked one idle tick later, after the default
        binding (which runs on the same event dispatch, just after this one)
        has already moved the cursor, so there's something real to check."""
        widget = event.widget
        self.after_idle(lambda: self._keep_cursor_in_viewport(key, widget))

    def _keep_cursor_in_viewport(self, key: Tuple[int, str], widget: tk.Text) -> None:
        """If `widget`'s cursor ("insert") ended up above/below the canvas's
        visible viewport after an Up/Down keypress, scroll just enough to
        bring it back in - aligning the *box's* top/bottom edge (not just the
        cursor's own line) with the viewport's, so the rest of the box reads
        as much as fits rather than only the cursor's line peeking into view.
        Pressing Down can only ever push the cursor past the bottom edge (and
        Up past the top), so which edge is violated already says which way
        to scroll - no separate direction argument needed.

        Box position is computed the same way the rest of this module is
        forced to (see _scroll_into_view's docstring): self._offset_of(index)
        for the row's own document-space offset, plus a winfo_rooty() delta
        for the box's offset *within* that row, which - unlike the row's own
        winfo_y() - isn't affected by _scroll_frame being repositioned on
        every reconcile, since that repositioning doesn't change a box's
        position relative to its own row."""
        index, _ = key
        if widget is not self._text_widgets.get(key):
            return  # row was torn down/rebuilt before this idle tick ran
        container = self._text_containers.get(key)
        row = self._row_frames.get(index)
        if container is None or row is None:
            return
        try:
            bbox = widget.bbox("insert")
            if bbox is None:
                return
            container_top = self._offset_of(index) + (container.winfo_rooty() - row.winfo_rooty())
            cursor_top = container_top + (widget.winfo_rooty() - container.winfo_rooty()) + bbox[1]
        except tk.TclError:
            return  # a widget along the way was destroyed mid-check
        container_bottom = container_top + container.winfo_height()
        cursor_bottom = cursor_top + bbox[3]

        canvas = self._canvas
        viewport_height = canvas.winfo_height()
        total_height = sum(self._row_heights)
        if viewport_height <= 1 or total_height <= 0:
            return
        view_top = canvas.canvasy(0)
        view_bottom = canvas.canvasy(viewport_height)

        action = "none"
        if cursor_top < view_top:
            canvas.yview_moveto(max(container_top, 0) / total_height)
            action = "scroll_up_to_box_top"
        elif cursor_bottom > view_bottom:
            canvas.yview_moveto(max(container_bottom - viewport_height, 0) / total_height)
            action = "scroll_down_to_box_bottom"

        self._log_event(
            "arrow_scroll_into_view",
            key=key,
            cursor_top=round(cursor_top, 1),
            cursor_bottom=round(cursor_bottom, 1),
            view_top=round(view_top, 1),
            view_bottom=round(view_bottom, 1),
            action=action,
        )
        if action != "none":
            self._schedule_reconcile()
            self._update_finalize_button_visibility()

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
        self._scroll_box_into_view((index, role))

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
