"""App._on_start's validation branches (missing fields, invalid start
date, an empty approved-users list) and its pending-session resume prompt
had no test coverage before - only the standalone _match_saved_edits/
_match_focus_slot helpers (test_main_window_resume.py) were tested, not
the orchestration that calls into them. A fake setup frame stands in for
the real SetupFrame (pure Tk widget wiring, not under test here) so these
tests exercise just _on_start's own decision logic."""
from unittest.mock import patch

import pytest
import tkinter as tk

from gui_transcription.app import config
from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.gui import main_window
from gui_transcription.app.gui.main_window import App, RunContext
from gui_transcription.app.review_item import ReviewItem

# Every test here builds a real App (and so a real Tk root) - excluded from
# the default run (see pyproject.toml's addopts) since the brief window it
# creates can flash on screen; run with `-m gui` to include it.
pytestmark = pytest.mark.gui


class _FakeSetupFrame:
    def __init__(
        self,
        html_path="chat.html",
        image_folder="images",
        output_path="out.txt",
        start_date="2024-01-01",
        use_cache=True,
        use_all_users=False,
        approved_users_text="123456789012345678 - Alice",
    ):
        self._html_path = html_path
        self._image_folder = image_folder
        self._output_path = output_path
        self._start_date = start_date
        self._use_cache = use_cache
        self._use_all_users = use_all_users
        self._approved_users_text = approved_users_text
        self.errors = []

    def get_html_path(self):
        return self._html_path

    def get_image_folder(self):
        return self._image_folder

    def get_output_path(self):
        return self._output_path

    def get_start_date(self):
        return self._start_date

    def get_use_cache(self):
        return self._use_cache

    def get_use_all_users(self):
        return self._use_all_users

    def get_approved_users_text(self):
        return self._approved_users_text

    def set_error(self, message):
        self.errors.append(message)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(config, "RECENT_PATHS_FILE", tmp_path / "recent_paths.json")
    monkeypatch.setattr(config, "APPROVED_USERS_STATE_FILE", tmp_path / "approved_users.json")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    root.withdraw()
    instance = App(root)
    yield instance
    root.destroy()


_NO_DIALOG = object()


def _start(app, setup, *, resume_answer=_NO_DIALOG):
    app._setup_frame = setup
    begin_run_calls = []
    app._begin_run = lambda *a, **kw: begin_run_calls.append((a, kw))
    resume_calls = []
    app._resume_session = lambda *a, **kw: resume_calls.append((a, kw))
    if resume_answer is _NO_DIALOG:
        app._on_start()
    else:
        with patch.object(main_window.messagebox, "askyesnocancel", return_value=resume_answer):
            app._on_start()
    return begin_run_calls, resume_calls


@pytest.mark.parametrize("missing_field", ["_html_path", "_image_folder", "_output_path"])
def test_missing_required_field_sets_error_and_does_not_start(app, missing_field):
    setup = _FakeSetupFrame()
    setattr(setup, missing_field, "")

    begin_run_calls, resume_calls = _start(app, setup)

    assert begin_run_calls == []
    assert resume_calls == []
    assert setup.errors[-1] == "Please select the HTML file, image folder, and output file."


def test_invalid_start_date_sets_error_and_does_not_start(app):
    setup = _FakeSetupFrame(start_date="not-a-date")

    begin_run_calls, resume_calls = _start(app, setup)

    assert begin_run_calls == []
    assert resume_calls == []
    assert "not-a-date" in setup.errors[-1]


def test_empty_approved_users_without_all_users_checked_sets_error(app):
    setup = _FakeSetupFrame(use_all_users=False, approved_users_text="")

    begin_run_calls, _ = _start(app, setup)

    assert begin_run_calls == []
    assert setup.errors[-1] == 'Please enter at least one user, or check "all users".'


def test_all_users_checked_starts_with_no_author_filter(app):
    setup = _FakeSetupFrame(use_all_users=True, approved_users_text="")

    begin_run_calls, _ = _start(app, setup)

    assert len(begin_run_calls) == 1
    args, kwargs = begin_run_calls[0]
    approved_author_ids = args[4]
    assert approved_author_ids is None


def test_valid_approved_users_text_starts_with_parsed_author_ids(app):
    setup = _FakeSetupFrame(
        use_all_users=False,
        approved_users_text="123456789012345678 - Alice\n987654321098765432 - Bob",
    )

    begin_run_calls, _ = _start(app, setup)

    assert len(begin_run_calls) == 1
    args, kwargs = begin_run_calls[0]
    approved_author_ids = args[4]
    assert approved_author_ids == {"123456789012345678", "987654321098765432"}


def test_pending_session_declined_clears_it_and_starts_a_fresh_run(app):
    setup = _FakeSetupFrame()
    with patch.object(main_window.state, "load_session", return_value={"html_path": "chat.html"}), \
         patch.object(main_window.state, "clear_session") as clear_session:
        begin_run_calls, resume_calls = _start(app, setup, resume_answer=False)

    assert resume_calls == []
    assert len(begin_run_calls) == 1
    clear_session.assert_called_once_with("chat.html")


def test_pending_session_cancelled_leaves_it_untouched_and_does_not_start(app):
    setup = _FakeSetupFrame()
    with patch.object(main_window.state, "load_session", return_value={"html_path": "chat.html"}), \
         patch.object(main_window.state, "clear_session") as clear_session:
        begin_run_calls, resume_calls = _start(app, setup, resume_answer=None)

    assert begin_run_calls == []
    assert resume_calls == []
    clear_session.assert_not_called()


def test_pending_session_accepted_resumes_instead_of_starting_fresh(app):
    setup = _FakeSetupFrame()
    with patch.object(main_window.state, "load_session", return_value={"html_path": "chat.html"}):
        begin_run_calls, resume_calls = _start(app, setup, resume_answer=True)

    assert begin_run_calls == []
    assert len(resume_calls) == 1


def test_pending_session_accepted_archives_it_before_resuming(app):
    """Accepting resume means the pending session is about to be
    progressively overwritten by the new session's own autosave ticks -
    clear_session is never called on this path (there's something to
    resume), so archiving has to happen here instead, or the previous
    session's final state would just be lost the moment the new one
    autosaves over it."""
    setup = _FakeSetupFrame()
    pending = {"html_path": "chat.html", "edited_texts": {"1": "old edit"}}
    with patch.object(main_window.state, "load_session", return_value=pending), \
         patch.object(main_window.state, "archive_session_backup") as archive_session_backup:
        _start(app, setup, resume_answer=True)

    archive_session_backup.assert_called_once_with("chat.html", pending)


def test_pending_session_declined_archives_it_before_clearing(app):
    setup = _FakeSetupFrame()
    pending = {"html_path": "chat.html", "edited_texts": {"1": "old edit"}}
    with patch.object(main_window.state, "load_session", return_value=pending), \
         patch.object(main_window.state, "clear_session"), \
         patch.object(main_window.state, "archive_session_backup") as archive_session_backup:
        _start(app, setup, resume_answer=False)

    archive_session_backup.assert_called_once_with("chat.html", pending)


# -- _resume_session tests ---------------------------------------------------

def _resume_session_directly(app, session):
    """Call app._resume_session with _begin_run mocked out, returning the
    args/kwargs it was called with (or None if it wasn't called)."""
    begin_run_calls = []
    app._begin_run = lambda *a, **kw: begin_run_calls.append((a, kw))
    app._resume_session("chat.html", session)
    return begin_run_calls


def test_resume_session_forces_use_cache_true_even_when_session_saved_false(app, tmp_path):
    """Resuming must never redo OCR - newly added images are handled by a
    later fresh run, not a resume. A session's saved use_cache reflects
    whatever the *original* run was started with (see _snapshot_and_save),
    which could be False (e.g. that first run deliberately forced fresh OCR)
    - that stale value must not leak into every future resume of the same
    session and keep re-running OCR forever."""
    session = {
        "html_path": str(tmp_path / "chat.html"),
        "image_folder": str(tmp_path / "images"),
        "output_path": str(tmp_path / "out.txt"),
        "start_time": 0,
        "approved_author_ids": None,
        "use_cache": False,
    }

    begin_run_calls = _resume_session_directly(app, session)

    assert len(begin_run_calls) == 1
    _, kwargs = begin_run_calls[0]
    assert kwargs["use_cache"] is True


def test_resume_session_forces_use_cache_true_when_saved_use_cache_missing(app, tmp_path):
    """use_cache is no longer read from the saved session at all, so its
    absence (e.g. an older session file predating this field) must not be
    treated as a malformed session - resuming still forces True."""
    session = {
        "html_path": str(tmp_path / "chat.html"),
        "image_folder": str(tmp_path / "images"),
        "output_path": str(tmp_path / "out.txt"),
        "start_time": 0,
        "approved_author_ids": None,
    }

    begin_run_calls = _resume_session_directly(app, session)

    assert len(begin_run_calls) == 1
    _, kwargs = begin_run_calls[0]
    assert kwargs["use_cache"] is True


def test_resume_session_discards_session_missing_other_required_fields(app, tmp_path):
    """A session missing a field _resume_session still actually needs (not
    use_cache, which is no longer read) is discarded via clear_session,
    same as before this change."""
    session = {
        "html_path": str(tmp_path / "chat.html"),
        # image_folder missing
        "output_path": str(tmp_path / "out.txt"),
        "start_time": 0,
        "approved_author_ids": None,
    }

    with patch.object(main_window.state, "clear_session") as clear_session:
        begin_run_calls = _resume_session_directly(app, session)

    assert begin_run_calls == []
    clear_session.assert_called_once_with("chat.html")


# -- _on_finalize_clicked tests -----------------------------------------------

def _make_text_item(message_id: str) -> ReviewItem:
    return ReviewItem(
        entry=MessageEntry(message_id=message_id, text_lines=["hello"], image_names=[]),
        image_paths=[],
        initial_message_text="hello",
        initial_ocr_texts=[],
        initial_spacer_texts={"spacer_end": r"\n\n\n\n"},
    )


def _set_up_run(app, tmp_path, items):
    html = tmp_path / "chat.html"
    html.write_text("<html></html>", encoding="utf8")
    app._run = RunContext(
        html_path=html,
        image_folder=tmp_path / "images",
        output_path=tmp_path / "out.txt",
        start_time=0,
        approved_author_ids=None,
        use_cache=True,
    )
    app._review_items = items


def _finalize(
    app, tmp_path, items, edited_texts, *, confirm=True, finalize_result=None,
    finalize_error=None, clear_session_error=None,
):
    """Set up app._run/_review_items and call _on_finalize_clicked with the
    confirmation prompt answered `confirm`, pipeline.finalize_run mocked to
    return `finalize_result` (or raise `finalize_error`), and state calls
    mocked out. Returns a dict of what each mock was called with."""
    _set_up_run(app, tmp_path, items)
    if finalize_result is None:
        finalize_result = main_window.pipeline.FinalizeResult(
            just_added="added", copied_to_clipboard=True
        )

    calls = {"save_finalized": [], "show_setup": [], "show_done": []}
    app.show_setup = lambda: calls["show_setup"].append(True)
    real_show_done = app._show_done
    app._show_done = lambda *a: (calls["show_done"].append(a), real_show_done(*a))
    with patch.object(main_window.messagebox, "askyesno", return_value=confirm), \
         patch.object(main_window.messagebox, "showerror") as showerror, \
         patch.object(main_window.pipeline, "finalize_run",
                      return_value=finalize_result, side_effect=finalize_error) as finalize_run, \
         patch.object(main_window.state, "save_finalized_edits",
                      side_effect=lambda *a: calls["save_finalized"].append(a)), \
         patch.object(main_window.state, "clear_session",
                      side_effect=clear_session_error) as clear_session:
        app._on_finalize_clicked(edited_texts)

    calls["finalize_run"] = finalize_run.call_args_list
    calls["clear_session"] = clear_session.call_args_list
    calls["showerror"] = showerror.call_args_list
    return calls


def test_on_finalize_clicked_saves_non_none_edits_as_finalized(app, tmp_path, monkeypatch):
    """_on_finalize_clicked must call state.save_finalized_edits with only
    the non-None edited values, keyed by each item's message_id."""
    items = [_make_text_item("msg1"), _make_text_item("msg2")]
    edited_texts = [
        {"message": "edited text", "spacer_end": None},   # non-None message, None spacer
        {"message": None, "spacer_end": r"\n\n\n\n"},     # None message, non-None spacer
    ]

    calls = _finalize(app, tmp_path, items, edited_texts)

    assert len(calls["save_finalized"]) == 1
    _, by_id = calls["save_finalized"][0]
    assert by_id == {
        "msg1": {"message": "edited text"},   # spacer_end None -> dropped
        "msg2": {"spacer_end": r"\n\n\n\n"},  # message None -> dropped
    }
    assert len(calls["clear_session"]) == 1
    assert len(calls["show_done"]) == 1


def test_on_finalize_clicked_does_nothing_when_confirmation_declined(app, tmp_path):
    items = [_make_text_item("msg1")]

    calls = _finalize(app, tmp_path, items, [{"message": "edited"}], confirm=False)

    assert calls["finalize_run"] == []
    assert calls["save_finalized"] == []
    assert calls["clear_session"] == []
    assert calls["show_done"] == []


def test_on_finalize_clicked_stays_on_review_screen_when_output_write_fails(app, tmp_path):
    """If finalize_run raises, nothing was written - so the saved session
    must be kept (so nothing is lost) and the app must stay put rather than
    tearing down the review screen, letting the user retry."""
    items = [_make_text_item("msg1")]
    edited_texts = [{"message": "edited text", "spacer_end": None}]

    calls = _finalize(
        app, tmp_path, items, edited_texts, finalize_error=OSError("disk full")
    )

    assert calls["save_finalized"] == []
    assert calls["clear_session"] == []
    assert calls["show_setup"] == []
    assert calls["show_done"] == []
    assert len(calls["showerror"]) == 1
    assert "disk full" in calls["showerror"][0].args[1]


def test_on_finalize_clicked_still_finishes_when_clearing_session_fails(app, tmp_path):
    """Once the output is written the run is finalized - a later failure
    must surface as a warning on the done screen, not an error that leaves
    a resumable (and so re-finalizable) session behind silently."""
    items = [_make_text_item("msg1")]

    calls = _finalize(
        app, tmp_path, items, [{"message": None, "spacer_end": None}],
        clear_session_error=OSError("locked"),
    )

    assert len(calls["show_done"]) == 1
    _, _, warnings = calls["show_done"][0]
    assert len(warnings) == 1
    assert "locked" in warnings[0]
    assert calls["showerror"] == []


def test_on_finalize_clicked_passes_pipeline_warnings_to_done_screen(app, tmp_path):
    items = [_make_text_item("msg1")]
    result = main_window.pipeline.FinalizeResult(
        just_added="added", copied_to_clipboard=False,
        warnings=["Could not copy the new text to the clipboard (no xclip)."],
    )

    calls = _finalize(
        app, tmp_path, items, [{"message": None, "spacer_end": None}], finalize_result=result
    )

    just_added, copied, warnings = calls["show_done"][0]
    assert copied is False
    assert warnings == result.warnings
