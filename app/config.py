"""Configuration constants for the GUI transcription tool.

These mirror the hardcoded values in the original
``discord_tesseract_transcription_v3.py`` script. The author allow-list and
file paths stay as plain constants for now (not exposed in the GUI) per the
agreed v1 scope; they are isolated here so a future settings screen only
needs to change this one module.
"""

from pathlib import Path

TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# File extensions DiscordChatExporter downloads alongside media that are
# never image attachments worth OCR'ing.
SKIP_TYPES: tuple[str, ...] = (".svg", ".woff2", ".js", ".css")

# Discord user IDs whose messages are included in the transcript
# (the GM and the dice-roller bot).
APPROVED_AUTHOR_IDS: tuple[str, ...] = (
    "130636614807322624",
    "567170431413387265",
)

BREAK_MARKER = "[BREAK]"

APP_DATA_DIR = Path.home() / ".discord_transcription_gui"
RUN_DATE_FILE = APP_DATA_DIR / "run_dates.json"
OCR_CACHE_FILE = APP_DATA_DIR / "ocr_cache.json"
LOG_FILE = APP_DATA_DIR / "app.log"

TIMESTAMP_FORMAT = "%d/%m/%Y %H:%M"
