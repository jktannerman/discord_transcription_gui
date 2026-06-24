import datetime
from typing import Optional

import pytest

from gui_transcription.app.chatlog import parse_message_groups

APPROVED_USER_ID = "130636614807322624"


DEFAULT_MESSAGE_ID = "100000000000000001"


def _make_html(
    timestamp: str,
    user_id: str,
    text: str | None = None,
    image_src: str | None = None,
    timezone: Optional[str] = "UTC+0",
    message_id: Optional[str] = DEFAULT_MESSAGE_ID,
) -> str:
    text_html = (
        f'<div class="chatlog__markdown-preserve">{text}</div>' if text is not None else ""
    )
    image_html = (
        f'<img class="chatlog__attachment-media" src="{image_src}">'
        if image_src is not None
        else ""
    )
    # Real exports always include this postamble (see
    # example_inputs/pq_wm_0001_p2.html) - timezone=None simulates a
    # malformed/unexpected export missing it, to test that failure path.
    postamble_html = (
        f'<div class="postamble"><div class="postamble__entry">Timezone: {timezone}</div></div>'
        if timezone is not None
        else ""
    )
    # message_id=None simulates a malformed/unexpected export missing the
    # chatlog__message-container wrapper, to test that failure path.
    message_primary_html = f"""
      <div class="chatlog__message-primary">
        {text_html}
        {image_html}
      </div>
    """
    message_html = (
        f'<div class="chatlog__message-container" data-message-id="{message_id}">'
        f"{message_primary_html}</div>"
        if message_id is not None
        else message_primary_html
    )
    return f"""
    <div class="chatlog__message-group">
      <span class="chatlog__timestamp">{timestamp}</span>
      <span class="chatlog__author" data-user-id="{user_id}"></span>
      {message_html}
    </div>
    {postamble_html}
    """


def _start_time(date_str: str) -> int:
    date = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    return int(date.timestamp())


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


def test_image_filename_url_decoded():
    html = _make_html(
        "01/01/2025 00:00",
        APPROVED_USER_ID,
        image_src="https://cdn.example.com/path/my%20image.png",
    )
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert entries[0].image_name == "my image.png"


def test_positive_export_timezone_offset_applied():
    # 02/01/2024 03:00 at UTC+5 is 2024-01-01 22:00 UTC - still before a
    # start_time of 2024-01-01 22:01 UTC, so it must be excluded. Reading
    # the timestamp as if it were already UTC (ignoring the declared
    # offset) would instead place it at 2024-01-02 03:00 UTC - well after
    # start_time - so this only passes if the offset is actually applied.
    html = _make_html("02/01/2024 03:00", APPROVED_USER_ID, text="hello", timezone="UTC+5")
    start_time = int(datetime.datetime(2024, 1, 1, 22, 1, tzinfo=datetime.timezone.utc).timestamp())
    entries = parse_message_groups(html, start_time, {APPROVED_USER_ID})
    assert entries == []


def test_negative_export_timezone_offset_applied():
    # 01/01/2024 22:00 at UTC-3 is 2024-01-02 01:00 UTC - after a start_time
    # of 2024-01-02 00:59 UTC, so it must be included.
    html = _make_html("01/01/2024 22:00", APPROVED_USER_ID, text="hello", timezone="UTC-3")
    start_time = int(datetime.datetime(2024, 1, 2, 0, 59, tzinfo=datetime.timezone.utc).timestamp())
    entries = parse_message_groups(html, start_time, {APPROVED_USER_ID})
    assert len(entries) == 1


def test_export_timezone_offset_with_minutes_applied():
    # 01/01/2024 06:00 at UTC+5:30 is 2024-01-01 00:30 UTC - after a
    # start_time of 2024-01-01 00:29 UTC, so it must be included.
    html = _make_html("01/01/2024 06:00", APPROVED_USER_ID, text="hello", timezone="UTC+5:30")
    start_time = int(datetime.datetime(2024, 1, 1, 0, 29, tzinfo=datetime.timezone.utc).timestamp())
    entries = parse_message_groups(html, start_time, {APPROVED_USER_ID})
    assert len(entries) == 1


def test_missing_export_timezone_raises():
    html = _make_html("01/01/2025 00:00", APPROVED_USER_ID, text="hello", timezone=None)
    with pytest.raises(ValueError):
        parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})


def test_unparseable_export_timezone_raises():
    html = _make_html("01/01/2025 00:00", APPROVED_USER_ID, text="hello", timezone="not a timezone")
    with pytest.raises(ValueError):
        parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})


def test_parse_message_extracts_message_id():
    html = _make_html(
        "01/01/2025 00:00", APPROVED_USER_ID, text="hello", message_id="222222222222222222"
    )
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].message_id == "222222222222222222"


def test_parse_message_missing_container_raises():
    html = _make_html("01/01/2025 00:00", APPROVED_USER_ID, text="hello", message_id=None)
    with pytest.raises(ValueError):
        parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
