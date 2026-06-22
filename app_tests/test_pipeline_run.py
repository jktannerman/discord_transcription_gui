import os
import time

from gui_transcription.app import config, ocr, pipeline, state


def _touch(path) -> None:
    # pipeline.run_ocr_batch checks os.path.getctime (creation time), which
    # can't be backdated via os.utime (that only touches mtime/atime) - so
    # "old" vs. "new enough" is controlled by where start_time falls relative
    # to creation time (effectively "now"), not by editing file timestamps.
    path.write_bytes(b"fake image bytes")


def test_run_ocr_batch_transcribes_new_enough_images(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OCR_CACHE_FILE", tmp_path / "ocr_cache.json")
    monkeypatch.setattr(ocr, "transcribe_image", lambda path: f"raw text for {os.path.basename(path)}")

    image_folder = tmp_path / "images"
    image_folder.mkdir()
    _touch(image_folder / "new.png")

    file_info = pipeline.run_ocr_batch(str(image_folder), start_time=0, use_cache=False)

    assert file_info == {"new.png": ["raw text for new.png"]}


def test_run_ocr_batch_skips_files_older_than_start_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OCR_CACHE_FILE", tmp_path / "ocr_cache.json")
    monkeypatch.setattr(ocr, "transcribe_image", lambda path: "should not be called")

    image_folder = tmp_path / "images"
    image_folder.mkdir()
    _touch(image_folder / "old.png")

    file_info = pipeline.run_ocr_batch(
        str(image_folder), start_time=int(time.time()) + 3600, use_cache=False
    )

    assert file_info == {}


def test_run_ocr_batch_skips_non_image_extensions(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OCR_CACHE_FILE", tmp_path / "ocr_cache.json")
    monkeypatch.setattr(ocr, "transcribe_image", lambda path: "should not be called")

    image_folder = tmp_path / "images"
    image_folder.mkdir()
    _touch(image_folder / "styles.css")

    file_info = pipeline.run_ocr_batch(str(image_folder), start_time=0, use_cache=False)

    assert file_info == {}


def test_run_ocr_batch_uses_cache_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OCR_CACHE_FILE", tmp_path / "ocr_cache.json")
    monkeypatch.setattr(
        ocr, "transcribe_image", lambda path: (_ for _ in ()).throw(AssertionError("OCR should not run"))
    )

    image_folder = tmp_path / "images"
    image_folder.mkdir()
    state.save_cache(str(image_folder), {"cached.png": ["cached paragraph"]})

    file_info = pipeline.run_ocr_batch(str(image_folder), start_time=0, use_cache=True)

    assert file_info == {"cached.png": ["cached paragraph"]}


def test_run_ocr_batch_ignores_cache_when_use_cache_false(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OCR_CACHE_FILE", tmp_path / "ocr_cache.json")
    monkeypatch.setattr(ocr, "transcribe_image", lambda path: "freshly ocr'd")

    image_folder = tmp_path / "images"
    image_folder.mkdir()
    state.save_cache(str(image_folder), {"cached.png": ["stale paragraph"]})
    _touch(image_folder / "new.png")

    file_info = pipeline.run_ocr_batch(str(image_folder), start_time=0, use_cache=False)

    assert file_info == {"new.png": ["freshly ocr'd"]}


def test_run_ocr_batch_saves_results_to_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OCR_CACHE_FILE", tmp_path / "ocr_cache.json")
    monkeypatch.setattr(ocr, "transcribe_image", lambda path: "fresh text")

    image_folder = tmp_path / "images"
    image_folder.mkdir()
    _touch(image_folder / "new.png")

    pipeline.run_ocr_batch(str(image_folder), start_time=0, use_cache=False)

    assert state.load_cache(str(image_folder)) == {"new.png": ["fresh text"]}
