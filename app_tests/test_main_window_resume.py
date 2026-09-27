from pathlib import Path

from discord_transcription.chatlog import MessageEntry
from discord_transcription.gui.main_window import (
    _build_finalized_updates,
    _match_finalized_edits,
    _match_focus_slot,
    _match_saved_edits,
    _match_touched_slots,
)
from discord_transcription.review_item import ReviewItem


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


# -- _match_finalized_edits tests -------------------------------------------

def test_match_finalized_edits_basic_mapping():
    items = [_item("A"), _item("B")]
    finalized = {"A": {"message": "final A"}, "B": {"message": "final B"}}

    built = _match_finalized_edits(items, finalized)

    assert built == [{"message": "final A"}, {"message": "final B"}]


def test_match_finalized_edits_drops_orphaned_message_id():
    """An entry whose message_id no longer appears in the current item list
    (e.g. the author was later removed from the approved list) is dropped."""
    items = [_item("A")]
    finalized = {"A": {"message": "final A"}, "GONE": {"message": "orphan"}}

    built = _match_finalized_edits(items, finalized)

    assert built == [{"message": "final A"}]


def test_match_finalized_edits_filters_to_current_slot_roles():
    """A stored role that no longer exists on the current item (e.g. a second
    image was removed in a re-export) is dropped rather than misapplied."""
    items = [_image_item("A", image_count=1)]
    finalized = {"A": {"ocr0": "first image", "ocr1": "stale second image"}}

    built = _match_finalized_edits(items, finalized)

    assert built == [{"ocr0": "first image"}]


def test_match_finalized_edits_with_inserted_message():
    """After a re-export inserts B between A and C, finalized edits for A
    and C must still land on A and C rather than shifting by position."""
    items = [_item("A"), _item("B"), _image_item("C", image_count=1)]
    finalized = {"A": {"message": "final A"}, "C": {"ocr0": "final C ocr"}}

    built = _match_finalized_edits(items, finalized)

    assert built == [{"message": "final A"}, {}, {"ocr0": "final C ocr"}]


def test_match_finalized_edits_maps_multiple_ocr_slots_for_multi_image_message():
    """A message with two images has ocr0 and ocr1 slots; both must be
    mapped onto the correct item independently."""
    items = [_image_item("A", image_count=2)]
    finalized = {"A": {"ocr0": "first image text", "ocr1": "second image text"}}

    built = _match_finalized_edits(items, finalized)

    assert built == [{"ocr0": "first image text", "ocr1": "second image text"}]


def test_match_finalized_edits_includes_spacer_roles_in_slot_roles():
    """Spacer roles (spacer_end, spacer_msg_img, etc.) are part of each
    item's slot_roles, so stored spacer edits must pass the role filter
    and land on the item correctly."""
    items = [_item("A")]
    finalized = {"A": {"message": "msg", "spacer_end": r"\n\n\n\n"}}

    built = _match_finalized_edits(items, finalized)

    assert built == [{"message": "msg", "spacer_end": r"\n\n\n\n"}]


def test_match_finalized_edits_returns_empty_dicts_for_all_items_when_finalized_is_empty():
    """An empty finalized dict (no runs ever finalized) must produce an
    all-empty per-item list, not raise or produce fewer items than requested."""
    items = [_item("A"), _item("B")]

    built = _match_finalized_edits(items, {})

    assert built == [{}, {}]


# -- touched slots / finalized-edit updates ------------------------------------

def test_match_touched_slots_maps_message_ids_to_current_indices():
    items = [_item("a"), _image_item("b", 1)]

    matched = _match_touched_slots(items, [["b", "ocr0"], ["a", "message"]])

    assert matched == {(1, "ocr0"), (0, "message")}


def test_match_touched_slots_drops_vanished_messages_roles_and_junk():
    items = [_item("a"), _image_item("b", 1)]

    matched = _match_touched_slots(
        items, [["gone", "message"], ["b", "ocr3"], "junk", ["a"], ["a", "message"]]
    )

    assert matched == {(0, "message")}
    assert _match_touched_slots(items, None) == set()


def test_build_finalized_updates_stores_edits_and_removes_only_touched_reverts():
    items = [_item("a"), _image_item("b", 2)]
    edited_texts = [
        {"message": "edited", "spacer_end": None},
        {"ocr0": None, "spacer_img0": None, "ocr1": None, "spacer_end": None},
    ]
    # b's ocr0 was deliberately unticked; b's ocr1 is unticked/default but
    # was never acted on, so any stored edit for it must be left alone.
    touched = {(0, "message"), (1, "ocr0")}

    updates = _build_finalized_updates(items, edited_texts, touched)

    assert updates == {
        "a": {"message": "edited"},
        "b": {"ocr0": None},
    }


def test_build_finalized_updates_leaves_untouched_default_boxes_out_entirely():
    items = [_item("a")]

    assert _build_finalized_updates(items, [{"message": None, "spacer_end": None}], set()) == {}
