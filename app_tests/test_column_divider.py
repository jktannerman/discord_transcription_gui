"""Display-free position math for the image/text column divider."""

import pytest

from discord_transcription.gui.column_divider import divider_x_for_width, width_for_divider_x
from discord_transcription.gui.layout_constants import COLUMN_DIVIDER_WIDTH_PX


@pytest.mark.parametrize("width", [200, 760, 1234])
def test_pointer_at_divider_center_maps_back_to_the_same_width(width):
    center = divider_x_for_width(width) + COLUMN_DIVIDER_WIDTH_PX / 2
    assert width_for_divider_x(center) == width


def test_wider_column_moves_divider_right_by_the_same_amount():
    assert divider_x_for_width(900) - divider_x_for_width(700) == 200
