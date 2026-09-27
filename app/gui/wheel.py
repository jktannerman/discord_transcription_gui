"""Cross-platform mouse wheel/touchpad scroll events."""

import tkinter as tk

# Every event a mouse wheel/touchpad scroll can arrive as: <MouseWheel> on
# Windows/macOS (and X11 from Tk 8.7), <Button-4>/<Button-5> (up/down) on
# X11 with Tk 8.6 - which never sends <MouseWheel> at all, so binding only
# that left the review window unscrollable by wheel on Linux.
WHEEL_EVENT_SEQUENCES = ("<MouseWheel>", "<Button-4>", "<Button-5>")


# Shift modifier bit in an event's state.
_SHIFT_MASK = 0x1


def is_horizontal(event: tk.Event) -> bool:
    """Whether a wheel event is a horizontal scroll.

    On X11, Tk 8.6 turns horizontal scroll buttons 6/7 (e.g. a touchpad's
    sideways drift during a two-finger scroll) into Shift+Button-4/5, and a
    plain <Button-4>/<Button-5> binding matches those too. Shift+wheel is
    also the horizontal scroll convention on Windows/macOS.

    Args:
        event: Any event from WHEEL_EVENT_SEQUENCES.

    Returns:
        True if the Shift modifier is set.
    """
    return isinstance(event.state, int) and bool(event.state & _SHIFT_MASK)


def wheel_delta(event: tk.Event) -> int:
    """Normalize a wheel event to <MouseWheel>-style vertical delta units.

    Args:
        event: Any event from WHEEL_EVENT_SEQUENCES.

    Returns:
        Positive to scroll up, negative to scroll down, 0 for none - one
        notch is 120, as on Windows. X11 Button-4/5 each count as one notch.
        Horizontal scrolls (see is_horizontal) return 0: nothing on the
        review screen scrolls sideways, and treating them as vertical made
        a touchpad's sideways drift cancel out downward scrolling.
    """
    if is_horizontal(event):
        return 0
    if event.num == 4:
        return 120
    if event.num == 5:
        return -120
    return int(event.delta)
