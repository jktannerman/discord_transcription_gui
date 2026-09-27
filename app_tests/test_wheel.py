"""wheel_delta's normalization of wheel events across platforms."""
from types import SimpleNamespace

import pytest

from gui_transcription.app.gui.wheel import is_horizontal, wheel_delta

SHIFT = 0x1


def _event(num="??", delta=0, state=0):
    return SimpleNamespace(num=num, delta=delta, state=state)


@pytest.mark.parametrize(
    "event, expected",
    [
        (_event(num=4), 120),
        (_event(num=5), -120),
        (_event(delta=240), 240),
        (_event(delta=-120), -120),
        # Other modifiers (e.g. NumLock, 0x10) don't make a scroll horizontal.
        (_event(num=5, state=0x10), -120),
    ],
)
def test_vertical_scrolls_map_to_mousewheel_delta(event, expected):
    assert wheel_delta(event) == expected


@pytest.mark.parametrize(
    "event",
    [
        # X11 Tk 8.6 delivers horizontal buttons 6/7 as Shift+Button-4/5.
        _event(num=4, state=SHIFT),
        _event(num=5, state=SHIFT),
        _event(delta=120, state=SHIFT),
    ],
)
def test_horizontal_scrolls_are_ignored(event):
    assert is_horizontal(event)
    assert wheel_delta(event) == 0


def test_non_integer_state_is_not_horizontal():
    # Tk reports state as a string for some synthetic events.
    assert not is_horizontal(_event(num=4, state="??"))
