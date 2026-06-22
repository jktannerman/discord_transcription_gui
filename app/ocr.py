"""OCR backend for image transcription.

Tesseract is the only active backend, but the call is routed through
``transcribe_image`` and the ``_BACKENDS`` dict so an EasyOCR implementation
(abandoned in the original script, but possibly revived later) could be
added as a second entry without changing any calling code.
"""

import pytesseract

from . import config, logging_config

logger = logging_config.get_logger(__name__)

pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD


def _tesseract_read(file_path: str) -> str:
    return pytesseract.image_to_string(file_path)


_BACKENDS = {
    "tesseract": _tesseract_read,
}

DEFAULT_BACKEND = "tesseract"


def transcribe_image(file_path: str, backend: str = DEFAULT_BACKEND) -> str:
    """Run OCR on file_path using the named backend and return raw text."""
    logger.debug("running OCR", extra=logging_config.extra(file_path=file_path, backend=backend))
    try:
        text = _BACKENDS[backend](file_path)
    except Exception:
        logger.exception(
            "OCR failed", extra=logging_config.extra(file_path=file_path, backend=backend)
        )
        raise
    logger.debug(
        "OCR complete", extra=logging_config.extra(file_path=file_path, char_count=len(text))
    )
    return text


def split_into_paragraphs(raw_text: str) -> list[str]:
    """Split raw OCR output into paragraphs.

    Blank lines (``\\n\\n``) separate paragraphs; single newlines within a
    paragraph are treated as wrapped text and collapsed to spaces.
    """
    paragraphs = (
        raw_text.replace("\n\n", " %10 %10")
        .replace("\n", " ")
        .replace("  ", " ")
        .split("%10 %10")
    )
    logger.debug("split OCR text into paragraphs", extra=logging_config.extra(count=len(paragraphs)))
    return paragraphs
