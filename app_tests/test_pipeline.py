from pathlib import Path

import pytest

from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.pipeline import (
    build_review_items,
    lines_for_item,
    parse_approved_user_ids,
    parse_start_date,
    write_all_items,
    write_message_lines,
)


def test_parse_start_date_valid():
    ts = parse_start_date("2024-01-01")
    assert ts > 0


def test_parse_start_date_with_time():
    ts = parse_start_date("2024-01-01-12-30-00")
    assert ts > 0


def test_parse_start_date_invalid_raises():
    with pytest.raises(ValueError):
        parse_start_date("not-a-date")


def test_parse_start_date_wrong_field_count_raises():
    with pytest.raises(ValueError):
        parse_start_date("2024")


def test_parse_approved_user_ids_extracts_leading_digits():
    ids = parse_approved_user_ids("123456789 - Alice\n987654321 - Bob")
    assert ids == {"123456789", "987654321"}


def test_parse_approved_user_ids_ignores_blank_lines():
    ids = parse_approved_user_ids("123456789 - Alice\n\n   \n987654321 - Bob\n")
    assert ids == {"123456789", "987654321"}


def test_parse_approved_user_ids_dedupes():
    ids = parse_approved_user_ids("123456789 - Alice\n123456789 - Alice again")
    assert ids == {"123456789"}


def test_parse_approved_user_ids_no_leading_digits_raises():
    with pytest.raises(ValueError):
        parse_approved_user_ids("Alice - 123456789")


def test_parse_approved_user_ids_empty_text_returns_empty_set():
    assert parse_approved_user_ids("") == set()


def test_build_review_items_text_only_message():
    entries = [MessageEntry(message_id="m", text_lines=["hello", "world"], image_names=[])]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    assert len(items) == 1
    assert items[0].image_paths == []
    # initial_message_text is the editable copy a text-only message gets,
    # initialized from (not stripped of) its own original lines so the
    # user can adjust spacing freely without losing it up front.
    assert items[0].initial_message_text == "hello\nworld"
    assert items[0].initial_ocr_texts == []
    assert items[0].entry.text_lines == ["hello", "world"]


def test_build_review_items_image_message_joins_paragraphs():
    entries = [MessageEntry(message_id="m", text_lines=[], image_names=["card.png"])]
    file_info = {"card.png": ["first paragraph ", " second paragraph"]}
    items = build_review_items(entries, file_info, image_folder=Path("/images"))

    assert len(items) == 1
    assert items[0].image_paths == [Path("/images/card.png")]
    assert items[0].initial_ocr_texts == ["first paragraph\n\nsecond paragraph"]
    # No caption on this message, so there's nothing to give a message-text
    # box to.
    assert items[0].initial_message_text is None


def test_build_review_items_image_with_caption_gets_both_boxes():
    entries = [MessageEntry(message_id="m", text_lines=["look at this"], image_names=["card.png"])]
    file_info = {"card.png": ["ocr text"]}
    items = build_review_items(entries, file_info, image_folder=Path("/images"))

    assert items[0].initial_message_text == "look at this"
    assert items[0].initial_ocr_texts == ["ocr text"]


def test_build_review_items_image_missing_from_cache_uses_empty_text():
    entries = [MessageEntry(message_id="m", text_lines=[], image_names=["missing.png"])]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    assert items[0].initial_ocr_texts == [""]


def test_build_review_items_message_with_multiple_images_gets_one_box_per_image():
    entries = [MessageEntry(message_id="m", text_lines=["red and black"], image_names=["red.png", "black.png"])]
    file_info = {"red.png": ["RED"], "black.png": ["BLACK"]}
    items = build_review_items(entries, file_info, image_folder=Path("/images"))

    assert items[0].image_paths == [Path("/images/red.png"), Path("/images/black.png")]
    assert items[0].initial_ocr_texts == ["RED", "BLACK"]
    assert items[0].initial_message_text == "red and black"


def test_lines_for_item_uses_initial_text_by_default():
    entries = [MessageEntry(message_id="m", text_lines=["caption"], image_names=["card.png"])]
    items = build_review_items(
        entries, {"card.png": ["the ocr text"]}, image_folder=Path("/images")
    )

    lines = lines_for_item(items[0])
    assert lines == ["caption\n", "the ocr text\n"]


def test_lines_for_item_uses_edited_ocr_text_when_given():
    entries = [MessageEntry(message_id="m", text_lines=[], image_names=["card.png"])]
    items = build_review_items(
        entries, {"card.png": ["original"]}, image_folder=Path("/images")
    )

    lines = lines_for_item(items[0], edited_ocr_texts=["user-corrected text"])
    assert lines == ["user-corrected text\n"]


def test_lines_for_item_image_with_caption_uses_both_edited_texts():
    entries = [MessageEntry(message_id="m", text_lines=["original caption"], image_names=["card.png"])]
    items = build_review_items(
        entries, {"card.png": ["original ocr"]}, image_folder=Path("/images")
    )

    lines = lines_for_item(
        items[0], edited_message_text="edited caption", edited_ocr_texts=["edited ocr"]
    )
    assert lines == ["edited caption\n", "edited ocr\n"]


def test_lines_for_item_multiple_images_each_use_their_own_edited_text():
    entries = [MessageEntry(message_id="m", text_lines=[], image_names=["red.png", "black.png"])]
    items = build_review_items(
        entries, {"red.png": ["original red"], "black.png": ["original black"]}, image_folder=Path("/images")
    )

    # Only the second image's OCR text is edited - the first falls back to
    # its initial OCR text.
    lines = lines_for_item(items[0], edited_ocr_texts=[None, "edited black"])
    assert lines == ["original red\n", "edited black\n"]


def test_lines_for_item_text_only_message_uses_edited_text_when_given():
    entries = [MessageEntry(message_id="m", text_lines=["just text"], image_names=[])]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    lines = lines_for_item(items[0], edited_message_text="user-adjusted spacing")
    assert lines == ["user-adjusted spacing\n"]


def test_lines_for_item_text_only_message_uses_initial_text_by_default():
    entries = [MessageEntry(message_id="m", text_lines=["just text"], image_names=[])]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    lines = lines_for_item(items[0])
    assert lines == ["just text\n"]


def test_write_all_items_writes_each_message_in_order(tmp_path):
    output_path = tmp_path / "out.txt"
    output_path.write_text("", encoding="utf8")

    entries = [
        MessageEntry(message_id="m1", text_lines=["first message"], image_names=[]),
        MessageEntry(message_id="m2", text_lines=[], image_names=["card.png"]),
    ]
    items = build_review_items(
        entries, {"card.png": ["card text"]}, image_folder=tmp_path
    )

    write_all_items(
        output_path, items, edited_texts=[(None, []), (None, ["edited card text"])]
    )

    written = output_path.read_text(encoding="utf8")
    assert "first message" in written
    assert "edited card text" in written
    assert written.index("first message") < written.index("edited card text")


def test_write_message_lines_appends_with_padding(tmp_path):
    output_path = tmp_path / "out.txt"
    output_path.write_text("", encoding="utf8")

    write_message_lines(output_path, ["line one\n", "line two\n"])

    assert output_path.read_text(encoding="utf8") == "\n\n\n\nline one\nline two\n\n\n"
