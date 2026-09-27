from pathlib import Path

from discord_transcription.chatlog import MessageEntry
from discord_transcription.gui.layout_constants import (
    GAP_BETWEEN_STACKED_PX,
    ROW_FRAME_OVERHEAD_PX,
    ROW_PACK_PADY_PX,
    TEXT_BOX_MARGIN_PX,
)
from discord_transcription.gui.virtualization import (
    DEFAULT_TEXT_METRICS,
    TextMetrics,
    _estimate_message_text_height,
    compute_visible_range,
    estimate_row_height,
)
from discord_transcription.review_item import ReviewItem


def test_estimate_row_height_image_item_is_positive():
    item = ReviewItem(
        entry=MessageEntry(message_id="m", text_lines=[], image_names=["card.png"]),
        image_paths=[Path("card.png")],
        initial_message_text=None,
        initial_ocr_texts=[""],
    )
    assert estimate_row_height(item) > 0


def test_estimate_row_height_text_only_scales_with_line_count():
    short = ReviewItem(
        entry=MessageEntry(message_id="m", text_lines=["hi"], image_names=[]),
        image_paths=[], initial_message_text="hi", initial_ocr_texts=[],
    )
    long = ReviewItem(
        entry=MessageEntry(message_id="m", text_lines=["hi " * 200], image_names=[]),
        image_paths=[], initial_message_text="hi " * 200, initial_ocr_texts=[],
    )
    assert estimate_row_height(long) > estimate_row_height(short)


def test_estimate_row_height_empty_text_is_still_positive():
    item = ReviewItem(
        entry=MessageEntry(message_id="m", text_lines=[], image_names=[]),
        image_paths=[], initial_message_text="", initial_ocr_texts=[],
    )
    assert estimate_row_height(item) > 0


def test_estimate_row_height_image_with_caption_taller_than_image_alone():
    image_only = ReviewItem(
        entry=MessageEntry(message_id="m", text_lines=[], image_names=["card.png"]),
        image_paths=[Path("card.png")], initial_message_text=None, initial_ocr_texts=[""],
    )
    image_with_caption = ReviewItem(
        entry=MessageEntry(message_id="m", text_lines=["a caption"], image_names=["card.png"]),
        image_paths=[Path("card.png")], initial_message_text="a caption", initial_ocr_texts=[""],
    )
    assert estimate_row_height(image_with_caption) > estimate_row_height(image_only)


def test_estimate_row_height_two_images_taller_than_one():
    one_image = ReviewItem(
        entry=MessageEntry(message_id="m", text_lines=[], image_names=["card.png"]),
        image_paths=[Path("card.png")], initial_message_text=None, initial_ocr_texts=[""],
    )
    two_images = ReviewItem(
        entry=MessageEntry(message_id="m", text_lines=[], image_names=["card.png", "card2.png"]),
        image_paths=[Path("card.png"), Path("card2.png")],
        initial_message_text=None, initial_ocr_texts=["", ""],
    )
    assert estimate_row_height(two_images) > estimate_row_height(one_image)


def test_estimate_row_height_text_only_includes_spacer_end_height():
    # A text-only item's slot_roles is ["message", "spacer_end"] - the
    # spacer contributes the measured spacer box height to the right column
    # only (it has no left-column counterpart at all).
    item = ReviewItem(
        entry=MessageEntry(message_id="m", text_lines=["hi"], image_names=[]),
        image_paths=[], initial_message_text="hi", initial_ocr_texts=[],
    )
    metrics = DEFAULT_TEXT_METRICS
    label_h = _estimate_message_text_height(item, metrics)
    expected_left = label_h + GAP_BETWEEN_STACKED_PX
    expected_right = (
        label_h + TEXT_BOX_MARGIN_PX + GAP_BETWEEN_STACKED_PX + metrics.spacer_box_height_px
    )
    expected = max(expected_left, expected_right) + ROW_FRAME_OVERHEAD_PX + 2 * ROW_PACK_PADY_PX
    assert estimate_row_height(item, metrics=metrics) == expected


def _text_item(text: str) -> ReviewItem:
    return ReviewItem(
        entry=MessageEntry(message_id="m", text_lines=[text], image_names=[]),
        image_paths=[], initial_message_text=text, initial_ocr_texts=[],
    )


def test_estimate_message_text_height_uses_measured_metrics():
    # 760px wrap width / 20px chars = 38 chars per line; 100 chars of
    # unbroken text wraps to 3 lines of 30px, plus 5px label padding.
    metrics = TextMetrics(
        char_width_px=20, line_height_px=30, label_padding_px=5, spacer_box_height_px=40,
    )
    assert _estimate_message_text_height(_text_item("x" * 100), metrics) == 3 * 30 + 5


def test_estimate_row_height_grows_with_larger_measured_font():
    # The same text is taller in a bigger font: fewer characters fit on a
    # line, and each line is taller.
    text = "word " * 100
    small = TextMetrics(char_width_px=7, line_height_px=18, label_padding_px=4, spacer_box_height_px=30)
    large = TextMetrics(char_width_px=14, line_height_px=28, label_padding_px=4, spacer_box_height_px=40)
    item = _text_item(text)
    assert estimate_row_height(item, metrics=large) > estimate_row_height(item, metrics=small)


def test_compute_visible_range_empty_list_returns_empty_range():
    first, last = compute_visible_range([], scroll_top=0, viewport_height=500, buffer=100)
    assert last < first  # range(first, last + 1) is empty


def test_compute_visible_range_at_top_includes_index_zero():
    heights = [100] * 10
    first, last = compute_visible_range(heights, scroll_top=0, viewport_height=300, buffer=0)
    assert first == 0
    assert last >= 2  # at least covers the 300px viewport


def test_compute_visible_range_at_bottom_includes_last_index():
    heights = [100] * 10
    total = sum(heights)
    # scrolled all the way down: viewport's bottom edge is at the document's end
    first, last = compute_visible_range(
        heights, scroll_top=total - 300, viewport_height=300, buffer=0
    )
    assert last == len(heights) - 1


def test_compute_visible_range_single_item_list():
    first, last = compute_visible_range([500], scroll_top=0, viewport_height=300, buffer=0)
    assert (first, last) == (0, 0)


def test_compute_visible_range_buffer_expands_range_at_boundary():
    heights = [100] * 10
    # near the top, a buffer should not pull first_idx below 0 or crash
    first, last = compute_visible_range(heights, scroll_top=0, viewport_height=100, buffer=1000)
    assert first == 0
    assert last == len(heights) - 1


def test_compute_visible_range_scroll_top_past_end_clamps_to_last_index():
    heights = [100] * 10
    first, last = compute_visible_range(
        heights, scroll_top=10_000, viewport_height=300, buffer=0
    )
    assert first == last == len(heights) - 1


def test_compute_visible_range_is_idempotent():
    heights = [100, 250, 80, 400, 120]
    args = (heights, 137.0, 300.0, 50.0)
    assert compute_visible_range(*args) == compute_visible_range(*args)
