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

from .. import logging_config
from .layout_constants import ROW_PACK_PADY_PX

logger = logging_config.get_logger(__name__)

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

    def _key_for_widget(self, widget: tk.Text) -> Optional[Tuple[int, str]]:
        """(item_index, role) of the box currently backed by `widget`, or
        None if it isn't one of this frame's currently-materialized boxes -
        used both by _record_undo_replacement and by the logging in
        _undo_text/_redo_text below."""
        for key, candidate in self._text_widgets.items():
            if candidate is widget:
                return key
        return None

    def _undo_text(self, event: tk.Event) -> str:
        widget = event.widget
        key = self._key_for_widget(widget)
        log = self._undo_logs.get(key) if key is not None else None
        op_count_before = len(log.ops) if log is not None else None
        # Suppress the recording proxy (text_undo.py) while edit_undo()
        # runs - its own internal delete/insert side effects would
        # otherwise get captured as ordinary ops *in addition to* the
        # "replace" op _record_undo_replacement appends below, double-
        # recording this single undo and making a later replay apply it
        # twice (see UndoLog.suppress's docstring).
        if log is not None:
            log.suppress = True
        # Also suppress _on_text_modified's unconditional "any change checks
        # the box" reaction (row_building.py) - undo/redo always re-derives
        # checked/unchecked by comparing the resulting text to the OCR
        # default instead (see _resync_ocr_checkbox_after_undo), never the
        # unconditional rule that applies to ordinary typing/paste.
        if key is not None:
            self._suppress_ocr_auto_check.add(key)
        try:
            widget.edit_undo()
        except tk.TclError:
            logger.info(
                "ctrl+z pressed, nothing to undo",
                extra=logging_config.extra(key=key, widget=str(widget)),
            )
        else:
            self._record_undo_replacement(widget, "undo")
            self._resync_ocr_checkbox_after_undo(key, widget)
            logger.info(
                "ctrl+z pressed, undo applied",
                extra=logging_config.extra(
                    key=key,
                    widget=str(widget),
                    op_count_before=op_count_before,
                    op_count_after=len(log.ops) if log is not None else None,
                    **logging_config.text_fingerprint(widget.get("1.0", "end-1c")),
                ),
            )
        finally:
            if log is not None:
                log.suppress = False
            if key is not None:
                self.after_idle(lambda k=key: self._suppress_ocr_auto_check.discard(k))
        return "break"

    def _redo_text(self, event: tk.Event) -> str:
        widget = event.widget
        key = self._key_for_widget(widget)
        log = self._undo_logs.get(key) if key is not None else None
        op_count_before = len(log.ops) if log is not None else None
        if log is not None:
            log.suppress = True
        if key is not None:
            self._suppress_ocr_auto_check.add(key)
        try:
            widget.edit_redo()
        except tk.TclError:
            logger.info(
                "ctrl+shift+z pressed, nothing to redo",
                extra=logging_config.extra(key=key, widget=str(widget)),
            )
        else:
            self._record_undo_replacement(widget, "redo")
            self._resync_ocr_checkbox_after_undo(key, widget)
            logger.info(
                "ctrl+shift+z pressed, redo applied",
                extra=logging_config.extra(
                    key=key,
                    widget=str(widget),
                    op_count_before=op_count_before,
                    op_count_after=len(log.ops) if log is not None else None,
                    **logging_config.text_fingerprint(widget.get("1.0", "end-1c")),
                ),
            )
        finally:
            if log is not None:
                log.suppress = False
            if key is not None:
                self.after_idle(lambda k=key: self._suppress_ocr_auto_check.discard(k))
        return "break"

    def _resync_ocr_checkbox_after_undo(self, key: Optional[Tuple[int, str]], widget: tk.Text) -> None:
        """After a successful undo/redo on an "ocr" box, re-derive its
        checkbox's checked state by comparing the resulting text to the OCR
        default - the same rule used to seed it at build time
        (ReviewFrame.__init__) - rather than leaving it at whatever an
        earlier edit or checkbox click last set it to. Undo/redo can land
        the box back on exactly its OCR default (e.g. undoing a checkbox
        toggle's own delete/insert, or undoing the only edit a box ever
        had), and without this the checkbox would keep showing "edited"
        for text that no longer is - or vice versa for a redo that lands
        back on edited text."""
        if key is None or not key[1].startswith("ocr"):
            return
        index, role = key
        image_index = int(role[len("ocr"):])
        ocr_default = self._items[index].initial_ocr_texts[image_index]
        current_text = widget.get("1.0", "end-1c")
        checked = current_text != ocr_default
        self._checkbox_checked[key] = checked
        if checked:
            self._user_edited_texts[key] = current_text
        var = self._checkbox_vars.get(key)
        if var is not None:
            var.set(checked)

    def _record_undo_replacement(self, widget: tk.Text, source: str) -> None:
        """Record a successful Ctrl+Z/Ctrl+Shift+Z as a "replace" op -
        `widget`'s exact resulting text, captured right after edit_undo()/
        edit_redo() ran - appended directly to `widget`'s UndoLog (see
        text_undo.py), so that if this box's row is later torn down and
        rebuilt, replaying its log reproduces this undo/redo's result too -
        not just the insert/delete calls either side of it.

        This replaced an earlier design that recorded a bare "undo"/"redo"
        marker and replayed it by calling text_widget.edit_undo()/
        .edit_redo() again on the rebuilt widget - which silently produced
        the *wrong* result whenever the rebuilt widget's own native undo
        stack happened to group the replayed insert/delete calls
        differently than the live widget's stack was grouped at the moment
        of the original Ctrl+Z (e.g. because of edit_separator() calls -
        such as row_building.py's _on_ocr_checkbox_toggle makes - that
        never appear in `ops` at all, so replay can't reproduce their
        effect on grouping). Recording the *result* directly instead makes
        replay of this step deterministic and content-correct regardless
        of how replay's own native stack ends up grouped - see
        text_undo.py's module docstring and
        INVESTIGATION_undo_redo_replay_divergence.md for the full story.

        Also traces this exact moment to scroll_trace.log (via
        self._log_event, the same "box_op_recorded" event row_building.py's
        attach_undo_recording on_op hook emits for ordinary insert/delete
        ops) - unlike those, this was previously invisible in
        scroll_trace.log entirely: it's appended directly to log.ops here,
        never through attach_undo_recording's proxy, so nothing logged the
        fact that a Ctrl+Z/Ctrl+Shift+Z happened until this box's *next*
        rebuild replayed it."""
        key = self._key_for_widget(widget)
        if key is None:
            return
        log = self._undo_logs.get(key)
        if log is None:
            return
        after_text = widget.get("1.0", "end-1c")
        log.ops.append(("replace", (after_text,)))
        self._log_event(
            "box_op_recorded",
            key=key,
            op="replace",
            source=source,
            args=repr((after_text,))[:200],
            args_full_len=len(repr((after_text,))),
            total_ops=len(log.ops),
            **logging_config.text_fingerprint(after_text),
        )

    def _on_page_up(self, event: Optional[tk.Event] = None) -> str:
        if self._scroll_frozen:
            return "break"
        self._log_event("input_page_up")
        self._canvas.yview_scroll(-1, "pages")
        self._schedule_reconcile()
        return "break"

    def _on_page_down(self, event: Optional[tk.Event] = None) -> str:
        if self._scroll_frozen:
            return "break"
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
        return self._key_for_widget(focused)
        return None

    def _box_document_top(
        self, index: int, container: tk.Widget, row: tk.Widget
    ) -> float:
        """Document-space (canvas-absolute) pixel offset of `container`'s
        own top edge - shared by _scroll_box_into_view and
        _keep_cursor_in_viewport, which both need a box's real position
        rather than just its row's. self._offset_of(index) is where row
        `index`'s full pack-allocated slot starts, not where its Frame's
        own visible top edge (row.winfo_rooty(), what the winfo_rooty()
        delta below is anchored to) sits - that's ROW_PACK_PADY_PX further
        down, past the row's own leading pack pady (see that constant's
        docstring in layout_constants.py). container.winfo_rooty() -
        row.winfo_rooty() is then the box's offset *within* the row, which
        isn't affected by _scroll_frame being repositioned on every
        reconcile, since that repositioning doesn't change a box's
        position relative to its own row.

        Raises tk.TclError if a widget along the way was destroyed mid-
        check - callers catch that and bail out, same as before this was
        factored out."""
        return (
            self._offset_of(index) + ROW_PACK_PADY_PX
            + (container.winfo_rooty() - row.winfo_rooty())
        )

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
        same row-level bounds) was fully visible. Box top is computed by
        _box_document_top, shared with _keep_cursor_in_viewport - see its
        docstring for why the formula is what it is."""
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
            box_top = self._box_document_top(index, container, row)
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

        # Cross-check the document-space model (box_top/view_top, built from
        # self._row_heights) against real screen pixels, which Tk's pack
        # manager guarantees are correct regardless of what our model
        # thinks: container.winfo_rooty() - canvas.winfo_rooty() is the
        # box's real on-screen offset from the canvas's own (fixed) top
        # edge, and box_top - view_top is what the model claims that same
        # offset is. These two *must* agree if self._row_heights accurately
        # reflects the real, already-built layout above this row - any
        # nonzero model_real_discrepancy_px means self._offset_of(index) (or
        # something it sums over) has drifted from the rows' true on-screen
        # heights, which is exactly the failure mode this logging exists to
        # catch (see ARCHITECTURE.md's "Row geometry" section).
        real_offset_px = container.winfo_rooty() - canvas.winfo_rooty()
        model_offset_px = box_top - view_top
        self._log_event(
            "scroll_box_into_view",
            key=key,
            box_top=round(box_top, 1),
            box_bottom=round(box_bottom, 1),
            view_top=round(view_top, 1),
            view_bottom=round(view_bottom, 1),
            total_height=total_height,
            action=action,
            recorded_row_height=self._row_heights[index] if index < len(self._row_heights) else None,
            real_row_winfo_height=row.winfo_height(),
            real_offset_px=real_offset_px,
            model_offset_px=round(model_offset_px, 1),
            model_real_discrepancy_px=round(model_offset_px - real_offset_px, 1),
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

        Box top is computed by _box_document_top, shared with
        _scroll_box_into_view - see its docstring for why the formula is
        what it is."""
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
            container_top = self._box_document_top(index, container, row)
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
        widget = self._text_widgets.get((index, role))
        if widget is None:
            # Listed in self._slots (the static, built-once nav list) but
            # missing from self._text_widgets - normally impossible, since
            # _goto_slot always calls _ensure_materialized first, but a
            # row whose build failed (see review_view.py's _try_build_row)
            # leaves exactly this gap. Log once per attempt rather than
            # raising KeyError on every Tab/Shift-Tab press aimed at it -
            # that repeated-crash-on-every-keypress was itself part of the
            # user-visible damage in
            # INVESTIGATION_shift_tab_reconcile_lockup.md.
            logger.warning(
                "no live widget for this slot - its row's build likely "
                "failed; nothing to focus",
                extra=logging_config.extra(index=index, role=role),
            )
            return
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
