from pathlib import Path

from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.gui.main_window import _match_focus_slot, _match_saved_edits
from gui_transcription.app.pipeline import ReviewItem


def _item(message_id: str) -> ReviewItem:
    return ReviewItem(
        entry=MessageEntry(message_id=message_id, text_lines=["text"], image_name=None),
        image_path=None,
        initial_message_text="text",
        initial_ocr_text=None,
    )


def test_matches_edits_by_message_id_when_messages_appended_at_end():
    # Saved session only knew about A, B; a re-export then added C after them.
    items = [_item("A"), _item("B"), _item("C")]
    saved_texts = {
        "A": {"message": "edited A", "ocr": None},
        "B": {"message": "edited B", "ocr": None},
    }

    built = _match_saved_edits(items, saved_texts)

    assert built == [("edited A", None), ("edited B", None), (None, None)]


def test_matches_edits_by_message_id_when_messages_inserted_in_middle():
    # B is new in the re-export, inserted between A and C - saved edits for
    # A/C must still land on A/C, not shift onto B/C's old positions. This
    # is the core regression test for the original index-based bug.
    items = [_item("A"), _item("B"), _item("C")]
    saved_texts = {
        "A": {"message": "edited A", "ocr": None},
        "C": {"message": None, "ocr": "edited C ocr"},
    }

    built = _match_saved_edits(items, saved_texts)

    assert built == [("edited A", None), (None, None), (None, "edited C ocr")]


def test_drops_orphaned_edits_for_filtered_out_messages_silently():
    # D no longer appears (e.g. its author was later removed from the
    # approved list) - its saved edit must be dropped without raising.
    items = [_item("A"), _item("B")]
    saved_texts = {
        "A": {"message": "edited A", "ocr": None},
        "D": {"message": "edited D", "ocr": None},
    }

    built = _match_saved_edits(items, saved_texts)

    assert built == [("edited A", None), (None, None)]


def test_focus_slot_falls_back_to_none_when_message_id_not_present():
    items = [_item("A"), _item("B")]

    assert _match_focus_slot(items, ["D", "message"]) is None


def test_focus_slot_returns_none_for_no_saved_focus():
    items = [_item("A"), _item("B")]

    assert _match_focus_slot(items, None) is None


def test_focus_slot_translates_message_id_to_new_index_after_insertion():
    # C was focused when autosaved at index 1; after B is inserted before
    # it in a re-export, C is now at index 2 - the slot must follow C, not
    # stay pinned to index 1.
    items = [_item("A"), _item("B"), _item("C")]

    assert _match_focus_slot(items, ["C", "ocr"]) == (2, "ocr")
