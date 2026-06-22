import json

from gui_transcription.app import config, pipeline


def test_finalize_run_returns_text_added_since_last_break_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RUN_DATE_FILE", tmp_path / "run_dates.json")
    monkeypatch.setattr(pipeline.pyperclip, "copy", lambda text: None)

    output_path = tmp_path / "out.txt"
    output_path.write_text(f"old content\n\n\n{config.BREAK_MARKER}\n\n\nnew content here", encoding="utf8")
    html_path = tmp_path / "chat.html"
    html_path.write_text("<html></html>", encoding="utf8")

    just_added = pipeline.finalize_run(output_path, html_path)

    assert just_added.strip() == "new content here"


def test_finalize_run_appends_fresh_break_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RUN_DATE_FILE", tmp_path / "run_dates.json")
    monkeypatch.setattr(pipeline.pyperclip, "copy", lambda text: None)

    output_path = tmp_path / "out.txt"
    output_path.write_text("content", encoding="utf8")
    html_path = tmp_path / "chat.html"
    html_path.write_text("<html></html>", encoding="utf8")

    pipeline.finalize_run(output_path, html_path)

    final_text = output_path.read_text(encoding="utf8")
    assert final_text.startswith("content")
    assert final_text.rstrip().endswith(config.BREAK_MARKER)


def test_finalize_run_copies_added_text_to_clipboard(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RUN_DATE_FILE", tmp_path / "run_dates.json")
    copied = {}
    monkeypatch.setattr(pipeline.pyperclip, "copy", lambda text: copied.setdefault("text", text))

    output_path = tmp_path / "out.txt"
    output_path.write_text(f"{config.BREAK_MARKER}\n\n\nfresh text", encoding="utf8")
    html_path = tmp_path / "chat.html"
    html_path.write_text("<html></html>", encoding="utf8")

    just_added = pipeline.finalize_run(output_path, html_path)

    assert copied["text"] == just_added


def test_finalize_run_records_run_date_from_html_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RUN_DATE_FILE", tmp_path / "run_dates.json")
    monkeypatch.setattr(pipeline.pyperclip, "copy", lambda text: None)

    output_path = tmp_path / "out.txt"
    output_path.write_text("content", encoding="utf8")
    html_path = tmp_path / "chat.html"
    html_path.write_text("<html></html>", encoding="utf8")

    pipeline.finalize_run(output_path, html_path)

    recorded_dates = json.loads((tmp_path / "run_dates.json").read_text(encoding="utf8"))
    assert len(recorded_dates) == 1


def test_finalize_run_applies_cleanup_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RUN_DATE_FILE", tmp_path / "run_dates.json")
    monkeypatch.setattr(pipeline.pyperclip, "copy", lambda text: None)

    output_path = tmp_path / "out.txt"
    # cleanup.clean_transcript replaces stray literal "\n" sequences - see test_cleanup.py
    output_path.write_text("line one\\nline two", encoding="utf8")
    html_path = tmp_path / "chat.html"
    html_path.write_text("<html></html>", encoding="utf8")

    pipeline.finalize_run(output_path, html_path)

    final_text = output_path.read_text(encoding="utf8")
    assert "\\n" not in final_text.split(config.BREAK_MARKER)[0]
