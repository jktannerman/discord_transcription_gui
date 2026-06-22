"""Parsing of the DiscordChatExporter HTML export.

Mirrors the filtering logic in the original script: only messages from
``config.APPROVED_AUTHOR_IDS``, timestamped after ``start_time``, are kept.
Each accepted message is reduced to its plain text lines plus the filename
of any attached image (if present) — OCR and the interactive correction step
are handled separately in pipeline.py.
"""

import datetime
import time
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

from . import config, logging_config

logger = logging_config.get_logger(__name__)


@dataclass
class MessageEntry:
    text_lines: list[str]
    image_name: Optional[str]


def parse_message_groups(html_text: str, start_time: int) -> list[MessageEntry]:
    """Return the approved, post-start_time messages from the export."""
    parsed_html = BeautifulSoup(html_text, features="lxml")
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
        message_time = int(time.mktime(raw_message_time.timetuple()))
        if message_time < start_time:
            skip_counts["too_early"] += 1
            continue

        author_element = group.find(attrs={"class": "chatlog__author"})
        if author_element is None:
            skip_counts["no_author"] += 1
            continue
        if author_element.attrs.get("data-user-id") not in config.APPROVED_AUTHOR_IDS:
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
    text = message.find(attrs={"class": "chatlog__markdown-preserve"})
    text_lines = text.get_text().split("\n") if text else []

    image = message.find(attrs={"class": "chatlog__attachment-media"})
    image_name = None
    if image:
        file_name = image.attrs["src"]
        image_name = file_name.split("/")[-1]

    logger.debug(
        "parsed message",
        extra=logging_config.extra(line_count=len(text_lines), image_name=image_name),
    )
    return MessageEntry(text_lines=text_lines, image_name=image_name)
