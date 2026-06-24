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
from gui_transcription.app.gui import main_window
from gui_transcription.app.gui.main_window import App


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
