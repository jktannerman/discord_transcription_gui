"""Configuration constants for the GUI transcription tool.

Not exposed in the GUI yet; kept in this one module so a future settings
screen only needs to change it. The approved-users list is entered on the
setup screen instead (see state.read_approved_users_state).
"""

import re
import sys
from pathlib import Path

# On Windows, Tesseract isn't normally on PATH, so pytesseract is pointed at
# the default install location explicitly. On other platforms (e.g. Linux,
# where it's installed via the system package manager) it's expected to
# already be on PATH, so pytesseract is left to resolve "tesseract" itself.
TESSERACT_CMD = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if sys.platform == "win32"
    else "tesseract"
)

# How many newly OCR'd images to process between cache saves, so closing or
# crashing mid-batch loses at most this many images' worth of OCR.
OCR_CACHE_SAVE_EVERY = 10

# Pre-fills the approved-users box on a first run, before any list has been
# saved. Empty so no real Discord IDs are committed; fill in locally if wanted.
DEFAULT_APPROVED_USERS: tuple[str, ...] = tuple()

BREAK_MARKER = "[BREAK]"

# User-editable regex find/replace rules for common OCR misreads (see
# ocr_corrections.py). Kept next to the source, under version control,
# rather than in APP_DATA_DIR, as are the two spellcheck word lists below.
OCR_CORRECTIONS_FILE = Path(__file__).resolve().parent / "ocr_corrections.txt"

# Words the review screen's spellcheck (see spellcheck.py) never flags, for
# Discord usernames and slang: one word per line, blank lines and lines
# starting with "#" ignored.
SPELLCHECK_WHITELIST_FILE = Path(__file__).resolve().parent / "spellcheck_whitelist.txt"

# Words the spellcheck always flags, although the dictionary knows them,
# because here they're usually OCR misreads or typos. Same format as the
# whitelist; a word in both files is not flagged.
SPELLCHECK_BLACKLIST_FILE = Path(__file__).resolve().parent / "spellcheck_blacklist.txt"

APP_DATA_DIR = Path.home() / ".discord_transcription_gui"
# {html_path: [end date, ...]} - each chatlog's finalized run-end dates,
# oldest first; the last one pre-fills that chatlog's next start date.
RUN_DATE_FILE = APP_DATA_DIR / "run_dates.json"
# {"version": 2, "folders": {image_folder: {image_name: entry}}} - see
# state.load_cache for the entry shape. Kept indefinitely for every image
# folder.
OCR_CACHE_FILE = APP_DATA_DIR / "ocr_cache.json"
RECENT_PATHS_FILE = APP_DATA_DIR / "recent_paths.json"
APPROVED_USERS_STATE_FILE = APP_DATA_DIR / "approved_users.json"
# {html_path: session_dict} - one in-progress review session per chatlog,
# kept until that chatlog's run is finalized.
SESSIONS_FILE = APP_DATA_DIR / "sessions.json"
# {html_path: [session_dict, ...]} - up to SESSION_BACKUP_COUNT end-of-
# session snapshots per chatlog, most-recent-first (see
# state.archive_session_backup). SESSIONS_FILE's own .bak holds only the
# previous autosave, so it can't do this.
SESSION_BACKUPS_FILE = APP_DATA_DIR / "session_backups.json"
SESSION_BACKUP_COUNT = 3
# {html_path: {message_id: {role: text}}} - user-edited transcriptions written
# at Finalize, spacer boxes included, and merged into on each later Finalize
# (see session.build_finalized_updates). Keyed by Discord message_id so they
# survive a re-export with messages inserted anywhere.
FINALIZED_EDITS_FILE = APP_DATA_DIR / "finalized_edits.json"
# [{html_path, message_id, role, old_text, new_text, replaced_at}, ...] -
# append-only record of every stored finalized edit that a later Finalize
# replaced or removed (see state.save_finalized_edits), so an edit is never
# lost for good even if it was removed by mistake.
FINALIZED_EDITS_HISTORY_FILE = APP_DATA_DIR / "finalized_edits_history.json"
# {html_path: fraction} - the review screen's image column width as a share
# of the review area's width, as last dragged for each chatlog (see
# gui/column_divider.py).
IMAGE_COLUMN_WIDTHS_FILE = APP_DATA_DIR / "image_column_widths.json"
LOG_FILE = APP_DATA_DIR / "app.log"
# The review screen's high-frequency scroll and text-box tracing, kept out
# of LOG_FILE so it can't crowd out or rotate away the lifecycle events
# there - see logging_config.setup_logging.
SCROLL_TRACE_LOG_FILE = APP_DATA_DIR / "scroll_trace.log"
# Rotation budget for SCROLL_TRACE_LOG_FILE (10MB x 3 backups), and a
# switch to turn it off.
SCROLL_TRACE_ENABLED = True
SCROLL_TRACE_MAX_BYTES = 10_000_000
SCROLL_TRACE_BACKUP_COUNT = 3
# Level for LOG_FILE ("DEBUG", "INFO", "WARNING", ...). Overridable for a
# single launch via the LOG_LEVEL_ENV_VAR environment variable, e.g.
# DISCORD_TRANSCRIPTION_LOG_LEVEL=DEBUG for per-image OCR detail.
LOG_LEVEL = "INFO"
# The console only shows problems; everything else is in LOG_FILE.
CONSOLE_LOG_LEVEL = "WARNING"
LOG_LEVEL_ENV_VAR = "DISCORD_TRANSCRIPTION_LOG_LEVEL"
# Held (OS-level file lock) for as long as the app runs, so a second copy
# can't start and silently overwrite the first one's saved state.
INSTANCE_LOCK_FILE = APP_DATA_DIR / "app.lock"

# How many previously-used values to keep, per setup-screen field, for the
# dropdown history (most-recently-used first).
MAX_RECENT_PATHS = 8

# How often the review screen's in-progress edits/scroll position/focus are
# autosaved to SESSIONS_FILE, so a session can be resumed after closing the
# app mid-review.
AUTOSAVE_INTERVAL_MS = 5000

# A message matching this is a die-roll command (e.g. "%roll 2d6",
# "%draw 1 20"). Its result is taken to be the next approved message, and
# the spacer defaults keep the pair together - see
# docs/ARCHITECTURE_SPACER_SLOTS.md.
DICE_COMMAND_RE = re.compile(r"^%roll \d*(d|l|h)\d+|^%draw \d+ \d+")

# Default empty-line counts for spacer boxes (see
# docs/ARCHITECTURE_SPACER_SLOTS.md). A spacer box holds one more "\n"
# token than this, for the newline ending the line before the gap.
EMPTY_LINES_NORMAL = 3
EMPTY_LINES_TEXT_TO_IMAGE = 1
EMPTY_LINES_BETWEEN_IMAGES = 2
EMPTY_LINES_DICE_COMMAND_TO_RESULT = 0
EMPTY_LINES_RESULT_TO_NEXT_COMMAND = 1
