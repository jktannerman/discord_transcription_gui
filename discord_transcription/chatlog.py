"""Parsing of the DiscordChatExporter HTML export.

Mirrors the filtering logic in the original script: only messages from an
approved set of author IDs (entered on the setup screen, not hardcoded -
see pipeline.parse_approved_user_ids), sent at or after ``start_time``, are
kept. Each accepted message is reduced to its Discord message ID, plain
text lines, and the filenames of any attached images (a message can have
more than one, each either an uploaded ``chatlog__attachment`` or a
Discord-unfurled ``chatlog__embed`` image link) - OCR and the interactive
correction step are handled separately in pipeline.py. The message ID is
Discord's own stable per-message identifier (read from the export's
``data-message-id`` attribute), kept so a resumed review session can match
its saved edits back onto the right message even if a later re-export of
the same chatlog appends or inserts messages and shifts every list
position.

Each message's time also comes from its ID: a Discord ID is a "snowflake"
whose top bits are the creation time in milliseconds since the Discord
epoch. That is exact UTC for every message individually. The export's
visible timestamps aren't used: they're rounded to the minute, formatted
per locale, shown only once per message group (a run of consecutive
messages by one author), and written in the exporting device's local time,
with a postamble offset that leaves out daylight saving time.
"""

from dataclasses import dataclass
from typing import Optional

from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag

from . import logging_config

logger = logging_config.get_logger(__name__)

# Discord's snowflake epoch (2015-01-01T00:00:00Z), in Unix milliseconds.
DISCORD_EPOCH_MS = 1_420_070_400_000
# A snowflake's low 22 bits are worker/process/sequence numbers; the rest is
# the timestamp.
_SNOWFLAKE_TIMESTAMP_SHIFT = 22


@dataclass
class MessageEntry:
    message_id: str
    text_lines: list[str]
    image_names: list[str]


def snowflake_timestamp_ms(message_id: str) -> int:
    """The Unix time (ms, UTC) a Discord message was sent, from its ID.

    Args:
        message_id: The message's Discord ID (a decimal snowflake).

    Returns:
        Milliseconds since the Unix epoch.

    Raises:
        ValueError: If message_id isn't a non-negative integer.
    """
    try:
        snowflake = int(message_id)
    except ValueError:
        snowflake = -1
    if snowflake < 0:
        raise ValueError(f"Message ID {message_id!r} isn't a Discord snowflake ID.")
    return (snowflake >> _SNOWFLAKE_TIMESTAMP_SHIFT) + DISCORD_EPOCH_MS


def parse_message_groups(
    html_text: str, start_time: int, approved_author_ids: Optional[set[str]]
) -> list[MessageEntry]:
    """Return the approved messages sent at or after start_time.

    Args:
        html_text: The export's HTML.
        start_time: Unix time (seconds, UTC); earlier messages are skipped.
            Checked per message, so a message group that straddles it is
            split rather than kept or dropped whole.
        approved_author_ids: Discord user IDs to keep messages from, or None
            to keep every author's (the "all users" mode).

    Returns:
        The kept messages, in export order.

    Raises:
        ValueError: If a message has no data-message-id, or one that isn't
            a snowflake ID.
    """
    parsed_html = BeautifulSoup(html_text, features="lxml")
    groups = parsed_html.find_all(attrs={"class": "chatlog__message-group"})
    start_time_ms = start_time * 1000

    skip_counts = {"too_early": 0, "no_author": 0, "unapproved_author": 0}
    entries: list[MessageEntry] = []

    for group in groups:
        author_element = group.find(attrs={"class": "chatlog__author"})
        if author_element is None:
            skip_counts["no_author"] += 1
            continue
        if (
            approved_author_ids is not None
            and author_element.attrs.get("data-user-id") not in approved_author_ids
        ):
            skip_counts["unapproved_author"] += 1
            continue

        for message in group.find_all(attrs={"class": "chatlog__message-primary"}):
            message_id = _message_id(message)
            if snowflake_timestamp_ms(message_id) < start_time_ms:
                skip_counts["too_early"] += 1
                continue
            entries.append(_parse_message(message, message_id))

    logger.info(
        "parsed chatlog HTML",
        extra=logging_config.extra(
            group_count=len(groups), entries_kept=len(entries), **skip_counts
        ),
    )
    return entries


def _message_id(message: Tag) -> str:
    """The Discord ID of the message whose chatlog__message-primary is `message`.

    Raises:
        ValueError: If its chatlog__message-container, or that container's
            data-message-id, is missing.
    """
    container = message.find_parent(attrs={"class": "chatlog__message-container"})
    if container is None or "data-message-id" not in container.attrs:
        raise ValueError(
            "Could not find a chatlog__message-container with a "
            "data-message-id for this message - the export HTML may be "
            "from an unsupported DiscordChatExporter version."
        )
    return container.attrs["data-message-id"]


def _parse_message(message: Tag, message_id: str) -> MessageEntry:
    text = message.find(attrs={"class": "chatlog__markdown-preserve"})
    text_lines = text.get_text().split("\n") if text else []

    # find_all, not find: a message can have more than one image, each in
    # its own chatlog__attachment (an uploaded file) or chatlog__embed (a
    # pasted image URL/link that Discord unfurled - chatlog__embed-generic-image)
    # block. Passing both class names to a single find_all keeps them in
    # the document order they actually appear in.
    images = message.find_all(
        "img", class_=["chatlog__attachment-media", "chatlog__embed-generic-image"]
    )
    image_names = []
    for image in images:
        file_name = image.attrs["src"]
        # Decode after splitting off the basename, not before - a literal
        # "%2F" in a path segment shouldn't be misread as a "/" separator.
        image_names.append(unquote(file_name.split("/")[-1]))

    logger.debug(
        "parsed message",
        extra=logging_config.extra(
            message_id=message_id, line_count=len(text_lines), image_count=len(image_names)
        ),
    )
    return MessageEntry(message_id=message_id, text_lines=text_lines, image_names=image_names)
