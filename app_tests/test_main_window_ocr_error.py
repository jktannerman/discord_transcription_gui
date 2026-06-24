"""_on_ocr_done runs inside a root.after() callback, so an exception it
doesn't catch itself never reaches the app's own error handling - it would
only surface via Tk's default report_callback_exception (a console
traceback), leaving the user stuck on the OCR progress screen with no
error dialog. chatlog.parse_message_groups documents raising a clear
ValueError for a malformed export (missing/unparseable timezone postamble,
missing per-message id) - this covers that _on_ocr_done actually surfaces
that error through the normal _on_run_error path instead of letting it
escape uncaught."""
from unittest.mock import patch

import pytest
import tkinter as tk

from gui_transcription.app.gui import main_window
from gui_transcription.app.gui.main_window import App, RunContext

# Every test here builds a real App (and so a real Tk root) - excluded from
# the default run (see pyproject.toml's addopts) since the brief window it
# creates can flash on screen; run with `-m gui` to include it.
pytestmark = pytest.mark.gui


@pytest.fixture
def app():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    root.withdraw()
    instance = App(root)
    yield instance
    root.destroy()


def _run_context(tmp_path) -> RunContext:
    html_path = tmp_path / "chat.html"
    html_path.write_text("<html></html>", encoding="utf8")
    return RunContext(
        html_path=html_path,
        image_folder=tmp_path,
        output_path=tmp_path / "out.txt",
        start_time=0,
        approved_author_ids=None,
        use_cache=True,
    )


def test_malformed_chatlog_reports_error_instead_of_raising(app, tmp_path):
    app._run = _run_context(tmp_path)
    reported_errors = []
    app._on_run_error = reported_errors.append

    with patch.object(
        main_window.chatlog, "parse_message_groups",
        side_effect=ValueError("Could not parse export timezone from 'bogus'."),
    ):
        app._on_ocr_done({})  # must not raise

    assert reported_errors == ["Could not parse export timezone from 'bogus'."]


def test_unreadable_html_file_still_reports_error(app, tmp_path):
    run = _run_context(tmp_path)
    run.html_path.unlink()
    app._run = run
    reported_errors = []
    app._on_run_error = reported_errors.append

    app._on_ocr_done({})  # must not raise

    assert len(reported_errors) == 1
    assert "Could not read HTML file" in reported_errors[0]
