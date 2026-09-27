import datetime
from pathlib import Path
from typing import Optional

import pytest

from discord_transcription.chatlog import (
    DISCORD_EPOCH_MS,
    parse_message_groups,
    snowflake_timestamp_ms,
)

_EXAMPLE_INPUTS = Path(__file__).resolve().parent.parent / "example_inputs"
_SHORT_TEST_INPUT = _EXAMPLE_INPUTS / "short_test_input.html"
# example_inputs/ is gitignored (it holds a private export), so tests that
# read it are skipped on a checkout that doesn't have it.
_needs_short_test_input = pytest.mark.skipif(
    not _SHORT_TEST_INPUT.exists(), reason="example_inputs/short_test_input.html not present"
)

APPROVED_USER_ID = "130636614807322624"


def _utc(*args: int) -> datetime.datetime:
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


def _snowflake(when: datetime.datetime, sequence: int = 0) -> str:
    """A Discord message ID for a message sent at `when` (UTC)."""
    ms = round(when.timestamp() * 1000) - DISCORD_EPOCH_MS
    return str((ms << 22) | sequence)


DEFAULT_MESSAGE_ID = _snowflake(_utc(2025, 1, 1))


def _message_html(
    message_id: Optional[str],
    text: str | None = None,
    image_srcs: Optional[list[str]] = None,
    embed_image_srcs: Optional[list[str]] = None,
) -> str:
    text_html = (
        f'<div class="chatlog__markdown-preserve">{text}</div>' if text is not None else ""
    )
    # One chatlog__attachment block per image, mirroring the real export's
    # markup for a message with more than one attachment.
    image_html = "".join(
        f'<div class="chatlog__attachment">'
        f'<img class="chatlog__attachment-media" src="{src}">'
        f"</div>"
        for src in image_srcs or []
    )
    # A pasted image URL/link Discord unfurled - markup DiscordChatExporter
    # renders as chatlog__embed / chatlog__embed-generic-image rather than
    # chatlog__attachment / chatlog__attachment-media.
    embed_html = "".join(
        f'<div class="chatlog__embed">'
        f'<img class="chatlog__embed-generic-image" src="{src}">'
        f"</div>"
        for src in embed_image_srcs or []
    )
    message_primary_html = f"""
      <div class="chatlog__message-primary">
        {text_html}
        {image_html}
        {embed_html}
      </div>
    """
    # message_id=None simulates a malformed/unexpected export missing the
    # chatlog__message-container wrapper, to test that failure path.
    if message_id is None:
        return message_primary_html
    return (
        f'<div class="chatlog__message-container" data-message-id="{message_id}">'
        f"{message_primary_html}</div>"
    )


def _group_html(
    user_id: str,
    messages: list[str],
    timestamp: str = "01/01/2025 00:00",
    timezone: Optional[str] = "UTC+0",
) -> str:
    """One chatlog__message-group holding `messages` (see _message_html).

    `timestamp` and `timezone` are the group header's visible time and the
    postamble's declared offset - present in real exports, but not what the
    parser goes by.
    """
    postamble_html = (
        f'<div class="postamble"><div class="postamble__entry">Timezone: {timezone}</div></div>'
        if timezone is not None
        else ""
    )
    return f"""
    <div class="chatlog__message-group">
      <span class="chatlog__timestamp">{timestamp}</span>
      <span class="chatlog__author" data-user-id="{user_id}"></span>
      {"".join(messages)}
    </div>
    {postamble_html}
    """


def _make_html(
    user_id: str,
    text: str | None = None,
    image_src: str | None = None,
    image_srcs: Optional[list[str]] = None,
    embed_image_srcs: Optional[list[str]] = None,
    message_id: Optional[str] = DEFAULT_MESSAGE_ID,
) -> str:
    """A one-message export. image_src is shorthand for a single image."""
    if image_srcs is None and image_src is not None:
        image_srcs = [image_src]
    return _group_html(user_id, [_message_html(message_id, text, image_srcs, embed_image_srcs)])


def _start_time(date_str: str) -> int:
    date = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    return int(date.timestamp())


def test_snowflake_timestamp_ms_decodes_a_real_message_id():
    # From example_inputs/short_test_input.html; Discord shows this message
    # as sent at 2026-09-27 16:49:29 UTC.
    ms = snowflake_timestamp_ms("1553810828250058903")
    sent = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    assert sent.replace(microsecond=0) == _utc(2026, 9, 27, 16, 49, 29)


def test_snowflake_timestamp_ms_round_trips():
    when = _utc(2025, 6, 1, 12, 34, 56, 789000)
    assert snowflake_timestamp_ms(_snowflake(when, sequence=4095)) == round(when.timestamp() * 1000)


@pytest.mark.parametrize("bad_id", ["", "abc", "-5", "12.5"])
def test_snowflake_timestamp_ms_rejects_non_snowflake_ids(bad_id):
    with pytest.raises(ValueError):
        snowflake_timestamp_ms(bad_id)


def test_message_before_start_date_excluded():
    html = _make_html(APPROVED_USER_ID, text="hello", message_id=_snowflake(_utc(2020, 1, 1)))
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert entries == []


def test_message_exactly_at_start_time_included():
    start = _utc(2025, 3, 1, 12, 0, 0)
    html = _make_html(APPROVED_USER_ID, text="hello", message_id=_snowflake(start))
    entries = parse_message_groups(html, int(start.timestamp()), {APPROVED_USER_ID})
    assert len(entries) == 1


def test_start_time_cutoff_has_sub_minute_precision():
    # The visible timestamps are rounded to the minute; the ID isn't. A
    # message 20 seconds before the cutoff, in the same minute, is excluded
    # and one 20 seconds after it is kept.
    start = _utc(2025, 3, 1, 12, 0, 30)
    html = _group_html(
        APPROVED_USER_ID,
        [
            _message_html(_snowflake(_utc(2025, 3, 1, 12, 0, 10)), text="before"),
            _message_html(_snowflake(_utc(2025, 3, 1, 12, 0, 50)), text="after"),
        ],
        timestamp="01/03/2025 12:00",
    )
    entries = parse_message_groups(html, int(start.timestamp()), {APPROVED_USER_ID})
    assert [e.text_lines for e in entries] == [["after"]]


def test_message_group_straddling_start_time_is_split():
    # A group (consecutive messages by one author) that was still going
    # when the previous export was made: only the messages sent after the
    # cutoff are new. Judging the group by its header timestamp alone
    # dropped all of them.
    start = _utc(2025, 3, 1, 12, 5)
    ids = [_snowflake(_utc(2025, 3, 1, 12, minute)) for minute in (0, 3, 6, 9)]
    html = _group_html(
        APPROVED_USER_ID,
        [_message_html(message_id, text=f"m{i}") for i, message_id in enumerate(ids)],
        timestamp="01/03/2025 12:00",
    )
    entries = parse_message_groups(html, int(start.timestamp()), {APPROVED_USER_ID})
    assert [e.message_id for e in entries] == ids[2:]


def test_visible_timestamp_and_postamble_timezone_are_ignored():
    # DiscordChatExporter writes local time with a postamble offset that
    # leaves out daylight saving time (a BST export says "UTC+0"). The
    # visible header time and offset here claim 2020; the ID says 2025.
    html = _group_html(
        APPROVED_USER_ID,
        [_message_html(_snowflake(_utc(2025, 7, 1, 16, 49)), text="hello")],
        timestamp="01/01/2020 00:00",
        timezone="UTC+5",
    )
    entries = parse_message_groups(html, _start_time("2025-07-01"), {APPROVED_USER_ID})
    assert len(entries) == 1


def test_export_without_postamble_is_parsed():
    html = _group_html(
        APPROVED_USER_ID, [_message_html(DEFAULT_MESSAGE_ID, text="hello")], timezone=None
    )
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1


def test_message_from_unapproved_author_excluded():
    html = _make_html("999999999999999999", text="hello")
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert entries == []


def test_message_included_regardless_of_author_when_no_filter():
    html = _make_html("999999999999999999", text="hello")
    entries = parse_message_groups(html, _start_time("2024-01-01"), None)
    assert len(entries) == 1


def test_text_only_message_included():
    html = _make_html(APPROVED_USER_ID, text="line one\nline two")
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].text_lines == ["line one", "line two"]
    assert entries[0].image_names == []


def test_image_attached_message_extracts_filename():
    html = _make_html(APPROVED_USER_ID, image_src="https://cdn.example.com/path/my_image.png")
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].image_names == ["my_image.png"]


def test_image_filename_url_decoded():
    html = _make_html(APPROVED_USER_ID, image_src="https://cdn.example.com/path/my%20image.png")
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert entries[0].image_names == ["my image.png"]


def test_message_with_multiple_images_extracts_all_filenames_in_order():
    html = _make_html(
        APPROVED_USER_ID,
        text="red and black",
        image_srcs=[
            "https://cdn.example.com/path/red.png",
            "https://cdn.example.com/path/black.png",
        ],
    )
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].image_names == ["red.png", "black.png"]


def test_embedded_image_extracts_filename():
    # A pasted image URL/link that Discord unfurled into an embed uses
    # chatlog__embed / chatlog__embed-generic-image markup rather than
    # chatlog__attachment / chatlog__attachment-media - must be picked up too.
    html = _make_html(
        APPROVED_USER_ID, embed_image_srcs=["https://cdn.example.com/path/embedded.png"]
    )
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].image_names == ["embedded.png"]


def test_attachment_and_embedded_image_both_extracted_in_order():
    html = _make_html(
        APPROVED_USER_ID,
        image_srcs=["https://cdn.example.com/path/attached.png"],
        embed_image_srcs=["https://cdn.example.com/path/embedded.png"],
    )
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].image_names == ["attached.png", "embedded.png"]


def test_parse_message_extracts_message_id():
    message_id = _snowflake(_utc(2025, 2, 2), sequence=7)
    html = _make_html(APPROVED_USER_ID, text="hello", message_id=message_id)
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].message_id == message_id


def test_parse_message_missing_container_raises():
    html = _make_html(APPROVED_USER_ID, text="hello", message_id=None)
    with pytest.raises(ValueError):
        parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})


def test_parse_message_non_snowflake_id_raises():
    html = _make_html(APPROVED_USER_ID, text="hello", message_id="not-an-id")
    with pytest.raises(ValueError):
        parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})


@_needs_short_test_input
def test_real_export_group_straddling_start_time_is_split():
    """short_test_input.html is one message group of eight messages, sent
    16:49:29-16:51:04 UTC on 2026-09-27 (its visible timestamps say 17:49,
    local BST, with a postamble claiming UTC+0). A cutoff between the 4th
    and 5th message keeps exactly the last four."""
    html = _SHORT_TEST_INPUT.read_text(encoding="utf8")
    cutoff = int(_utc(2026, 9, 27, 16, 49, 50).timestamp())
    entries = parse_message_groups(html, cutoff, {"209767680100663296"})
    assert [e.message_id for e in entries] == [
        "1553810919195410643",
        "1553811099399364648",
        "1553811127211925556",
        "1553811226444955661",
    ]


@_needs_short_test_input
def test_real_export_with_multiple_attachments_per_message():
    """short_test_input.html is a real DiscordChatExporter export with eight
    messages from one author: two image-only messages, an image with a
    caption, a message with *two* image attachments and a caption, a
    text-only message, an embedded-image-only message (a pasted image link
    Discord unfurled - chatlog__embed/chatlog__embed-generic-image rather
    than chatlog__attachment/chatlog__attachment-media), a message pasting
    two image links (two embeds, with the links themselves as its text),
    and a message mixing one attachment and one embed - exercising find_all
    (not just find) picking up every image rather than only the first, and
    attachment and embed markup together.

    DiscordChatExporter stores an identical image once, so the same file can
    belong to more than one message: image-cc45... is in messages 6 and 7,
    image-c071... in messages 7 and 8."""
    html = _SHORT_TEST_INPUT.read_text(encoding="utf8")
    entries = parse_message_groups(html, _start_time("2024-01-01"), {"209767680100663296"})

    assert len(entries) == 8
    assert [e.image_names for e in entries] == [
        ["image-146b30e31122eeab.png"],
        ["image-2e3b24fcf40bacb4.png"],
        ["image-62475a558cc055a5.png"],
        ["image-5f4b19f240421816.png", "image-74e31a3ad4779ff9.png"],
        [],
        ["image-cc45839ef063b7ef.png"],
        ["image-cc45839ef063b7ef.png", "image-c0713fe969456d83.png"],
        ["image-a70d4a1db4cadc23.png", "image-c0713fe969456d83.png"],
    ]
    assert entries[0].text_lines == []
    assert entries[2].text_lines == ["red"]
    assert entries[3].text_lines == ["red and black"]
    assert entries[4].text_lines == ["text-only message"]
    assert entries[5].text_lines == []
    # The pasted links themselves are the message's text.
    assert len(entries[6].text_lines) == 1
    assert entries[6].text_lines[0].count("https://cdn.discordapp.com/attachments/") == 2
    assert entries[7].text_lines == []
    assert [e.message_id for e in entries][:2] == ["1553810828250058903", "1553810842804293652"]
