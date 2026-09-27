"""Pure logic tests for spellcheck.find_misspelled_spans and its whitelist
loading - no Tk widgets involved (the tag application itself is covered by
the gui-marked tests in test_review_view.py, since it needs a real Text
widget's tag_add/tag_ranges)."""
import os

from discord_transcription import config, spellcheck


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
    monkeypatch.setattr(spellcheck, "_get_whitelist", lambda: {"xyzzyplugh"})
    assert spellcheck.find_misspelled_spans("Xyzzyplugh said hello") == []


def test_get_whitelist_parses_file_ignoring_blanks_and_comments(tmp_path, monkeypatch):
    path = tmp_path / "whitelist.txt"
    path.write_text("# comment\n\nAlice\nBOB\n", encoding="utf8")
    monkeypatch.setattr(spellcheck, "_wordlists", {})
    assert spellcheck._get_whitelist(path) == {"alice", "bob"}


def test_get_whitelist_missing_file_returns_empty_set(tmp_path, monkeypatch):
    monkeypatch.setattr(spellcheck, "_wordlists", {})
    assert spellcheck._get_whitelist(tmp_path / "missing.txt") == set()


def test_get_whitelist_reuses_an_unchanged_file(tmp_path, monkeypatch):
    path = tmp_path / "whitelist.txt"
    path.write_text("alice\n", encoding="utf8")
    monkeypatch.setattr(spellcheck, "_wordlists", {})
    first = spellcheck._get_whitelist(path)
    assert spellcheck._get_whitelist(path) is first


def test_get_whitelist_reloads_after_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "whitelist.txt"
    path.write_text("alice\n", encoding="utf8")
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(spellcheck, "_wordlists", {})
    assert spellcheck._get_whitelist(path) == {"alice"}
    path.write_text("bob\n", encoding="utf8")
    os.utime(path, ns=(2_000_000_000, 2_000_000_000))
    assert spellcheck._get_whitelist(path) == {"bob"}


def test_default_whitelist_file_parses_without_error(monkeypatch):
    # The real, user-editable discord_transcription/spellcheck_whitelist.txt - this just
    # confirms it stays well-formed as it's edited over time.
    monkeypatch.setattr(spellcheck, "_wordlists", {})
    words = spellcheck._get_whitelist(config.SPELLCHECK_WHITELIST_FILE)
    assert isinstance(words, set)


def test_blacklisted_word_is_flagged_even_though_dictionary_knows_it(monkeypatch):
    monkeypatch.setattr(spellcheck, "_get_blacklist", lambda: {"there"})
    text = "Put it over there please"
    spans = spellcheck.find_misspelled_spans(text)
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:end] == "there"


def test_word_in_both_whitelist_and_blacklist_is_not_flagged(monkeypatch):
    monkeypatch.setattr(spellcheck, "_get_whitelist", lambda: {"there"})
    monkeypatch.setattr(spellcheck, "_get_blacklist", lambda: {"there"})
    assert spellcheck.find_misspelled_spans("Put it over there please") == []


def test_get_blacklist_parses_file_ignoring_blanks_and_comments(tmp_path, monkeypatch):
    path = tmp_path / "blacklist.txt"
    path.write_text("# comment\n\nAlice\nBOB\n", encoding="utf8")
    monkeypatch.setattr(spellcheck, "_wordlists", {})
    assert spellcheck._get_blacklist(path) == {"alice", "bob"}


def test_get_blacklist_missing_file_returns_empty_set(tmp_path, monkeypatch):
    monkeypatch.setattr(spellcheck, "_wordlists", {})
    assert spellcheck._get_blacklist(tmp_path / "missing.txt") == set()


def test_get_blacklist_reuses_an_unchanged_file(tmp_path, monkeypatch):
    path = tmp_path / "blacklist.txt"
    path.write_text("alice\n", encoding="utf8")
    monkeypatch.setattr(spellcheck, "_wordlists", {})
    first = spellcheck._get_blacklist(path)
    assert spellcheck._get_blacklist(path) is first


def test_get_blacklist_reloads_after_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "blacklist.txt"
    path.write_text("alice\n", encoding="utf8")
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(spellcheck, "_wordlists", {})
    assert spellcheck._get_blacklist(path) == {"alice"}
    path.write_text("bob\n", encoding="utf8")
    os.utime(path, ns=(2_000_000_000, 2_000_000_000))
    assert spellcheck._get_blacklist(path) == {"bob"}


def test_default_blacklist_file_parses_without_error(monkeypatch):
    # The real, user-editable discord_transcription/spellcheck_blacklist.txt - this just
    # confirms it stays well-formed as it's edited over time.
    monkeypatch.setattr(spellcheck, "_wordlists", {})
    words = spellcheck._get_blacklist(config.SPELLCHECK_BLACKLIST_FILE)
    assert isinstance(words, set)
