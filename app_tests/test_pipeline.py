import pytest

from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.pipeline import (
    parse_approved_user_ids,
    parse_start_date,
    write_all_items,
    write_message_lines,
)
from gui_transcription.app.review_item import build_review_items


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
