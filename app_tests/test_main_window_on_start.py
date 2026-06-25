"""App._on_start's validation branches (missing fields, invalid start
date, an empty approved-users list) and its pending-session resume prompt
had no test coverage before - only the standalone _match_saved_edits/
_match_focus_slot helpers (test_main_window_resume.py) were tested, not
the orchestration that calls into them. A fake setup frame stands in for
the real SetupFrame (pure Tk widget wiring, not under test here) so these
tests exercise just _on_start's own decision logic."""
from pathlib import Path
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


def _start(app, setup, *, resume_answer=None):
    app._setup_frame = setup
    begin_run_calls = []
    app._begin_run = lambda *a, **kw: begin_run_calls.append((a, kw))
    resume_calls = []
    app._resume_session = lambda *a, **kw: resume_calls.append((a, kw))
    if resume_answer is None:
        app._on_start()
    else:
        with patch.object(main_window.messagebox, "askyesno", return_value=resume_answer):
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


# -- _on_finalize_clicked tests -----------------------------------------------

def _make_text_item(message_id: str) -> ReviewItem:
    return ReviewItem(
        entry=MessageEntry(message_id=message_id, text_lines=["hello"], image_names=[]),
        image_paths=[],
        initial_message_text="hello",
        initial_ocr_texts=[],
        initial_spacer_texts={"spacer_end": r"\n\n\n\n"},
    )


def _finalize(app, tmp_path, items, edited_texts):
    """Set up app._run/_review_items and call _on_finalize_clicked with
    pipeline and most state calls mocked out, returning the args each call
    received as a list."""
    html = tmp_path / "chat.html"
    html.write_text("<html></html>", encoding="utf8")
    output = tmp_path / "out.txt"
    output.write_text("", encoding="utf8")

    app._run = RunContext(
        html_path=html,
        image_folder=tmp_path / "images",
        output_path=output,
        start_time=0,
        approved_author_ids=None,
        use_cache=True,
    )
    app._review_items = items

    save_calls = []
    with patch.object(main_window.pipeline, "write_all_items"), \
         patch.object(main_window.pipeline, "finalize_run", return_value="added"), \
         patch.object(main_window.state, "save_finalized_edits",
                      side_effect=lambda *a: save_calls.append(a)), \
         patch.object(main_window.state, "clear_session"):
        app._on_finalize_clicked(edited_texts)

    return save_calls


def test_on_finalize_clicked_saves_non_none_edits_as_finalized(app, tmp_path, monkeypatch):
    """_on_finalize_clicked must call state.save_finalized_edits with only
    the non-None edited values, keyed by each item's message_id."""
    monkeypatch.setattr(config, "FINALIZED_EDITS_FILE", tmp_path / "finalized_edits.json")

    items = [_make_text_item("msg1"), _make_text_item("msg2")]
    edited_texts = [
        {"message": "edited text", "spacer_end": None},   # non-None message, None spacer
        {"message": None, "spacer_end": r"\n\n\n\n"},     # None message, non-None spacer
    ]

    save_calls = _finalize(app, tmp_path, items, edited_texts)

    assert len(save_calls) == 1
    _, by_id = save_calls[0]
    assert by_id == {
        "msg1": {"message": "edited text"},   # spacer_end None → dropped
        "msg2": {"spacer_end": r"\n\n\n\n"},  # message None → dropped
    }


def test_on_finalize_clicked_does_not_save_finalized_edits_on_pipeline_failure(app, tmp_path, monkeypatch):
    """If write_all_items or finalize_run raises, _on_finalize_clicked must
    abort before saving finalized edits, so a failed finalize never
    overwrites a prior good run's stored edits."""
    monkeypatch.setattr(config, "FINALIZED_EDITS_FILE", tmp_path / "finalized_edits.json")

    items = [_make_text_item("msg1")]
    edited_texts = [{"message": "edited text", "spacer_end": None}]

    save_calls = []
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

    with patch.object(main_window.pipeline, "write_all_items",
                      side_effect=OSError("disk full")), \
         patch.object(main_window.state, "save_finalized_edits",
                      side_effect=lambda *a: save_calls.append(a)), \
         patch.object(main_window.messagebox, "showerror"):
        app._on_finalize_clicked(edited_texts)

    assert save_calls == []
