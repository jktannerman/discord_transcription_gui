import json

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


def test_cache_keeps_multiple_folders_indefinitely(tmp_path, monkeypatch):
    """Caching a second chatlog's image folder must not evict the first's -
    each folder gets its own indefinitely-kept entry."""
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OCR_CACHE_FILE", tmp_path / "ocr_cache.json")

    folder_a = str(tmp_path / "images_a")
    folder_b = str(tmp_path / "images_b")

    state.save_cache(folder_a, {"a.png": ["text a"]})
    state.save_cache(folder_b, {"b.png": ["text b"]})

    assert state.load_cache(folder_a) == {"a.png": ["text a"]}
    assert state.load_cache(folder_b) == {"b.png": ["text b"]}


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


def test_load_session_returns_none_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")

    assert state.load_session("chat.html") is None


def test_session_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")

    session = {
        "html_path": "chat.html",
        "image_folder": "images",
        "output_path": "out.txt",
        "start_time": 12345,
        "approved_author_ids": ["111", "222"],
        "use_cache": True,
        "edited_texts": {"222222222222222222": {"message": None, "ocr": "edited text"}},
        "focus_slot": ["222222222222222222", "ocr"],
        "scroll_fraction": 0.5,
    }

    state.save_session("chat.html", session)

    assert state.load_session("chat.html") == session


def test_save_session_overwrites_previous_session_for_same_html_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")

    state.save_session("chat.html", {"output_path": "first.txt", "edited_texts": []})
    state.save_session("chat.html", {"output_path": "second.txt", "edited_texts": []})

    assert state.load_session("chat.html")["output_path"] == "second.txt"


def test_sessions_for_different_html_paths_kept_independently(tmp_path, monkeypatch):
    """Saving a session for one chatlog must not evict another chatlog's
    saved session - both should remain resumable independently."""
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")

    state.save_session("chat_a.html", {"output_path": "a.txt", "edited_texts": []})
    state.save_session("chat_b.html", {"output_path": "b.txt", "edited_texts": []})

    assert state.load_session("chat_a.html")["output_path"] == "a.txt"
    assert state.load_session("chat_b.html")["output_path"] == "b.txt"


def test_clear_session_removes_only_that_html_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")

    state.save_session("chat_a.html", {"output_path": "a.txt", "edited_texts": []})
    state.save_session("chat_b.html", {"output_path": "b.txt", "edited_texts": []})
    state.clear_session("chat_a.html")

    assert state.load_session("chat_a.html") is None
    assert state.load_session("chat_b.html")["output_path"] == "b.txt"


def test_clear_session_is_a_noop_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")

    state.clear_session("chat.html")  # should not raise


def test_session_backup_created_on_second_save(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    sessions_file = tmp_path / "sessions.json"
    monkeypatch.setattr(config, "SESSIONS_FILE", sessions_file)
    backup_file = sessions_file.with_suffix(".bak")

    state.save_session("chat.html", {"output_path": "first.txt", "edited_texts": []})
    assert not backup_file.exists()

    state.save_session("chat.html", {"output_path": "second.txt", "edited_texts": []})
    assert backup_file.exists()
    assert json.loads(backup_file.read_text(encoding="utf8"))["chat.html"]["output_path"] == "first.txt"


def test_session_recovers_from_backup_when_primary_corrupt(tmp_path, monkeypatch):
    """Mimics a crash mid-write: sessions.json left empty/truncated, but the
    previous good sessions survive in sessions.bak."""
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    sessions_file = tmp_path / "sessions.json"
    monkeypatch.setattr(config, "SESSIONS_FILE", sessions_file)
    backup_file = sessions_file.with_suffix(".bak")

    state.save_session("chat.html", {"output_path": "good.txt", "edited_texts": []})
    state.save_session("chat.html", {"output_path": "overwritten.txt", "edited_texts": []})
    sessions_file.write_text("", encoding="utf8")

    assert state.load_session("chat.html")["output_path"] == "good.txt"
    assert backup_file.exists()


def test_clear_session_removes_backup_too(tmp_path, monkeypatch):
    """A stale backup must not resurrect a session that was already
    finalized and explicitly cleared."""
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    sessions_file = tmp_path / "sessions.json"
    monkeypatch.setattr(config, "SESSIONS_FILE", sessions_file)
    backup_file = sessions_file.with_suffix(".bak")

    state.save_session("chat.html", {"output_path": "first.txt", "edited_texts": []})
    state.save_session("chat.html", {"output_path": "second.txt", "edited_texts": []})
    assert backup_file.exists()

    state.clear_session("chat.html")

    assert state.load_session("chat.html") is None
    assert backup_file.exists()
    assert json.loads(backup_file.read_text(encoding="utf8"))["chat.html"]["output_path"] == "second.txt"


def test_no_session_temp_files_left_after_save(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")

    state.save_session("chat.html", {"output_path": "first.txt", "edited_texts": []})
    state.save_session("chat.html", {"output_path": "second.txt", "edited_texts": []})

    assert list(tmp_path.glob("sessions_*.tmp")) == []


def test_cache_recovers_from_backup_when_primary_corrupt(tmp_path, monkeypatch):
    """Mimics a crash mid-write to the OCR cache, which can represent a
    long Tesseract pass over a large image folder."""
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    cache_file = tmp_path / "ocr_cache.json"
    monkeypatch.setattr(config, "OCR_CACHE_FILE", cache_file)
    backup_file = cache_file.with_suffix(".bak")

    folder = str(tmp_path / "images")
    state.save_cache(folder, {"good.png": ["text"]})
    state.save_cache(folder, {"overwritten.png": ["text"]})
    cache_file.write_text("", encoding="utf8")

    assert state.load_cache(folder) == {"good.png": ["text"]}
    assert backup_file.exists()
