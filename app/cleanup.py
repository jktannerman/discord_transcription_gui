"""Post-run regex cleanup pass, extracted unchanged from the original script."""

import re

from . import config, logging_config

logger = logging_config.get_logger(__name__)


def clean_transcript(text: str) -> str:
    cleaned = re.sub(r"\|", "I", text)  # usually mistranscribed
    cleaned = re.sub(r"(%roll .*?)((\s)*?\r|\n)+", r"\1\n", cleaned)  # excess newlines
    cleaned = re.sub(r"(%draw .*?)((\s)*?\r|\n)+", r"\1\n", cleaned)  # future-proofing
    cleaned = re.sub(r"([ \t]*\n){4,}", "\n\n\n\n", cleaned)  # excess newlines
    cleaned = re.sub(r"\\n", "", cleaned)  # stray misformatted newlines
    cleaned = re.sub(
        rf"({re.escape(config.BREAK_MARKER)}(\s|\r|\n)*)+\Z", "", cleaned
    )  # trailing BREAK markers

    logger.info(
        "cleaned transcript",
        extra=logging_config.extra(input_chars=len(text), output_chars=len(cleaned)),
    )
    return cleaned
