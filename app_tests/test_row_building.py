"""Box-sizing decision logic in RowBuildingMixin (_max_text_box_height_px,
_fixed_text_box_height) - the rule that replaced the old per-keystroke
remeasuring design that caused this screen's worst scroll-position bugs
(see ARCHITECTURE.md). Tested via a minimal stub the same way
keyboard_nav.py's mixin is (test_keyboard_nav.py's _NavStub), since these
methods only reach into self._canvas.winfo_height() and
self.winfo_screenheight() - no real Tk widgets needed."""
from types import SimpleNamespace

from gui_transcription.app.gui.layout_constants import TEXT_BOX_MARGIN_PX
from gui_transcription.app.gui.row_building import (
    TEXT_BOX_MAX_HEIGHT_FRACTION,
    RowBuildingMixin,
)


class _SizingStub(RowBuildingMixin):
    def __init__(self, canvas_height: int, screen_height: int = 1000):
        self._canvas = SimpleNamespace(winfo_height=lambda: canvas_height)
        self._screen_height = screen_height

    def winfo_screenheight(self) -> int:
        return self._screen_height


def test_max_text_box_height_uses_canvas_viewport_when_laid_out():
    stub = _SizingStub(canvas_height=800)
    assert stub._max_text_box_height_px() == int(800 * TEXT_BOX_MAX_HEIGHT_FRACTION)


def test_max_text_box_height_falls_back_to_screen_when_canvas_unsized():
    stub = _SizingStub(canvas_height=1, screen_height=1000)
    assert stub._max_text_box_height_px() == int(1000 * TEXT_BOX_MAX_HEIGHT_FRACTION)


def test_fixed_text_box_height_tracks_paired_element_plus_margin():
    stub = _SizingStub(canvas_height=800)
    paired_height = 50
    assert stub._fixed_text_box_height(paired_height) == paired_height + TEXT_BOX_MARGIN_PX


def test_fixed_text_box_height_caps_at_max_for_a_very_tall_paired_element():
    stub = _SizingStub(canvas_height=800)
    max_px = stub._max_text_box_height_px()
    assert stub._fixed_text_box_height(paired_height=max_px * 10) == max_px


def test_fixed_text_box_height_is_never_less_than_one():
    stub = _SizingStub(canvas_height=1, screen_height=1)
    assert stub._fixed_text_box_height(paired_height=0) >= 1
