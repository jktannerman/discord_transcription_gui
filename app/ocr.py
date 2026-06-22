"""OCR backend for image transcription.

Tesseract is the only active backend, but the call is routed through
``transcribe_image`` and the ``_BACKENDS`` dict so an EasyOCR implementation
(abandoned in the original script, but possibly revived later) could be
added as a second entry without changing any calling code.
"""

import pytesseract

from . import config

pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD


def _tesseract_read(file_path: str) -> str:
    return pytesseract.image_to_string(file_path)


_BACKENDS = {
    "tesseract": _tesseract_read,
}

DEFAULT_BACKEND = "tesseract"


def transcribe_image(file_path: str, backend: str = DEFAULT_BACKEND) -> str:
    """Run OCR on file_path using the named backend and return raw text."""
    return _BACKENDS[backend](file_path)


def split_into_paragraphs(raw_text: str) -> list[str]:
    """Split raw OCR output into paragraphs.

    Blank lines (``\\n\\n``) separate paragraphs; single newlines within a
    paragraph are treated as wrapped text and collapsed to spaces.
    """
    return (
        raw_text.replace("\n\n", " %10 %10")
        .replace("\n", " ")
        .replace("  ", " ")
        .split("%10 %10")
    )
