from pathlib import Path

import pytest

from gui_transcription.app import config
from gui_transcription.app.ocr_corrections import apply_corrections, load_corrections


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "corrections.txt"
    path.write_text(content, encoding="utf8")
    return path


def test_missing_file_returns_no_corrections(tmp_path):
    assert load_corrections(tmp_path / "does_not_exist.txt") == []


def test_empty_file_returns_no_corrections(tmp_path):
    assert load_corrections(_write(tmp_path, "")) == []


def test_header_only_comment_block_is_skipped(tmp_path):
    path = _write(tmp_path, "# just a header, no entries here\n# still just comments\n")
    assert load_corrections(path) == []


def test_parses_a_single_entry_with_comment(tmp_path):
    path = _write(tmp_path, "foo\nbar\n# explains the fix\n")
    corrections = load_corrections(path)
    assert len(corrections) == 1
    assert corrections[0].pattern.pattern == "foo"
    assert corrections[0].replacement == "bar"
    assert corrections[0].comment == "# explains the fix"


def test_parses_an_entry_with_no_comment(tmp_path):
    path = _write(tmp_path, "foo\nbar\n")
    corrections = load_corrections(path)
    assert len(corrections) == 1
    assert corrections[0].comment == ""


def test_parses_multiple_blank_line_separated_entries_in_order(tmp_path):
    path = _write(tmp_path, "a\nb\n\nc\nd\n")
    corrections = load_corrections(path)
    assert [(c.pattern.pattern, c.replacement) for c in corrections] == [("a", "b"), ("c", "d")]


def test_entry_missing_replacement_line_raises(tmp_path):
    path = _write(tmp_path, "only-a-find-pattern\n")
    with pytest.raises(ValueError, match="no replacement"):
        load_corrections(path)


def test_non_comment_line_after_replacement_raises(tmp_path):
    path = _write(tmp_path, "foo\nbar\nthis isn't a comment\n")
    with pytest.raises(ValueError, match="comments must start with"):
        load_corrections(path)


def test_invalid_find_regex_raises(tmp_path):
    path = _write(tmp_path, "foo(\nbar\n")
    with pytest.raises(ValueError, match="invalid"):
        load_corrections(path)


def test_apply_corrections_runs_every_entry_in_order(tmp_path):
    corrections = load_corrections(_write(tmp_path, "a\nb\n\nb\nc\n"))
    assert apply_corrections("a", corrections) == "c"


def test_apply_corrections_supports_capture_groups(tmp_path):
    corrections = load_corrections(_write(tmp_path, "\\b([A-Z]),\nfix(\\1)\n"))
    assert apply_corrections("X, Y,", corrections) == "fix(X) fix(Y)"


def test_default_corrections_file_loads_without_error():
    # The real, user-editable app/ocr_corrections.txt - this just confirms
    # it stays well-formed as it's edited over time, not any particular
    # entry's content.
    corrections = load_corrections(config.OCR_CORRECTIONS_FILE)
    assert len(corrections) > 0


def test_default_corrections_fix_pipe_misread_as_capital_i():
    corrections = load_corrections(config.OCR_CORRECTIONS_FILE)
    assert apply_corrections("Health: ||||", corrections) == "Health: IIII"


def test_default_corrections_fix_im_missing_apostrophe():
    corrections = load_corrections(config.OCR_CORRECTIONS_FILE)
    assert apply_corrections("Im going home", corrections) == "I'm going home"


def test_load_corrections_logs_the_full_rule_set(tmp_path, caplog):
    path = _write(tmp_path, "a\nb\n# comment\n\nc\nd\n")
    with caplog.at_level("INFO", logger="gui_transcription.app.ocr_corrections"):
        load_corrections(path)
    records = [r for r in caplog.records if r.getMessage() == "loaded OCR corrections"]
    assert len(records) == 1
    fields = records[0].extra_fields
    assert fields["path"] == str(path)
    assert fields["count"] == 2
    assert fields["rules"] == [
        {"find": "a", "replacement": "b"},
        {"find": "c", "replacement": "d"},
    ]


def test_apply_corrections_logs_each_matching_substitution(tmp_path, caplog):
    corrections = load_corrections(_write(tmp_path, "\\b([A-Z]),\nfix(\\1)\n"))
    with caplog.at_level("INFO", logger="gui_transcription.app.ocr_corrections"):
        apply_corrections("X, Y,", corrections, context="some_image.png")
    records = [r for r in caplog.records if r.getMessage() == "OCR correction applied"]
    assert len(records) == 1
    fields = records[0].extra_fields
    assert fields["context"] == "some_image.png"
    assert fields["find"] == "\\b([A-Z]),"
    assert fields["replacement"] == "fix(\\1)"
    assert fields["count"] == 2
    assert fields["substitutions"] == [
        {"from": "X,", "to": "fix(X)"},
        {"from": "Y,", "to": "fix(Y)"},
    ]


def test_apply_corrections_logs_nothing_when_no_match(tmp_path, caplog):
    corrections = load_corrections(_write(tmp_path, "zzz\nyyy\n"))
    with caplog.at_level("INFO", logger="gui_transcription.app.ocr_corrections"):
        apply_corrections("no match here", corrections)
    assert not [r for r in caplog.records if r.getMessage() == "OCR correction applied"]
