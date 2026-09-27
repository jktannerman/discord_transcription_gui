"""The structural cleanup applied to a run's text at Finalize."""

import re

from . import config, logging_config

logger = logging_config.get_logger(__name__)


def clean_transcript(existing: str, added: str) -> str:
    """Join this run's text onto the existing output, cleaned up.

    Stray literal "\n" sequences are removed from this run's text only
    (past runs' text is never rewritten), and trailing BREAK markers are
    trimmed from the end of the result, so an empty run doesn't leave a
    second, empty bookmark. Blank-line spacing is set by the spacer boxes
    (docs/ARCHITECTURE_SPACER_SLOTS.md), and OCR misreads are fixed earlier
    by ocr_corrections.py.

    Args:
        existing: The output file's current contents.
        added: This run's rendered text.

    Returns:
        The new output contents, before the fresh BREAK marker is appended.
    """
    cleaned_added = re.sub(r"\\n", "", added)  # stray misformatted newlines
    cleaned = re.sub(
        rf"({re.escape(config.BREAK_MARKER)}(\s|\r|\n)*)+\Z", "", existing + cleaned_added
    )  # trailing BREAK markers

    logger.info(
        "cleaned transcript",
        extra=logging_config.extra(
            existing_chars=len(existing), added_chars=len(added), output_chars=len(cleaned)
        ),
    )
    return cleaned
