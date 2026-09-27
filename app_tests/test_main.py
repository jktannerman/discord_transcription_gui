"""_log_tk_callback_exception - installed as tk.Tk.report_callback_exception
in main.py's main(). Tk's default implementation only prints to
stderr/console for any exception raised inside a Tk-bound callback (a
button command, a <Key> binding, an after()/after_idle() job, ...) - which
is exactly why a real, reproduced review-screen crash (see
INVESTIGATION_shift_tab_reconcile_lockup.md) never showed up in app.log and
took a full console-log-capture pass to even find. This just verifies the
hook logs through the app's own logger with the exception attached, rather
than needing a real Tk mainloop to exercise."""
import logging

from discord_transcription.main import _log_tk_callback_exception


def test_uncaught_tk_callback_exception_is_logged_with_traceback(caplog):
    try:
        raise ValueError("simulated uncaught Tk callback exception")
    except ValueError as exc:
        exc_info = (type(exc), exc, exc.__traceback__)

    with caplog.at_level(logging.ERROR):
        _log_tk_callback_exception(*exc_info)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert "uncaught exception in a Tkinter callback" in record.getMessage()
    assert record.exc_info[0] is ValueError
    assert record.exc_info[1] is exc_info[1]
