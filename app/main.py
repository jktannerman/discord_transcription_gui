"""Entry point for the GUI transcription tool.

Run with: py -3.13 -m gui_transcription.app.main
(from the directory containing gui_transcription/).
"""

import logging
import tkinter as tk

from . import config, logging_config
from .gui.main_window import App

logger = logging_config.get_logger(__name__)


def main() -> None:
    # DEBUG (rather than the default INFO) while diagnosing the review-screen
    # pagination loop - app/gui/review_view.py logs every scroll/page/focus
    # event at DEBUG so the exact sequence leading to a runaway loop can be
    # reconstructed from ~/.discord_transcription_gui/app.log afterward.
    logging_config.setup_logging(level=logging.DEBUG)
    logger.info("application starting", extra=logging_config.extra(log_file=str(config.LOG_FILE)))

    root = tk.Tk()
    App(root)

    try:
        root.mainloop()
    finally:
        logger.info("application exiting")


if __name__ == "__main__":
    main()
