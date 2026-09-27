"""Progress screen shown while the OCR batch runs on a background thread."""

import tkinter as tk
from tkinter import ttk


class ProgressFrame(ttk.Frame):
    def __init__(self, master: tk.Widget, status_text: str = "") -> None:
        super().__init__(master)

        self._status_var = tk.StringVar(value=status_text)
        ttk.Label(self, textvariable=self._status_var).pack(pady=12)

        self._progress = ttk.Progressbar(self, orient="horizontal", length=400, mode="determinate")
        self._progress.pack(pady=12)

    def set_progress(self, fraction: float) -> None:
        self._progress["value"] = max(0.0, min(1.0, fraction)) * 100

    def set_status(self, text: str) -> None:
        self._status_var.set(text)
