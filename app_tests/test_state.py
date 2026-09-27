import json

import pytest

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


def _patch_finalized(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "FINALIZED_EDITS_FILE", tmp_path / "finalized_edits.json")


def test_load_finalized_edits_returns_none_when_no_file(tmp_path, monkeypatch):
    _patch_finalized(monkeypatch, tmp_path)
    assert state.load_finalized_edits("/path/to/chatlog.html") is None


def test_finalized_edits_round_trip(tmp_path, monkeypatch):
    _patch_finalized(monkeypatch, tmp_path)
    html = "/path/to/chatlog.html"
    edits = {"msg1": {"message": "hello", "spacer_end": r"\n\n\n\n"}}

    state.save_finalized_edits(html, edits)

    assert state.load_finalized_edits(html) == edits


def test_save_finalized_edits_merges_without_deleting_prior_roles(tmp_path, monkeypatch):
    """A second save for the same message_id must update only the roles present
    in the new dict; prior roles absent from the new dict are kept unchanged."""
    _patch_finalized(monkeypatch, tmp_path)
    html = "/path/to/chatlog.html"

    state.save_finalized_edits(html, {"msg1": {"message": "original", "ocr0": "ocr text"}})
    # Second save updates only "message"; "ocr0" must be preserved.
    state.save_finalized_edits(html, {"msg1": {"message": "updated"}})

    result = state.load_finalized_edits(html)
    assert result == {"msg1": {"message": "updated", "ocr0": "ocr text"}}


def test_save_finalized_edits_isolates_html_paths(tmp_path, monkeypatch):
    """Saving finalized edits for one chatlog must not affect another chatlog's
    stored edits."""
    _patch_finalized(monkeypatch, tmp_path)
    html_a = "/path/to/a.html"
    html_b = "/path/to/b.html"

    state.save_finalized_edits(html_a, {"msgA": {"message": "text A"}})
    state.save_finalized_edits(html_b, {"msgB": {"message": "text B"}})

    assert state.load_finalized_edits(html_a) == {"msgA": {"message": "text A"}}
    assert state.load_finalized_edits(html_b) == {"msgB": {"message": "text B"}}


def test_save_finalized_edits_removes_roles_set_to_none(tmp_path, monkeypatch):
    """None means the user deliberately reverted that box - its stored edit
    is removed, while other roles of the same message are kept."""
    _patch_finalized(monkeypatch, tmp_path)
    html = "/path/to/chatlog.html"

    state.save_finalized_edits(html, {"msg1": {"message": "original", "ocr0": "ocr text"}})
    state.save_finalized_edits(html, {"msg1": {"message": None}})

    assert state.load_finalized_edits(html) == {"msg1": {"ocr0": "ocr text"}}


def test_save_finalized_edits_drops_a_message_once_all_its_roles_are_removed(tmp_path, monkeypatch):
    _patch_finalized(monkeypatch, tmp_path)
    html = "/path/to/chatlog.html"

    state.save_finalized_edits(html, {"msg1": {"message": "a"}, "msg2": {"message": "b"}})
    state.save_finalized_edits(html, {"msg1": {"message": None}})

    assert state.load_finalized_edits(html) == {"msg2": {"message": "b"}}


def test_removing_or_replacing_a_finalized_edit_archives_the_old_text(tmp_path, monkeypatch):
    _patch_finalized(monkeypatch, tmp_path)
    html = "/path/to/chatlog.html"

    state.save_finalized_edits(html, {"msg1": {"message": "first", "ocr0": "long edit"}})
    state.save_finalized_edits(html, {"msg1": {"message": "second", "ocr0": None}})

    history = state.load_finalized_edits_history(html)
    by_role = {entry["role"]: entry for entry in history}
    assert set(by_role) == {"message", "ocr0"}
    assert by_role["message"]["old_text"] == "first"
    assert by_role["message"]["new_text"] == "second"
    assert by_role["ocr0"]["old_text"] == "long edit"
    assert by_role["ocr0"]["new_text"] is None
    assert all(entry["message_id"] == "msg1" for entry in history)


def test_history_only_records_real_changes_and_is_never_trimmed(tmp_path, monkeypatch):
    _patch_finalized(monkeypatch, tmp_path)
    html = "/path/to/chatlog.html"

    state.save_finalized_edits(html, {"msg1": {"message": "v1"}})  # new - nothing to archive
    state.save_finalized_edits(html, {"msg1": {"message": "v1"}})  # unchanged
    state.save_finalized_edits(html, {"msg1": {"message": "v2"}})
    state.save_finalized_edits(html, {"msg1": {"message": "v3"}})
    state.save_finalized_edits("/other.html", {"x": {"message": "y"}})

    assert [e["old_text"] for e in state.load_finalized_edits_history(html)] == ["v1", "v2"]
    assert len(state.load_finalized_edits_history()) == 2


def test_finalized_edits_unchanged_if_history_cannot_be_written(tmp_path, monkeypatch):
    """The history is written first - if that fails, the stored edits must
    not have been modified (so the old text is never removed unarchived)."""
    _patch_finalized(monkeypatch, tmp_path)
    html = "/path/to/chatlog.html"
    state.save_finalized_edits(html, {"msg1": {"message": "precious"}})

    def failing_append(entries):
        raise OSError("disk full")

    monkeypatch.setattr(state, "_append_finalized_edits_history", failing_append)

    with pytest.raises(OSError):
        state.save_finalized_edits(html, {"msg1": {"message": None}})

    assert state.load_finalized_edits(html) == {"msg1": {"message": "precious"}}


def test_load_finalized_edits_returns_none_for_unknown_path_when_file_exists(tmp_path, monkeypatch):
    """load returns None for a chatlog that isn't in the file, even when the
    file already exists with entries for other chatlogs."""
    _patch_finalized(monkeypatch, tmp_path)

    state.save_finalized_edits("/path/to/known.html", {"msg": {"message": "text"}})

    assert state.load_finalized_edits("/path/to/unknown.html") is None


def test_save_finalized_edits_stores_multiple_messages_in_one_call(tmp_path, monkeypatch):
    """A batch save with several message_ids must store all of them."""
    _patch_finalized(monkeypatch, tmp_path)
    html = "/path/to/chatlog.html"
    edits = {
        "msg1": {"message": "text 1", "spacer_end": r"\n\n\n\n"},
        "msg2": {"ocr0": "image ocr"},
        "msg3": {"message": "text 3", "ocr0": "image text"},
    }

    state.save_finalized_edits(html, edits)

    assert state.load_finalized_edits(html) == edits


def test_finalized_edits_recover_from_backup_when_primary_corrupt(tmp_path, monkeypatch):
    """Mimics a crash mid-write: finalized_edits.json left corrupt, but the
    previous good data survives in the .bak sibling."""
    _patch_finalized(monkeypatch, tmp_path)
    finalized_file = tmp_path / "finalized_edits.json"
    backup_file = finalized_file.with_suffix(".bak")
    html = "/path/to/chatlog.html"

    state.save_finalized_edits(html, {"msg1": {"message": "good text"}})
    state.save_finalized_edits(html, {"msg1": {"message": "overwritten"}})
    finalized_file.write_text("", encoding="utf8")

    result = state.load_finalized_edits(html)
    assert result == {"msg1": {"message": "good text"}}
    assert backup_file.exists()


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


def _backups_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(config, "SESSION_BACKUPS_FILE", tmp_path / "session_backups.json")


def test_load_session_backups_returns_empty_when_no_file(tmp_path, monkeypatch):
    _backups_config(tmp_path, monkeypatch)

    assert state.load_session_backups("chat.html") == []


def test_archive_session_backup_round_trip(tmp_path, monkeypatch):
    _backups_config(tmp_path, monkeypatch)

    session = {"output_path": "out.txt", "edited_texts": {"1": {"message": "edited"}}}
    state.archive_session_backup("chat.html", session)

    assert state.load_session_backups("chat.html") == [session]


def test_archive_session_backup_orders_most_recent_first(tmp_path, monkeypatch):
    _backups_config(tmp_path, monkeypatch)

    state.archive_session_backup("chat.html", {"output_path": "first.txt"})
    state.archive_session_backup("chat.html", {"output_path": "second.txt"})
    state.archive_session_backup("chat.html", {"output_path": "third.txt"})

    backups = state.load_session_backups("chat.html")
    assert [b["output_path"] for b in backups] == ["third.txt", "second.txt", "first.txt"]


def test_archive_session_backup_caps_at_session_backup_count(tmp_path, monkeypatch):
    _backups_config(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SESSION_BACKUP_COUNT", 3)

    for i in range(5):
        state.archive_session_backup("chat.html", {"output_path": f"v{i}.txt"})

    backups = state.load_session_backups("chat.html")
    assert [b["output_path"] for b in backups] == ["v4.txt", "v3.txt", "v2.txt"]


def test_archive_session_backup_dedupes_identical_consecutive_entry(tmp_path, monkeypatch):
    """A pending session loaded then immediately declined (or accepted,
    with no edits made before the next archive) must not burn two backup
    slots on the exact same content."""
    _backups_config(tmp_path, monkeypatch)

    session = {"output_path": "out.txt"}
    state.archive_session_backup("chat.html", session)
    state.archive_session_backup("chat.html", dict(session))

    assert state.load_session_backups("chat.html") == [session]


def test_archive_session_backup_does_not_dedupe_non_consecutive_repeat(tmp_path, monkeypatch):
    """Deduping only looks at the single most recent entry - the same
    content recurring later (e.g. after round-tripping back through an
    edit and an undo) is still a real, distinct session boundary and
    should be archived again."""
    _backups_config(tmp_path, monkeypatch)

    state.archive_session_backup("chat.html", {"output_path": "a.txt"})
    state.archive_session_backup("chat.html", {"output_path": "b.txt"})
    state.archive_session_backup("chat.html", {"output_path": "a.txt"})

    backups = state.load_session_backups("chat.html")
    assert [b["output_path"] for b in backups] == ["a.txt", "b.txt", "a.txt"]


def test_archive_session_backup_keeps_different_html_paths_independent(tmp_path, monkeypatch):
    _backups_config(tmp_path, monkeypatch)

    state.archive_session_backup("chat_a.html", {"output_path": "a.txt"})
    state.archive_session_backup("chat_b.html", {"output_path": "b.txt"})

    assert [b["output_path"] for b in state.load_session_backups("chat_a.html")] == ["a.txt"]
    assert [b["output_path"] for b in state.load_session_backups("chat_b.html")] == ["b.txt"]


def test_clear_session_archives_the_cleared_session(tmp_path, monkeypatch):
    _backups_config(tmp_path, monkeypatch)

    state.save_session("chat.html", {"output_path": "final.txt", "edited_texts": []})
    state.clear_session("chat.html")

    backups = state.load_session_backups("chat.html")
    assert [b["output_path"] for b in backups] == ["final.txt"]


def test_clear_session_is_a_noop_for_backups_when_no_session(tmp_path, monkeypatch):
    _backups_config(tmp_path, monkeypatch)

    state.clear_session("chat.html")  # should not raise, nothing to archive

    assert state.load_session_backups("chat.html") == []


def test_session_backups_survive_being_overwritten_by_a_later_session(tmp_path, monkeypatch):
    """The core scenario this feature exists for: closing the app
    mid-review (leaving a resumable session), then later resuming and
    continuing to edit - the prior session's final state must not vanish
    just because the live sessions.json entry for that chatlog moved on."""
    _backups_config(tmp_path, monkeypatch)

    # Session 1 ends (app closed mid-review, leaving a resumable session).
    state.save_session("chat.html", {"output_path": "out.txt", "edited_texts": {"1": "session 1 final"}})

    # App restarts, finds the pending session, archives it, then resumes
    # and continues editing (mirroring App._on_start/_resume_session).
    pending = state.load_session("chat.html")
    state.archive_session_backup("chat.html", pending)
    state.save_session("chat.html", {"output_path": "out.txt", "edited_texts": {"1": "session 2 final"}})

    assert state.load_session("chat.html")["edited_texts"] == {"1": "session 2 final"}
    backups = state.load_session_backups("chat.html")
    assert [b["edited_texts"] for b in backups] == [{"1": "session 1 final"}]


def test_session_backups_recover_from_backup_when_primary_corrupt(tmp_path, monkeypatch):
    _backups_config(tmp_path, monkeypatch)
    backups_file = tmp_path / "session_backups.json"
    backup_file = backups_file.with_suffix(".bak")

    state.archive_session_backup("chat.html", {"output_path": "good.txt"})
    state.archive_session_backup("chat.html", {"output_path": "overwritten.txt"})
    backups_file.write_text("", encoding="utf8")

    assert [b["output_path"] for b in state.load_session_backups("chat.html")] == ["good.txt"]
    assert backup_file.exists()


def test_no_session_backup_temp_files_left_after_archiving(tmp_path, monkeypatch):
    _backups_config(tmp_path, monkeypatch)

    state.archive_session_backup("chat.html", {"output_path": "first.txt"})
    state.archive_session_backup("chat.html", {"output_path": "second.txt"})

    assert list(tmp_path.glob("session_backups_*.tmp")) == []


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
