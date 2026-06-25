from pathlib import Path

from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.gui.main_window import _match_focus_slot, _match_saved_edits
from gui_transcription.app.review_item import ReviewItem


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
        "A": {"message": "edited A"},
        "B": {"message": "edited B"},
    }

    built = _match_saved_edits(items, saved_texts)

    assert built == [{"message": "edited A"}, {"message": "edited B"}, {}]


def test_matches_edits_by_message_id_when_messages_inserted_in_middle():
    # B is new in the re-export, inserted between A and C - saved edits for
    # A/C must still land on A/C, not shift onto B/C's old positions. This
    # is the core regression test for the original index-based bug.
    items = [_item("A"), _item("B"), _image_item("C", image_count=1)]
    saved_texts = {
        "A": {"message": "edited A"},
        "C": {"ocr0": "edited C ocr"},
    }

    built = _match_saved_edits(items, saved_texts)

    assert built == [{"message": "edited A"}, {}, {"ocr0": "edited C ocr"}]


def test_drops_orphaned_edits_for_filtered_out_messages_silently():
    # D no longer appears (e.g. its author was later removed from the
    # approved list) - its saved edit must be dropped without raising.
    items = [_item("A"), _item("B")]
    saved_texts = {
        "A": {"message": "edited A"},
        "D": {"message": "edited D"},
    }

    built = _match_saved_edits(items, saved_texts)

    assert built == [{"message": "edited A"}, {}]


def test_matches_edits_keeps_only_roles_the_current_item_actually_has():
    items = [_image_item("A", image_count=2)]
    saved_texts = {"A": {"ocr1": "edited second image"}}

    built = _match_saved_edits(items, saved_texts)

    assert built == [{"ocr1": "edited second image"}]


def test_matches_edits_keeps_existing_role_when_image_count_grew():
    # The re-export now has two images for A, but the saved session only
    # ever knew about one - the new second image's "ocr1" role simply
    # isn't a key in the result, falling back to its default like any
    # other never-edited role, rather than needing an explicit pad.
    items = [_image_item("A", image_count=2)]
    saved_texts = {"A": {"ocr0": "edited first image"}}

    built = _match_saved_edits(items, saved_texts)

    assert built == [{"ocr0": "edited first image"}]


def test_matches_edits_drops_role_no_longer_present_when_image_count_shrank():
    # The re-export now has only one image for A, but the saved session
    # had an edit for a second image's OCR slot too - that role no longer
    # exists on the current item, so it must be dropped, not misapplied
    # to a different image.
    items = [_image_item("A", image_count=1)]
    saved_texts = {"A": {"ocr0": "edited first image", "ocr1": "edited second image"}}

    built = _match_saved_edits(items, saved_texts)

    assert built == [{"ocr0": "edited first image"}]


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
