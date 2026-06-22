"""Review screen shown after the OCR batch completes.

A single long, scrollable window listing every approved message in order.
Each row shows the message's image (if any) on the left, paired with one
freely-editable text box on the right pre-filled with that image's OCR
text - copy/paste and arbitrary edits are allowed, nothing is parsed or
restricted. Text-only messages are shown for context with no editable box.
Nothing is written to disk until the Finalize button at the bottom is
clicked, which writes every message's final lines in one pass.

Images are loaded/decoded lazily, only for rows within (or near) the
visible viewport, and unloaded again once scrolled away. Decoding every
attached image up front made initial build and scrolling laggy once there
were more than a handful of messages.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional

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
        self._text_widgets: List[Optional[tk.Text]] = []
        self._image_slots: List[_ImageSlot] = []

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
            self._update_visible_images()

        scrollbar.configure(command=_on_scrollbar)
        canvas.configure(yscrollcommand=scrollbar.set)

        self._scroll_frame = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")

        self._scroll_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        def _on_canvas_configure(e):
            canvas.itemconfig(canvas_window, width=e.width)
            self._update_visible_images()

        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")
            self._update_visible_images()

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        # bind_all is global, so undo it when this frame goes away, otherwise
        # the next screen's scrolling would dispatch to this destroyed canvas
        self.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        for item in items:
            self._build_row(item)

        self.after_idle(self._update_visible_images)

    def _build_row(self, item: ReviewItem) -> None:
        row = ttk.Frame(self._scroll_frame, relief="groove", borderwidth=1, padding=6)
        row.pack(fill="x", pady=4, padx=4)

        if item.image_path is None:
            preview = "\n".join(item.entry.text_lines).strip() or "(no text)"
            ttk.Label(row, text=preview, wraplength=900, justify="left").pack(anchor="w")
            self._text_widgets.append(None)
            return

        left = ttk.Frame(row)
        left.pack(side="left", padx=6, fill="y")

        # Fixed-size placeholder so loading/unloading the image doesn't
        # change the row's layout (which would jump the scroll position).
        container = ttk.Frame(left, width=THUMBNAIL_SIZE[0], height=THUMBNAIL_SIZE[1])
        container.pack_propagate(False)
        container.pack()
        image_label = ttk.Label(container, text="(scroll to load image)", anchor="center")
        image_label.pack(fill="both", expand=True)

        self._image_slots.append(_ImageSlot(item=item, row=row, label=image_label))

        if item.entry.text_lines:
            message_text = "\n".join(item.entry.text_lines).strip()
            if message_text:
                ttk.Label(
                    left, text=message_text, wraplength=THUMBNAIL_SIZE[0], justify="left"
                ).pack(pady=4)

        text_widget = tk.Text(row, width=40, height=30, wrap="word")
        text_widget.insert("1.0", item.initial_text or "")
        text_widget.pack(side="left", fill="both", expand=True, padx=6)
        self._text_widgets.append(text_widget)

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

        for slot in self._image_slots:
            row_top = slot.row.winfo_y()
            row_bottom = row_top + slot.row.winfo_height()
            should_be_loaded = row_bottom >= visible_top and row_top <= visible_bottom

            if should_be_loaded and not slot.loaded:
                self._load_image(slot)
            elif not should_be_loaded and slot.loaded:
                self._unload_image(slot)

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
        edited_texts = [
            widget.get("1.0", "end-1c") if widget is not None else None
            for widget in self._text_widgets
        ]
        self._on_finalize(edited_texts)
