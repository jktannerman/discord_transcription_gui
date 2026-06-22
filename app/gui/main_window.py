"""Top-level application window.

Manages the setup screen (file/folder pickers, start date, cache checkbox),
runs the OCR batch on a background thread while showing a progress screen,
then shows the full review screen (every approved message, images paired
with editable OCR text) and writes everything out once Finalize is clicked,
finishing with a summary screen.

Also owns session persistence: while the review screen is up, the current
edits/focus/scroll position are autosaved every
config.AUTOSAVE_INTERVAL_MS (see _start_autosave/_run_autosave) so closing
the app mid-review doesn't lose progress. On the next launch, App.__init__
checks for a saved session and offers to resume it (_offer_resume),
rebuilding the same run from its saved inputs and re-applying the saved
edits/position once OCR/parsing finish (_resume_session/_show_review). The
saved session is cleared once a run is actually finalized.
"""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from .. import chatlog, config, logging_config, pipeline, state
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

        approved_users_state = state.read_approved_users_state()
        if approved_users_state is None:
            self._initial_approved_users_text = "\n".join(config.DEFAULT_APPROVED_USERS)
            self._use_all_users = tk.BooleanVar(value=False)
        else:
            self._initial_approved_users_text = approved_users_state["text"]
            self._use_all_users = tk.BooleanVar(value=approved_users_state["use_all_users"])
        self._known_user_pick = tk.StringVar()

        self._review_items: list[pipeline.ReviewItem] | None = None
        self._autosave_job: Optional[str] = None
        self._resume_payload: Optional[dict] = None

        pending_session = state.load_session()

        self.show_setup()
        self.root.deiconify()

        if pending_session is not None:
            self.root.after(100, self._offer_resume, pending_session)

    # -- frame management -------------------------------------------------

    def _set_frame(self, frame: tk.Widget) -> None:
        if self.current_frame is not None:
            self.current_frame.destroy()
        self.current_frame = frame
        frame.pack(fill="both", expand=True)

    # -- setup screen -------------------------------------------------------

    def show_setup(self) -> None:
        self._cancel_autosave()
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

        ttk.Checkbutton(
            frame, text="Transcribe messages from all users", variable=self._use_all_users,
            command=self._update_approved_users_enabled,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=4)

        approved_frame = ttk.Frame(frame)
        approved_frame.grid(row=6, column=0, columnspan=3, sticky="w", pady=4)

        ttk.Label(
            approved_frame, text='Approved users (one per line, e.g. "123456789 - Alice"):'
        ).pack(anchor="w")

        text_container = ttk.Frame(approved_frame)
        text_container.pack(anchor="w")
        self._approved_users_text = tk.Text(
            text_container, width=60, height=5, wrap="none", undo=True,
            font=(theme.TEXT_FONT_FAMILY, theme.TEXT_FONT_SIZE - 2),
            bg=theme.DARK_TEXT_BG, fg=theme.DARK_FG, insertbackground=theme.DARK_INSERT,
            selectbackground=theme.DARK_ACCENT, selectforeground="white",
            highlightthickness=1, highlightbackground=theme.DARK_BG_ALT,
            highlightcolor=theme.DARK_FOCUS_HIGHLIGHT,
        )
        self._approved_users_text.insert("1.0", self._initial_approved_users_text)
        self._approved_users_text.pack(side="left")
        approved_scroll = ttk.Scrollbar(
            text_container, orient="vertical", command=self._approved_users_text.yview
        )
        approved_scroll.pack(side="left", fill="y")
        self._approved_users_text.configure(yscrollcommand=approved_scroll.set)

        known_users_frame = ttk.Frame(approved_frame)
        known_users_frame.pack(anchor="w", pady=4)
        ttk.Label(known_users_frame, text="Known users:").pack(side="left")
        self._known_user_combo = ttk.Combobox(
            known_users_frame, textvariable=self._known_user_pick, width=40,
            values=state.load_recent_paths("approved_user"),
        )
        self._known_user_combo.pack(side="left", padx=4)
        self._add_known_user_button = ttk.Button(
            known_users_frame, text="Add", command=self._add_known_user
        )
        self._add_known_user_button.pack(side="left")

        self._update_approved_users_enabled()

        self._error_label = ttk.Label(frame, text="", foreground="red")
        self._error_label.grid(row=7, column=0, columnspan=3, sticky="w", pady=4)

        ttk.Button(frame, text="Start", command=self._on_start).grid(row=8, column=1, pady=12)

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

    def _add_known_user(self) -> None:
        value = self._known_user_pick.get().strip()
        if not value:
            return
        self._approved_users_text.insert("end", value + "\n")

    def _update_approved_users_enabled(self) -> None:
        """The approved-users list and "known users" picker are irrelevant
        once "all users" is checked, so grey them out rather than leaving
        them interactive but ignored."""
        if self._use_all_users.get():
            self._approved_users_text.configure(state="disabled")
            self._known_user_combo.state(["disabled"])
            self._add_known_user_button.state(["disabled"])
        else:
            self._approved_users_text.configure(state="normal")
            self._known_user_combo.state(["!disabled"])
            self._add_known_user_button.state(["!disabled"])

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

        use_all_users = self._use_all_users.get()
        approved_users_text = self._approved_users_text.get("1.0", "end-1c")
        approved_author_ids: Optional[set[str]] = None
        if not use_all_users:
            try:
                approved_author_ids = pipeline.parse_approved_user_ids(approved_users_text)
            except ValueError as exc:
                self._error_label.config(text=str(exc))
                return
            if not approved_author_ids:
                self._error_label.config(
                    text='Please enter at least one user, or check "all users".'
                )
                return

        state.add_recent_path("html_path", html_path)
        state.add_recent_path("image_folder", image_folder)
        state.add_recent_path("output_path", output_path)
        for line in approved_users_text.splitlines():
            line = line.strip()
            if line:
                state.add_recent_path("approved_user", line)
        state.save_approved_users_state(approved_users_text, use_all_users)

        logger.info(
            "starting run",
            extra=logging_config.extra(
                html_path=html_path,
                image_folder=image_folder,
                output_path=output_path,
                use_cache=self._use_cache.get(),
                use_all_users=use_all_users,
            ),
        )
        self._begin_run(
            Path(html_path), Path(image_folder), Path(output_path), start_time, approved_author_ids
        )

    # -- run orchestration --------------------------------------------------

    def _begin_run(
        self,
        html_path: Path,
        image_folder: Path,
        output_path: Path,
        start_time: int,
        approved_author_ids: Optional[set[str]],
        use_cache: Optional[bool] = None,
    ) -> None:
        progress = ProgressFrame(self.container, status_text="Running OCR on images...")
        self._set_frame(progress)

        if use_cache is None:
            use_cache = self._use_cache.get()

        self._image_folder_for_run = image_folder
        self._start_time_for_run = start_time
        self._approved_author_ids_for_run = approved_author_ids
        self._use_cache_for_run = use_cache

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

            self.root.after(
                0, self._on_ocr_done, html_path, output_path, start_time, approved_author_ids, file_info
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_run_error(self, message: str) -> None:
        logger.error("run failed", extra=logging_config.extra(error=message))
        messagebox.showerror("Error", message)
        self.show_setup()

    # -- session resume ------------------------------------------------------

    def _offer_resume(self, session: dict) -> None:
        """Called shortly after launch if state.load_session() found a
        saved in-progress session. Discards it outright if declined, since
        there's nothing useful to do with a stale "no" - the user would
        just be asked again next launch otherwise."""
        if not messagebox.askyesno(
            "Resume previous session",
            "An in-progress review session was found. Resume it?",
        ):
            state.clear_session()
            return
        self._resume_session(session)

    def _resume_session(self, session: dict) -> None:
        """Re-run the saved session's inputs through the normal OCR/parse
        pipeline (use_cache forced from the saved value, so resuming
        doesn't necessarily redo OCR) - _show_review then re-applies the
        saved edits/focus/scroll position once that finishes, the same way
        a fresh run's review items are built either way."""
        try:
            html_path = Path(session["html_path"])
            image_folder = Path(session["image_folder"])
            output_path = Path(session["output_path"])
            start_time = session["start_time"]
            raw_ids = session["approved_author_ids"]
            approved_author_ids = set(raw_ids) if raw_ids is not None else None
            use_cache = session["use_cache"]
        except KeyError as exc:
            logger.warning(
                "malformed saved session, discarding",
                extra=logging_config.extra(error=str(exc)),
            )
            state.clear_session()
            return

        self._image_folder.set(str(image_folder))
        self._resume_payload = session
        self._begin_run(
            html_path, image_folder, output_path, start_time, approved_author_ids,
            use_cache=use_cache,
        )

    # -- autosave -------------------------------------------------------------

    def _cancel_autosave(self) -> None:
        if self._autosave_job is not None:
            self.root.after_cancel(self._autosave_job)
            self._autosave_job = None

    def _start_autosave(self) -> None:
        self._cancel_autosave()
        self._run_autosave()

    def _run_autosave(self) -> None:
        """Snapshot the review screen's current edits/focus/scroll position
        to disk, then reschedule itself - runs continuously while the
        review screen is up (see _start_autosave/_cancel_autosave), every
        config.AUTOSAVE_INTERVAL_MS, so closing the app at any point during
        review leaves a resumable session behind."""
        frame = getattr(self, "_review_frame", None)
        if frame is not None and frame.winfo_exists():
            session = {
                "html_path": str(self._html_path_for_run),
                "image_folder": str(self._image_folder_for_run),
                "output_path": str(self._output_path_for_run),
                "start_time": self._start_time_for_run,
                "approved_author_ids": (
                    sorted(self._approved_author_ids_for_run)
                    if self._approved_author_ids_for_run is not None
                    else None
                ),
                "use_cache": self._use_cache_for_run,
                "edited_texts": frame.collect_edited_texts(),
                "focus_index": frame.get_focused_index(),
                "scroll_fraction": frame.get_scroll_top_fraction(),
            }
            state.save_session(session)
        self._autosave_job = self.root.after(config.AUTOSAVE_INTERVAL_MS, self._run_autosave)

    def _on_ocr_done(
        self,
        html_path: Path,
        output_path: Path,
        start_time: int,
        approved_author_ids: Optional[set[str]],
        file_info: dict,
    ) -> None:
        try:
            html_text = html_path.read_text(encoding="utf8")
        except OSError as exc:
            self._on_run_error(f"Could not read HTML file: {exc}")
            return

        entries = chatlog.parse_message_groups(html_text, start_time, approved_author_ids)
        self._review_items = pipeline.build_review_items(
            entries, file_info, Path(self._image_folder.get())
        )
        self._html_path_for_run = html_path
        self._output_path_for_run = output_path
        logger.info("OCR done, showing review screen", extra=logging_config.extra(item_count=len(self._review_items)))
        self._show_review()

    def _show_review(self) -> None:
        resume = self._resume_payload
        self._resume_payload = None

        initial_saved_texts = None
        initial_focus_index = None
        initial_scroll_fraction = None
        if resume is not None:
            saved_texts = resume.get("edited_texts")
            if isinstance(saved_texts, list) and len(saved_texts) == len(self._review_items):
                initial_saved_texts = saved_texts
                initial_focus_index = resume.get("focus_index")
                initial_scroll_fraction = resume.get("scroll_fraction")
            else:
                logger.warning(
                    "saved session item count mismatch, discarding saved edits",
                    extra=logging_config.extra(current_item_count=len(self._review_items)),
                )
                messagebox.showwarning(
                    "Resume",
                    "The chatlog appears to have changed since the saved session - "
                    "starting the review fresh instead of restoring saved edits.",
                )

        frame = ReviewFrame(
            self.container,
            self._review_items,
            self._on_finalize_clicked,
            initial_saved_texts=initial_saved_texts,
            initial_focus_index=initial_focus_index,
            initial_scroll_fraction=initial_scroll_fraction,
        )
        self._set_frame(frame)
        self._review_frame = frame
        self._start_autosave()

    def _on_finalize_clicked(self, edited_texts: list[str | None]) -> None:
        logger.info("finalize clicked")
        try:
            pipeline.write_all_items(self._output_path_for_run, self._review_items, edited_texts)
            just_added = pipeline.finalize_run(self._output_path_for_run, self._html_path_for_run)
        except Exception as exc:
            logger.exception("finalize failed")
            self._on_run_error(f"Failed to write output: {exc}")
            return

        self._cancel_autosave()
        state.clear_session()

        frame = ttk.Frame(self.container)
        ttk.Label(frame, text="Done! The new content has been copied to your clipboard.", padding=12).pack()
        ttk.Label(frame, text=f"{len(just_added.splitlines())} lines added.", padding=4).pack()
        ttk.Button(frame, text="Start another run", command=self.show_setup).pack(pady=12)
        self._set_frame(frame)
