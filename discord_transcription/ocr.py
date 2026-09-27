"""OCR backend for image transcription.

Tesseract is the only active backend, but the call is routed through
``transcribe_image`` and the ``_BACKENDS`` dict so an EasyOCR implementation
(abandoned in the original script, but possibly revived later) could be
added as a second entry without changing any calling code.
"""

import re

import pytesseract

from . import config, logging_config

logger = logging_config.get_logger(__name__)

# Same blank-line-separated-block convention as ocr_corrections.py's
# _BLOCK_SEPARATOR_RE - a run of whitespace containing at least one blank
# line marks a paragraph break.
_PARAGRAPH_SEPARATOR_RE = re.compile(r"\n\s*\n")
_INTERNAL_WHITESPACE_RE = re.compile(r"\s+")

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

    Blank lines separate paragraphs; any other run of whitespace within a
    paragraph (a wrapped single newline, a run of spaces, ...) is collapsed
    to a single space.
    """
    paragraphs = [
        _INTERNAL_WHITESPACE_RE.sub(" ", para)
        for para in _PARAGRAPH_SEPARATOR_RE.split(raw_text)
    ]
    logger.debug("split OCR text into paragraphs", extra=logging_config.extra(count=len(paragraphs)))
    return paragraphs
