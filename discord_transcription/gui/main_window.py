"""Top-level application window.

Manages the setup screen (file/folder pickers, start date, cache checkbox),
runs the OCR batch on a background thread while showing a progress screen,
then shows the full review screen (every approved message, images paired
with editable OCR text) and writes everything out once Finalize is clicked,
finishing with a summary screen.

The parsing and OCR itself is pipeline.prepare_run, run on a worker thread.

Also decides when sessions are saved and resumed (what a session holds, and
how it maps back onto a re-parsed chatlog, is session.py's job). While the
review screen is up, its edits, focus and scroll position are autosaved
every config.AUTOSAVE_INTERVAL_MS, and once more when the window closes.
Sessions are kept per chatlog. When Start is clicked for a chatlog with a
saved session, _on_start offers to resume it: _resume_session re-runs the
session's saved inputs, and _show_review re-applies its edits and position.
A chatlog's session is cleared when its run is finalized, or when the user
declines to resume it.
"""

import dataclasses
import queue
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

from .. import chatlog, config, logging_config, pipeline, review_item, state
from ..pipeline import RunContext
from ..session import (
    EditsByMessage,
    MalformedSessionError,
    SavedSession,
    build_finalized_updates,
    log_edit_changes,
    match_finalized_edits,
)
from . import keyboard_nav, theme
from .progress_view import ProgressFrame
from .review_view import ReviewFrame
from .setup_view import SetupFrame

logger = logging_config.get_logger(__name__)


# How often the Tk thread checks the OCR worker's event queue.
_WORKER_POLL_MS = 50


# How many missing image names _warn_missing_images lists before summarizing
# the rest as a count.
_MISSING_IMAGES_LISTED = 10


def _warn_missing_images(missing_images: list[str]) -> None:
    """Tell the user some referenced images weren't in the image folder.

    Args:
        missing_images: The missing image names.
    """
    listed = "\n".join(missing_images[:_MISSING_IMAGES_LISTED])
    remainder = len(missing_images) - _MISSING_IMAGES_LISTED
    if remainder > 0:
        listed += f"\n...and {remainder} more"
    messagebox.showwarning(
        "Missing images",
        f"{len(missing_images)} image(s) referenced by the chatlog weren't found in "
        f"the image folder, so their OCR boxes will be empty:\n\n{listed}",
    )


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        # Stay hidden until everything (including the dark title bar) is
        # set up - see theme.enable_dark_title_bar.
        self.root.withdraw()
        self.root.title("Discord Transcription Tool")
        self.root.geometry("700x500")
        # Start maximized. The "zoomed" wm state only exists on Windows and
        # macOS; X11 Tk rejects it and exposes maximizing as the -zoomed
        # attribute instead.
        try:
            self.root.state("zoomed")
        except tk.TclError:
            self.root.attributes("-zoomed", True)
        theme.apply_dark_theme(self.root)
        keyboard_nav.bind_select_all(self.root)

        self.container = ttk.Frame(self.root, padding=12)
        self.container.pack(fill="both", expand=True)

        self.current_frame: tk.Widget | None = None

        self._review_items: list[review_item.ReviewItem] | None = None
        self._run: Optional[RunContext] = None
        self._autosave_job: Optional[str] = None
        self._resume_payload: Optional[SavedSession] = None
        # A resumed session's image folder, so that if the resume fails and
        # falls back to show_setup(), the setup screen shows that folder.
        # Cleared by the next show_setup().
        self._resume_image_folder_override: Optional[str] = None
        # The edits as of the most recent save - diagnostic only, so each
        # save can log exactly which edits changed (session.log_edit_changes).
        self._last_autosave_snapshot: EditsByMessage = {}
        # True from an autosave failure until the next successful save, so
        # the user is warned once per run of failures (see _run_autosave).
        self._autosave_failing = False

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show_setup()
        self.root.deiconify()

    def _on_close(self) -> None:
        """Window-close handler: save the review session one last time (so
        nothing since the last autosave is lost), then close. If that save
        fails, ask before closing anyway."""
        frame = getattr(self, "_review_frame", None)
        if frame is not None and frame.winfo_exists():
            logger.info(
                "window closing while review screen is open - flushing a final autosave",
                extra=logging_config.extra(
                    materialized_range=frame.get_materialized_range(),
                    focused_slot=frame.get_focused_slot(),
                    last_autosaved_count=len(self._last_autosave_snapshot),
                ),
            )
            try:
                self._snapshot_and_save(frame, tag="window_close")
            except Exception as exc:
                # Left unhandled, this would also skip root.destroy(), so the
                # window could never be closed while saving keeps failing.
                logger.exception("final save on window close failed")
                close_anyway = messagebox.askokcancel(
                    "Couldn't save",
                    f"Your latest review progress couldn't be saved:\n{exc}\n\n"
                    "Close anyway? Edits since the last successful save will be lost.",
                    icon="warning",
                )
                if not close_anyway:
                    return
            self._cancel_autosave()
        self.root.destroy()

    # -- frame management -------------------------------------------------

    def _set_frame(self, frame: tk.Widget) -> None:
        if self.current_frame is not None:
            self.current_frame.destroy()
        self.current_frame = frame
        frame.pack(fill="both", expand=True)

    # -- setup screen -------------------------------------------------------

    def show_setup(self) -> None:
        self._cancel_autosave()
        frame = SetupFrame(
            self.container, on_start=self._on_start,
            initial_image_folder=self._resume_image_folder_override,
        )
        self._resume_image_folder_override = None
        self._setup_frame = frame
        self._set_frame(frame)

    def _on_start(self) -> None:
        setup = self._setup_frame
        setup.set_error("")

        html_path = setup.get_html_path()
        image_folder = setup.get_image_folder()
        output_path = setup.get_output_path()

        if not html_path or not image_folder or not output_path:
            setup.set_error("Please select the HTML file, image folder, and output file.")
            return

        try:
            pipeline.check_output_path(Path(output_path))
        except ValueError as exc:
            setup.set_error(str(exc))
            return

        pending_session = state.load_session(html_path)
        if pending_session is not None:
            # Resuming overwrites this session with autosaves, and declining
            # clears it, so archive it first either way.
            state.archive_session_backup(html_path, pending_session)
            start_time = pending_session.get("start_time")
            if start_time is not None:
                start_date_str = datetime.fromtimestamp(
                    start_time, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S UTC")
                prompt = (
                    "A saved in-progress review session exists for this chatlog "
                    f"(start date {start_date_str}). Resume it?"
                )
            else:
                prompt = (
                    "A saved in-progress review session exists for this chatlog. Resume it?"
                )
            resume_choice = messagebox.askyesnocancel("Resume previous session", prompt)
            if resume_choice is None:
                # Cancel - leave the saved session untouched and don't start a run.
                return
            if resume_choice:
                state.add_recent_path("output_path", output_path)
                self._resume_session(html_path, pending_session, Path(output_path))
                return
            state.clear_session(html_path)

        try:
            start_time = pipeline.parse_start_date(setup.get_start_date())
        except ValueError as exc:
            setup.set_error(str(exc))
            return

        use_all_users = setup.get_use_all_users()
        approved_users_text = setup.get_approved_users_text()
        approved_author_ids: Optional[set[str]] = None
        if not use_all_users:
            try:
                approved_author_ids = pipeline.parse_approved_user_ids(approved_users_text)
            except ValueError as exc:
                setup.set_error(str(exc))
                return
            if not approved_author_ids:
                setup.set_error('Please enter at least one user, or check "all users".')
                return

        state.add_recent_path("html_path", html_path)
        state.add_recent_path("image_folder", image_folder)
        state.add_recent_path("output_path", output_path)
        for line in approved_users_text.splitlines():
            line = line.strip()
            if line:
                state.add_recent_path("approved_user", line)
        state.save_approved_users_state(approved_users_text, use_all_users)

        use_cache = setup.get_use_cache()
        logger.info(
            "starting run",
            extra=logging_config.extra(
                html_path=html_path,
                image_folder=image_folder,
                output_path=output_path,
                use_cache=use_cache,
                use_all_users=use_all_users,
            ),
        )
        self._begin_run(
            RunContext(
                html_path=Path(html_path),
                image_folder=Path(image_folder),
                output_path=Path(output_path),
                start_time=start_time,
                approved_author_ids=approved_author_ids,
                use_cache=use_cache,
            )
        )

    # -- run orchestration --------------------------------------------------

    def _begin_run(self, run: RunContext) -> None:
        """Show the progress screen and parse/OCR on a worker thread.

        Args:
            run: The run's inputs.
        """
        progress = ProgressFrame(self.container, status_text="Reading chatlog...")
        self._set_frame(progress)
        self._run = run
        # Tk isn't guaranteed to be safe to call from another thread, so the
        # worker only ever puts (callback, args, is_final) onto this queue,
        # and _poll_worker_events runs the callbacks on the Tk thread.
        events: "queue.Queue[tuple[Callable[..., None], tuple, bool]]" = queue.Queue()

        def worker() -> None:
            try:
                entries, ocr_result = pipeline.prepare_run(
                    run,
                    progress_callback=lambda frac: events.put((progress.set_progress, (frac,), False)),
                    status_callback=lambda text: events.put((progress.set_status, (text,), False)),
                )
            except pipeline.RunError as exc:
                events.put((self._on_run_error, (str(exc),), True))
                return
            except Exception as exc:  # surfaced to the user, not a crash
                logger.exception("OCR batch failed")
                events.put((self._on_run_error, (str(exc),), True))
                return

            events.put((self._on_ocr_done, (entries, ocr_result), True))

        self.root.after(_WORKER_POLL_MS, self._poll_worker_events, events)
        threading.Thread(target=worker, daemon=True).start()

    def _poll_worker_events(
        self, events: "queue.Queue[tuple[Callable[..., None], tuple, bool]]"
    ) -> None:
        """Run the callbacks the OCR worker queued, on the Tk thread.

        Reschedules itself until the worker's final callback (the run's
        result or error) has run.

        Args:
            events: The worker's queue of (callback, args, is_final).
        """
        while True:
            try:
                callback, args, is_final = events.get_nowait()
            except queue.Empty:
                break
            callback(*args)
            if is_final:
                return
        self.root.after(_WORKER_POLL_MS, self._poll_worker_events, events)

    def _on_run_error(self, message: str) -> None:
        logger.error("run failed", extra=logging_config.extra(error=message))
        messagebox.showerror("Error", message)
        self.show_setup()

    # -- session resume ------------------------------------------------------

    def _resume_session(
        self, html_path_key: str, session: dict, output_path: Optional[Path] = None
    ) -> None:
        """Re-run a saved session's inputs through the normal parse/OCR
        pipeline; _show_review then re-applies its edits, focus and scroll
        position.

        use_cache is always forced on, whatever the session was started
        with: resuming continues a review whose images were already OCR'd
        (new or changed images are still OCR'd, since the cache is per
        image). Otherwise a session whose first run forced a full re-OCR
        would repeat it on every resume.

        The output path can be replaced too (the setup screen's current
        one), since it only matters at Finalize. The other inputs decide
        which messages the saved edits belong to, so they're the session's.

        Args:
            html_path_key: The path the session was loaded under, used to
                clear it if it turns out to be malformed.
            session: What state.load_session returned.
            output_path: The output file to finalize to, or None to keep
                the session's saved one.
        """
        try:
            saved = SavedSession.from_json(session)
        except MalformedSessionError as exc:
            logger.warning(
                "malformed saved session, discarding",
                extra=logging_config.extra(error=str(exc)),
            )
            state.clear_session(html_path_key)
            return

        run = dataclasses.replace(saved.run, use_cache=True)
        if output_path is not None and output_path != run.output_path:
            logger.info(
                "resuming with the setup screen's output path",
                extra=logging_config.extra(
                    saved_output_path=str(run.output_path), output_path=str(output_path)
                ),
            )
            run = dataclasses.replace(run, output_path=output_path)

        self._resume_image_folder_override = str(saved.run.image_folder)
        self._resume_payload = saved
        self._begin_run(run)

    # -- autosave -------------------------------------------------------------

    def _cancel_autosave(self) -> None:
        if self._autosave_job is not None:
            self.root.after_cancel(self._autosave_job)
            self._autosave_job = None

    def _start_autosave(self) -> None:
        self._cancel_autosave()
        self._run_autosave()

    def _run_autosave(self) -> None:
        """Save the review session, then reschedule itself for
        config.AUTOSAVE_INTERVAL_MS later. Runs while the review screen is
        up (see _start_autosave/_cancel_autosave)."""
        try:
            frame = getattr(self, "_review_frame", None)
            if frame is not None and frame.winfo_exists():
                self._snapshot_and_save(frame, tag="autosave_tick")
        except Exception as exc:
            logger.exception("autosave failed")
            if not self._autosave_failing:
                # Once per run of failures, not on every tick.
                self._autosave_failing = True
                messagebox.showwarning(
                    "Autosave failed",
                    f"Your review progress couldn't be saved:\n{exc}\n\n"
                    "Autosave will keep retrying. Until it succeeds, edits "
                    "since the last successful save would be lost if the app "
                    "closed.",
                )
        else:
            if self._autosave_failing:
                self._autosave_failing = False
                logger.info("autosave succeeded again after failing")
        finally:
            # Rescheduled whatever happened, so one failure can't stop
            # autosave for the rest of the session.
            self._autosave_job = self.root.after(config.AUTOSAVE_INTERVAL_MS, self._run_autosave)

    def _snapshot_and_save(self, frame: ReviewFrame, tag: str) -> None:
        """Save `frame`'s current edits/focus/scroll position as this run's
        session - shared by the autosave tick, the final save on window
        close, and the save just before Finalize (`tag` says which, in the
        log of what changed since the previous save)."""
        saved = SavedSession.capture(
            self._run,
            self._review_items,
            frame.collect_edited_texts(),
            frame.get_touched_slots(),
            frame.get_focused_slot(),
            frame.get_scroll_top_fraction(),
        )
        log_edit_changes(self._last_autosave_snapshot, saved.edited_texts, tag=tag)
        self._last_autosave_snapshot = saved.edited_texts
        state.save_session(str(self._run.html_path), saved.to_json())

    def _on_ocr_done(
        self, entries: list[chatlog.MessageEntry], ocr_result: pipeline.OcrBatchResult
    ) -> None:
        run = self._run
        try:
            self._review_items = review_item.build_review_items(
                entries, ocr_result.file_info, run.image_folder
            )
        except Exception as exc:
            # Runs inside a root.after() callback: anything uncaught here
            # would only reach report_callback_exception, leaving the user
            # stuck on the progress screen.
            logger.exception("building review items failed")
            self._on_run_error(f"Unexpected error while building the review screen: {exc!r}")
            return

        if ocr_result.missing_images:
            _warn_missing_images(ocr_result.missing_images)

        logger.info("OCR done, showing review screen", extra=logging_config.extra(item_count=len(self._review_items)))
        self._show_review()

    def _show_review(self) -> None:
        resume = self._resume_payload
        self._resume_payload = None
        restored = resume.restore_onto(self._review_items) if resume is not None else None

        finalized_raw = state.load_finalized_edits(str(self._run.html_path))
        initial_finalized_texts = None
        if finalized_raw:
            initial_finalized_texts = match_finalized_edits(self._review_items, finalized_raw)

        html_path = str(self._run.html_path)
        frame = ReviewFrame(
            self.container,
            self._review_items,
            self._on_finalize_clicked,
            html_path=self._run.html_path,
            initial_saved_texts=restored.saved_texts if restored else None,
            initial_focus_slot=restored.focus_slot if restored else None,
            initial_scroll_fraction=restored.scroll_fraction if restored else None,
            initial_finalized_texts=initial_finalized_texts,
            initial_touched_slots=restored.touched_slots if restored else None,
            initial_image_column_fraction=state.load_image_column_fraction(html_path),
            on_image_column_fraction_changed=self._save_image_column_fraction,
        )
        self._set_frame(frame)
        self._review_frame = frame
        self._start_autosave()

    def _save_image_column_fraction(self, fraction: float) -> None:
        """Remember a dragged image column width for this run's chatlog.

        A failed write is only logged: losing the width is harmless.

        Args:
            fraction: The image column's share of the review area's width.
        """
        if self._run is None:
            return
        try:
            state.save_image_column_fraction(str(self._run.html_path), fraction)
        except OSError:
            logger.exception("could not save image column width")

    def _on_finalize_clicked(self, edited_texts: list[dict[str, str | None]]) -> None:
        """Confirm, save the session, then append this run to the output file.

        If writing the output fails, nothing has been written, so the review
        screen stays up (autosave still running) and the user can fix the
        problem and click Finalize again. Once the output *has* been written,
        the run counts as finalized no matter what fails afterwards: the
        saved session is cleared so it can't be resumed and finalized a
        second time (which would append everything twice), and any later
        failures are shown as warnings on the done screen.

        Args:
            edited_texts: One role->text dict per review item, from
                ReviewFrame.collect_edited_texts.
        """
        logger.info("finalize clicked")
        run = self._run
        confirmed = messagebox.askyesno(
            "Finalize",
            f"Append this run's transcript to\n{run.output_path}?\n\n"
            "This can't be undone from within the app.",
        )
        if not confirmed:
            logger.info("finalize cancelled at confirmation prompt")
            return

        # Flush the latest edits first, so a failure below loses nothing.
        frame = getattr(self, "_review_frame", None)
        if frame is not None and frame.winfo_exists():
            try:
                self._snapshot_and_save(frame, tag="pre_finalize")
            except Exception:
                logger.exception("pre-finalize session save failed; finalizing anyway")

        try:
            result = pipeline.finalize_run(
                run.output_path, run.html_path, self._review_items, edited_texts
            )
        except Exception as exc:
            logger.exception("finalize failed; output file left unchanged")
            messagebox.showerror(
                "Finalize failed",
                f"Nothing was written to the output file:\n{exc}\n\n"
                "Your edits are still here and saved. Fix the problem and "
                "click Finalize again.",
            )
            return

        warnings = list(result.warnings)

        touched_slots = (
            frame.get_touched_slots() if frame is not None and frame.winfo_exists() else set()
        )
        updates = build_finalized_updates(self._review_items, edited_texts, touched_slots)
        if updates:
            try:
                state.save_finalized_edits(str(run.html_path), updates)
            except Exception as exc:
                logger.exception("could not save finalized edits")
                warnings.append(
                    f"Could not store this run's edits for future runs ({exc})."
                )

        self._cancel_autosave()
        try:
            state.clear_session(str(run.html_path))
        except Exception as exc:
            logger.exception("could not clear saved session after finalize")
            warnings.append(
                f"Could not clear the saved session ({exc}). If you're asked to "
                "resume a session for this chatlog, choose No - it has already "
                "been written to the output file."
            )

        self._show_done(result.just_added, result.copied_to_clipboard, warnings)

    def _show_done(self, just_added: str, copied_to_clipboard: bool, warnings: list[str]) -> None:
        """Show the post-finalize summary screen.

        Args:
            just_added: The text this run appended to the output file.
            copied_to_clipboard: Whether just_added was copied to the clipboard.
            warnings: Problems from steps after the output was written.
        """
        frame = ttk.Frame(self.container)
        headline = (
            "Done! The new content has been copied to your clipboard."
            if copied_to_clipboard
            else "Done! The output file was updated."
        )
        ttk.Label(frame, text=headline, padding=12).pack()
        ttk.Label(frame, text=f"{len(just_added.splitlines())} lines added.", padding=4).pack()
        for warning in warnings:
            ttk.Label(
                frame, text=f"Warning: {warning}", foreground="orange", wraplength=700, padding=4
            ).pack()
        ttk.Button(frame, text="Start another run", command=self.show_setup).pack(pady=12)
        self._set_frame(frame)
