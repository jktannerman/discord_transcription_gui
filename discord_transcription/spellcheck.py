"""Basic English spellcheck for the review screen's editable text boxes.

A word is flagged if the English dictionary (the `pyspellchecker` package)
doesn't know it, or if it's in the user-editable blacklist
(`spellcheck_blacklist.txt`, for real words that are usually OCR misreads
here) - unless it's in the whitelist (`spellcheck_whitelist.txt`, for
Discord usernames and slang), which wins over both. It's meant to catch
obvious OCR noise, not to judge informal chat text. Spacer boxes are never
checked (SlotBoxes.build_spacer_box doesn't call into this module).
"""

import re
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from . import config, logging_config

if TYPE_CHECKING:
    from spellchecker import SpellChecker

logger = logging_config.get_logger(__name__)

_WORD_RE = re.compile(r"[A-Za-z']+")

# Words shorter than this are skipped - initials, "ok", OCR noise, etc. are
# dominated by false positives at this length.
MIN_WORD_LENGTH = 3

_checker = None  # Constructed lazily - loading the dictionary isn't free.
# {path: (file mtime_ns or None if missing, parsed words)} - see _load_wordlist.
_wordlists: Dict[Path, Tuple[Optional[int], Set[str]]] = {}

def _get_checker() -> "SpellChecker":
    """Lazily construct (and cache) the SpellChecker instance. Deferred
    rather than built at import time so importing this module - e.g. from a
    non-GUI test - never pays the dictionary-load cost unless spellchecking
    is actually used."""
    global _checker
    if _checker is None:
        from spellchecker import SpellChecker

        _checker = SpellChecker()
    return _checker


def _parse_wordlist_file(path: Path) -> Set[str]:
    """Parse a whitelist/blacklist sidecar file: one lowercased word per
    line, blank lines and lines starting with "#" ignored. A missing file
    just means an empty list, same convention as ocr_corrections.py's
    load_corrections."""
    words: Set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line.lower())
    return words


def _load_wordlist(path: Path, label: str) -> Set[str]:
    """Return a word list file's words, re-reading it only when it changed.

    Cached per path and keyed on the file's modification time, so edits to
    the whitelist/blacklist take effect on the next spellcheck without
    restarting the app, while an unchanged file costs one stat() per check.

    Args:
        path: The word list file.
        label: Its name for log lines ("whitelist"/"blacklist").

    Returns:
        The parsed, lowercased words (empty if the file is missing).
    """
    try:
        stamp: Optional[int] = path.stat().st_mtime_ns
    except OSError:
        stamp = None
    cached = _wordlists.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    words = _parse_wordlist_file(path)
    _wordlists[path] = (stamp, words)
    logger.info(
        f"loaded spellcheck {label}", extra=logging_config.extra(path=str(path), count=len(words))
    )
    return words


def _get_whitelist(path: Optional[Path] = None) -> Set[str]:
    """Return the user-editable whitelist (config.SPELLCHECK_WHITELIST_FILE by default)."""
    return _load_wordlist(path or config.SPELLCHECK_WHITELIST_FILE, "whitelist")


def _get_blacklist(path: Optional[Path] = None) -> Set[str]:
    """Return the user-editable blacklist (config.SPELLCHECK_BLACKLIST_FILE by default)."""
    return _load_wordlist(path or config.SPELLCHECK_BLACKLIST_FILE, "blacklist")


def find_misspelled_spans(text: str) -> List[Tuple[int, int]]:
    """Return (start, end) character offsets into `text` for every word that
    looks misspelled - i.e. at least MIN_WORD_LENGTH letters long, not
    ALL-CAPS (skipped as a likely acronym/abbreviation - "OCR", "GM", etc. -
    rather than a spelling mistake), not in the whitelist, and either not
    recognized by the dictionary or explicitly in the blacklist. Offsets are
    plain Python string indices (into `text` as given, real newlines and
    all) - callers translate them into Tk text-widget indices themselves
    (e.g. "1.0+{start}c")."""
    matches = [
        m
        for m in _WORD_RE.finditer(text)
        if len(m.group()) >= MIN_WORD_LENGTH and not m.group().isupper()
    ]
    if not matches:
        return []
    whitelist = _get_whitelist()
    candidates = {m.group().lower() for m in matches} - whitelist
    if not candidates:
        return []
    blacklisted = candidates & _get_blacklist()
    unknown = _get_checker().unknown(candidates) | blacklisted
    if not unknown:
        return []
    return [(m.start(), m.end()) for m in matches if m.group().lower() in unknown]
