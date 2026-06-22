"""Entry point for the GUI transcription tool.

Run with: py -3.13 -m gui_transcription.app.main
(from the directory containing gui_transcription/).
"""

import tkinter as tk

from .gui.main_window import App


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
