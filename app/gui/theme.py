"""Dark theme shared by every screen in this app, modelled on the dark
theme in ``song_folder_player/gui.py`` (same palette, same ttk Style
configuration, same dark-title-bar trick on Windows)."""

import ctypes
import sys
import tkinter as tk
from tkinter import ttk

# Dark theme colors (matching song_folder_player's style)
DARK_BG = "#1e1e1e"  # Main background (darkest)
DARK_BG_ALT = "#2d2d2d"  # Frames, canvases
DARK_BG_WIDGET = "#3c3c3c"  # Buttons, entry/text fields
DARK_FG = "#d4d4d4"  # Text color
DARK_ACCENT = "#264f78"  # Selection highlight

# Colors and font for message/transcript text content and editable input
# boxes specifically, matched to multi_file_search/multi_file_search.py's
# text area and search-entry styling, which reads more clearly than the
# general UI palette above for dense body text and typed input.
DARK_TEXT_BG = "#171717"  # Darker than DARK_BG, so text boxes read as the
                          # darkest element on screen (darker than the image
                          # panel's DARK_BG_ALT background next to them)
DARK_INSERT = "white"  # Brighter text-cursor color than DARK_FG
DARK_FOCUS_HIGHLIGHT = "#569cd6"  # Border color for a focused input box

TEXT_FONT_FAMILY = "Consolas"
TEXT_FONT_SIZE = 14


def enable_dark_title_bar(window: tk.Tk) -> None:
    """Enable the dark window title bar on Windows 10/11. No-op elsewhere.

    DWM only repaints the non-client area (the title bar) lazily - setting
    the attribute alone leaves the title bar light until the next resize,
    which is what made it look like the dark theme "kicked in on resize".
    The SetWindowPos SWP_FRAMECHANGED call below forces that repaint
    immediately, without actually changing the window's size or position.
    """
    if sys.platform != "win32":
        return

    try:
        window.update()  # Ensure window is created
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())

        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (Windows 10 20H1+)
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
        )

        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        SWP_FRAMECHANGED = 0x0020
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
        )
    except (AttributeError, OSError):
        pass  # Silently fail on older Windows versions


def apply_dark_theme(root: tk.Tk) -> ttk.Style:
    """Apply the dark theme to all ttk widgets created under root, and to the
    root window itself. Raw tk widgets (Canvas, Text, Listbox, ...) aren't
    covered by ttk styles and need their colors set individually using the
    constants above."""
    style = ttk.Style()
    style.theme_use("clam")

    style.configure(".", background=DARK_BG_ALT, foreground=DARK_FG)
    style.configure("TFrame", background=DARK_BG_ALT)
    style.configure("TLabel", background=DARK_BG_ALT, foreground=DARK_FG)
    style.configure("TButton", background=DARK_BG_WIDGET, foreground=DARK_FG, borderwidth=1)
    style.map(
        "TButton",
        background=[("active", "#4a4a4a"), ("pressed", "#5a5a5a")],
        foreground=[("active", DARK_FG), ("pressed", DARK_FG)],
    )
    style.configure("TCheckbutton", background=DARK_BG_ALT, foreground=DARK_FG)
    style.map(
        "TCheckbutton",
        background=[("active", DARK_BG_ALT)],
        foreground=[("active", DARK_FG)],
    )
    style.configure(
        "TCombobox",
        fieldbackground=DARK_BG_WIDGET,
        background=DARK_BG_WIDGET,
        foreground=DARK_FG,
        selectbackground=DARK_ACCENT,
        selectforeground="white",
        arrowcolor=DARK_FG,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", DARK_BG_WIDGET)],
        foreground=[("readonly", DARK_FG)],
        selectbackground=[("readonly", DARK_ACCENT)],
        selectforeground=[("readonly", "white")],
    )
    style.configure("TEntry", fieldbackground=DARK_BG_WIDGET, foreground=DARK_FG)
    style.configure(
        "TScrollbar",
        background="#5a5a5a",
        troughcolor=DARK_BG_ALT,
        borderwidth=0,
        arrowcolor=DARK_FG,
    )
    style.map("TScrollbar", background=[("active", "#6a6a6a"), ("pressed", "#7a7a7a")])
    style.configure("TProgressbar", background=DARK_ACCENT, troughcolor=DARK_BG_WIDGET)

    # Message/transcript text content sits on the darkest background for
    # better contrast against its foreground than the general UI palette,
    # and uses a larger monospace font for readability. The Frame style is
    # for the container the text label sits in - a label alone only paints
    # the rectangle directly behind its (possibly wrapped, shrink-fit) text,
    # leaving the surrounding container's lighter background showing
    # through; wrapping it in a same-colored, width-filling frame closes
    # that gap so the whole block reads as one dark element.
    style.configure(
        "MessageText.TLabel",
        background=DARK_TEXT_BG,
        foreground=DARK_FG,
        font=(TEXT_FONT_FAMILY, TEXT_FONT_SIZE),
    )
    style.configure("MessageText.TFrame", background=DARK_TEXT_BG)

    # The combobox dropdown listbox is a plain tk.Listbox under the hood and
    # isn't covered by ttk styling - set its colors via the option database.
    root.option_add("*TCombobox*Listbox.background", DARK_BG_WIDGET)
    root.option_add("*TCombobox*Listbox.foreground", DARK_FG)
    root.option_add("*TCombobox*Listbox.selectBackground", DARK_ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "white")

    root.configure(bg=DARK_BG_ALT)
    enable_dark_title_bar(root)
    return style
