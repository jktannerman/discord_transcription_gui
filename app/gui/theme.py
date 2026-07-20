"""Dark theme shared by every screen in this app."""

import ctypes
import sys
import tkinter as tk
from tkinter import ttk

# Dark theme colors
DARK_BG = "#1e1e1e"  # Main background (darkest)
DARK_BG_ALT = "#2d2d2d"  # Frames, canvases
DARK_BG_WIDGET = "#3c3c3c"  # Buttons, entry/text fields
DARK_FG = "#d4d4d4"  # Text color
DARK_ACCENT = "#264f78"  # Selection highlight

# Colors and font for message/transcript text content and editable input
# boxes specifically - darker/higher-contrast than the general UI palette
# above, which reads more clearly for dense body text and typed input.
DARK_TEXT_BG = "#171717"  # Darker than DARK_BG, so text boxes read as the
                          # darkest element on screen (darker than the image
                          # panel's DARK_BG_ALT background next to them)
DARK_INSERT = "white"  # Brighter text-cursor color than DARK_FG
DARK_FOCUS_HIGHLIGHT = "#569cd6"  # Border color for a focused input box
SPELLCHECK_UNDERLINE = "#ff5555"  # Underline color for a flagged misspelling

TEXT_FONT_FAMILY = "Consolas"
TEXT_FONT_SIZE = 14


def enable_dark_title_bar(window: tk.Tk) -> None:
    """Enable the dark window title bar on Windows 10/11. No-op elsewhere.

    DWM only repaints the non-client area (the title bar) lazily - setting
    the attribute alone leaves the title bar light until the next resize,
    which is what made it look like the dark theme "kicked in on resize".
    The SetWindowPos SWP_FRAMECHANGED call below forces that repaint
    immediately, without actually changing the window's size or position.

    Must be called while the window is still withdrawn (see the
    withdraw()/deiconify() pair in App.__init__) - i.e. before the window has
    ever actually been shown on screen. Forcing a repaint *after* the
    window's first real paint (whether synchronously during construction, or
    deferred via root.after() to run once mainloop() starts - both were
    tried) was unreliable, since the window can already have been mapped by
    Tk before either point. Setting the attribute while still withdrawn and
    only then showing the window means DWM picks up the dark theme on the
    window's actual first paint, avoiding the race entirely.
    """
    if sys.platform != "win32":
        return

    try:
        window.update_idletasks()
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
    # Used by each review-screen OCR box's own checkbox (row_building.py) -
    # a separate named style rather than reusing plain "TCheckbutton" above,
    # since this one needs DARK_TEXT_BG (the text box's own background, so
    # the checkbox's column blends into it) instead of the general UI's
    # DARK_BG_ALT.
    style.configure("OcrCheckbox.TCheckbutton", background=DARK_TEXT_BG, foreground=DARK_FG)
    style.map(
        "OcrCheckbox.TCheckbutton",
        background=[("active", DARK_TEXT_BG)],
        foreground=[("active", DARK_FG)],
    )
    # The checkbox's own column frame (row_building.py) - plain "TFrame" is
    # DARK_BG_ALT, which would leave a visible seam around the checkbox
    # above wherever its own background doesn't fully cover the frame.
    style.configure("OcrCheckboxColumn.TFrame", background=DARK_TEXT_BG)
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

    # The combobox dropdown listbox is a plain tk.Listbox under the hood and
    # isn't covered by ttk styling - set its colors via the option database.
    root.option_add("*TCombobox*Listbox.background", DARK_BG_WIDGET)
    root.option_add("*TCombobox*Listbox.foreground", DARK_FG)
    root.option_add("*TCombobox*Listbox.selectBackground", DARK_ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "white")

    root.configure(bg=DARK_BG_ALT)
    enable_dark_title_bar(root)
    return style
