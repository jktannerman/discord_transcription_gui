"""Top-level application window.

Manages the setup screen (file/folder pickers, start date, cache checkbox),
runs the OCR batch on a background thread while showing a progress screen,
then shows the full review screen (every approved message, images paired
with editable OCR text) and writes everything out once Finalize is clicked,
finishing with a summary screen.
"""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import chatlog, logging_config, pipeline, state
from . import theme
from .progress_view import ProgressFrame
from .review_view import ReviewFrame

logger = logging_config.get_logger(__name__)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        # Stay hidden until everything (including the dark title bar) is
        # set up, then show it all in one shot - see theme.enable_dark_title_bar's
        # docstring for why showing the window before that is set is what
        # caused the title bar to start out light.
        self.root.withdraw()
        self.root.title("Discord Transcription Tool")
        self.root.geometry("700x500")
        self.root.state("zoomed")
        theme.apply_dark_theme(self.root)

        self.container = ttk.Frame(self.root, padding=12)
        self.container.pack(fill="both", expand=True)

        self.current_frame: tk.Widget | None = None

        def _most_recent(field: str) -> str:
            recent = state.load_recent_paths(field)
            return recent[0] if recent else ""

        self._html_path = tk.StringVar(value=_most_recent("html_path"))
        self._image_folder = tk.StringVar(value=_most_recent("image_folder"))
        self._output_path = tk.StringVar(value=_most_recent("output_path"))
        self._start_date = tk.StringVar(value=state.read_last_run_date() or "")
        self._use_cache = tk.BooleanVar(value=True)

        self._review_items: list[pipeline.ReviewItem] | None = None

        self.show_setup()
        self.root.deiconify()

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
        ttk.Combobox(
            frame, textvariable=self._html_path, width=58,
            values=state.load_recent_paths("html_path"),
        ).grid(row=0, column=1, pady=4)
        ttk.Button(frame, text="Browse...", command=self._pick_html).grid(row=0, column=2, padx=4)

        ttk.Label(frame, text="Image folder:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(
            frame, textvariable=self._image_folder, width=58,
            values=state.load_recent_paths("image_folder"),
        ).grid(row=1, column=1, pady=4)
        ttk.Button(frame, text="Browse...", command=self._pick_image_folder).grid(row=1, column=2, padx=4)

        ttk.Label(frame, text="Output .txt file:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(
            frame, textvariable=self._output_path, width=58,
            values=state.load_recent_paths("output_path"),
        ).grid(row=2, column=1, pady=4)
        ttk.Button(frame, text="Browse...", command=self._pick_output).grid(row=2, column=2, padx=4)

        ttk.Label(frame, text="Start date (YYYY-MM-DD):").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self._start_date, width=30).grid(row=3, column=1, sticky="w", pady=4)

        self._cache_check = ttk.Checkbutton(
            frame, text="Use cached OCR data for this image folder", variable=self._use_cache
        )
        self._cache_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=4)

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

        state.add_recent_path("html_path", html_path)
        state.add_recent_path("image_folder", image_folder)
        state.add_recent_path("output_path", output_path)

        logger.info(
            "starting run",
            extra=logging_config.extra(
                html_path=html_path,
                image_folder=image_folder,
                output_path=output_path,
                use_cache=self._use_cache.get(),
            ),
        )
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
                logger.exception("OCR batch failed")
                self.root.after(0, self._on_run_error, str(exc))
                return

            self.root.after(0, self._on_ocr_done, html_path, output_path, start_time, file_info)

        threading.Thread(target=worker, daemon=True).start()

    def _on_run_error(self, message: str) -> None:
        logger.error("run failed", extra=logging_config.extra(error=message))
        messagebox.showerror("Error", message)
        self.show_setup()

    def _on_ocr_done(self, html_path: Path, output_path: Path, start_time: int, file_info: dict) -> None:
        try:
            html_text = html_path.read_text(encoding="utf8")
        except OSError as exc:
            self._on_run_error(f"Could not read HTML file: {exc}")
            return

        entries = chatlog.parse_message_groups(html_text, start_time)
        self._review_items = pipeline.build_review_items(
            entries, file_info, Path(self._image_folder.get())
        )
        self._html_path_for_run = html_path
        self._output_path_for_run = output_path
        logger.info("OCR done, showing review screen", extra=logging_config.extra(item_count=len(self._review_items)))
        self._show_review()

    def _show_review(self) -> None:
        frame = ReviewFrame(self.container, self._review_items, self._on_finalize_clicked)
        self._set_frame(frame)

    def _on_finalize_clicked(self, edited_texts: list[str | None]) -> None:
        logger.info("finalize clicked")
        try:
            pipeline.write_all_items(self._output_path_for_run, self._review_items, edited_texts)
            just_added = pipeline.finalize_run(self._output_path_for_run, self._html_path_for_run)
        except Exception as exc:
            logger.exception("finalize failed")
            self._on_run_error(f"Failed to write output: {exc}")
            return

        frame = ttk.Frame(self.container)
        ttk.Label(frame, text="Done! The new content has been copied to your clipboard.", padding=12).pack()
        ttk.Label(frame, text=f"{len(just_added.splitlines())} lines added.", padding=4).pack()
        ttk.Button(frame, text="Start another run", command=self.show_setup).pack(pady=12)
        self._set_frame(frame)
