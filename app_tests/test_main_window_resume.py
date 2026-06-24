from pathlib import Path

from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.gui.main_window import _match_focus_slot, _match_saved_edits
from gui_transcription.app.pipeline import ReviewItem


def _item(message_id: str) -> ReviewItem:
    return ReviewItem(
        entry=MessageEntry(message_id=message_id, text_lines=["text"], image_names=[]),
        image_paths=[],
        initial_message_text="text",
        initial_ocr_texts=[],
    )


def _image_item(message_id: str, image_count: int) -> ReviewItem:
    image_names = [f"img{i}.png" for i in range(image_count)]
    return ReviewItem(
        entry=MessageEntry(message_id=message_id, text_lines=[], image_names=image_names),
        image_paths=[Path(name) for name in image_names],
        initial_message_text=None,
        initial_ocr_texts=[""] * image_count,
    )


def test_matches_edits_by_message_id_when_messages_appended_at_end():
    # Saved session only knew about A, B; a re-export then added C after them.
    items = [_item("A"), _item("B"), _item("C")]
    saved_texts = {
        "A": {"message": "edited A", "ocr": []},
        "B": {"message": "edited B", "ocr": []},
    }

    built = _match_saved_edits(items, saved_texts)

    assert built == [("edited A", []), ("edited B", []), (None, [])]


def test_matches_edits_by_message_id_when_messages_inserted_in_middle():
    # B is new in the re-export, inserted between A and C - saved edits for
    # A/C must still land on A/C, not shift onto B/C's old positions. This
    # is the core regression test for the original index-based bug.
    items = [_item("A"), _item("B"), _image_item("C", image_count=1)]
    saved_texts = {
        "A": {"message": "edited A", "ocr": []},
        "C": {"message": None, "ocr": ["edited C ocr"]},
    }

    built = _match_saved_edits(items, saved_texts)

    assert built == [("edited A", []), (None, []), (None, ["edited C ocr"])]


def test_drops_orphaned_edits_for_filtered_out_messages_silently():
    # D no longer appears (e.g. its author was later removed from the
    # approved list) - its saved edit must be dropped without raising.
    items = [_item("A"), _item("B")]
    saved_texts = {
        "A": {"message": "edited A", "ocr": []},
        "D": {"message": "edited D", "ocr": []},
    }

    built = _match_saved_edits(items, saved_texts)

    assert built == [("edited A", []), (None, [])]


def test_matches_edits_aligns_saved_ocr_list_by_position():
    items = [_image_item("A", image_count=2)]
    saved_texts = {"A": {"message": None, "ocr": [None, "edited second image"]}}

    built = _match_saved_edits(items, saved_texts)

    assert built == [(None, [None, "edited second image"])]


def test_matches_edits_pads_short_saved_ocr_list_when_image_count_grew():
    # The re-export now has two images for A, but the saved session only
    # ever knew about one - the new second image's slot must fall back to
    # "not edited" (None) rather than raising an index error.
    items = [_image_item("A", image_count=2)]
    saved_texts = {"A": {"message": None, "ocr": ["edited first image"]}}

    built = _match_saved_edits(items, saved_texts)

    assert built == [(None, ["edited first image", None])]


def test_matches_edits_truncates_long_saved_ocr_list_when_image_count_shrank():
    # The re-export now has only one image for A, but the saved session
    # had edits for two - the extra saved entry must be ignored, not
    # misapplied to a different image.
    items = [_image_item("A", image_count=1)]
    saved_texts = {"A": {"message": None, "ocr": ["edited first image", "edited second image"]}}

    built = _match_saved_edits(items, saved_texts)

    assert built == [(None, ["edited first image"])]


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

    assert _match_focus_slot(items, ["C", "ocr0"]) == (2, "ocr0")
