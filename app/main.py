"""Entry point for the GUI transcription tool.

Run with the installed console script (works from any directory, since
`pip install -e .` puts the `app` package on sys.path globally):
    discord-transcription-gui

Or, from inside gui_transcription/ without installing:
    py -3.13 -m app.main
"""

import logging
import tkinter as tk

from . import config, logging_config
from .gui.main_window import App

logger = logging_config.get_logger(__name__)


def main() -> None:
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
