"""Pure logic tests for spellcheck.find_misspelled_spans and its whitelist
loading - no Tk widgets involved (the tag application itself is covered by
the gui-marked tests in test_review_view.py, since it needs a real Text
widget's tag_add/tag_ranges)."""
from gui_transcription.app import config, spellcheck


def test_correctly_spelled_text_has_no_misspelled_spans():
    assert spellcheck.find_misspelled_spans("This is a normal sentence.") == []


def test_garbled_ocr_word_is_flagged():
    text = "This is garbld text"
    spans = spellcheck.find_misspelled_spans(text)
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:end] == "garbld"


def test_short_words_are_never_flagged():
    # "a" and "ok" are both below MIN_WORD_LENGTH even if not recognized -
    # OCR noise and initials at that length are dominated by false positives.
    assert spellcheck.find_misspelled_spans("a ok") == []


def test_all_caps_words_are_never_flagged():
    # Acronyms/abbreviations (OCR, GM, ...) aren't spelling mistakes.
    assert spellcheck.find_misspelled_spans("ask the GM about the OCR run") == []


def test_whitelisted_word_is_not_flagged(monkeypatch):
    monkeypatch.setattr(spellcheck, "_whitelist_loaded", True)
    monkeypatch.setattr(spellcheck, "_whitelist", {"xyzzyplugh"})
    assert spellcheck.find_misspelled_spans("Xyzzyplugh said hello") == []


def test_get_whitelist_parses_file_ignoring_blanks_and_comments(tmp_path, monkeypatch):
    path = tmp_path / "whitelist.txt"
    path.write_text("# comment\n\nAlice\nBOB\n", encoding="utf8")
    monkeypatch.setattr(spellcheck, "_whitelist_loaded", False)
    assert spellcheck._get_whitelist(path) == {"alice", "bob"}


def test_get_whitelist_missing_file_returns_empty_set(tmp_path, monkeypatch):
    monkeypatch.setattr(spellcheck, "_whitelist_loaded", False)
    assert spellcheck._get_whitelist(tmp_path / "missing.txt") == set()


def test_get_whitelist_caches_after_first_load(tmp_path, monkeypatch):
    path = tmp_path / "whitelist.txt"
    path.write_text("alice\n", encoding="utf8")
    monkeypatch.setattr(spellcheck, "_whitelist_loaded", False)
    first = spellcheck._get_whitelist(path)
    path.write_text("bob\n", encoding="utf8")
    # Second call reuses the cached result rather than re-reading the file -
    # same convention as ocr_corrections.py's per-process loading.
    assert spellcheck._get_whitelist(path) is first


def test_default_whitelist_file_parses_without_error(monkeypatch):
    # The real, user-editable app/spellcheck_whitelist.txt - this just
    # confirms it stays well-formed as it's edited over time.
    monkeypatch.setattr(spellcheck, "_whitelist_loaded", False)
    words = spellcheck._get_whitelist(config.SPELLCHECK_WHITELIST_FILE)
    assert isinstance(words, set)
