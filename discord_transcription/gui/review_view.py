"""Review screen shown after the OCR batch completes.

An infinite-scroll listing of every approved message in order: an immutable
left column (the message's own text and/or its images) paired with
editable boxes on the right - a copy of the message's text, one OCR box per
image, and a one-line spacer box between/after them holding the literal
"\\n" tokens that control blank-line spacing (see
review_item.ReviewItem.slot_roles and docs/ARCHITECTURE_SPACER_SLOTS.md).
Nothing is written to disk until Finalize, which writes every message's
final lines in one pass.

ReviewFrame wires together the objects that do the work, each in its own
module:
- VirtualRows (virtual_rows.py): the canvas, and which rows are built as
  widgets at any moment (only those near the viewport);
- RowBuilder (row_building.py): builds a row's widgets, and estimates rows'
  heights before they're built;
- SlotBoxes (slot_boxes.py): every editable box's state and widgets -
  editing, the OCR checkbox, spellcheck, undo/redo;
- FocusNavigator (keyboard_nav.py): Tab/Shift-Tab, keeping the focused box
  on screen, and restoring focus across a row rebuild;
- ColumnDivider (column_divider.py): the draggable image/text divider;
- ImageContextMenu (image_context_menu.py): the right-click image menu;
- ImageLoader (image_loading.py): lazy loading of the built rows' images.

ReviewFrame itself handles scroll input (the wheel and Page Up/Down, bound
app-wide while this screen exists), the floating Finalize button, the first
layout (restoring a resumed session's focus/scroll position), and the
public methods App uses for autosave and Finalize.
"""

import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable, Iterable, Optional

from .. import logging_config
from ..review_item import ReviewItem
from .column_divider import ColumnDivider
from .image_context_menu import ImageContextMenu
from .image_loading import ImageLoader
from .keyboard_nav import FocusNavigator
from .row_building import RowBuilder, measure_text_metrics
from .slot_boxes import Slot, SlotBoxes
from .virtual_rows import SCROLL_BUFFER_VIEWPORTS, VirtualRows
from .wheel import WHEEL_EVENT_SEQUENCES, wheel_delta

logger = logging_config.get_logger(__name__)


class ReviewFrame(ttk.Frame):
    def __init__(
        self,
        master: tk.Widget,
        items: list[ReviewItem],
        on_finalize: Callable[[list[dict[str, Optional[str]]]], None],
        html_path: Path,
        initial_saved_texts: Optional[list[dict[str, Optional[str]]]] = None,
        initial_focus_slot: Optional[Slot] = None,
        initial_scroll_fraction: Optional[float] = None,
        initial_finalized_texts: Optional[list[dict[str, Optional[str]]]] = None,
        initial_touched_slots: Optional[Iterable[Slot]] = None,
        initial_image_column_fraction: Optional[float] = None,
        on_image_column_fraction_changed: Optional[Callable[[float], None]] = None,
    ) -> None:
        """
        Args:
            master: The parent widget.
            items: The review items, in transcript order.
            on_finalize: Called with collect_edited_texts() when Finalize is
                clicked.
            html_path: The run's chatlog export (for "Open Chatlog at
                Message").
            initial_saved_texts: A resumed session's per-item edits.
            initial_focus_slot: A resumed session's focused box.
            initial_scroll_fraction: A resumed session's scroll position,
                used when there's no focused box to restore.
            initial_finalized_texts: Previously-finalized per-item edits.
            initial_touched_slots: A resumed session's touched slots.
            initial_image_column_fraction: The saved image column width, as
                a share of the canvas width.
            on_image_column_fraction_changed: Called with the new fraction
                after the divider is dragged.
        """
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
        self._initial_focus_slot = initial_focus_slot
        self._initial_scroll_fraction = initial_scroll_fraction
        self._initial_position_job: Optional[str] = None

        self._rows = VirtualRows(
            self,
            fill_row=self._fill_row,
            on_row_destroying=self._on_row_destroying,
            after_reconcile=self._after_reconcile,
            on_width_change=lambda: self._divider.schedule_width_sync(),
        )
        self._images = ImageLoader()
        self._boxes = SlotBoxes(
            items,
            log_event=self._rows.log_event,
            bind_navigation=self._bind_box_navigation,
            on_focused_box_edited=lambda key: self._nav.scroll_box_into_view(key),
            initial_saved_texts=initial_saved_texts,
            initial_finalized_texts=initial_finalized_texts,
            initial_touched_slots=initial_touched_slots,
        )
        self._menu = ImageContextMenu(self, html_path, set_scroll_frozen=self._rows.set_frozen)
        self._builder = RowBuilder(
            items, self._boxes, self._images, self._menu,
            viewport=self._rows.canvas, text_metrics=measure_text_metrics(self),
        )
        # Estimated only now that the canvas exists, so the estimate caps
        # content boxes the same way the real layout does.
        self._rows.heights = self._builder.estimate_heights()

        # Created before the Finalize button, so the button stays on top.
        self._divider = ColumnDivider(
            self, self._rows, self._builder,
            focused_slot=lambda: self._nav.focused_slot(),
            box_top=lambda key: self._nav.box_top(key),
            initial_fraction=initial_image_column_fraction,
            on_fraction_changed=on_image_column_fraction_changed,
        )

        # Floating Finalize button: place()'d over the canvas and pinned to
        # the bottom of the viewport, shown only once scrolled to the end of
        # the transcript (see _update_finalize_button_visibility), so the
        # canvas gets the full height the rest of the time.
        button_row = ttk.Frame(self, relief="raised", borderwidth=1, padding=(16, 8))
        self._finalize_button = ttk.Button(
            button_row, text="Finalize and write to file", command=self._on_finalize_clicked
        )
        self._finalize_button.pack()
        self._finalize_button_row = button_row
        self._finalize_button_visible = False

        self._nav = FocusNavigator(
            self,
            [(idx, role) for idx, item in enumerate(items) for role in item.slot_roles],
            self._boxes, self._rows, self._finalize_button,
            on_view_moved=self._update_finalize_button_visibility,
        )
        # <<PrevWindow>>, not <Shift-Tab>: on X11, Shift+Tab arrives as the
        # ISO_Left_Tab key, which <Shift-Tab> never matches. <<PrevWindow>>
        # is Tk's own name for every platform's "previous" key.
        self._finalize_button.bind("<<PrevWindow>>", self._nav.on_shift_tab)
        self._finalize_button.bind(
            "<FocusIn>", lambda e: self._nav.note_focused_slot(None), add="+"
        )

        canvas = self._rows.canvas
        for sequence in WHEEL_EVENT_SEQUENCES:
            canvas.bind_all(sequence, self._on_mousewheel)
        # Global fallback for Page Up/Down, so they scroll the review window
        # even when focus is on the Finalize button (each box also binds
        # them, overriding Tk's default of scrolling within the box).
        canvas.bind_all("<Prior>", self._on_page_up)
        canvas.bind_all("<Next>", self._on_page_down)
        self.bind("<Destroy>", self._on_destroy)

        self.after_idle(self._apply_initial_position)

    def _on_destroy(self, event: tk.Event) -> None:
        """Undo the app-wide bindings (otherwise the next screen's scrolling
        would go to this destroyed canvas) and cancel pending timers."""
        canvas = self._rows.canvas
        for sequence in WHEEL_EVENT_SEQUENCES:
            canvas.unbind_all(sequence)
        canvas.unbind_all("<Prior>")
        canvas.unbind_all("<Next>")
        if self._initial_position_job is not None:
            self.after_cancel(self._initial_position_job)
            self._initial_position_job = None
        self._divider.cancel_pending()
        # Rows still built when the whole frame goes away never go through
        # destroy_row, so their pending spellcheck timers would otherwise
        # fire after their widgets are gone.
        self._boxes.cancel_all_spellchecks()

    # -- wiring the components together -------------------------------------------

    def _fill_row(self, index: int, row: tk.Widget) -> None:
        self._builder.fill_row(index, row)
        self._nav.restore_focus_after_build(index)

    def _on_row_destroying(self, index: int) -> None:
        """Save a row's boxes into their SlotStates (remembering which one
        had focus, to restore it once the row is rebuilt) and forget its
        images."""
        had_focus = self._boxes.release_row(index, self.focus_get())
        if had_focus is not None:
            self._nav.refocus_slot = had_focus
        self._images.unregister_row(index)

    def _bind_box_navigation(self, text_widget: tk.Text, key: Slot) -> None:
        """Bind the navigation and scrolling keys on a newly built box."""
        nav = self._nav
        text_widget.bind("<FocusIn>", lambda e, k=key: nav.note_focused_slot(k), add="+")
        text_widget.bind("<Tab>", nav.on_tab)
        # <<PrevWindow>> rather than <Shift-Tab> - see the Finalize button.
        text_widget.bind("<<PrevWindow>>", nav.on_shift_tab)
        # Widget-level, so it runs before - and via "break" replaces - Tk's
        # own Text class wheel binding, which would scroll the box too.
        for sequence in WHEEL_EVENT_SEQUENCES:
            text_widget.bind(sequence, self._on_mousewheel)
        text_widget.bind("<Prior>", self._on_page_up)
        text_widget.bind("<Next>", self._on_page_down)
        text_widget.bind("<Up>", lambda e, k=key: nav.on_vertical_arrow(e, k))
        text_widget.bind("<Down>", lambda e, k=key: nav.on_vertical_arrow(e, k))

    def _after_reconcile(self) -> None:
        self._update_visible_images()
        self._update_finalize_button_visibility()

    def _update_visible_images(self) -> None:
        """Load images for rows within the (buffered) viewport and unload
        the rest, using document coordinates (the built block is
        repositioned on the canvas, so widget geometry isn't usable)."""
        canvas = self._rows.canvas
        canvas.update_idletasks()
        viewport_height = canvas.winfo_height()
        self._rows.log_event("update_visible_images", viewport_height=viewport_height)
        if viewport_height <= 1:
            return

        buffer = viewport_height * SCROLL_BUFFER_VIEWPORTS
        self._images.update_visible(
            self._rows.offset_of, self._rows.heights,
            canvas.canvasy(0) - buffer, canvas.canvasy(viewport_height) + buffer,
            log_event=self._rows.log_event,
        )

    def _update_finalize_button_visibility(self) -> None:
        """Show the floating Finalize button only once scrolled to the end
        of the transcript (which includes a transcript short enough to fit
        on screen)."""
        at_bottom = self._rows.at_bottom()
        if at_bottom and not self._finalize_button_visible:
            self._finalize_button_row.place(relx=0.5, rely=1.0, anchor="s", y=-10)
            self._finalize_button_visible = True
        elif not at_bottom and self._finalize_button_visible:
            self._finalize_button_row.place_forget()
            self._finalize_button_visible = False

    # -- first layout -------------------------------------------------------------

    def _apply_initial_position(self) -> None:
        """First-layout hook, run once via after_idle: applies the saved
        column width, then restores a resumed session's focus (which scrolls
        its box into view) or, failing that, its scroll fraction, otherwise
        just reconciles at the top.

        Polls until the canvas has a real height, since scroll offsets
        computed against a height of 0 would be meaningless.
        """
        if self._rows.canvas.winfo_height() <= 1:
            self._initial_position_job = self.after(20, self._apply_initial_position)
            return
        self._initial_position_job = None
        width = self._divider.width_for_current_canvas()
        if self._rows.row_frames:
            # A debounced reconcile got here first and built rows at the
            # default width - re-lay them out.
            self._divider.set_width(width)
        elif width != self._builder.image_column_width_px:
            self._builder.image_column_width_px = width
            self._rows.heights = self._builder.estimate_heights()
        self._divider.position()
        if self._initial_focus_slot is not None and self._initial_focus_slot in self._boxes.states:
            index, role = self._initial_focus_slot
            self._rows.ensure_materialized(index)
            self._nav.focus_text_box(index, role)
            return
        if self._initial_scroll_fraction is not None and self._rows.total_height() > 0:
            # The scrollregion has to be set first, or yview_moveto is ignored.
            self._rows.set_scrollregion()
            self._rows.canvas.yview_moveto(self._initial_scroll_fraction)
        self._rows.reconcile()

    # -- scroll input --------------------------------------------------------------

    def _on_mousewheel(self, event: tk.Event) -> str:
        """One wheel/touchpad scroll event from anywhere on the screen.

        Bound app-wide, and on every box (replacing Tk's own Text
        scrolling). Hovering a box that can scroll scrolls it first; once
        it's at its limit in that direction, the whole window scrolls.

        Returns:
            "break", so no other binding also handles this event.
        """
        if self._rows.frozen:
            return "break"
        delta = wheel_delta(event)
        self._rows.log_event(
            "input_mousewheel", delta=delta, num=event.num, state=event.state,
            widget=str(event.widget),
        )
        if not delta:
            return "break"
        if isinstance(event.widget, tk.Text) and self._scroll_text_widget(event.widget, delta):
            return "break"
        # At least one unit: macOS reports small deltas that would round to 0.
        self._rows.scroll_by(int(-delta / 120) or (-1 if delta > 0 else 1), "units")
        return "break"

    @staticmethod
    def _scroll_text_widget(text_widget: tk.Text, delta: int) -> bool:
        """Scroll a box by one wheel notch, unless it's already at its limit
        in that direction (always true for a box whose content fits).

        Returns:
            Whether it scrolled.
        """
        first, last = text_widget.yview()
        at_limit = first <= 0.0 if delta > 0 else last >= 1.0
        if at_limit:
            return False
        text_widget.yview_scroll(int(-delta / 120) or (-1 if delta > 0 else 1), "units")
        return True

    def _on_page_up(self, event: Optional[tk.Event] = None) -> str:
        if self._rows.frozen:
            return "break"
        self._rows.log_event("input_page_up")
        self._rows.scroll_by(-1, "pages")
        return "break"

    def _on_page_down(self, event: Optional[tk.Event] = None) -> str:
        if self._rows.frozen:
            return "break"
        self._rows.log_event("input_page_down")
        self._rows.scroll_by(1, "pages")
        return "break"

    # -- used by App (autosave and Finalize) ----------------------------------------

    def collect_edited_texts(self) -> list[dict[str, Optional[str]]]:
        """One role->text dict per item, in transcript order; None means
        "unchanged from the default" (or an unchecked OCR box). The same
        snapshot serves Finalize and autosave."""
        return self._boxes.collect_edited_texts()

    def get_touched_slots(self) -> set[Slot]:
        """The slots the user has deliberately acted on this session - used
        by Finalize to decide which stored finalized edits may be removed,
        and saved with the session."""
        return self._boxes.touched_slots()

    def get_materialized_range(self) -> Optional[tuple[int, int]]:
        """The inclusive (first, last) item indices whose rows are built, or
        None before the first reconcile - for logging."""
        return self._rows.materialized_range

    def get_focused_slot(self) -> Optional[Slot]:
        """The box to restore focus to on resume - see
        FocusNavigator.slot_to_restore."""
        return self._nav.slot_to_restore()

    def get_scroll_top_fraction(self) -> float:
        """The scroll position as a 0-1 fraction - the fallback position
        saved for resume when no box is focused."""
        return self._rows.scroll_top_fraction()

    def _on_finalize_clicked(self) -> None:
        logger.info("finalize button clicked on review screen")
        self._on_finalize(self.collect_edited_texts())
