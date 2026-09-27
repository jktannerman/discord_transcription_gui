"""Box-sizing decision logic in RowBuilder (max_text_box_height_px,
fixed_text_box_height) - the rule that replaced the old per-keystroke
remeasuring design that caused this screen's worst scroll-position bugs
(see ARCHITECTURE.md). These only ask the viewport for its height and the
screen's, so a stand-in viewport is enough - no real Tk widgets needed."""
from types import SimpleNamespace

from discord_transcription.gui.layout_constants import TEXT_BOX_MARGIN_PX
from discord_transcription.gui.row_building import TEXT_BOX_MAX_HEIGHT_FRACTION, RowBuilder
from discord_transcription.gui.virtualization import DEFAULT_TEXT_METRICS


def _SizingStub(canvas_height: int, screen_height: int = 1000) -> RowBuilder:
    viewport = SimpleNamespace(
        winfo_height=lambda: canvas_height, winfo_screenheight=lambda: screen_height
    )
    return RowBuilder([], None, None, None, viewport=viewport, text_metrics=DEFAULT_TEXT_METRICS)


def test_max_text_box_height_uses_canvas_viewport_when_laid_out():
    stub = _SizingStub(canvas_height=800)
    assert stub.max_text_box_height_px() == int(800 * TEXT_BOX_MAX_HEIGHT_FRACTION)


def test_max_text_box_height_falls_back_to_screen_when_canvas_unsized():
    stub = _SizingStub(canvas_height=1, screen_height=1000)
    assert stub.max_text_box_height_px() == int(1000 * TEXT_BOX_MAX_HEIGHT_FRACTION)


def test_fixed_text_box_height_tracks_paired_element_plus_margin():
    stub = _SizingStub(canvas_height=800)
    paired_height = 50
    assert stub.fixed_text_box_height(paired_height) == paired_height + TEXT_BOX_MARGIN_PX


def test_fixed_text_box_height_caps_at_max_for_a_very_tall_paired_element():
    stub = _SizingStub(canvas_height=800)
    max_px = stub.max_text_box_height_px()
    assert stub.fixed_text_box_height(paired_height=max_px * 10) == max_px


def test_fixed_text_box_height_is_never_less_than_one():
    stub = _SizingStub(canvas_height=1, screen_height=1)
    assert stub.fixed_text_box_height(paired_height=0) >= 1
