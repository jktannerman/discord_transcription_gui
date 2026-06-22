import datetime
import time

from gui_transcription.app.chatlog import parse_message_groups

APPROVED_USER_ID = "130636614807322624"


def _make_html(timestamp: str, user_id: str, text: str | None = None, image_src: str | None = None) -> str:
    text_html = (
        f'<div class="chatlog__markdown-preserve">{text}</div>' if text is not None else ""
    )
    image_html = (
        f'<img class="chatlog__attachment-media" src="{image_src}">'
        if image_src is not None
        else ""
    )
    return f"""
    <div class="chatlog__message-group">
      <span class="chatlog__timestamp">{timestamp}</span>
      <span class="chatlog__author" data-user-id="{user_id}"></span>
      <div class="chatlog__message-primary">
        {text_html}
        {image_html}
      </div>
    </div>
    """


def _start_time(date_str: str) -> int:
    date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    return int(time.mktime(date.timetuple()))


def test_message_before_start_date_excluded():
    html = _make_html("01/01/2020 00:00", APPROVED_USER_ID, text="hello")
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert entries == []


def test_message_from_unapproved_author_excluded():
    html = _make_html("01/01/2025 00:00", "999999999999999999", text="hello")
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert entries == []


def test_message_included_regardless_of_author_when_no_filter():
    html = _make_html("01/01/2025 00:00", "999999999999999999", text="hello")
    entries = parse_message_groups(html, _start_time("2024-01-01"), None)
    assert len(entries) == 1


def test_text_only_message_included():
    html = _make_html("01/01/2025 00:00", APPROVED_USER_ID, text="line one\nline two")
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].text_lines == ["line one", "line two"]
    assert entries[0].image_name is None


def test_image_attached_message_extracts_filename():
    html = _make_html(
        "01/01/2025 00:00",
        APPROVED_USER_ID,
        image_src="https://cdn.example.com/path/my_image.png",
    )
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].image_name == "my_image.png"
