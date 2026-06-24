import re
from pathlib import Path

import pytest

from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.ocr_corrections import Correction
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
    # A text-only message gets just a message box and the spacer for the
    # gap before the next message - no image-related spacers.
    assert items[0].slot_roles == ["message", "spacer_end"]
    # Normal default: 3 empty lines = 4 literal "\n" tokens.
    assert items[0].initial_spacer_texts["spacer_end"] == "\\n" * 4


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
    assert items[0].slot_roles == ["ocr0", "spacer_end"]


def test_build_review_items_applies_corrections_to_ocr_text_only():
    """ocr_corrections.py's fixes are applied to each image's joined OCR
    text (build_review_items' corrections param, here overridden so this
    doesn't depend on app/ocr_corrections.txt's actual, user-editable
    contents) - but never to a message's own original text, which never
    went through OCR in the first place."""
    corrections = [Correction(re.compile(r"\bfoo\b"), "bar", "")]
    entries = [MessageEntry(message_id="m", text_lines=["foo here too"], image_names=["card.png"])]
    file_info = {"card.png": ["foo there"]}

    items = build_review_items(entries, file_info, image_folder=Path("/images"), corrections=corrections)

    assert items[0].initial_ocr_texts == ["bar there"]
    assert items[0].initial_message_text == "foo here too"


def test_build_review_items_image_with_caption_gets_both_boxes():
    entries = [MessageEntry(message_id="m", text_lines=["look at this"], image_names=["card.png"])]
    file_info = {"card.png": ["ocr text"]}
    items = build_review_items(entries, file_info, image_folder=Path("/images"))

    assert items[0].initial_message_text == "look at this"
    assert items[0].initial_ocr_texts == ["ocr text"]
    assert items[0].slot_roles == ["message", "spacer_msg_img", "ocr0", "spacer_end"]
    # Text-to-image gap default: 1 empty line = 2 tokens.
    assert items[0].initial_spacer_texts["spacer_msg_img"] == "\\n" * 2


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
    assert items[0].slot_roles == [
        "message", "spacer_msg_img", "ocr0", "spacer_img0", "ocr1", "spacer_end",
    ]
    # Between-images gap default: 2 empty lines = 3 tokens.
    assert items[0].initial_spacer_texts["spacer_img0"] == "\\n" * 3


def test_build_review_items_dice_command_has_no_gap_before_result():
    entries = [
        MessageEntry(message_id="cmd", text_lines=["%roll 2d6"], image_names=[]),
        MessageEntry(message_id="result", text_lines=["You rolled a 7."], image_names=[]),
    ]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    # 0 empty lines = 1 token.
    assert items[0].initial_spacer_texts["spacer_end"] == "\\n"


def test_build_review_items_dice_result_followed_by_normal_message_uses_normal_default():
    entries = [
        MessageEntry(message_id="cmd", text_lines=["%roll 2d6"], image_names=[]),
        MessageEntry(message_id="result", text_lines=["You rolled a 7."], image_names=[]),
        MessageEntry(message_id="next", text_lines=["unrelated chat"], image_names=[]),
    ]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    # 3 empty lines = 4 tokens, same as a normal message.
    assert items[1].initial_spacer_texts["spacer_end"] == "\\n" * 4


def test_build_review_items_dice_result_followed_by_another_command_uses_one_empty_line():
    entries = [
        MessageEntry(message_id="cmd1", text_lines=["%roll 2d6"], image_names=[]),
        MessageEntry(message_id="result1", text_lines=["You rolled a 7."], image_names=[]),
        MessageEntry(message_id="cmd2", text_lines=["%draw 1 20"], image_names=[]),
    ]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    # 1 empty line = 2 tokens.
    assert items[1].initial_spacer_texts["spacer_end"] == "\\n" * 2


def test_lines_for_item_uses_initial_text_by_default():
    entries = [MessageEntry(message_id="m", text_lines=["caption"], image_names=["card.png"])]
    items = build_review_items(
        entries, {"card.png": ["the ocr text"]}, image_folder=Path("/images")
    )

    lines = lines_for_item(items[0])
    assert "".join(lines) == "caption\n\nthe ocr text\n\n\n\n"


def test_lines_for_item_uses_edited_text_for_a_role_when_given():
    entries = [MessageEntry(message_id="m", text_lines=[], image_names=["card.png"])]
    items = build_review_items(
        entries, {"card.png": ["original"]}, image_folder=Path("/images")
    )

    lines = lines_for_item(items[0], edited={"ocr0": "user-corrected text"})
    assert "".join(lines) == "user-corrected text\n\n\n\n"


def test_lines_for_item_image_with_caption_uses_both_edited_texts():
    entries = [MessageEntry(message_id="m", text_lines=["original caption"], image_names=["card.png"])]
    items = build_review_items(
        entries, {"card.png": ["original ocr"]}, image_folder=Path("/images")
    )

    lines = lines_for_item(items[0], edited={"message": "edited caption", "ocr0": "edited ocr"})
    assert "".join(lines) == "edited caption\n\nedited ocr\n\n\n\n"


def test_lines_for_item_multiple_images_each_use_their_own_edited_text():
    entries = [MessageEntry(message_id="m", text_lines=[], image_names=["red.png", "black.png"])]
    items = build_review_items(
        entries, {"red.png": ["original red"], "black.png": ["original black"]}, image_folder=Path("/images")
    )

    # Only the second image's OCR text is edited - the first falls back to
    # its initial OCR text.
    lines = lines_for_item(items[0], edited={"ocr1": "edited black"})
    assert "".join(lines) == "original red\n\n\nedited black\n\n\n\n"


def test_lines_for_item_text_only_message_uses_edited_text_when_given():
    entries = [MessageEntry(message_id="m", text_lines=["just text"], image_names=[])]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    lines = lines_for_item(items[0], edited={"message": "user-adjusted text"})
    assert "".join(lines) == "user-adjusted text\n\n\n\n"


def test_lines_for_item_text_only_message_uses_initial_text_by_default():
    entries = [MessageEntry(message_id="m", text_lines=["just text"], image_names=[])]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    lines = lines_for_item(items[0])
    assert "".join(lines) == "just text\n\n\n\n"


def test_lines_for_item_edited_spacer_overrides_default_gap():
    entries = [MessageEntry(message_id="m", text_lines=["just text"], image_names=[])]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    # User shortened the gap to 1 empty line (2 tokens) instead of the
    # normal-message default of 3.
    lines = lines_for_item(items[0], edited={"spacer_end": "\\n\\n"})
    assert "".join(lines) == "just text\n\n"


def test_lines_for_item_spacer_strips_real_newlines_mixed_with_tokens():
    entries = [MessageEntry(message_id="m", text_lines=["just text"], image_names=[])]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    # Real newlines (e.g. from pressing Enter while editing) anywhere in
    # the spacer box - before, after, or mixed through the tokens - are
    # discarded; only the literal "\n" tokens count.
    lines = lines_for_item(items[0], edited={"spacer_end": "\n\\n\n\\n\n"})
    assert "".join(lines) == "just text\n\n"


def test_lines_for_item_spacer_ignores_non_token_characters():
    entries = [MessageEntry(message_id="m", text_lines=["just text"], image_names=[])]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    lines = lines_for_item(items[0], edited={"spacer_end": "garbage\\n\\nmore garbage"})
    assert "".join(lines) == "just text\n\n"


def test_lines_for_item_strips_trailing_real_newlines_from_content():
    entries = [MessageEntry(message_id="m", text_lines=["just text"], image_names=[])]
    items = build_review_items(entries, file_info={}, image_folder=Path("/images"))

    lines = lines_for_item(items[0], edited={"message": "hello\n\n"})
    assert "".join(lines) == "hello\n\n\n\n"


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

    write_all_items(output_path, items, edited_texts=[{}, {}])

    written = output_path.read_text(encoding="utf8")
    # No fixed leading padding - this is exactly the behavior spacer slots
    # replaced.
    assert written.startswith("first message")
    assert "first message" in written
    assert "card text" in written
    assert written.index("first message") < written.index("card text")


def test_write_message_lines_appends_chunks_with_no_added_padding(tmp_path):
    output_path = tmp_path / "out.txt"
    output_path.write_text("", encoding="utf8")

    write_message_lines(output_path, ["chunk one", "\n\n", "chunk two"])

    assert output_path.read_text(encoding="utf8") == "chunk one\n\nchunk two"
