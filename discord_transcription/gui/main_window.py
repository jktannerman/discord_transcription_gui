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

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

from .. import chatlog, config, logging_config, pipeline, review_item, state
from . import keyboard_nav, theme
from .progress_view import ProgressFrame
from .review_view import ReviewFrame
from .setup_view import SetupFrame

logger = logging_config.get_logger(__name__)


def _match_edits_by_message_id(
    review_items: list["review_item.ReviewItem"],
    edits: dict,
    *,
    summary_event: str,
    summary_level: str,
    log_per_box_detail: bool,
) -> list[dict[str, Optional[str]]]:
    """Translate a {message_id: {role: text}} dict (a resumed session's saved
    edits, or a prior run's finalized edits) onto the freshly-parsed
    review_items' current positions, producing one role->text dict per item.
    Entries whose message_id no longer appears (the message was filtered
    out, or removed from a later re-export) are simply dropped - silently,
    since this is the expected outcome of normal chatlog growth, not an
    error. Each matched entry's roles are filtered down to the matched
    item's *current* slot_roles - covers a re-export changing how many
    images (and so how many "ocrN"/"spacer_imgN" slots) that exact message
    has, which would otherwise misalign an edit onto the wrong slot.

    `summary_event`/`summary_level` distinguish _match_saved_edits' and
    _match_finalized_edits' otherwise-identical matched/dropped-count log
    line. `log_per_box_detail`, only used by the saved-session path, logs
    each matched box at DEBUG - matched/dropped counts alone can't show
    *which* box's saved text now equals this run's freshly-computed default
    (which would mean either it was never really edited, or an edit was
    lost upstream of this point) vs. one that genuinely differs - logged
    once per resume, not per autosave tick, so the volume is bounded by
    transcript size rather than time."""
    by_id = {item.message_id: idx for idx, item in enumerate(review_items)}
    built: list[dict[str, Optional[str]]] = [{} for _ in review_items]
    matched = dropped = 0
    for message_id, edit in edits.items():
        idx = by_id.get(message_id)
        if idx is None:
            dropped += 1
            continue
        matched += 1
        item = review_items[idx]
        valid_roles = set(item.slot_roles)
        built[idx] = {role: text for role, text in edit.items() if role in valid_roles}
        if log_per_box_detail:
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
    getattr(logger, summary_level)(
        summary_event,
        extra=logging_config.extra(
            matched_count=matched, dropped_count=dropped, current_item_count=len(review_items)
        ),
    )
    return built


def _match_saved_edits(
    review_items: list["review_item.ReviewItem"], saved_texts: dict
) -> list[dict[str, Optional[str]]]:
    return _match_edits_by_message_id(
        review_items, saved_texts,
        summary_event="resumed session edits matched by message_id",
        summary_level="info",
        log_per_box_detail=True,
    )


def _match_finalized_edits(
    review_items: list["review_item.ReviewItem"], finalized: dict
) -> list[dict[str, Optional[str]]]:
    return _match_edits_by_message_id(
        review_items, finalized,
        summary_event="finalized edits matched by message_id",
        summary_level="debug",
        log_per_box_detail=False,
    )


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


def _match_touched_slots(
    review_items: list["review_item.ReviewItem"], raw_slots: Optional[list]
) -> set[tuple[int, str]]:
    """Translate a saved session's [[message_id, role], ...] touched slots
    onto current (index, role) slots, dropping any whose message or role no
    longer exists (same rules as _match_edits_by_message_id)."""
    if not isinstance(raw_slots, list):
        return set()
    by_id = {item.message_id: idx for idx, item in enumerate(review_items)}
    matched = set()
    for entry in raw_slots:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        message_id, role = entry
        idx = by_id.get(message_id)
        if idx is not None and role in review_items[idx].slot_roles:
            matched.add((idx, role))
    return matched


def _build_finalized_updates(
    review_items: list["review_item.ReviewItem"],
    edited_texts: list[dict[str, Optional[str]]],
    touched_slots: set[tuple[int, str]],
) -> dict[str, dict[str, Optional[str]]]:
    """Work out what Finalize should change in the stored finalized edits.

    Args:
        review_items: This run's items, in transcript order.
        edited_texts: One role->text dict per item (None = default/unchecked).
        touched_slots: (index, role) slots the user deliberately acted on.

    Returns:
        {message_id: {role: text_or_None}} for state.save_finalized_edits:
        an edited box stores its text; a box at its default (or unchecked)
        maps to None - remove the stored edit - *only* if the user touched
        it this session. Any other box is left out, keeping whatever is
        stored for it.
    """
    updates: dict[str, dict[str, Optional[str]]] = {}
    for idx, (item, edited) in enumerate(zip(review_items, edited_texts)):
        per_msg: dict[str, Optional[str]] = {}
        for role, text in edited.items():
            if text is not None:
                per_msg[role] = text
            elif (idx, role) in touched_slots:
                per_msg[role] = None
        if per_msg:
            updates[item.message_id] = per_msg
    return updates


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


# How often the Tk thread checks the OCR worker's event queue.
_WORKER_POLL_MS = 50


class _RunError(Exception):
    """A run failure whose message is ready to show the user as-is."""


def _prepare_run(
    run: RunContext,
    progress_callback: Callable[[float], None],
    status_callback: Callable[[str], None],
) -> tuple[list[chatlog.MessageEntry], pipeline.OcrBatchResult]:
    """Parse the chatlog, then OCR the images its kept messages reference.

    Runs on the worker thread, so it must not touch Tk directly; both
    callbacks are expected to marshal onto the Tk thread themselves.

    Args:
        run: The run's inputs.
        progress_callback: Receives the fraction of OCR work done.
        status_callback: Receives a short description of the current stage.

    Returns:
        The kept messages and the OCR results for their images.

    Raises:
        _RunError: The chatlog couldn't be read or parsed.
    """
    try:
        html_text = run.html_path.read_text(encoding="utf8")
    except OSError as exc:
        raise _RunError(f"Could not read HTML file: {exc}") from exc
    try:
        entries = chatlog.parse_message_groups(
            html_text, run.start_time, run.approved_author_ids
        )
    except ValueError as exc:
        # parse_message_groups raises a clear ValueError for a malformed
        # export (missing/unparseable timezone postamble, missing
        # per-message data-message-id).
        logger.exception("chatlog parsing failed")
        raise _RunError(str(exc)) from exc
    except Exception as exc:
        logger.exception("unexpected error while parsing chatlog")
        raise _RunError(f"Unexpected error while reading the chatlog: {exc!r}") from exc

    status_callback("Running OCR on images...")
    ocr_result = pipeline.run_ocr_batch(
        str(run.image_folder),
        pipeline.referenced_image_names(entries),
        run.use_cache,
        progress_callback=progress_callback,
    )
    return entries, ocr_result


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
        # set up, then show it all in one shot - see theme.enable_dark_title_bar's
        # docstring for why showing the window before that is set is what
        # caused the title bar to start out light.
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
                    materialized_range=frame.get_materialized_range(),
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
        progress = ProgressFrame(self.container, status_text="Reading chatlog...")
        self._set_frame(progress)

        self._run = RunContext(
            html_path=html_path,
            image_folder=image_folder,
            output_path=output_path,
            start_time=start_time,
            approved_author_ids=approved_author_ids,
            use_cache=use_cache,
        )

        run = self._run
        # Tk isn't guaranteed to be safe to call from another thread, so the
        # worker only ever puts (callback, args, is_final) onto this queue,
        # and _poll_worker_events runs the callbacks on the Tk thread.
        events: "queue.Queue[tuple[Callable[..., None], tuple, bool]]" = queue.Queue()

        def worker() -> None:
            try:
                entries, ocr_result = _prepare_run(
                    run,
                    progress_callback=lambda frac: events.put((progress.set_progress, (frac,), False)),
                    status_callback=lambda text: events.put((progress.set_status, (text,), False)),
                )
            except _RunError as exc:
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

    def _resume_session(self, html_path_key: str, session: dict) -> None:
        """Re-run the saved session's inputs through the normal OCR/parse
        pipeline - _show_review then re-applies the saved edits/focus/scroll
        position once that finishes, the same way a fresh run's review items
        are built either way. html_path_key is the exact key this session
        was loaded under (the chatlog's HTML path), used to clear the right
        chatlog's saved session if it turns out to be malformed.

        use_cache is always forced to True here, regardless of the setup
        screen's checkbox or what the session originally recorded: resuming
        continues a review whose images were already OCR'd, so re-OCR'ing
        them all is wasted work. Images that aren't cached yet, or whose
        file changed, are still OCR'd, since the cache is per image. Without
        this override, a session whose *first* run happened to force a full
        re-OCR would repeat it on every resume (session["use_cache"] is only
        ever the value the session was originally started with - see
        _snapshot_and_save)."""
        try:
            html_path = Path(session["html_path"])
            image_folder = Path(session["image_folder"])
            output_path = Path(session["output_path"])
            start_time = session["start_time"]
            raw_ids = session["approved_author_ids"]
            approved_author_ids = set(raw_ids) if raw_ids is not None else None
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
            use_cache=True,
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
            "touched_slots": sorted(
                [self._review_items[idx].message_id, role]
                for idx, role in frame.get_touched_slots()
            ),
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

        initial_saved_texts = None
        initial_focus_slot = None
        initial_scroll_fraction = None
        initial_touched_slots = None
        if resume is not None:
            initial_touched_slots = _match_touched_slots(
                self._review_items, resume.get("touched_slots")
            )
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

        finalized_raw = state.load_finalized_edits(str(self._run.html_path))
        initial_finalized_texts = None
        if finalized_raw:
            initial_finalized_texts = _match_finalized_edits(self._review_items, finalized_raw)

        frame = ReviewFrame(
            self.container,
            self._review_items,
            self._on_finalize_clicked,
            html_path=self._run.html_path,
            initial_saved_texts=initial_saved_texts,
            initial_focus_slot=initial_focus_slot,
            initial_scroll_fraction=initial_scroll_fraction,
            initial_finalized_texts=initial_finalized_texts,
            initial_touched_slots=initial_touched_slots,
        )
        self._set_frame(frame)
        self._review_frame = frame
        self._start_autosave()

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
        updates = _build_finalized_updates(self._review_items, edited_texts, touched_slots)
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
