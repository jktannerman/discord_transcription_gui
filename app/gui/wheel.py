"""Cross-platform mouse wheel/touchpad scroll events."""

import tkinter as tk

# Every event a mouse wheel/touchpad scroll can arrive as: <MouseWheel> on
# Windows/macOS (and X11 from Tk 8.7), <Button-4>/<Button-5> (up/down) on
# X11 with Tk 8.6 - which never sends <MouseWheel> at all, so binding only
# that left the review window unscrollable by wheel on Linux.
WHEEL_EVENT_SEQUENCES = ("<MouseWheel>", "<Button-4>", "<Button-5>")


def wheel_delta(event: tk.Event) -> int:
    """Normalize a wheel event to <MouseWheel>-style delta units.

    Args:
        event: Any event from WHEEL_EVENT_SEQUENCES.

    Returns:
        Positive to scroll up, negative to scroll down, 0 for none - one
        notch is 120, as on Windows. X11 Button-4/5 each count as one notch.
    """
    if event.num == 4:
        return 120
    if event.num == 5:
        return -120
    return int(event.delta)
