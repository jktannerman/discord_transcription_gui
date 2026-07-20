"""Basic English spellcheck for the review screen's editable text boxes.

Flags a word as potentially misspelled by looking it up in a standard
English frequency dictionary (the `pyspellchecker` package), with a small
user-editable whitelist sidecar file (`spellcheck_whitelist.txt`, next to
this module) for Discord usernames/slang/jargon that would otherwise be
flagged every time they appear, and a complementary blacklist sidecar file
(`spellcheck_blacklist.txt`) for words the dictionary treats as real but
that are usually OCR misreads/typos in this transcript's context - a word
in both files is treated as whitelisted (never flagged). This is
deliberately basic - a dictionary lookup, not a real language model - since
the goal is to catch obvious OCR noise (garbled words), not to be a
correctness oracle for informal chat text.

Never applied to spacer boxes - callers control that by simply never
calling into this module for them (see
row_building.RowBuildingMixin._build_spacer_text_box, which has no call
into this module at all).
"""

import re
from pathlib import Path
from typing import List, Set, Tuple

from . import config, logging_config

logger = logging_config.get_logger(__name__)

_WORD_RE = re.compile(r"[A-Za-z']+")

# Words shorter than this are skipped - initials, "ok", OCR noise, etc. are
# dominated by false positives at this length.
MIN_WORD_LENGTH = 3

_checker = None  # Constructed lazily - loading the dictionary isn't free.
_whitelist: Set[str] = set()
_whitelist_loaded = False
_blacklist: Set[str] = set()
_blacklist_loaded = False


def _get_checker():
    """Lazily construct (and cache) the SpellChecker instance. Deferred
    rather than built at import time so importing this module - e.g. from a
    non-GUI test - never pays the dictionary-load cost unless spellchecking
    is actually used."""
    global _checker
    if _checker is None:
        from spellchecker import SpellChecker

        _checker = SpellChecker()
    return _checker


def _get_whitelist(path: Path = config.SPELLCHECK_WHITELIST_FILE) -> Set[str]:
    """Load (and cache) the user-editable whitelist file - a missing file
    just means no whitelist, same convention as ocr_corrections.py's
    load_corrections."""
    global _whitelist, _whitelist_loaded
    if _whitelist_loaded:
        return _whitelist
    words: Set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line.lower())
    _whitelist = words
    _whitelist_loaded = True
    logger.info(
        "loaded spellcheck whitelist", extra=logging_config.extra(path=str(path), count=len(words))
    )
    return _whitelist


def _get_blacklist(path: Path = config.SPELLCHECK_BLACKLIST_FILE) -> Set[str]:
    """Load (and cache) the user-editable blacklist file - a missing file
    just means no blacklist, same convention as `_get_whitelist`."""
    global _blacklist, _blacklist_loaded
    if _blacklist_loaded:
        return _blacklist
    words: Set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line.lower())
    _blacklist = words
    _blacklist_loaded = True
    logger.info(
        "loaded spellcheck blacklist", extra=logging_config.extra(path=str(path), count=len(words))
    )
    return _blacklist


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
