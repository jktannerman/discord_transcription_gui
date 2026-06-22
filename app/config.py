"""Configuration constants for the GUI transcription tool.

These mirror the hardcoded values in the original
``discord_tesseract_transcription_v3.py`` script. File paths stay as plain
constants for now (not exposed in the GUI) per the agreed v1 scope; they are
isolated here so a future settings screen only needs to change this one
module. The approved-author allow-list is no longer one of these constants -
it's entered and cached from the setup screen instead (see
state.read_approved_users_state/save_approved_users_state) - but the IDs
below (the GM and the dice-roller bot) are still used as the very first
run's default content, before anything has been cached yet.
"""

from pathlib import Path

TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# File extensions DiscordChatExporter downloads alongside media that are
# never image attachments worth OCR'ing.
SKIP_TYPES: tuple[str, ...] = (".svg", ".woff2", ".js", ".css")

DEFAULT_APPROVED_USERS: tuple[str, ...] = tuple()

BREAK_MARKER = "[BREAK]"

APP_DATA_DIR = Path.home() / ".discord_transcription_gui"
RUN_DATE_FILE = APP_DATA_DIR / "run_dates.json"
# {image_folder: {image_name: [paragraphs]}} - one entry per image folder,
# kept indefinitely so OCR'ing one chatlog's images never evicts another
# chatlog's already-OCR'd cache.
OCR_CACHE_FILE = APP_DATA_DIR / "ocr_cache.json"
RECENT_PATHS_FILE = APP_DATA_DIR / "recent_paths.json"
APPROVED_USERS_STATE_FILE = APP_DATA_DIR / "approved_users.json"
# {html_path: session_dict} - one in-progress review session per chatlog,
# kept indefinitely until that specific chatlog's run is finalized, so two
# different chatlogs can each be partially transcribed and resumed
# independently of one another.
SESSIONS_FILE = APP_DATA_DIR / "sessions.json"
LOG_FILE = APP_DATA_DIR / "app.log"

# How many previously-used values to keep, per setup-screen field, for the
# dropdown history (most-recently-used first).
MAX_RECENT_PATHS = 8

# How often the review screen's in-progress edits/scroll position/focus are
# autosaved to SESSIONS_FILE, so a session can be resumed after closing the
# app mid-review.
AUTOSAVE_INTERVAL_MS = 5000

TIMESTAMP_FORMAT = "%d/%m/%Y %H:%M"
