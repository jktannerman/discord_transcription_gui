"""Parsing of the DiscordChatExporter HTML export.

Mirrors the filtering logic in the original script: only messages from an
approved set of author IDs (entered on the setup screen, not hardcoded -
see pipeline.parse_approved_user_ids), timestamped after ``start_time``, are
kept. Each accepted message is reduced to its Discord message ID, plain
text lines, and the filenames of any attached images (a message can have
more than one, each in its own ``chatlog__attachment`` block) - OCR and
the interactive correction step are handled separately in pipeline.py. The
message ID is Discord's own stable per-message identifier (read from the
export's ``data-message-id`` attribute), kept so a resumed review session
can match its saved edits back onto the right message even if a later
re-export of the same chatlog appends or inserts messages and shifts every
list position.

DiscordChatExporter timestamps every message in the *exporting device's*
local timezone by default (not UTC, and not necessarily this machine's
timezone either) - but it also records exactly which offset that was as a
"Timezone: UTC+H[:MM]" line in the export's postamble (see
_parse_export_timezone). Reading that explicit offset, rather than assuming
either UTC or whatever timezone happens to be running this tool, is what
lets every message timestamp be converted to a true UTC epoch second
unambiguously - matching how pipeline.parse_start_date/finalize_run already
treat start/end dates as UTC.
"""

import datetime
import re
from dataclasses import dataclass
from typing import Optional

from urllib.parse import unquote

from bs4 import BeautifulSoup

from . import config, logging_config

logger = logging_config.get_logger(__name__)

_TIMEZONE_RE = re.compile(r"UTC([+-])(\d{1,2})(?::?(\d{2}))?")


@dataclass
class MessageEntry:
    message_id: str
    text_lines: list[str]
    image_names: list[str]


def _parse_export_timezone(parsed_html: BeautifulSoup) -> datetime.timezone:
    """The offset every timestamp in this export is recorded in, read from
    its postamble's "Timezone: UTC+H[:MM]" line - see the module docstring
    for why this can't just be assumed to be UTC or the local machine's own
    timezone. Raises ValueError (naming the problem, not a silent guess) if
    the postamble is missing or doesn't contain a recognizable offset."""
    for entry in parsed_html.find_all(attrs={"class": "postamble__entry"}):
        text = entry.get_text()
        if not text.startswith("Timezone:"):
            continue
        match = _TIMEZONE_RE.search(text)
        if not match:
            raise ValueError(f"Could not parse export timezone from {text!r}.")
        sign = 1 if match.group(1) == "+" else -1
        hours, minutes = int(match.group(2)), int(match.group(3) or 0)
        return datetime.timezone(sign * datetime.timedelta(hours=hours, minutes=minutes))

    raise ValueError(
        "Could not find this export's timezone (a \"Timezone: UTC...\" line "
        "in the chatlog's postamble) - DiscordChatExporter records every "
        "message's timestamp in the exporting device's local timezone by "
        "default, so this is needed to interpret them correctly."
    )


def parse_message_groups(
    html_text: str, start_time: int, approved_author_ids: Optional[set[str]]
) -> list[MessageEntry]:
    """Return the approved, post-start_time messages from the export.

    approved_author_ids is the set of Discord user IDs to keep messages
    from, or None to keep messages from every author (the "all users" mode).
    """
    parsed_html = BeautifulSoup(html_text, features="lxml")
    export_tz = _parse_export_timezone(parsed_html)
    groups = parsed_html.find_all(attrs={"class": "chatlog__message-group"})

    skip_counts = {"no_date": 0, "too_early": 0, "no_author": 0, "unapproved_author": 0}
    entries: list[MessageEntry] = []

    for group in groups:
        date_element = group.find(attrs={"class": "chatlog__timestamp"})
        if date_element is None:
            skip_counts["no_date"] += 1
            continue

        raw_message_time = datetime.datetime.strptime(
            date_element.get_text(), config.TIMESTAMP_FORMAT
        )
        message_time = int(raw_message_time.replace(tzinfo=export_tz).timestamp())
        if message_time < start_time:
            skip_counts["too_early"] += 1
            continue

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

        messages = group.find_all(attrs={"class": "chatlog__message-primary"})
        for message in messages:
            entries.append(_parse_message(message))

    logger.info(
        "parsed chatlog HTML",
        extra=logging_config.extra(
            group_count=len(groups), entries_kept=len(entries), **skip_counts
        ),
    )
    return entries


def _parse_message(message) -> MessageEntry:
    container = message.find_parent(attrs={"class": "chatlog__message-container"})
    if container is None or "data-message-id" not in container.attrs:
        raise ValueError(
            "Could not find a chatlog__message-container with a "
            "data-message-id for this message - the export HTML may be "
            "from an unsupported DiscordChatExporter version."
        )
    message_id = container.attrs["data-message-id"]

    text = message.find(attrs={"class": "chatlog__markdown-preserve"})
    text_lines = text.get_text().split("\n") if text else []

    # find_all, not find: a message can have more than one attachment, each
    # in its own chatlog__attachment block with its own
    # chatlog__attachment-media img - using find() here used to silently
    # keep only the first and drop the rest.
    images = message.find_all(attrs={"class": "chatlog__attachment-media"})
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
