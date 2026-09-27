"""Autosave and window-close saving when writing the session fails.

A failed save must neither stop autosave for the rest of the session nor
make the window impossible to close. App is built without Tk here: its
root and review frame are mocks, so these run without a display."""

from unittest.mock import MagicMock, patch

from discord_transcription import config
from discord_transcription.gui import main_window
from discord_transcription.gui.main_window import App


def _app() -> App:
    app = App.__new__(App)
    app.root = MagicMock()
    app.root.after.return_value = "after#1"
    app._autosave_job = None
    app._autosave_failing = False
    app._last_autosave_snapshot = {}
    app._review_frame = MagicMock()
    app._review_frame.winfo_exists.return_value = True
    return app


def test_autosave_keeps_rescheduling_after_a_failed_save():
    app = _app()
    with patch.object(App, "_snapshot_and_save", side_effect=OSError("disk full")), \
            patch.object(main_window.messagebox, "showwarning"):
        app._run_autosave()

    app.root.after.assert_called_once_with(config.AUTOSAVE_INTERVAL_MS, app._run_autosave)
    assert app._autosave_job == "after#1"


def test_autosave_failure_is_shown_once_per_run_of_failures():
    app = _app()
    save = MagicMock(side_effect=OSError("disk full"))
    with patch.object(App, "_snapshot_and_save", save), \
            patch.object(main_window.messagebox, "showwarning") as warn:
        app._run_autosave()
        app._run_autosave()
        assert warn.call_count == 1

        save.side_effect = None
        app._run_autosave()
        assert app._autosave_failing is False

        save.side_effect = OSError("disk full again")
        app._run_autosave()
        assert warn.call_count == 2


def test_successful_autosave_shows_nothing():
    app = _app()
    with patch.object(App, "_snapshot_and_save") as save, \
            patch.object(main_window.messagebox, "showwarning") as warn:
        app._run_autosave()

    save.assert_called_once()
    warn.assert_not_called()
    assert app.root.after.call_count == 1


def test_close_still_closes_after_a_failed_final_save_when_confirmed():
    app = _app()
    with patch.object(App, "_snapshot_and_save", side_effect=OSError("disk full")), \
            patch.object(main_window.messagebox, "askokcancel", return_value=True):
        app._on_close()

    app.root.destroy.assert_called_once()


def test_close_is_cancelled_after_a_failed_final_save_when_declined():
    app = _app()
    with patch.object(App, "_snapshot_and_save", side_effect=OSError("disk full")), \
            patch.object(main_window.messagebox, "askokcancel", return_value=False):
        app._on_close()

    app.root.destroy.assert_not_called()
