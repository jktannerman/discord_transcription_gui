"""Post-run regex cleanup pass, originally extracted unchanged from the
original script - now trimmed down to the parts spacer slots and
ocr_corrections.py didn't replace (see clean_transcript)."""

import re

from . import config, logging_config

logger = logging_config.get_logger(__name__)


def clean_transcript(text: str) -> str:
    # Blank-line spacing (between text/images and between messages,
    # including die-roll command/result pairs) is now entirely owned by
    # the review screen's spacer slots (see review_item.ReviewItem.slot_roles
    # and ARCHITECTURE.md's "Spacer slots" section) - it used to be patched
    # up here by collapsing %roll/%draw runs and capping excess blank lines
    # to 3, which would silently clobber a spacer count the user
    # deliberately chose, so neither regex runs anymore.
    #
    # The "|" -> "I" OCR-misread fix that used to run here moved to
    # ocr_corrections.py/review_item.build_review_items, which runs on each
    # image's text individually, before the user ever sees it, rather than
    # over the whole accumulated output file (including already-finalized
    # text from past runs) every time Finalize is clicked. What's left
    # below is structural - literal "\n" artifacts and the BREAK-marker
    # bookmark - not OCR text-quality fixes, so it stays here rather than
    # moving with it.
    cleaned = re.sub(r"\\n", "", text)  # stray misformatted newlines
    cleaned = re.sub(
        rf"({re.escape(config.BREAK_MARKER)}(\s|\r|\n)*)+\Z", "", cleaned
    )  # trailing BREAK markers

    logger.info(
        "cleaned transcript",
        extra=logging_config.extra(input_chars=len(text), output_chars=len(cleaned)),
    )
    return cleaned
