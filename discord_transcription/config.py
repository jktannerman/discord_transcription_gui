"""Configuration constants for the GUI transcription tool.

These mirror the hardcoded values in the original
``discord_tesseract_transcription_v3.py`` script. File paths stay as plain
constants for now (not exposed in the GUI) per the agreed v1 scope; they are
isolated here so a future settings screen only needs to change this one
module. The approved-author allow-list is no longer one of these constants -
it's entered and cached from the setup screen instead (see
state.read_approved_users_state/save_approved_users_state).
DEFAULT_APPROVED_USERS below is its fallback for a genuinely first-ever
run, before anything has been cached yet - empty by default, since this is
shared, version-controlled code and shouldn't ship with anyone's real
Discord user IDs baked in; the project owner can fill in their own
go-to IDs (e.g. the GM and the dice-roller bot) locally if they want the
approved-users box pre-filled on a fresh install.
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

DEFAULT_APPROVED_USERS: tuple[str, ...] = tuple()

BREAK_MARKER = "[BREAK]"

# User-editable regex find/replace rules for common OCR misreads (see
# ocr_corrections.py) - kept next to the source rather than in
# APP_DATA_DIR, since it's app config the project owner seeds and tunes
# over time (and is sensible to keep under version control), not per-user
# runtime state.
OCR_CORRECTIONS_FILE = Path(__file__).resolve().parent / "ocr_corrections.txt"

# User-editable list of words the review screen's spellcheck (see
# spellcheck.py) should never flag - one word per line, blank lines and
# lines starting with "#" ignored - for Discord usernames/slang/jargon that
# would otherwise be (mis)flagged on every box that contains them. Kept next
# to the source for the same reason OCR_CORRECTIONS_FILE is.
SPELLCHECK_WHITELIST_FILE = Path(__file__).resolve().parent / "spellcheck_whitelist.txt"

# User-editable list of words the review screen's spellcheck should always
# flag - one word per line, same file format as SPELLCHECK_WHITELIST_FILE -
# for words the dictionary treats as real (so they'd never be flagged
# otherwise) but that are usually OCR misreads/typos in this transcript
# context (e.g. a common word that's frequently confused with a
# similar-looking Discord username). Kept next to the source for the same
# reason OCR_CORRECTIONS_FILE is. If a word appears in both files, the
# whitelist wins - it stays unflagged.
SPELLCHECK_BLACKLIST_FILE = Path(__file__).resolve().parent / "spellcheck_blacklist.txt"

APP_DATA_DIR = Path.home() / ".discord_transcription_gui"
RUN_DATE_FILE = APP_DATA_DIR / "run_dates.json"
# {"version": 2, "folders": {image_folder: {image_name: entry}}} - see
# state.load_cache for the entry shape. One entry per image folder, kept
# indefinitely so OCR'ing one chatlog's images never evicts another
# chatlog's already-OCR'd cache.
OCR_CACHE_FILE = APP_DATA_DIR / "ocr_cache.json"
RECENT_PATHS_FILE = APP_DATA_DIR / "recent_paths.json"
APPROVED_USERS_STATE_FILE = APP_DATA_DIR / "approved_users.json"
# {html_path: session_dict} - one in-progress review session per chatlog,
# kept indefinitely until that specific chatlog's run is finalized, so two
# different chatlogs can each be partially transcribed and resumed
# independently of one another.
SESSIONS_FILE = APP_DATA_DIR / "sessions.json"
# {html_path: [session_dict, ...]} - up to SESSION_BACKUP_COUNT end-of-
# session snapshots per chatlog, most-recent-first (see
# state.archive_session_backup). Separate from SESSIONS_FILE's own
# write-time .bak (which only ever holds the single immediately-previous
# write and is itself overwritten by the very next autosave tick) - this
# file instead preserves whatever a session actually looked like the last
# few times it stopped being the live, in-progress one.
SESSION_BACKUPS_FILE = APP_DATA_DIR / "session_backups.json"
SESSION_BACKUP_COUNT = 3
# {html_path: {message_id: {role: text}}} - user-edited transcriptions written
# at Finalize, kept indefinitely per message (keyed by Discord message_id so
# they survive a re-export with new messages inserted anywhere). Only non-None
# role values are stored; spacer edits and content edits are both included.
# Merged on each Finalize (never deleted) so unchecked/untouched boxes at
# finalize time leave prior stored edits intact.
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
# Separate, much higher-frequency stream for the review screen's per-scroll-
# tick tracing (reconcile/debounce/remeasure/image-load events) - kept out of
# LOG_FILE so lifecycle events (session save/load, OCR batch, errors) stay
# readable on their own, and so this stream can rotate independently without
# evicting those - see logging_config.setup_logging.
SCROLL_TRACE_LOG_FILE = APP_DATA_DIR / "scroll_trace.log"
# Rotation budget for SCROLL_TRACE_LOG_FILE (10MB x 3 backups, about 40MB
# in all - several busy review sessions), and a switch to turn it off
# entirely.
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
# "%draw 1 20") - its result is assumed to be the very next approved
# message, so spacer defaults treat the pair as one continuous block
# rather than separating them like a normal message - see
# review_item.build_review_items and docs/ARCHITECTURE_SPACER_SLOTS.md.
DICE_COMMAND_RE = re.compile(r"^%roll \d*(d|l|h)\d+|^%draw \d+ \d+")

# Default blank-line counts a spacer slot is pre-filled with (see
# docs/ARCHITECTURE_SPACER_SLOTS.md for the full table this
# implements) - the literal "\n" token count written into a spacer box is
# always one more than the empty-line count, since the gap also includes
# the newline that terminates the line right before it.
EMPTY_LINES_NORMAL = 3
EMPTY_LINES_TEXT_TO_IMAGE = 1
EMPTY_LINES_BETWEEN_IMAGES = 2
EMPTY_LINES_DICE_COMMAND_TO_RESULT = 0
EMPTY_LINES_RESULT_TO_NEXT_COMMAND = 1
