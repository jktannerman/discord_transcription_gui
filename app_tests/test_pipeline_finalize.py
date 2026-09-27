import json
from pathlib import Path

import pytest

from discord_transcription import config, pipeline
from discord_transcription.chatlog import MessageEntry
from discord_transcription.review_item import build_review_items


def _items(tmp_path: Path, text: str = "new content here"):
    entries = [MessageEntry(message_id="m1", text_lines=[text], image_names=[])]
    return build_review_items(entries, {}, image_folder=tmp_path, corrections=[])


def _html(tmp_path: Path) -> Path:
    html_path = tmp_path / "chat.html"
    html_path.write_text("<html></html>", encoding="utf8")
    return html_path


@pytest.fixture
def clipboard(monkeypatch):
    copied = {}
    monkeypatch.setattr(pipeline.pyperclip, "copy", lambda text: copied.setdefault("text", text))
    return copied


def test_finalize_run_appends_items_after_existing_content(tmp_path, clipboard):
    output_path = tmp_path / "out.txt"
    output_path.write_text(f"old content\n\n\n{config.BREAK_MARKER}\n\n\n", encoding="utf8")

    result = pipeline.finalize_run(output_path, _html(tmp_path), _items(tmp_path), [{}])

    final_text = output_path.read_text(encoding="utf8")
    assert final_text.startswith(f"old content\n\n\n{config.BREAK_MARKER}\n\n\nnew content here")
    assert result.just_added.strip() == "new content here"


def test_finalize_run_appends_fresh_break_marker(tmp_path, clipboard):
    output_path = tmp_path / "out.txt"
    output_path.write_text("content", encoding="utf8")

    pipeline.finalize_run(output_path, _html(tmp_path), _items(tmp_path), [{}])

    final_text = output_path.read_text(encoding="utf8")
    assert final_text.startswith("content")
    assert final_text.rstrip().endswith(config.BREAK_MARKER)


def test_finalize_run_creates_missing_output_file(tmp_path, clipboard):
    output_path = tmp_path / "out.txt"

    pipeline.finalize_run(output_path, _html(tmp_path), _items(tmp_path), [{}])

    assert output_path.read_text(encoding="utf8").startswith("new content here")


def test_finalize_run_uses_edited_text(tmp_path, clipboard):
    output_path = tmp_path / "out.txt"

    pipeline.finalize_run(
        output_path, _html(tmp_path), _items(tmp_path), [{"message": "edited version"}]
    )

    final_text = output_path.read_text(encoding="utf8")
    assert "edited version" in final_text
    assert "new content here" not in final_text


def test_finalize_run_copies_added_text_to_clipboard(tmp_path, clipboard):
    output_path = tmp_path / "out.txt"
    output_path.write_text(f"{config.BREAK_MARKER}\n\n\n", encoding="utf8")

    result = pipeline.finalize_run(output_path, _html(tmp_path), _items(tmp_path), [{}])

    assert clipboard["text"] == result.just_added
    assert result.copied_to_clipboard
    assert result.warnings == []


def test_finalize_run_records_run_date_from_html_mtime(tmp_path, clipboard):
    output_path = tmp_path / "out.txt"

    pipeline.finalize_run(output_path, _html(tmp_path), _items(tmp_path), [{}])

    recorded_dates = json.loads(config.RUN_DATE_FILE.read_text(encoding="utf8"))["data"]
    assert len(recorded_dates) == 1


def test_finalize_run_leaves_past_runs_text_untouched(tmp_path, clipboard):
    output_path = tmp_path / "out.txt"
    # cleanup.clean_transcript only strips stray literal "\n" sequences from
    # this run's text - see test_cleanup.py.
    output_path.write_text("line one\\nline two", encoding="utf8")

    pipeline.finalize_run(output_path, _html(tmp_path), _items(tmp_path), [{}])

    final_text = output_path.read_text(encoding="utf8")
    assert final_text.startswith("line one\\nline two")


def test_finalize_run_keeps_the_previous_output_as_a_backup(tmp_path, clipboard):
    output_path = tmp_path / "out.txt"
    output_path.write_text("previous contents", encoding="utf8")

    pipeline.finalize_run(output_path, _html(tmp_path), _items(tmp_path), [{}])

    backup = pipeline.output_backup_path(output_path)
    assert backup.name == "out.txt.bak"
    assert backup.read_text(encoding="utf8") == "previous contents"
    assert output_path.read_text(encoding="utf8") != "previous contents"


def test_finalize_run_makes_no_backup_for_a_new_output_file(tmp_path, clipboard):
    output_path = tmp_path / "out.txt"

    pipeline.finalize_run(output_path, _html(tmp_path), _items(tmp_path), [{}])

    assert not pipeline.output_backup_path(output_path).exists()


def test_finalize_run_leaves_output_unchanged_when_it_fails_before_writing(tmp_path, clipboard):
    """A failure before the commit point (here: the chatlog is missing, so
    its mtime can't be read) must leave the output exactly as it was, and
    record nothing - so the run can simply be retried."""
    output_path = tmp_path / "out.txt"
    output_path.write_text("original", encoding="utf8")

    with pytest.raises(OSError):
        pipeline.finalize_run(output_path, tmp_path / "missing.html", _items(tmp_path), [{}])

    assert output_path.read_text(encoding="utf8") == "original"
    assert not config.RUN_DATE_FILE.exists()
    assert "text" not in clipboard


def test_finalize_run_leaves_output_unchanged_when_the_write_fails(tmp_path, clipboard, monkeypatch):
    output_path = tmp_path / "out.txt"
    output_path.write_text("original", encoding="utf8")

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline.state.os, "replace", failing_replace)

    with pytest.raises(OSError):
        pipeline.finalize_run(output_path, _html(tmp_path), _items(tmp_path), [{}])

    assert output_path.read_text(encoding="utf8") == "original"
    assert list(tmp_path.glob("*.tmp")) == []


def test_finalize_run_reports_clipboard_failure_as_warning(tmp_path, monkeypatch):
    """No xclip/xsel on Linux makes pyperclip raise - that must not undo or
    abort a run whose output has already been written."""

    def failing_copy(text):
        raise pipeline.pyperclip.PyperclipException("no clipboard mechanism")

    monkeypatch.setattr(pipeline.pyperclip, "copy", failing_copy)
    output_path = tmp_path / "out.txt"

    result = pipeline.finalize_run(output_path, _html(tmp_path), _items(tmp_path), [{}])

    assert not result.copied_to_clipboard
    assert len(result.warnings) == 1
    assert "clipboard" in result.warnings[0]
    assert output_path.read_text(encoding="utf8").rstrip().endswith(config.BREAK_MARKER)
    assert config.RUN_DATE_FILE.exists()


def test_finalize_run_reports_run_date_failure_as_warning(tmp_path, clipboard, monkeypatch):
    def failing_append(date_str):
        raise OSError("read-only")

    monkeypatch.setattr(pipeline.state, "append_run_date", failing_append)
    output_path = tmp_path / "out.txt"

    result = pipeline.finalize_run(output_path, _html(tmp_path), _items(tmp_path), [{}])

    assert len(result.warnings) == 1
    assert "end date" in result.warnings[0]
    assert result.copied_to_clipboard
    assert output_path.read_text(encoding="utf8").startswith("new content here")
