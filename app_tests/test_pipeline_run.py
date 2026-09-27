import os
from unittest.mock import patch

import pytest

from discord_transcription import config, ocr, pipeline, state
from discord_transcription.chatlog import MessageEntry


@pytest.fixture
def image_folder(tmp_path):
    folder = tmp_path / "images"
    folder.mkdir()
    return folder


@pytest.fixture
def ocr_calls(monkeypatch):
    """Record every image OCR runs on; the fake OCR text names the file."""
    calls = []

    def fake_transcribe(path):
        calls.append(os.path.basename(path))
        return f"raw text for {os.path.basename(path)}"

    monkeypatch.setattr(ocr, "transcribe_image", fake_transcribe)
    return calls


def _write_image(folder, name, content=b"fake image bytes"):
    (folder / name).write_bytes(content)


def test_referenced_image_names_dedupes_in_first_reference_order():
    entries = [
        MessageEntry(message_id="1", text_lines=[], image_names=["b.png", "a.png"]),
        MessageEntry(message_id="2", text_lines=["hi"], image_names=[]),
        MessageEntry(message_id="3", text_lines=[], image_names=["a.png", "c.png"]),
    ]

    assert pipeline.referenced_image_names(entries) == ["b.png", "a.png", "c.png"]


def test_ocrs_only_the_referenced_images(image_folder, ocr_calls):
    _write_image(image_folder, "wanted.png")
    _write_image(image_folder, "unreferenced.png")

    result = pipeline.run_ocr_batch(str(image_folder), ["wanted.png"], use_cache=True)

    assert result.file_info == {"wanted.png": ["raw text for wanted.png"]}
    assert result.missing_images == []
    assert ocr_calls == ["wanted.png"]


def test_reports_referenced_images_missing_from_the_folder(image_folder, ocr_calls):
    _write_image(image_folder, "present.png")

    result = pipeline.run_ocr_batch(
        str(image_folder), ["gone.png", "present.png", "also_gone.png"], use_cache=True
    )

    assert result.missing_images == ["gone.png", "also_gone.png"]
    assert set(result.file_info) == {"present.png"}


def test_reuses_cache_and_only_ocrs_images_missing_from_it(image_folder, ocr_calls):
    _write_image(image_folder, "old.png")
    pipeline.run_ocr_batch(str(image_folder), ["old.png"], use_cache=True)
    _write_image(image_folder, "new.png")
    ocr_calls.clear()

    result = pipeline.run_ocr_batch(str(image_folder), ["old.png", "new.png"], use_cache=True)

    assert ocr_calls == ["new.png"]
    assert result.file_info == {
        "old.png": ["raw text for old.png"],
        "new.png": ["raw text for new.png"],
    }


def test_reocrs_an_image_whose_file_changed(image_folder, ocr_calls):
    _write_image(image_folder, "shot.png")
    pipeline.run_ocr_batch(str(image_folder), ["shot.png"], use_cache=True)
    _write_image(image_folder, "shot.png", content=b"different, longer image bytes")
    ocr_calls.clear()

    pipeline.run_ocr_batch(str(image_folder), ["shot.png"], use_cache=True)

    assert ocr_calls == ["shot.png"]


def test_use_cache_false_reocrs_everything_but_keeps_other_entries(image_folder, ocr_calls):
    _write_image(image_folder, "a.png")
    _write_image(image_folder, "b.png")
    pipeline.run_ocr_batch(str(image_folder), ["a.png", "b.png"], use_cache=True)
    ocr_calls.clear()

    pipeline.run_ocr_batch(str(image_folder), ["a.png"], use_cache=False)

    assert ocr_calls == ["a.png"]
    assert set(state.load_cache(str(image_folder))) == {"a.png", "b.png"}


def test_trusts_version_1_entries_and_adds_their_fingerprint(image_folder, ocr_calls):
    _write_image(image_folder, "legacy.png")
    state.save_cache(str(image_folder), {"legacy.png": {"paragraphs": ["cached text"]}})

    result = pipeline.run_ocr_batch(str(image_folder), ["legacy.png"], use_cache=True)

    assert ocr_calls == []
    assert result.file_info == {"legacy.png": ["cached text"]}
    stat = (image_folder / "legacy.png").stat()
    assert state.load_cache(str(image_folder))["legacy.png"] == {
        "paragraphs": ["cached text"], "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
    }


def test_saves_cache_periodically_during_the_batch(image_folder, ocr_calls, monkeypatch):
    monkeypatch.setattr(config, "OCR_CACHE_SAVE_EVERY", 2)
    names = [f"{i}.png" for i in range(5)]
    for name in names:
        _write_image(image_folder, name)
    saved_sizes = []
    real_save = state.save_cache
    monkeypatch.setattr(
        state, "save_cache",
        lambda folder, entries: (saved_sizes.append(len(entries)), real_save(folder, entries)),
    )

    pipeline.run_ocr_batch(str(image_folder), names, use_cache=True)

    assert saved_sizes == [2, 4, 5]


def test_keeps_completed_work_when_ocr_fails_partway(image_folder, monkeypatch):
    for name in ("ok.png", "broken.png"):
        _write_image(image_folder, name)

    def flaky_transcribe(path):
        if path.endswith("broken.png"):
            raise RuntimeError("tesseract crashed")
        return "fine"

    monkeypatch.setattr(ocr, "transcribe_image", flaky_transcribe)

    with pytest.raises(RuntimeError):
        pipeline.run_ocr_batch(str(image_folder), ["ok.png", "broken.png"], use_cache=True)

    assert set(state.load_cache(str(image_folder))) == {"ok.png"}


def test_progress_reaches_one_when_everything_is_cached(image_folder, ocr_calls):
    _write_image(image_folder, "a.png")
    pipeline.run_ocr_batch(str(image_folder), ["a.png"], use_cache=True)
    progress = []

    pipeline.run_ocr_batch(
        str(image_folder), ["a.png"], use_cache=True, progress_callback=progress.append
    )

    assert progress == [1.0]


# -- prepare_run: parse the chatlog, then OCR what it references ---------------


def _run_context(tmp_path) -> pipeline.RunContext:
    html_path = tmp_path / "chat.html"
    html_path.write_text("<html></html>", encoding="utf8")
    return pipeline.RunContext(
        html_path=html_path,
        image_folder=tmp_path,
        output_path=tmp_path / "out.txt",
        start_time=0,
        approved_author_ids=None,
        use_cache=True,
    )


def _prepare(run: pipeline.RunContext):
    return pipeline.prepare_run(
        run, progress_callback=lambda frac: None, status_callback=lambda text: None
    )


def test_malformed_chatlog_raises_run_error_with_its_message(tmp_path):
    with patch.object(
        pipeline.chatlog, "parse_message_groups",
        side_effect=ValueError("Message ID 'bogus' isn't a Discord snowflake ID."),
    ):
        with pytest.raises(pipeline.RunError) as exc_info:
            _prepare(_run_context(tmp_path))

    assert str(exc_info.value) == "Message ID 'bogus' isn't a Discord snowflake ID."


def test_unreadable_html_file_raises_run_error(tmp_path):
    run = _run_context(tmp_path)
    run.html_path.unlink()

    with pytest.raises(pipeline.RunError, match="Could not read HTML file"):
        _prepare(run)


def test_unexpected_parse_exception_raises_run_error(tmp_path):
    with patch.object(
        pipeline.chatlog, "parse_message_groups", side_effect=KeyError("src"),
    ):
        with pytest.raises(pipeline.RunError, match="KeyError"):
            _prepare(_run_context(tmp_path))


def test_prepare_run_ocrs_the_images_the_kept_messages_reference(tmp_path):
    entries = [
        MessageEntry(message_id="1", text_lines=[], image_names=["a.png"]),
    ]
    calls = []

    def fake_batch(image_folder, image_names, use_cache, progress_callback=None):
        calls.append((image_folder, image_names, use_cache))
        return pipeline.OcrBatchResult(file_info={})

    with (
        patch.object(pipeline.chatlog, "parse_message_groups", return_value=entries),
        patch.object(pipeline, "run_ocr_batch", side_effect=fake_batch),
    ):
        result_entries, _ = _prepare(_run_context(tmp_path))

    assert result_entries == entries
    assert calls == [(str(tmp_path), ["a.png"], True)]
