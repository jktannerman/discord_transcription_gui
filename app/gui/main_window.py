"""Top-level application window.

Manages the setup screen (file/folder pickers, start date, cache checkbox),
runs the OCR batch on a background thread while showing a progress screen,
then walks the approved messages one at a time via the correction screen,
finishing with a summary screen.
"""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import chatlog, pipeline, state
from .correction_view import CorrectionFrame
from .progress_view import ProgressFrame


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Discord Transcription Tool")
        self.root.geometry("700x500")

        self.container = ttk.Frame(self.root, padding=12)
        self.container.pack(fill="both", expand=True)

        self.current_frame: tk.Widget | None = None

        self._html_path = tk.StringVar()
        self._image_folder = tk.StringVar()
        self._output_path = tk.StringVar()
        self._start_date = tk.StringVar(value=state.read_last_run_date() or "")
        self._use_cache = tk.BooleanVar(value=False)

        self._run_controller: pipeline.RunController | None = None

        self.show_setup()

    # -- frame management -------------------------------------------------

    def _set_frame(self, frame: tk.Widget) -> None:
        if self.current_frame is not None:
            self.current_frame.destroy()
        self.current_frame = frame
        frame.pack(fill="both", expand=True)

    # -- setup screen -------------------------------------------------------

    def show_setup(self) -> None:
        frame = ttk.Frame(self.container)

        ttk.Label(frame, text="Chatlog HTML file:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self._html_path, width=60).grid(row=0, column=1, pady=4)
        ttk.Button(frame, text="Browse...", command=self._pick_html).grid(row=0, column=2, padx=4)

        ttk.Label(frame, text="Image folder:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self._image_folder, width=60).grid(row=1, column=1, pady=4)
        ttk.Button(frame, text="Browse...", command=self._pick_image_folder).grid(row=1, column=2, padx=4)

        ttk.Label(frame, text="Output .txt file:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self._output_path, width=60).grid(row=2, column=1, pady=4)
        ttk.Button(frame, text="Browse...", command=self._pick_output).grid(row=2, column=2, padx=4)

        ttk.Label(frame, text="Start date (YYYY-MM-DD):").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self._start_date, width=30).grid(row=3, column=1, sticky="w", pady=4)

        self._cache_check = ttk.Checkbutton(
            frame, text="Use cached OCR data for this image folder", variable=self._use_cache
        )
        self._cache_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=4)
        self._cache_check.state(["disabled"])
        self._image_folder.trace_add("write", self._update_cache_checkbox)

        self._error_label = ttk.Label(frame, text="", foreground="red")
        self._error_label.grid(row=5, column=0, columnspan=3, sticky="w", pady=4)

        ttk.Button(frame, text="Start", command=self._on_start).grid(row=6, column=1, pady=12)

        self._set_frame(frame)

    def _pick_html(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("HTML files", "*.html"), ("All files", "*.*")])
        if path:
            self._html_path.set(path)

    def _pick_image_folder(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self._image_folder.set(path)

    def _pick_output(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if path:
            self._output_path.set(path)

    def _update_cache_checkbox(self, *_args) -> None:
        folder = self._image_folder.get()
        has_cache = bool(folder) and state.load_cache(folder) is not None
        if has_cache:
            self._cache_check.state(["!disabled"])
        else:
            self._use_cache.set(False)
            self._cache_check.state(["disabled"])

    def _on_start(self) -> None:
        self._error_label.config(text="")

        html_path = self._html_path.get().strip()
        image_folder = self._image_folder.get().strip()
        output_path = self._output_path.get().strip()

        if not html_path or not image_folder or not output_path:
            self._error_label.config(text="Please select the HTML file, image folder, and output file.")
            return

        try:
            start_time = pipeline.parse_start_date(self._start_date.get())
        except ValueError as exc:
            self._error_label.config(text=str(exc))
            return

        self._begin_run(Path(html_path), Path(image_folder), Path(output_path), start_time)

    # -- run orchestration --------------------------------------------------

    def _begin_run(self, html_path: Path, image_folder: Path, output_path: Path, start_time: int) -> None:
        progress = ProgressFrame(self.container, status_text="Running OCR on images...")
        self._set_frame(progress)

        use_cache = self._use_cache.get()

        def worker():
            try:
                file_info = pipeline.run_ocr_batch(
                    str(image_folder),
                    start_time,
                    use_cache,
                    progress_callback=lambda frac: self.root.after(0, progress.set_progress, frac),
                )
            except Exception as exc:  # surfaced to the user, not a crash
                self.root.after(0, self._on_run_error, str(exc))
                return

            self.root.after(0, self._on_ocr_done, html_path, output_path, start_time, file_info)

        threading.Thread(target=worker, daemon=True).start()

    def _on_run_error(self, message: str) -> None:
        messagebox.showerror("Error", message)
        self.show_setup()

    def _on_ocr_done(self, html_path: Path, output_path: Path, start_time: int, file_info: dict) -> None:
        try:
            html_text = html_path.read_text(encoding="utf8")
        except OSError as exc:
            self._on_run_error(f"Could not read HTML file: {exc}")
            return

        entries = chatlog.parse_message_groups(html_text, start_time)
        self._run_controller = pipeline.RunController(entries, file_info, output_path)
        self._html_path_for_run = html_path
        self._output_path_for_run = output_path
        self._advance_run()

    def _advance_run(self) -> None:
        controller = self._run_controller
        assert controller is not None

        if controller.done:
            self._finish_run()
            return

        entry = controller.current_entry()
        paragraphs = controller.paragraphs_for_current()

        if not paragraphs:
            controller.submit_current([])
            self._advance_run()
            return

        image_path = Path(self._image_folder.get()) / entry.image_name
        para_controller = pipeline.ParagraphCorrectionController(paragraphs)

        def on_message_done():
            controller.submit_current(para_controller.lines)
            self._advance_run()

        frame = CorrectionFrame(self.container, image_path, para_controller, on_message_done)
        self._set_frame(frame)

    def _finish_run(self) -> None:
        try:
            just_added = pipeline.finalize_run(self._output_path_for_run, self._html_path_for_run)
        except Exception as exc:
            self._on_run_error(f"Run completed but finishing steps failed: {exc}")
            return

        frame = ttk.Frame(self.container)
        ttk.Label(frame, text="Done! The new content has been copied to your clipboard.", padding=12).pack()
        ttk.Label(frame, text=f"{len(just_added.splitlines())} lines added.", padding=4).pack()
        ttk.Button(frame, text="Start another run", command=self.show_setup).pack(pady=12)
        self._set_frame(frame)
