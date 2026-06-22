"""Review screen shown after the OCR batch completes.

A single long, scrollable window listing every approved message in order.
Each row shows the message's image (if any) on the left, paired with one
freely-editable text box on the right pre-filled with that image's OCR
text - copy/paste and arbitrary edits are allowed, nothing is parsed or
restricted. Text-only messages are shown for context with no editable box.
Nothing is written to disk until the Finalize button at the bottom is
clicked, which writes every message's final lines in one pass.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional

from PIL import Image, ImageTk

from ..pipeline import ReviewItem

THUMBNAIL_SIZE = (300, 400)


class ReviewFrame(ttk.Frame):
    def __init__(
        self,
        master: tk.Widget,
        items: List[ReviewItem],
        on_finalize: Callable[[List[Optional[str]]], None],
    ):
        super().__init__(master)
        self._items = items
        self._on_finalize = on_finalize
        self._text_widgets: List[Optional[tk.Text]] = []
        self._photos: List[ImageTk.PhotoImage] = []  # keep refs alive

        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="top", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._scroll_frame = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")

        self._scroll_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind(
            "<Configure>", lambda e: canvas.itemconfig(canvas_window, width=e.width)
        )
        canvas.bind_all(
            "<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
        )
        # bind_all is global, so undo it when this frame goes away, otherwise
        # the next screen's scrolling would dispatch to this destroyed canvas
        self.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        for item in items:
            self._build_row(item)

        button_row = ttk.Frame(self)
        button_row.pack(side="bottom", fill="x", pady=8)
        ttk.Button(
            button_row, text="Finalize and write to file", command=self._on_finalize_clicked
        ).pack(pady=6)

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

        try:
            image = Image.open(item.image_path)
            image.thumbnail(THUMBNAIL_SIZE)
            photo = ImageTk.PhotoImage(image)
            self._photos.append(photo)
            ttk.Label(left, image=photo).pack()
        except Exception:
            ttk.Label(left, text=f"(could not preview {item.image_path.name})").pack()

        if item.entry.text_lines:
            message_text = "\n".join(item.entry.text_lines).strip()
            if message_text:
                ttk.Label(left, text=message_text, wraplength=300, justify="left").pack(pady=4)

        text_widget = tk.Text(row, width=60, height=12, wrap="word")
        text_widget.insert("1.0", item.initial_text or "")
        text_widget.pack(side="left", fill="both", expand=True, padx=6)
        self._text_widgets.append(text_widget)

    def _on_finalize_clicked(self) -> None:
        edited_texts = [
            widget.get("1.0", "end-1c") if widget is not None else None
            for widget in self._text_widgets
        ]
        self._on_finalize(edited_texts)
