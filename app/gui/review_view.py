"""Review screen shown after the OCR batch completes.

An infinite-scroll, paginated listing of every approved message in order.
Each row shows the message's image (if any) on the left, paired with one
freely-editable text box on the right pre-filled with that image's OCR
text - copy/paste and arbitrary edits are allowed, nothing is parsed or
restricted. Text-only messages are shown for context with no editable box.
Nothing is written to disk until the Finalize button at the bottom is
clicked, which writes every message's final lines in one pass.

Only a bounded window of PAGE_SIZE rows is ever materialized as widgets at
once (see _advance_forward/_advance_backward), rather than every message in
the transcript - building hundreds of full-size image rows and 30-line Text
widgets up front is what made the screen laggy, and that cost scaled with
transcript length regardless of any per-row image lazy-loading. As the user
scrolls near either edge of the current window, the next/previous half-page
is loaded in and the opposite half-page is torn down, so the window slides
along the transcript instead of growing. Edits made in a row are preserved
in self._saved_texts before that row is torn down, and restored if the row
is rebuilt later. The currently focused text box keeps focus across a
transition if it's still in the new window; otherwise focus is simply lost,
same as scrolling a focused widget off-screen.

Images are additionally loaded/decoded lazily within the materialized
window, only for rows within (or near) the visible viewport, and unloaded
again once scrolled away.

The load/unload pass (and the page-edge check) is debounced (see
DEBOUNCE_MS): fast scrolling fires many wheel/scrollbar events in quick
succession, and running the pass synchronously on every single one blocked
the Tk event loop with back-to-back image decodes, which both caused lag and
produced "ghost" partial images (a widget's image= being swapped again
before Tk finished painting the previous swap). Debouncing collapses a burst
of events into a single pass once scrolling actually pauses.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional

from PIL import Image, ImageTk

from .. import logging_config
from ..pipeline import ReviewItem

logger = logging_config.get_logger(__name__)

# Bounding box for the image preview - roughly two-thirds of a 1200px-wide
# review window, per the project owner's request that images be large
# enough to actually read while transcribing.
THUMBNAIL_SIZE = (760, 950)

# How many extra viewport-heights worth of rows to keep loaded above and
# below the visible area, so scrolling a little doesn't trigger a reload
# and neighboring messages are visible for spacing context.
SCROLL_BUFFER_VIEWPORTS = 1

# How long to wait, after the most recent scroll event, before actually
# loading/unloading images. Keeps a fast multi-event scroll burst from
# triggering a decode on every single tick.
DEBOUNCE_MS = 80

# Rows kept materialized at once, and how many of them get replaced per
# page transition. Rows are heavy (a full-size image placeholder plus a
# 30-line Text widget), so this is sized by widget count, not pixel height -
# each row is roughly a viewport-height tall on its own, so a 12-row window
# already covers a large scroll buffer on either side of the visible area.
PAGE_SIZE = 12
PAGE_STEP = PAGE_SIZE // 2

# Fraction of the canvas's scrollregion the visible viewport's far edge has
# to cross before the next/previous half-page is loaded in.
BOTTOM_TRIGGER_FRACTION = 0.7
TOP_TRIGGER_FRACTION = 0.3


class _ImageSlot:
    """Tracks one image row's load state for lazy loading/unloading."""

    __slots__ = ("item", "row", "label", "loaded", "photo")

    def __init__(self, item: ReviewItem, row: tk.Widget, label: tk.Widget):
        self.item = item
        self.row = row
        self.label = label
        self.loaded = False
        self.photo: Optional[ImageTk.PhotoImage] = None


class ReviewFrame(ttk.Frame):
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
        # Only items in [_window_start, _window_end) currently have widgets.
        self._window_start = 0
        self._window_end = 0
        self._row_frames: Dict[int, tk.Widget] = {}
        self._text_widgets: Dict[int, tk.Text] = {}
        self._image_slots: Dict[int, _ImageSlot] = {}
        # Text captured from a row's widget just before it's torn down, so
        # edits survive a row being paged out and back in. None means
        # "never edited/visited" - fall back to item.initial_text.
        self._saved_texts: List[Optional[str]] = [None] * len(items)
        self._update_job: Optional[str] = None

        # Pack the fixed-size widgets (button row, scrollbar) before the
        # expanding canvas - packing the expanding widget first starves the
        # others of space and squashes the scrollbar into a sliver.
        button_row = ttk.Frame(self)
        button_row.pack(side="bottom", fill="x", pady=8)
        ttk.Button(
            button_row, text="Finalize and write to file", command=self._on_finalize_clicked
        ).pack(pady=6)

        scrollbar = ttk.Scrollbar(self, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        self._canvas = canvas

        def _on_scrollbar(*args):
            canvas.yview(*args)
            self._schedule_update_visible_images()

        scrollbar.configure(command=_on_scrollbar)
        canvas.configure(yscrollcommand=scrollbar.set)

        self._scroll_frame = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")

        self._scroll_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        def _on_canvas_configure(e):
            canvas.itemconfig(canvas_window, width=e.width)
            self._schedule_update_visible_images()

        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")
            self._schedule_update_visible_images()

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        # bind_all is global, so undo it when this frame goes away, otherwise
        # the next screen's scrolling would dispatch to this destroyed canvas
        def _on_destroy(e):
            canvas.unbind_all("<MouseWheel>")
            if self._update_job is not None:
                self.after_cancel(self._update_job)
                self._update_job = None

        self.bind("<Destroy>", _on_destroy)

        self._window_end = min(PAGE_SIZE, len(items))
        for idx in range(self._window_start, self._window_end):
            self._build_row(idx)

        self.after_idle(self._update_visible_images)

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
            ttk.Label(row, text=preview, wraplength=900, justify="left").pack(anchor="w")
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

        self._image_slots[index] = _ImageSlot(item=item, row=row, label=image_label)

        if item.entry.text_lines:
            message_text = "\n".join(item.entry.text_lines).strip()
            if message_text:
                ttk.Label(
                    left, text=message_text, wraplength=THUMBNAIL_SIZE[0], justify="left"
                ).pack(pady=4)

        text_widget = tk.Text(row, width=40, height=30, wrap="word")
        saved = self._saved_texts[index]
        text_widget.insert("1.0", saved if saved is not None else (item.initial_text or ""))
        text_widget.pack(side="left", fill="both", expand=True, padx=6)
        self._text_widgets[index] = text_widget
        return row

    def _destroy_row(self, index: int) -> None:
        """Tear down the row widget(s) for items[index], saving any edited
        text first so it can be restored if the row is paged back in."""
        row = self._row_frames.pop(index, None)
        if row is None:
            return
        text_widget = self._text_widgets.pop(index, None)
        if text_widget is not None:
            self._saved_texts[index] = text_widget.get("1.0", "end-1c")
        self._image_slots.pop(index, None)
        row.destroy()

    def _focused_text_index(self) -> Optional[int]:
        focused = self.focus_get()
        if focused is None:
            return None
        for idx, widget in self._text_widgets.items():
            if widget is focused:
                return idx
        return None

    def _restore_focus(self, index: Optional[int]) -> None:
        if index is None:
            return
        widget = self._text_widgets.get(index)
        if widget is not None:
            widget.focus_set()

    def _advance_forward(self) -> None:
        """Page the window forward by PAGE_STEP items: drop rows leaving the
        top, build rows entering at the bottom."""
        total = len(self._items)
        old_start, old_end = self._window_start, self._window_end
        if old_end >= total:
            return
        new_start = min(old_start + PAGE_STEP, total)
        new_end = min(new_start + PAGE_SIZE, total)
        if new_start == old_start and new_end == old_end:
            return

        focused_index = self._focused_text_index()
        for idx in range(old_start, min(new_start, old_end)):
            self._destroy_row(idx)
        for idx in range(old_end, new_end):
            self._build_row(idx)
        self._window_start, self._window_end = new_start, new_end
        self._restore_focus(focused_index)
        self._schedule_update_visible_images()
        logger.debug(
            "advanced review page forward",
            extra=logging_config.extra(window_start=new_start, window_end=new_end),
        )

    def _advance_backward(self) -> None:
        """Page the window backward by PAGE_STEP items: drop rows leaving the
        bottom, build rows entering at the top."""
        old_start, old_end = self._window_start, self._window_end
        if old_start <= 0:
            return
        new_end = max(old_end - PAGE_STEP, 0)
        new_start = max(new_end - PAGE_SIZE, 0)
        if new_start == old_start and new_end == old_end:
            return

        focused_index = self._focused_text_index()
        for idx in range(new_end, old_end):
            self._destroy_row(idx)
        if new_start < old_start:
            anchor = self._row_frames[old_start]
            for idx in range(old_start - 1, new_start - 1, -1):
                anchor = self._build_row(idx, before=anchor)
        self._window_start, self._window_end = new_start, new_end
        self._restore_focus(focused_index)
        self._schedule_update_visible_images()
        logger.debug(
            "advanced review page backward",
            extra=logging_config.extra(window_start=new_start, window_end=new_end),
        )

    def _maybe_advance_page(self) -> None:
        top_frac, bottom_frac = self._canvas.yview()
        if bottom_frac > BOTTOM_TRIGGER_FRACTION and self._window_end < len(self._items):
            self._advance_forward()
        elif top_frac < TOP_TRIGGER_FRACTION and self._window_start > 0:
            self._advance_backward()

    def _schedule_update_visible_images(self) -> None:
        """Coalesce a burst of scroll events into a single load/unload pass,
        run shortly after the most recent event rather than on every one."""
        if self._update_job is not None:
            self.after_cancel(self._update_job)
        self._update_job = self.after(DEBOUNCE_MS, self._run_scheduled_update)

    def _run_scheduled_update(self) -> None:
        self._update_job = None
        self._update_visible_images()

    def _update_visible_images(self) -> None:
        """Load images for rows within the (buffered) visible viewport and
        unload images for rows outside it."""
        canvas = self._canvas
        canvas.update_idletasks()
        viewport_height = canvas.winfo_height()
        if viewport_height <= 1:
            return

        buffer = viewport_height * SCROLL_BUFFER_VIEWPORTS
        visible_top = canvas.canvasy(0) - buffer
        visible_bottom = canvas.canvasy(viewport_height) + buffer

        for slot in self._image_slots.values():
            row_top = slot.row.winfo_y()
            row_bottom = row_top + slot.row.winfo_height()
            should_be_loaded = row_bottom >= visible_top and row_top <= visible_bottom

            if should_be_loaded and not slot.loaded:
                self._load_image(slot)
            elif not should_be_loaded and slot.loaded:
                self._unload_image(slot)

        self._maybe_advance_page()

    def _load_image(self, slot: _ImageSlot) -> None:
        try:
            image = Image.open(slot.item.image_path)
            image.thumbnail(THUMBNAIL_SIZE)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            logger.warning(
                "could not load image preview",
                extra=logging_config.extra(image_path=str(slot.item.image_path)),
            )
            slot.label.config(image="", text=f"(could not preview {slot.item.image_path.name})")
            slot.loaded = True  # don't keep retrying a permanently-broken image every scroll
            return

        slot.photo = photo
        slot.label.config(image=photo, text="")
        slot.loaded = True

    def _unload_image(self, slot: _ImageSlot) -> None:
        slot.label.config(image="", text="(scroll to load image)")
        slot.photo = None  # drop the reference so Tk/PIL can free the memory
        slot.loaded = False

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
