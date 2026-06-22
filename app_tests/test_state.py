from gui_transcription.app import config, state


def test_read_last_run_date_returns_none_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RUN_DATE_FILE", tmp_path / "run_dates.json")

    assert state.read_last_run_date() is None


def test_append_and_read_last_run_date(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RUN_DATE_FILE", tmp_path / "run_dates.json")

    state.append_run_date("2024-01-01-00-00-00")
    state.append_run_date("2024-02-01-00-00-00")

    assert state.read_last_run_date() == "2024-02-01-00-00-00"


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OCR_CACHE_FILE", tmp_path / "ocr_cache.json")

    folder = str(tmp_path / "images")
    data = {"image1.png": ["paragraph one", "paragraph two"]}

    state.save_cache(folder, data)
    assert state.load_cache(folder) == data


def test_cache_returns_none_for_different_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OCR_CACHE_FILE", tmp_path / "ocr_cache.json")

    state.save_cache(str(tmp_path / "images_a"), {"x.png": ["p"]})
    assert state.load_cache(str(tmp_path / "images_b")) is None
