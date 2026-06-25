"""Top-level application window.

Manages the setup screen (file/folder pickers, start date, cache checkbox),
runs the OCR batch on a background thread while showing a progress screen,
then shows the full review screen (every approved message, images paired
with editable OCR text) and writes everything out once Finalize is clicked,
finishing with a summary screen.

Also owns session persistence: while the review screen is up, the current
edits/focus/scroll position are autosaved every
config.AUTOSAVE_INTERVAL_MS (see _start_autosave/_run_autosave) so closing
the app mid-review doesn't lose progress. Sessions are saved per HTML
chatlog file (see state.save_session/load_session/clear_session), not as
one global slot, so two different chatlogs can each be partially
transcribed and resumed independently - one isn't evicted by starting the
other. Rather than a launch-time global prompt, the check happens in
_on_start once a specific HTML file has been chosen: if a saved session
exists for that exact file, _on_start offers to resume it
(_resume_session), rebuilding the same run from its saved inputs and
re-applying the saved edits/position once OCR/parsing finish
(_show_review). That chatlog's saved session is cleared once its run is
actually finalized, or if the user declines to resume it.
"""

import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

from .. import chatlog, config, logging_config, pipeline, review_item, state
from . import theme
from .progress_view import ProgressFrame
from .review_view import ReviewFrame
from .setup_view import SetupFrame

logger = logging_config.get_logger(__name__)


def _match_saved_edits(
    review_items: list["review_item.ReviewItem"], saved_texts: dict
) -> list[dict[str, Optional[str]]]:
    """Translate a session's message_id-keyed saved edits onto the
    freshly-parsed review_items' current positions. Saved entries whose
    message_id no longer appears (the message was filtered out, or removed
    from a later re-export) are simply dropped - silently, since this is
    the expected outcome of normal chatlog growth, not an error.

    A saved edit's roles are filtered down to the matched item's *current*
    slot_roles - covers a re-export changing how many images (and so how
    many "ocrN"/"spacer_imgN" slots) that exact message has, which would
    otherwise misalign a saved edit onto the wrong slot."""
    by_id = {item.message_id: idx for idx, item in enumerate(review_items)}
    built: list[dict[str, Optional[str]]] = [{} for _ in review_items]
    matched = dropped = 0
    for message_id, edit in saved_texts.items():
        idx = by_id.get(message_id)
        if idx is None:
            dropped += 1
            continue
        matched += 1
        item = review_items[idx]
        valid_roles = set(item.slot_roles)
        built[idx] = {role: text for role, text in edit.items() if role in valid_roles}
        # Per-box detail at DEBUG (matched/dropped counts alone can't show
        # *which* box's saved text now equals this run's freshly-computed
        # default - which would mean either it was never really edited, or
        # an edit was lost upstream of this point - vs. one that genuinely
        # differs) - logged once per resume, not per autosave tick, so the
        # volume is bounded by transcript size rather than time.
        for role, text in built[idx].items():
            if text is None or role.startswith("spacer"):
                continue
            default_text = item.initial_text_for_role(role)
            logger.debug(
                "resumed box matched",
                extra=logging_config.extra(
                    message_id=message_id,
                    role=role,
                    saved=logging_config.text_fingerprint(text),
                    current_default=logging_config.text_fingerprint(default_text),
                    equals_current_default=(text == default_text),
                ),
            )
    logger.info(
        "resumed session edits matched by message_id",
        extra=logging_config.extra(
            matched_count=matched, dropped_count=dropped, current_item_count=len(review_items)
        ),
    )
    return built


def _match_focus_slot(
    review_items: list["review_item.ReviewItem"], focus_slot: Optional[list]
) -> Optional[tuple[int, str]]:
    """Translate a saved (message_id, role) focus slot onto its current
    index, or None if that message no longer appears (falls back to the
    saved scroll fraction / document top, same as any other unresolvable
    focus slot)."""
    if focus_slot is None:
        return None
    message_id, role = focus_slot
    by_id = {item.message_id: idx for idx, item in enumerate(review_items)}
    idx = by_id.get(message_id)
    return (idx, role) if idx is not None else None


@dataclass
class RunContext:
    """The inputs a single run (OCR -> review -> finalize) was started with.

    Replaces what used to be six separate ``self._*_for_run`` attributes set
    piecemeal across _begin_run/_on_ocr_done - autosave's session dict
    (_run_autosave) needs exactly this same group of fields together, so
    keeping them as one object removes that duplication and the "is this
    attr set yet" ambiguity of attributes that don't exist until partway
    through the App's life.
    """

    html_path: Path
    image_folder: Path
    output_path: Path
    start_time: int
    approved_author_ids: Optional[set[str]]
    use_cache: bool


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

        self._review_items: list[review_item.ReviewItem] | None = None
        self._run: Optional[RunContext] = None
        self._autosave_job: Optional[str] = None
        self._resume_payload: Optional[dict] = None
        # Set just before a resumed session's OCR/parse begins, so a failed
        # resume that falls back to show_setup() displays the session's
        # actual image folder rather than whatever's otherwise most-recent
        # on disk - consumed (reset to None) the next time show_setup() runs.
        self._resume_image_folder_override: Optional[str] = None
        # {message_id: {role: text}} as of the most recent autosave tick -
        # diagnostic only (see _diff_autosave), not used for anything the
        # app actually relies on. Lets each autosave log exactly which
        # boxes' persisted edit changed since the previous tick, rather
        # than just a running item_count that can't show *which* edit
        # appeared, changed, or vanished.
        self._last_autosave_snapshot: dict[str, dict[str, Optional[str]]] = {}

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show_setup()
        self.root.deiconify()

    def _on_close(self) -> None:
        """Logs a final snapshot of whatever's currently in memory, then
        flushes it to disk via one last _snapshot_and_save() call before the
        window actually closes - closing used to just leave whatever the
        last *periodic* tick happened to catch as the on-disk state, with
        up to AUTOSAVE_INTERVAL_MS worth of edits/scrolling never making it
        to disk at all if the window closed in between."""
        frame = getattr(self, "_review_frame", None)
        if frame is not None and frame.winfo_exists():
            logger.info(
                "window closing while review screen is open - flushing a final autosave",
                extra=logging_config.extra(
                    materialized_range=frame._materialized_range,
                    focused_slot=frame.get_focused_slot(),
                    last_autosaved_count=len(self._last_autosave_snapshot),
                ),
            )
            self._snapshot_and_save(frame, tag="window_close")
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

        pending_session = state.load_session(html_path)
        if pending_session is not None:
            # Either branch below ends this pending session's life as the
            # live, in-progress one for html_path - accepting moves it into
            # a new session that will progressively overwrite it via
            # autosave, declining clears it outright (which separately
            # archives it too - see state.clear_session) - so archive it
            # here first, covering the accepted case clear_session never
            # runs for.
            state.archive_session_backup(html_path, pending_session)
            if messagebox.askyesno(
                "Resume previous session",
                "A saved in-progress review session exists for this chatlog. Resume it?",
            ):
                self._resume_session(html_path, pending_session)
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
            Path(html_path), Path(image_folder), Path(output_path), start_time, approved_author_ids,
            use_cache=use_cache,
        )

    # -- run orchestration --------------------------------------------------

    def _begin_run(
        self,
        html_path: Path,
        image_folder: Path,
        output_path: Path,
        start_time: int,
        approved_author_ids: Optional[set[str]],
        use_cache: bool,
    ) -> None:
        progress = ProgressFrame(self.container, status_text="Running OCR on images...")
        self._set_frame(progress)

        self._run = RunContext(
            html_path=html_path,
            image_folder=image_folder,
            output_path=output_path,
            start_time=start_time,
            approved_author_ids=approved_author_ids,
            use_cache=use_cache,
        )

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

            self.root.after(0, self._on_ocr_done, file_info)

        threading.Thread(target=worker, daemon=True).start()

    def _on_run_error(self, message: str) -> None:
        logger.error("run failed", extra=logging_config.extra(error=message))
        messagebox.showerror("Error", message)
        self.show_setup()

    # -- session resume ------------------------------------------------------

    def _resume_session(self, html_path_key: str, session: dict) -> None:
        """Re-run the saved session's inputs through the normal OCR/parse
        pipeline (use_cache forced from the saved value, so resuming
        doesn't necessarily redo OCR) - _show_review then re-applies the
        saved edits/focus/scroll position once that finishes, the same way
        a fresh run's review items are built either way. html_path_key is
        the exact key this session was loaded under (the chatlog's HTML
        path), used to clear the right chatlog's saved session if it turns
        out to be malformed."""
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
            state.clear_session(html_path_key)
            return

        self._resume_image_folder_override = str(image_folder)
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
            self._snapshot_and_save(frame, tag="autosave_tick")
        self._autosave_job = self.root.after(config.AUTOSAVE_INTERVAL_MS, self._run_autosave)

    def _snapshot_and_save(self, frame: ReviewFrame, tag: str) -> None:
        """Snapshot `frame`'s current edits/focus/scroll position and write
        it to disk - the actual save logic shared by the periodic
        _run_autosave tick and _on_close's one-off final flush, with neither
        caller's own scheduling concerns (rescheduling the next tick vs.
        tearing the window down right after) folded in here. `tag`
        distinguishes a periodic tick's _diff_autosave log from _on_close's,
        since both call this."""
        run = self._run
        focused_slot = frame.get_focused_slot()
        focus_message_id = None
        if focused_slot is not None:
            idx, role = focused_slot
            focus_message_id = [self._review_items[idx].message_id, role]

        edited_texts_by_id = {}
        for idx, edited in enumerate(frame.collect_edited_texts()):
            if any(text is not None for text in edited.values()):
                edited_texts_by_id[self._review_items[idx].message_id] = edited

        self._diff_autosave(self._last_autosave_snapshot, edited_texts_by_id, tag=tag)
        self._last_autosave_snapshot = edited_texts_by_id

        session = {
            "html_path": str(run.html_path),
            "image_folder": str(run.image_folder),
            "output_path": str(run.output_path),
            "start_time": run.start_time,
            "approved_author_ids": (
                sorted(run.approved_author_ids)
                if run.approved_author_ids is not None
                else None
            ),
            "use_cache": run.use_cache,
            "edited_texts": edited_texts_by_id,
            "focus_slot": focus_message_id,
            "scroll_fraction": frame.get_scroll_top_fraction(),
        }
        state.save_session(str(run.html_path), session)

    def _diff_autosave(
        self, previous: dict[str, dict[str, Optional[str]]], current: dict[str, dict[str, Optional[str]]], tag: str
    ) -> None:
        """Log exactly which (message_id, role) edits appeared, disappeared,
        or changed value between the previous autosave snapshot and this
        one - a plain item_count (the only thing logged here before) can't
        show *which* box changed or distinguish "a new edit was made" from
        "an existing edit silently reverted". Diagnostic only - never
        changes what gets saved, just narrates it. `tag` distinguishes a
        periodic tick from the one-off snapshot taken in _on_close, since
        both call this."""
        for message_id in current.keys() - previous.keys():
            for role, text in current[message_id].items():
                logger.info(
                    "autosave diff: new edit",
                    extra=logging_config.extra(
                        tag=tag, message_id=message_id, role=role,
                        **logging_config.text_fingerprint(text),
                    ),
                )
        for message_id in previous.keys() - current.keys():
            logger.info(
                "autosave diff: edit entry removed entirely",
                extra=logging_config.extra(
                    tag=tag, message_id=message_id, roles=sorted(previous[message_id].keys()),
                ),
            )
        for message_id in current.keys() & previous.keys():
            old_edit = previous[message_id]
            new_edit = current[message_id]
            for role in old_edit.keys() | new_edit.keys():
                old_text = old_edit.get(role)
                new_text = new_edit.get(role)
                if old_text != new_text:
                    logger.info(
                        "autosave diff: edit changed",
                        extra=logging_config.extra(
                            tag=tag, message_id=message_id, role=role,
                            old=logging_config.text_fingerprint(old_text),
                            new=logging_config.text_fingerprint(new_text),
                        ),
                    )

    def _on_ocr_done(self, file_info: dict) -> None:
        run = self._run
        try:
            html_text = run.html_path.read_text(encoding="utf8")
            entries = chatlog.parse_message_groups(html_text, run.start_time, run.approved_author_ids)
            self._review_items = review_item.build_review_items(entries, file_info, run.image_folder)
        except OSError as exc:
            self._on_run_error(f"Could not read HTML file: {exc}")
            return
        except ValueError as exc:
            # chatlog.parse_message_groups raises a clear ValueError for a
            # malformed export (missing/unparseable postamble timezone,
            # missing per-message data-message-id) - this runs inside a
            # root.after() callback, so without catching it here it would
            # only ever reach Tk's default report_callback_exception (a
            # console traceback, never the app's own logger or an error
            # dialog), leaving the user stuck on the OCR progress screen
            # with no indication anything went wrong.
            logger.exception("chatlog parsing failed")
            self._on_run_error(str(exc))
            return

        logger.info("OCR done, showing review screen", extra=logging_config.extra(item_count=len(self._review_items)))
        self._show_review()

    def _show_review(self) -> None:
        resume = self._resume_payload
        self._resume_payload = None

        initial_saved_texts = None
        initial_focus_slot = None
        initial_scroll_fraction = None
        if resume is not None:
            saved_texts = resume.get("edited_texts")
            if not isinstance(saved_texts, dict):
                logger.warning(
                    "saved session edited_texts has unexpected shape, discarding",
                    extra=logging_config.extra(saved_texts_type=type(saved_texts).__name__),
                )
            else:
                initial_saved_texts = _match_saved_edits(self._review_items, saved_texts)
                initial_focus_slot = _match_focus_slot(self._review_items, resume.get("focus_slot"))
                initial_scroll_fraction = resume.get("scroll_fraction")

        frame = ReviewFrame(
            self.container,
            self._review_items,
            self._on_finalize_clicked,
            initial_saved_texts=initial_saved_texts,
            initial_focus_slot=initial_focus_slot,
            initial_scroll_fraction=initial_scroll_fraction,
        )
        self._set_frame(frame)
        self._review_frame = frame
        self._start_autosave()

    def _on_finalize_clicked(self, edited_texts: list[dict[str, str | None]]) -> None:
        logger.info("finalize clicked")
        run = self._run
        try:
            pipeline.write_all_items(run.output_path, self._review_items, edited_texts)
            just_added = pipeline.finalize_run(run.output_path, run.html_path)
        except Exception as exc:
            logger.exception("finalize failed")
            self._on_run_error(f"Failed to write output: {exc}")
            return

        self._cancel_autosave()
        state.clear_session(str(run.html_path))

        frame = ttk.Frame(self.container)
        ttk.Label(frame, text="Done! The new content has been copied to your clipboard.", padding=12).pack()
        ttk.Label(frame, text=f"{len(just_added.splitlines())} lines added.", padding=4).pack()
        ttk.Button(frame, text="Start another run", command=self.show_setup).pack(pady=12)
        self._set_frame(frame)
