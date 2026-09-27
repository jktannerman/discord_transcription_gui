import datetime
from pathlib import Path
from typing import Optional

import pytest

from discord_transcription.chatlog import parse_message_groups

_EXAMPLE_INPUTS = Path(__file__).resolve().parent.parent / "example_inputs"

APPROVED_USER_ID = "130636614807322624"


DEFAULT_MESSAGE_ID = "100000000000000001"


def _make_html(
    timestamp: str,
    user_id: str,
    text: str | None = None,
    image_src: str | None = None,
    image_srcs: Optional[list[str]] = None,
    embed_image_srcs: Optional[list[str]] = None,
    timezone: Optional[str] = "UTC+0",
    message_id: Optional[str] = DEFAULT_MESSAGE_ID,
) -> str:
    text_html = (
        f'<div class="chatlog__markdown-preserve">{text}</div>' if text is not None else ""
    )
    # image_src is a convenience for the single-image case; image_srcs (a
    # list) is for messages with more than one attachment, each in its own
    # chatlog__attachment block, mirroring the real export's markup.
    srcs = image_srcs if image_srcs is not None else ([image_src] if image_src is not None else [])
    image_html = "".join(
        f'<div class="chatlog__attachment">'
        f'<img class="chatlog__attachment-media" src="{src}">'
        f"</div>"
        for src in srcs
    )
    # embed_image_srcs mirrors a pasted image URL/link Discord unfurled -
    # markup DiscordChatExporter renders as chatlog__embed /
    # chatlog__embed-generic-image rather than chatlog__attachment /
    # chatlog__attachment-media.
    embed_srcs = embed_image_srcs if embed_image_srcs is not None else []
    embed_html = "".join(
        f'<div class="chatlog__embed">'
        f'<img class="chatlog__embed-generic-image" src="{src}">'
        f"</div>"
        for src in embed_srcs
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
        {embed_html}
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
    assert entries[0].image_names == []


def test_image_attached_message_extracts_filename():
    html = _make_html(
        "01/01/2025 00:00",
        APPROVED_USER_ID,
        image_src="https://cdn.example.com/path/my_image.png",
    )
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].image_names == ["my_image.png"]


def test_image_filename_url_decoded():
    html = _make_html(
        "01/01/2025 00:00",
        APPROVED_USER_ID,
        image_src="https://cdn.example.com/path/my%20image.png",
    )
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert entries[0].image_names == ["my image.png"]


def test_message_with_multiple_images_extracts_all_filenames_in_order():
    html = _make_html(
        "01/01/2025 00:00",
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
        "01/01/2025 00:00",
        APPROVED_USER_ID,
        embed_image_srcs=["https://cdn.example.com/path/embedded.png"],
    )
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].image_names == ["embedded.png"]


def test_attachment_and_embedded_image_both_extracted_in_order():
    html = _make_html(
        "01/01/2025 00:00",
        APPROVED_USER_ID,
        image_srcs=["https://cdn.example.com/path/attached.png"],
        embed_image_srcs=["https://cdn.example.com/path/embedded.png"],
    )
    entries = parse_message_groups(html, _start_time("2024-01-01"), {APPROVED_USER_ID})
    assert len(entries) == 1
    assert entries[0].image_names == ["attached.png", "embedded.png"]


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
    html = (_EXAMPLE_INPUTS / "short_test_input.html").read_text(encoding="utf8")
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
