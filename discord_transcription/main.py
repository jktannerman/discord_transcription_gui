"""Entry point for the GUI transcription tool.

Run with the installed console script (works from any directory once
installed - see the README's "Running it" section):
    discord-transcription-gui

Or, from inside gui_transcription/ without installing:
    python -m discord_transcription.main      (Linux)
    py -3 -m discord_transcription.main       (Windows)
"""

import tkinter as tk
from types import TracebackType
from typing import Optional
from tkinter import messagebox

from . import config, logging_config, state
from .gui.main_window import App

logger = logging_config.get_logger(__name__)


def _log_tk_callback_exception(
    exc: type[BaseException], val: BaseException, tb: Optional[TracebackType]
) -> None:
    """Installed as tk.Tk.report_callback_exception below. Tk's default
    implementation only prints "Exception in Tkinter callback" + traceback
    to stderr - any exception raised inside a Tk-bound callback (a button
    command, a <Key> binding, an after()/after_idle() job, ...) reaches
    this instead of this app's own logger or any surrounding try/except,
    since mainloop() catches it internally and calls this hook rather than
    letting it propagate to the caller of mainloop().

    This was the actual reason a real, reproduced review-screen crash
    (see INVESTIGATION_shift_tab_reconcile_lockup.md) was invisible in
    app.log and only ever showed up on a console/stderr the user usually
    isn't capturing - a multi-hour log-forensics pass was needed just to
    suspect it, before the raw console output surfaced and confirmed it
    directly. Routing this through the app's own logger means any future
    uncaught Tk-callback exception - whatever its cause - is loud and
    on-record the same way every other error in this app already is,
    instead of requiring the same archaeology to rediscover."""
    logger.error(
        "uncaught exception in a Tkinter callback",
        exc_info=(exc, val, tb),
    )


def main() -> None:
    """Start the app, unless another copy of it is already running."""
    logging_config.setup_logging(level=logging_config.resolve_log_level())
    logger.info("application starting", extra=logging_config.extra(log_file=str(config.LOG_FILE)))

    # Held until this function returns, i.e. for the app's whole lifetime.
    instance_lock = state.acquire_instance_lock()
    if instance_lock is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Already running",
            "Discord Transcription Tool is already running. Two copies at once "
            "would overwrite each other's saved sessions and settings.",
        )
        root.destroy()
        return

    root = tk.Tk()
    root.report_callback_exception = _log_tk_callback_exception
    App(root)

    try:
        root.mainloop()
    finally:
        instance_lock.close()
        logger.info("application exiting")


if __name__ == "__main__":
    main()
