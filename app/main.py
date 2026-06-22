"""Entry point for the GUI transcription tool.

Run with: py -3.13 -m gui_transcription.app.main
(from the directory containing gui_transcription/).
"""

import tkinter as tk

from . import config, logging_config
from .gui.main_window import App

logger = logging_config.get_logger(__name__)


def main() -> None:
    logging_config.setup_logging()
    logger.info("application starting", extra=logging_config.extra(log_file=str(config.LOG_FILE)))

    root = tk.Tk()
    App(root)

    try:
        root.mainloop()
    finally:
        logger.info("application exiting")


if __name__ == "__main__":
    main()
