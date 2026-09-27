import pytest

from discord_transcription.chatlog import MessageEntry
from discord_transcription.pipeline import (
    check_output_path,
    parse_approved_user_ids,
    parse_start_date,
    render_items,
)
from discord_transcription.review_item import build_review_items


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


def test_render_items_joins_each_message_in_order(tmp_path):
    entries = [
        MessageEntry(message_id="m1", text_lines=["first message"], image_names=[]),
        MessageEntry(message_id="m2", text_lines=[], image_names=["card.png"]),
    ]
    items = build_review_items(
        entries, {"card.png": ["card text"]}, image_folder=tmp_path
    )

    rendered = render_items(items, edited_texts=[{}, {}])

    # No fixed leading padding - this is exactly the behavior spacer slots
    # replaced.
    assert rendered.startswith("first message")
    assert "card text" in rendered
    assert rendered.index("first message") < rendered.index("card text")


def test_check_output_path_accepts_a_new_file_in_an_existing_folder(tmp_path):
    check_output_path(tmp_path / "out.txt")


def test_check_output_path_accepts_an_existing_file(tmp_path):
    output_path = tmp_path / "out.txt"
    output_path.write_text("old", encoding="utf8")

    check_output_path(output_path)


def test_check_output_path_rejects_a_missing_folder(tmp_path):
    with pytest.raises(ValueError, match="folder doesn't exist"):
        check_output_path(tmp_path / "typo" / "out.txt")


def test_check_output_path_rejects_a_folder_as_the_output(tmp_path):
    with pytest.raises(ValueError, match="isn't a file"):
        check_output_path(tmp_path)


def test_check_output_path_rejects_an_unwritable_folder(tmp_path, monkeypatch):
    monkeypatch.setattr("discord_transcription.pipeline.os.access", lambda path, mode: False)

    with pytest.raises(ValueError, match="isn't writable"):
        check_output_path(tmp_path / "out.txt")
