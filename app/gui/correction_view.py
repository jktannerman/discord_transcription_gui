"""One-paragraph-at-a-time OCR correction screen.

Shows the source image alongside an editable text box pre-filled with the
current OCR paragraph, with a button row mapped to the original script's
bbb/ccc/ddd/eee/fff/ggg suffix-code semantics:

- Accept       -> plain Enter / accept-as-is
- Back         -> bbb
- Retry        -> ccc (just resets the text box, no controller call needed)
- Accept all remaining -> ddd
- Skip rest of image   -> eee
- Skip this paragraph  -> fff
- Accept & stop        -> ggg
"""

import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable, Optional

import pyperclip
from PIL import Image, ImageTk

from ..pipeline import ParagraphCorrectionController

THUMBNAIL_SIZE = (350, 500)


class CorrectionFrame(ttk.Frame):
    def __init__(
        self,
        master: tk.Widget,
        image_path: Path,
        controller: ParagraphCorrectionController,
        on_done: Callable[[], None],
    ):
        super().__init__(master)
        self._controller = controller
        self._on_done = on_done
        self._image_path = image_path
        self._photo: Optional[ImageTk.PhotoImage] = None

        self._image_label = ttk.Label(self)
        self._image_label.pack(side="left", padx=8, pady=8)

        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True, padx=8, pady=8)

        self._progress_label = ttk.Label(right, text="")
        self._progress_label.pack(anchor="w")

        self._text = tk.Text(right, width=50, height=15, wrap="word")
        self._text.pack(fill="both", expand=True, pady=8)

        button_row = ttk.Frame(right)
        button_row.pack(fill="x")

        ttk.Button(button_row, text="Accept", command=self._on_accept).grid(
            row=0, column=0, padx=2, pady=2, sticky="ew"
        )
        ttk.Button(button_row, text="Back", command=self._on_back).grid(
            row=0, column=1, padx=2, pady=2, sticky="ew"
        )
        ttk.Button(button_row, text="Retry (reset text)", command=self._on_retry).grid(
            row=0, column=2, padx=2, pady=2, sticky="ew"
        )
        ttk.Button(button_row, text="Accept all remaining", command=self._on_accept_all).grid(
            row=1, column=0, padx=2, pady=2, sticky="ew"
        )
        ttk.Button(button_row, text="Skip rest of image", command=self._on_skip_rest).grid(
            row=1, column=1, padx=2, pady=2, sticky="ew"
        )
        ttk.Button(button_row, text="Skip this paragraph", command=self._on_skip_this).grid(
            row=1, column=2, padx=2, pady=2, sticky="ew"
        )
        ttk.Button(button_row, text="Accept && stop", command=self._on_accept_and_stop).grid(
            row=2, column=0, padx=2, pady=2, sticky="ew"
        )

        self._load_image()
        self._show_current_paragraph()

    def _load_image(self) -> None:
        try:
            image = Image.open(self._image_path)
            image.thumbnail(THUMBNAIL_SIZE)
            self._photo = ImageTk.PhotoImage(image)
            self._image_label.config(image=self._photo)
        except Exception:
            self._image_label.config(text=f"(could not preview {self._image_path.name})")

    def _current_text(self) -> str:
        return self._text.get("1.0", "end-1c")

    def _show_current_paragraph(self) -> None:
        if self._controller.done:
            self._on_done()
            return

        para = self._controller.current_paragraph
        index, total = self._controller.progress
        self._progress_label.config(text=f"Paragraph {index + 1} of {total}")

        self._text.delete("1.0", "end")
        self._text.insert("1.0", para)
        pyperclip.copy(para + " \\n\\n")

    def _accept_value(self) -> Optional[str]:
        """None if the text box is unedited (use default '\\n\\n' formatting),
        else the edited text verbatim."""
        para = self._controller.current_paragraph
        current = self._current_text()
        return None if current == para else current

    def _on_accept(self) -> None:
        self._controller.accept(self._accept_value())
        self._show_current_paragraph()

    def _on_back(self) -> None:
        self._controller.go_back()
        self._show_current_paragraph()

    def _on_retry(self) -> None:
        self._show_current_paragraph()

    def _on_accept_all(self) -> None:
        self._controller.accept_all_remaining(self._accept_value())
        self._show_current_paragraph()

    def _on_skip_rest(self) -> None:
        self._controller.skip_rest()
        self._show_current_paragraph()

    def _on_skip_this(self) -> None:
        self._controller.skip_this_only()
        self._show_current_paragraph()

    def _on_accept_and_stop(self) -> None:
        self._controller.accept_and_stop(self._accept_value())
        self._show_current_paragraph()
