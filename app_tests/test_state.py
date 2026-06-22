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


def test_load_recent_paths_returns_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RECENT_PATHS_FILE", tmp_path / "recent_paths.json")

    assert state.load_recent_paths("html_path") == []


def test_add_recent_path_orders_most_recent_first(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RECENT_PATHS_FILE", tmp_path / "recent_paths.json")

    state.add_recent_path("html_path", "a.html")
    state.add_recent_path("html_path", "b.html")

    assert state.load_recent_paths("html_path") == ["b.html", "a.html"]


def test_add_recent_path_dedupes_and_moves_to_front(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RECENT_PATHS_FILE", tmp_path / "recent_paths.json")

    state.add_recent_path("html_path", "a.html")
    state.add_recent_path("html_path", "b.html")
    state.add_recent_path("html_path", "a.html")

    assert state.load_recent_paths("html_path") == ["a.html", "b.html"]


def test_add_recent_path_caps_history_length(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RECENT_PATHS_FILE", tmp_path / "recent_paths.json")
    monkeypatch.setattr(config, "MAX_RECENT_PATHS", 2)

    state.add_recent_path("html_path", "a.html")
    state.add_recent_path("html_path", "b.html")
    state.add_recent_path("html_path", "c.html")

    assert state.load_recent_paths("html_path") == ["c.html", "b.html"]


def test_add_recent_path_keeps_fields_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RECENT_PATHS_FILE", tmp_path / "recent_paths.json")

    state.add_recent_path("html_path", "a.html")
    state.add_recent_path("output_path", "out.txt")

    assert state.load_recent_paths("html_path") == ["a.html"]
    assert state.load_recent_paths("output_path") == ["out.txt"]


def test_read_approved_users_state_returns_none_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "APPROVED_USERS_STATE_FILE", tmp_path / "approved_users.json")

    assert state.read_approved_users_state() is None


def test_approved_users_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "APPROVED_USERS_STATE_FILE", tmp_path / "approved_users.json")

    state.save_approved_users_state("123456789 - Alice", True)

    assert state.read_approved_users_state() == {"text": "123456789 - Alice", "use_all_users": True}
