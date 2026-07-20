"""ReviewFrame's windowing core (_reconcile/_sync_materialized_rows/
_remeasure_built_rows/_ensure_materialized) had no automated test coverage
before this - only the pure math it delegates to (virtualization.py) was
tested, leaving the actual stateful integration (which rows are real Tk
widgets at any moment, scroll-position correction, edit preservation
across paging) resting entirely on the README's manual smoke test. This is
explicitly the most failure-prone part of the app (see ARCHITECTURE.md's
account of the oscillation bug its current design replaced), so it's the
highest-value gap to close.
"""
import random
import tkinter as tk

import pytest
from PIL import Image

from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.gui.layout_constants import ROW_PACK_PADY_PX
from gui_transcription.app.gui.review_view import ReviewFrame
from gui_transcription.app.review_item import ReviewItem, build_review_items

# Unlike the other GUI-backed test files, this one can't withdraw() its
# root - a withdrawn window never gets real pixel geometry, which these
# tests need to verify actual row layout - so it's the one that visibly
# (briefly) appears on screen. Marked "gui" and excluded from the default
# run (see pyproject.toml's addopts) so it doesn't interrupt other work on
# the same machine; run explicitly with `-m gui` when you want to exercise
# the windowing core's actual layout, not just its decision logic.
pytestmark = pytest.mark.gui


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    # Deliberately not withdrawn: a withdrawn/never-mapped window never gets
    # real pixel geometry from the window manager, so the canvas's
    # winfo_height() would stay <=1 forever and _apply_initial_position's
    # poll-until-sized loop would never settle - this needs the same real
    # layout pass the manual smoke test relies on.
    r.geometry("900x700")
    yield r
    r.destroy()


@pytest.fixture
def sample_image(tmp_path):
    path = tmp_path / "sample.png"
    Image.new("RGB", (800, 400), color="blue").save(path)
    return path


def _two_image_items(sample_image):
    """A single message with two image attachments - used to test that
    finalized edits for ocr0 and ocr1 are independent of each other."""
    entries = [
        MessageEntry(message_id="multi", text_lines=[], image_names=["sample.png", "sample.png"])
    ]
    file_info = {"sample.png": ["ocr default"]}
    return build_review_items(entries, file_info, image_folder=sample_image.parent)


def _items(sample_image, count=40):
    """A mix of text-only, image-only, and image-with-caption rows, in a
    fixed repeating pattern - the same three row shapes _build_row has to
    lay out differently from one another. Built via build_review_items
    (not constructed by hand) so every item's spacer slots get properly
    populated defaults, the same way a real run's items would."""
    entries = []
    for i in range(count):
        if i % 3 == 0:
            entries.append(MessageEntry(message_id=str(i), text_lines=[f"text only {i}"], image_names=[]))
        elif i % 3 == 1:
            entries.append(MessageEntry(message_id=str(i), text_lines=[], image_names=["sample.png"]))
        else:
            entries.append(MessageEntry(message_id=str(i), text_lines=[f"caption {i}"], image_names=["sample.png"]))
    file_info = {"sample.png": ["ocr text"]}
    return build_review_items(entries, file_info, image_folder=sample_image.parent)


def _build_frame(root, items, **kwargs):
    finalized = []
    frame = ReviewFrame(root, items, finalized.append, **kwargs)
    frame.pack(fill="both", expand=True)
    # _apply_initial_position is scheduled via after_idle, and polls itself
    # via self.after(20, ...) until the canvas reports a real height - drive
    # the event loop (not just update_idletasks, which skips timer events)
    # until that settles, the same way a real mainloop tick would.
    for _ in range(20):
        root.update()
        if frame._materialized_range is not None:
            break
    return frame, finalized


def test_initial_build_materializes_a_window_starting_at_top(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    assert frame._materialized_range is not None
    first, last = frame._materialized_range
    assert first == 0
    assert last < len(items) - 1  # not every row materialized up front
    assert sorted(frame._row_frames) == list(range(first, last + 1))


def test_reconcile_is_idempotent_with_no_scroll_movement(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    range_before = frame._materialized_range
    rows_before = dict(frame._row_frames)

    frame._reconcile()

    assert frame._materialized_range == range_before
    assert frame._row_frames == rows_before


def test_scrolling_to_the_end_materializes_the_last_item(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)

    frame._canvas.yview_moveto(1.0)
    frame._reconcile()

    first, last = frame._materialized_range
    assert last == len(items) - 1
    assert sorted(frame._row_frames) == list(range(first, last + 1))


def test_ensure_materialized_jumps_to_a_far_away_row_without_crashing(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    target = len(items) - 1

    frame._ensure_materialized(target)

    assert target in frame._row_frames


def test_misspelled_word_gets_tagged_in_a_content_box(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    first_text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (first_text_item, "message")
    widget = frame._text_widgets[key]

    widget.delete("1.0", "end")
    widget.insert("1.0", "this is definitly garbld")
    # Debounced in real use (row_building.SPELLCHECK_DEBOUNCE_MS) - run the
    # pass directly rather than waiting on the timer, same as other tests
    # here call internal methods directly instead of driving real timing.
    frame._run_spellcheck(key, widget)

    ranges = widget.tag_ranges("misspelled")
    assert len(ranges) > 0
    tagged_words = {
        widget.get(ranges[i], ranges[i + 1]) for i in range(0, len(ranges), 2)
    }
    assert tagged_words == {"definitly", "garbld"}


def test_correctly_spelled_content_box_gets_no_tag(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    first_text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (first_text_item, "message")
    widget = frame._text_widgets[key]

    widget.delete("1.0", "end")
    widget.insert("1.0", "this is a perfectly normal sentence")
    frame._run_spellcheck(key, widget)

    assert widget.tag_ranges("misspelled") == ()


def test_spacer_box_never_gets_the_misspelled_tag_configured(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    spacer_key = next(k for k in frame._text_widgets if k[1].startswith("spacer"))
    widget = frame._text_widgets[spacer_key]

    assert "misspelled" not in widget.tag_names()
    assert spacer_key not in frame._spellcheck_after_ids


def test_spellcheck_tag_is_reapplied_after_a_row_is_paged_out_and_back_in(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    first_text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (first_text_item, "message")
    widget = frame._text_widgets[key]
    widget.delete("1.0", "end")
    widget.insert("1.0", "definitly misspelled")
    frame._run_spellcheck(key, widget)
    assert widget.tag_ranges("misspelled") != ()

    # Page far away (tears the row down, destroying that Text widget - tags
    # live on the widget instance, not text_undo.py's UndoLog, so they don't
    # survive this the way edited text/undo history do) and back to the top.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    rebuilt_widget = frame._text_widgets[key]
    assert rebuilt_widget is not widget
    # The rebuild schedules its own debounced pass (_build_editable_text_box)
    # rather than applying immediately - run it directly, as above.
    frame._run_spellcheck(key, rebuilt_widget)
    assert rebuilt_widget.tag_ranges("misspelled") != ()


def test_destroying_a_row_cancels_its_pending_spellcheck_timer(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    key = next(k for k in frame._text_widgets if k[1] == "message")
    assert key in frame._spellcheck_after_ids

    frame._destroy_row(key[0])

    assert key not in frame._spellcheck_after_ids


def test_edited_text_survives_a_row_being_paged_out_and_back_in(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    first_text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)

    widget = frame._text_widgets[(first_text_item, "message")]
    widget.delete("1.0", "end")
    widget.insert("1.0", "an edit the user made")

    # Page far away (tears the edited row down) and back to the top again.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    assert (first_text_item, "message") in frame._text_widgets
    restored = frame._text_widgets[(first_text_item, "message")].get("1.0", "end-1c")
    assert restored == "an edit the user made"


def test_undo_history_survives_a_row_being_paged_out_and_back_in(root, sample_image):
    """The whole point of text_undo.py: Tk's undo stack lives on the Text
    widget instance, which is destroyed and rebuilt fresh on every page
    out/in - without replaying the recorded ops back onto the new widget,
    Ctrl+Z here would have nothing to undo."""
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    first_text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (first_text_item, "message")
    original = items[first_text_item].initial_message_text

    widget = frame._text_widgets[key]
    widget.insert("end", " edited")

    # Page far away (tears the edited row down) and back to the top again.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    rebuilt = frame._text_widgets[key]
    assert rebuilt.get("1.0", "end-1c") == original + " edited"

    frame._undo_text(type("Event", (), {"widget": rebuilt})())

    assert rebuilt.get("1.0", "end-1c") == original


def test_undo_after_unchecking_an_ocr_box_restores_the_edit_and_rechecks_it(root, sample_image):
    """Unchecking an OCR box is itself an undoable delete/insert (see
    RowBuildingMixin._on_ocr_checkbox_toggle) - Ctrl+Z right after must
    bring the edited text back, and _undo_text's compare-to-default resync
    must re-check the checkbox to match, rather than leaving it unchecked
    while the edited text is back on screen (per the project owner's
    decision: undo/redo always re-derives checked state, never the
    unconditional "any change checks the box" rule typing uses)."""
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")
    default_text = items[image_item].initial_ocr_texts[0]
    widget = frame._text_widgets[key]
    widget.focus_force()
    root.update_idletasks()
    widget.insert("end", " typed")
    root.update()
    edited_text = widget.get("1.0", "end-1c")

    var = frame._checkbox_vars[key]
    var.set(False)
    frame._on_ocr_checkbox_toggle(key)
    assert widget.get("1.0", "end-1c") == default_text
    assert frame._checkbox_checked[key] is False

    frame._undo_text(type("Event", (), {"widget": widget})())

    assert widget.get("1.0", "end-1c") == edited_text
    assert frame._checkbox_checked[key] is True
    assert frame._checkbox_vars[key].get() is True


def test_focusing_a_box_scrolls_the_whole_box_fully_into_view_not_just_its_row(root, sample_image):
    """Regression test: _scroll_into_view (this method's predecessor) only
    checked a row's outer bounds. A multi-box row (caption + image, here)
    can be taller than the viewport, so a box near the row's bottom could
    end up only slightly overlapping the viewport edge - or almost entirely
    covered - without triggering a scroll, since the row-level bounds
    (spanning every box in it) could already satisfy that check.
    _scroll_box_into_view checks the focused box's own bounds instead, so
    it always ends up fully onscreen after a Tab/focus."""
    items = _items(sample_image, count=6)
    frame, _ = _build_frame(root, items)
    caption_image_index = next(
        i for i, item in enumerate(items) if item.initial_message_text and item.image_paths
    )
    key = (caption_image_index, "ocr0")

    # Scroll so only a 5px sliver of this row's bottom box - its very top
    # edge - pokes into view at the bottom of the viewport; the rest of the
    # box sits below it, offscreen.
    container_top, container_bottom = _container_bounds(frame, *key)
    total_height = sum(frame._row_heights)
    viewport_height = frame._canvas.winfo_height()
    target_view_bottom = container_top + 5
    frame._canvas.yview_moveto(max(target_view_bottom - viewport_height, 0) / total_height)
    frame._reconcile()
    root.update_idletasks()

    view_bottom_before = frame._canvas.canvasy(frame._canvas.winfo_height())
    assert container_top < view_bottom_before < container_bottom  # only a sliver overlaps

    frame._focus_text_box(*key)
    root.update_idletasks()

    container_top, container_bottom = _container_bounds(frame, *key)
    view_top = frame._canvas.canvasy(0)
    view_bottom = frame._canvas.canvasy(frame._canvas.winfo_height())
    assert container_top >= view_top - 1
    assert container_bottom <= view_bottom + 1


def test_collect_edited_texts_returns_initial_text_for_untouched_items(root, sample_image):
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)

    collected = frame.collect_edited_texts()

    assert len(collected) == len(items)
    for edited, item in zip(collected, items):
        for role in item.slot_roles:
            if role.startswith("ocr"):
                # An untouched OCR box's checkbox starts unchecked, so
                # _get_box_text reports None (meaning "use the default")
                # rather than the literal (default) text it displays - see
                # ReviewFrame._get_box_text.
                assert edited[role] is None
            else:
                assert edited[role] == item.initial_text_for_role(role)


def test_ocr_checkbox_starts_unchecked_for_an_untouched_box(root, sample_image):
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")

    assert frame._checkbox_checked[key] is False
    assert frame._checkbox_vars[key].get() is False
    assert frame._text_widgets[key].get("1.0", "end-1c") == items[image_item].initial_ocr_texts[0]


def test_ocr_checkbox_starts_checked_for_a_resumed_edit_differing_from_default(root, sample_image):
    items = _items(sample_image, count=5)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    saved_texts = [{} for _ in items]
    saved_texts[image_item] = {"ocr0": "a resumed ocr edit"}

    frame, _ = _build_frame(root, items, initial_saved_texts=saved_texts)
    key = (image_item, "ocr0")

    assert frame._checkbox_checked[key] is True
    assert frame._checkbox_vars[key].get() is True
    assert frame._text_widgets[key].get("1.0", "end-1c") == "a resumed ocr edit"


def test_typing_into_an_ocr_box_checks_its_checkbox(root, sample_image):
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")
    widget = frame._text_widgets[key]
    widget.focus_force()  # focus_set() alone doesn't reliably win real OS focus in a test run
    root.update_idletasks()

    widget.insert("end", " typed")
    root.update()  # let the queued <<Modified>> event fire

    assert frame._checkbox_checked[key] is True
    assert frame._checkbox_vars[key].get() is True


def test_unchecking_then_rechecking_an_ocr_box_round_trips_both_versions(root, sample_image):
    """Unchecking must restore the OCR default without discarding the
    user-edited version, and rechecking must bring that edit back - the
    save isn't overwritten by either toggle, only by typing while
    unchecked (see test_typing_while_unchecked_starts_a_fresh_edit)."""
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")
    default_text = items[image_item].initial_ocr_texts[0]
    widget = frame._text_widgets[key]
    widget.focus_force()
    root.update_idletasks()
    widget.insert("end", " typed")
    root.update()
    edited_text = widget.get("1.0", "end-1c")
    assert edited_text != default_text
    assert frame._checkbox_vars[key].get() is True

    var = frame._checkbox_vars[key]
    var.set(False)
    frame._on_ocr_checkbox_toggle(key)

    assert widget.get("1.0", "end-1c") == default_text
    assert frame._checkbox_checked[key] is False
    assert frame._user_edited_texts[key] == edited_text  # not discarded

    var.set(True)
    frame._on_ocr_checkbox_toggle(key)

    assert widget.get("1.0", "end-1c") == edited_text
    assert frame._checkbox_checked[key] is True


def test_collect_edited_texts_reports_none_for_an_unchecked_ocr_box(root, sample_image):
    """Even though the box still has live, different-from-default text
    cached for restoration (self._user_edited_texts), collect_edited_texts
    - what autosave/the session file persist - must report None while
    unchecked, since unchecked means "use the default" (see
    ReviewFrame._get_box_text). The actual Finalize output is unaffected
    either way, since None already falls back to the same default text."""
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")
    widget = frame._text_widgets[key]
    widget.focus_force()
    root.update_idletasks()
    widget.insert("end", " typed")
    root.update()
    var = frame._checkbox_vars[key]
    var.set(False)
    frame._on_ocr_checkbox_toggle(key)

    collected = frame.collect_edited_texts()

    assert collected[image_item]["ocr0"] is None


def test_ocr_checkbox_state_and_both_versions_survive_paging_out_and_back_in(root, sample_image):
    items = _items(sample_image, count=40)
    frame, _ = _build_frame(root, items)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")
    default_text = items[image_item].initial_ocr_texts[0]
    widget = frame._text_widgets[key]
    widget.focus_force()
    root.update_idletasks()
    widget.insert("end", " typed")
    root.update()
    edited_text = widget.get("1.0", "end-1c")

    var = frame._checkbox_vars[key]
    var.set(False)
    frame._on_ocr_checkbox_toggle(key)

    # Page far away (tears the row down) and back to the top again.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    assert key in frame._text_widgets
    assert frame._checkbox_checked[key] is False
    assert frame._checkbox_vars[key].get() is False
    assert frame._text_widgets[key].get("1.0", "end-1c") == default_text
    assert frame._user_edited_texts[key] == edited_text


def test_finalize_button_visible_for_a_transcript_that_fits_on_screen(root, sample_image):
    items = _items(sample_image, count=2)
    frame, _ = _build_frame(root, items)
    assert frame._finalize_button_visible is True


def test_finalize_button_hidden_until_scrolled_to_the_end_of_a_long_transcript(root, sample_image):
    items = _items(sample_image, count=40)
    frame, _ = _build_frame(root, items)
    assert frame._finalize_button_visible is False

    frame._canvas.yview_moveto(1.0)
    frame._reconcile()
    assert frame._finalize_button_visible is True


def test_finalize_collects_current_edits_and_calls_on_finalize(root, sample_image):
    items = _items(sample_image, count=3)
    frame, finalized = _build_frame(root, items)

    frame._on_finalize_clicked()

    assert len(finalized) == 1
    assert len(finalized[0]) == len(items)


def test_spacer_slot_boxes_are_one_line_tall_and_hold_their_own_text(root, sample_image):
    items = [
        ReviewItem(
            entry=MessageEntry(message_id="0", text_lines=["caption"], image_names=["sample.png"]),
            image_paths=[sample_image], initial_message_text="caption", initial_ocr_texts=["ocr"],
            initial_spacer_texts={"spacer_msg_img": "\\n\\n", "spacer_end": "\\n\\n\\n\\n"},
        ),
    ]
    frame, _ = _build_frame(root, items)

    spacer_widget = frame._text_widgets[(0, "spacer_msg_img")]
    assert int(spacer_widget.cget("height")) == 1
    assert spacer_widget.get("1.0", "end-1c") == "\\n\\n"

    end_widget = frame._text_widgets[(0, "spacer_end")]
    assert int(end_widget.cget("height")) == 1
    assert end_widget.get("1.0", "end-1c") == "\\n\\n\\n\\n"


def test_resuming_session_restores_saved_edit_and_focus(root, sample_image):
    """Real OS/window-manager focus delivery is too flaky to assert on
    directly in an automated run (several Tk windows get created/destroyed
    across this test session) - so this checks the deterministic part
    instead: _apply_initial_position actually calls _focus_text_box with
    the resumed (index, role) slot, which is what would put real focus
    there in a live app."""
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    saved_texts = [{} for _ in items]
    saved_texts[text_item] = {"message": "a resumed edit"}

    finalized = []
    frame = ReviewFrame(
        root, items, finalized.append,
        initial_saved_texts=saved_texts,
        initial_focus_slot=(text_item, "message"),
    )
    frame.pack(fill="both", expand=True)

    focus_calls = []
    original_focus_text_box = frame._focus_text_box
    def _spy_focus_text_box(index, role):
        focus_calls.append((index, role))
        return original_focus_text_box(index, role)
    frame._focus_text_box = _spy_focus_text_box

    for _ in range(20):
        root.update()
        if frame._materialized_range is not None:
            break

    widget = frame._text_widgets[(text_item, "message")]
    assert widget.get("1.0", "end-1c") == "a resumed edit"
    assert focus_calls == [(text_item, "message")]


def test_resumed_edit_survives_being_paged_out_and_back_in_with_no_further_edits(root, sample_image):
    """Regression test for a real data-loss bug: a resumed box's first
    build correctly showed the saved edit (the case
    test_resuming_session_restores_saved_edit_and_focus covers), but its
    UndoLog was seeded empty with no record of *which* text it started
    from. The next time that same box was torn down and rebuilt - here,
    with zero further edits in between - _populate_text_box re-based on
    the item's plain initial_message_text instead of the resumed edit and
    replayed an empty op log on top, silently reverting to the unedited
    default. Combines the two scenarios test_resuming_session_restores_
    saved_edit_and_focus and test_edited_text_survives_a_row_being_paged_
    out_and_back_in each cover separately - neither alone caught this,
    since the bug only appears once both are true at once."""
    # count=40 (not the smaller count the resume-focus test above uses) -
    # with too few items, the whole transcript fits inside the
    # virtualization buffer and text_item's row is never actually torn
    # down by the page-away below, which would make this test pass
    # regardless of whether the bug it's guarding against is present.
    items = _items(sample_image, count=40)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    assert items[text_item].initial_message_text != "a resumed edit"
    saved_texts = [{} for _ in items]
    saved_texts[text_item] = {"message": "a resumed edit"}

    frame, _ = _build_frame(root, items, initial_saved_texts=saved_texts)
    key = (text_item, "message")
    assert frame._text_widgets[key].get("1.0", "end-1c") == "a resumed edit"

    # Page far away (tears the row down with no edits made this build) and
    # back to the top again - no typing in between, matching the real
    # repro (edit made in an earlier session, just scrolled past in this
    # one).
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    assert key in frame._text_widgets
    assert frame._text_widgets[key].get("1.0", "end-1c") == "a resumed edit"


def _container_bounds(frame, index, role):
    """The (top, bottom) of a box's container in the same document-space
    coordinates _keep_cursor_in_viewport computes them in - see that
    method's docstring for why a winfo_rooty() delta against the row (not
    winfo_y(), and not the container's own position relative to the
    repositioned _scroll_frame) is what's reliable here, and ARCHITECTURE.md's
    "Row geometry" section for why ROW_PACK_PADY_PX has to be added on top
    of frame._offset_of(index): that offset is where row `index`'s full
    pack-allocated slot starts, not where its Frame's own visible top edge
    (what the winfo_rooty() delta below is anchored to) actually sits."""
    container = frame._text_containers[(index, role)]
    row = frame._row_frames[index]
    top = (
        frame._offset_of(index) + ROW_PACK_PADY_PX
        + (container.winfo_rooty() - row.winfo_rooty())
    )
    return top, top + container.winfo_height()


def _long_text_items(sample_image, tall_index=5, count=10):
    """A run of short one-line messages with one long, many-line message
    (tall_index) tall enough that its message box - capped at
    TEXT_BOX_MAX_HEIGHT_FRACTION of the canvas - is a sizeable fraction of
    the test window's height, so scrolling to the top or bottom of the
    10-row document reliably leaves part of that one row's box offscreen."""
    long_text = "\n".join(f"line {i}" for i in range(40))
    entries = [
        MessageEntry(
            message_id=str(i),
            text_lines=[long_text if i == tall_index else f"text {i}"],
            image_names=[],
        )
        for i in range(count)
    ]
    return build_review_items(entries, {}, image_folder=sample_image.parent)


def test_keep_cursor_in_viewport_scrolls_down_to_align_box_bottom_with_view_bottom(root, sample_image):
    items = _long_text_items(sample_image)
    frame, _ = _build_frame(root, items)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()
    root.update_idletasks()

    key = (5, "message")
    widget = frame._text_widgets[key]
    widget.focus_set()
    widget.mark_set("insert", "end-1c")
    widget.see("insert")
    root.update_idletasks()

    container_top, container_bottom = _container_bounds(frame, 5, "message")
    view_bottom_before = frame._canvas.canvasy(frame._canvas.winfo_height())
    assert container_bottom > view_bottom_before  # cursor (near box's end) starts offscreen below

    frame._keep_cursor_in_viewport(key, widget)
    root.update_idletasks()

    new_view_bottom = frame._canvas.canvasy(frame._canvas.winfo_height())
    assert abs(new_view_bottom - container_bottom) < 2


def test_keep_cursor_in_viewport_scrolls_up_to_align_box_top_with_view_top(root, sample_image):
    items = _long_text_items(sample_image)
    frame, _ = _build_frame(root, items)
    frame._canvas.yview_moveto(1.0)
    frame._reconcile()
    root.update_idletasks()

    key = (5, "message")
    widget = frame._text_widgets[key]
    widget.focus_set()
    widget.mark_set("insert", "1.0")
    widget.see("insert")
    root.update_idletasks()

    container_top, _ = _container_bounds(frame, 5, "message")
    view_top_before = frame._canvas.canvasy(0)
    assert container_top < view_top_before  # cursor (at box's start) starts offscreen above

    frame._keep_cursor_in_viewport(key, widget)
    root.update_idletasks()

    new_view_top = frame._canvas.canvasy(0)
    assert abs(new_view_top - container_top) < 2


def test_jumping_focus_to_a_far_row_lands_it_fully_within_the_real_canvas_viewport(root, sample_image):
    """Regression test for the bug where Tab/Shift-Tab's scroll-into-view
    silently stopped working partway through a long transcript: the
    document-space model (self._row_heights, self._offset_of) drifted away
    from each row's *real* on-screen position because the vertical gap
    pack() leaves outside a row's own Frame (ROW_PACK_PADY_PX, see
    ARCHITECTURE.md's "Row geometry" section) wasn't counted in either the
    pre-build estimate or the real remeasured height, nor added back when
    keyboard_nav.py converted a document-space offset into a real screen
    comparison. The drift compounded by row, so it only became visible far
    enough into a transcript - this jumps straight to a distant row (the
    same far-away-Tab-target path _ensure_materialized exists for) and
    checks the box's *real* winfo_rooty()/winfo_height() against the
    canvas's, rather than re-deriving the same (potentially still-buggy)
    document-space formula the production code uses, which an earlier
    version of this drift wouldn't have caught."""
    items = _items(sample_image, count=80)
    frame, _ = _build_frame(root, items)
    target_index = next(
        i for i in range(60, len(items)) if any(r.startswith("ocr") for r in items[i].slot_roles)
    )
    target_role = next(role for role in items[target_index].slot_roles if role.startswith("ocr"))

    frame._ensure_materialized(target_index)
    frame._focus_text_box(target_index, target_role)
    root.update()

    canvas = frame._canvas
    container = frame._text_containers[(target_index, target_role)]
    canvas_top = canvas.winfo_rooty()
    canvas_bottom = canvas_top + canvas.winfo_height()
    box_top = container.winfo_rooty()
    box_bottom = box_top + container.winfo_height()

    assert box_top >= canvas_top
    assert box_bottom <= canvas_bottom


def test_resuming_deep_in_a_long_transcript_remeasures_rows_correctly_on_first_build(root, sample_image):
    """Regression test for a real production bug (confirmed via
    scroll_trace.log): jumping straight to a resumed session's deep focus
    slot materializes that whole window of rows in the session's *very
    first* _reconcile call - a brand-new, several-levels-deep widget tree
    that has never been mapped to the screen before. A single
    canvas.update_idletasks() right after building them wasn't always
    enough to let Tk finish laying that tree out: winfo_height() still
    read back 1 (Tk's "no real geometry yet" default) for every row in
    that window, which _remeasure_built_rows took as ground truth and
    wrote into self._row_heights as 2*ROW_PACK_PADY_PX (9px) - permanently,
    since none of those rows get torn down and rebuilt again just because a
    later reconcile runs. That corrupted self._offset_of for every row
    after the resumed one for the rest of the session, by hundreds of px
    per corrupted row - the user-visible symptom was Tab/Shift-Tab's
    scroll-into-view looking broken from the moment a resumed session
    opened. _reconcile now retries update_idletasks() (_settle_pending_
    geometry) until every newly-built row reports real geometry before
    trusting any of their heights."""
    items = _long_text_items(sample_image, tall_index=30, count=80)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    saved_texts = [{} for _ in items]
    deep_index = 60
    saved_texts[deep_index] = {"message": items[deep_index].initial_message_text}

    frame, _ = _build_frame(
        root, items, initial_saved_texts=saved_texts, initial_focus_slot=(deep_index, "message"),
    )

    first, last = frame._materialized_range
    assert deep_index in range(first, last + 1)
    for idx in range(first, last + 1):
        real_height = frame._row_frames[idx].winfo_height() + 2 * ROW_PACK_PADY_PX
        assert frame._row_heights[idx] == real_height, (
            f"row {idx}: recorded height {frame._row_heights[idx]} doesn't match "
            f"its real on-screen height {real_height} - first-build geometry wasn't "
            "settled before being trusted"
        )


def test_jumping_focus_past_a_capped_long_message_row_lands_target_fully_in_view(root, sample_image):
    """Regression test: estimate_row_height's pre-build guess for a
    "message" box has no cap, but the real box is capped at
    TEXT_BOX_MAX_HEIGHT_FRACTION of the canvas (_fixed_text_box_height) -
    so a long message's row is overestimated by a large, fixed amount until
    it's actually built and remeasured. A row skipped entirely by a
    discontinuous jump (resume's saved focus slot, clicking far down the
    scrollbar) never gets remeasured, so that overestimate stays baked into
    every later row's document-space offset - this puts one such long
    message well outside the window built at startup, then jumps straight
    past it to a target several rows further down (without ever walking
    through the long row first, the same far jump _ensure_materialized
    exists for) and checks the target box's real screen position against
    the canvas's."""
    tall_index = 20
    items = _long_text_items(sample_image, tall_index=tall_index, count=40)
    frame, _ = _build_frame(root, items)
    # The long row must not have been part of the window built at startup -
    # otherwise it would already have been remeasured, which isn't the
    # scenario this test is about.
    assert tall_index not in frame._row_frames
    target_index = 35

    frame._ensure_materialized(target_index)
    frame._focus_text_box(target_index, "message")
    root.update()

    canvas = frame._canvas
    container = frame._text_containers[(target_index, "message")]
    canvas_top = canvas.winfo_rooty()
    canvas_bottom = canvas_top + canvas.winfo_height()
    box_top = container.winfo_rooty()
    box_bottom = box_top + container.winfo_height()

    assert box_top >= canvas_top
    assert box_bottom <= canvas_bottom


def test_keep_cursor_in_viewport_does_nothing_when_cursor_already_visible(root, sample_image):
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (text_item, "message")
    widget = frame._text_widgets[key]
    widget.focus_set()
    widget.mark_set("insert", "1.0")
    root.update_idletasks()
    view_before = frame._canvas.yview()

    frame._keep_cursor_in_viewport(key, widget)

    assert frame._canvas.yview() == view_before


def test_destroying_a_focused_rows_box_then_rebuilding_restores_focus_and_cursor(root, sample_image):
    """Simulates the part of a fast Page Up/Down burst that previously just
    dropped focus: _destroy_row tearing down a row whose box currently has
    focus, followed (once scrolling settles) by _build_row materializing
    that same row again. _build_row should notice (via self._refocus_slot)
    that this row's box was the one that lost focus, and restore both focus
    and the exact cursor position - not just re-show the row with the
    cursor reset to its start."""
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    frame, _ = _build_frame(root, items)

    key = (text_item, "message")
    widget = frame._text_widgets[key]
    widget.focus_force()  # focus_set() alone doesn't reliably win real OS focus in a test run
    widget.mark_set("insert", "1.3")
    root.update_idletasks()

    focus_calls = []
    original_focus_text_box = frame._focus_text_box
    def _spy_focus_text_box(index, role):
        focus_calls.append((index, role))
        return original_focus_text_box(index, role)
    frame._focus_text_box = _spy_focus_text_box

    frame._destroy_row(text_item)
    assert frame._refocus_slot == key
    assert frame._saved_cursor[key] == "1.3"

    frame._build_row(text_item)
    root.update()  # let the after_idle-scheduled refocus run

    assert focus_calls == [key]
    assert frame._text_widgets[key].index("insert") == "1.3"


def test_destroying_an_unfocused_rows_box_then_rebuilding_does_not_steal_focus(root, sample_image):
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    frame, _ = _build_frame(root, items)
    key = (text_item, "message")
    # Deliberately not focused - _destroy_row should leave self._refocus_slot
    # untouched (None) for a row whose box never had focus.

    frame._destroy_row(text_item)
    assert frame._refocus_slot is None

    frame._build_row(text_item)
    root.update()

    assert frame.focus_get() is None


def test_typing_in_a_focused_box_scrolled_offscreen_scrolls_its_row_back_into_view(root, sample_image):
    """Confirms _on_text_modified's existing snap-back-on-edit behavior:
    scrolling away (e.g. the mouse wheel) never touches Tk's keyboard focus
    by itself, so a still-focused, now-offscreen box should scroll its row
    back into view the moment the user types into it."""
    items = _items(sample_image, count=20)
    frame, _ = _build_frame(root, items)
    key = (0, "message")  # i % 3 == 0 -> text-only, per _items
    widget = frame._text_widgets[key]
    widget.focus_force()  # focus_set() alone doesn't reliably win real OS focus in a test run
    root.update_idletasks()

    # Scroll just past row 0's bottom - enough to leave it out of the
    # *visible* viewport (so there's something to scroll back into view),
    # but well within the buffered range _reconcile keeps materialized
    # (SCROLL_BUFFER_VIEWPORTS=1 full viewport), so it survives the scroll.
    total_height = sum(frame._row_heights)
    frame._canvas.yview_moveto((frame._row_heights[0] + 10) / total_height)
    frame._reconcile()
    root.update_idletasks()
    assert key in frame._text_widgets  # row 0 stays materialized (buffer covers it)

    scroll_calls = []
    original_scroll_box_into_view = frame._scroll_box_into_view
    def _spy_scroll_box_into_view(box_key):
        scroll_calls.append(box_key)
        return original_scroll_box_into_view(box_key)
    frame._scroll_box_into_view = _spy_scroll_box_into_view

    widget.insert("insert", "x")
    root.update()  # let the queued <<Modified>> event fire

    # Tk can deliver <<Modified>> more than once for a single edit; what
    # matters here is that every delivery scrolled this box, not the exact count.
    assert scroll_calls and set(scroll_calls) == {key}


# -- finalized-edit pre-population tests ------------------------------------

def test_fresh_run_uses_finalized_text_when_no_session(root, sample_image):
    """A fresh run (no session resume) must pre-populate each box with the
    corresponding finalized edit when one exists."""
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    finalized = [{} for _ in items]
    finalized[text_item] = {"message": "finalized message text"}

    frame, _ = _build_frame(root, items, initial_finalized_texts=finalized)

    assert frame._text_widgets[(text_item, "message")].get("1.0", "end-1c") == "finalized message text"


def test_session_takes_priority_over_finalized_text(root, sample_image):
    """When both a session edit and a finalized edit exist for the same slot,
    the session edit wins."""
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    saved_texts = [{} for _ in items]
    saved_texts[text_item] = {"message": "session edit"}
    finalized = [{} for _ in items]
    finalized[text_item] = {"message": "finalized edit"}

    frame, _ = _build_frame(root, items, initial_saved_texts=saved_texts, initial_finalized_texts=finalized)

    assert frame._text_widgets[(text_item, "message")].get("1.0", "end-1c") == "session edit"


def test_finalized_used_for_slot_not_covered_by_session(root, sample_image):
    """When session and finalized edits each cover different slots, both
    sources contribute: session wins for its slots, finalized fills the rest."""
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    saved_texts = [{} for _ in items]
    saved_texts[text_item] = {"message": "session message"}
    finalized = [{} for _ in items]
    finalized[image_item] = {"ocr0": "finalized ocr"}

    frame, _ = _build_frame(root, items, initial_saved_texts=saved_texts, initial_finalized_texts=finalized)

    assert frame._text_widgets[(text_item, "message")].get("1.0", "end-1c") == "session message"
    assert frame._text_widgets[(image_item, "ocr0")].get("1.0", "end-1c") == "finalized ocr"


def test_ocr_checkbox_starts_checked_when_finalized_differs_from_ocr(root, sample_image):
    """An OCR box pre-populated from a finalized edit that differs from the
    OCR default must start with its checkbox checked, so unchecking reverts
    to OCR and re-checking returns to the finalized text."""
    items = _items(sample_image, count=5)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    ocr_default = items[image_item].initial_ocr_texts[0]
    finalized = [{} for _ in items]
    finalized[image_item] = {"ocr0": "finalized ocr different from default"}
    assert finalized[image_item]["ocr0"] != ocr_default

    frame, _ = _build_frame(root, items, initial_finalized_texts=finalized)
    key = (image_item, "ocr0")

    assert frame._checkbox_checked[key] is True
    assert frame._checkbox_vars[key].get() is True
    assert frame._text_widgets[key].get("1.0", "end-1c") == "finalized ocr different from default"
    assert frame._user_edited_texts[key] == "finalized ocr different from default"


def test_ocr_checkbox_starts_unchecked_when_finalized_matches_ocr(root, sample_image):
    """If the finalized edit happens to equal the current OCR default (e.g.
    no corrections changed it, or the user typed it back exactly), the box
    must start unchecked - no false positive checked state."""
    items = _items(sample_image, count=5)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    ocr_default = items[image_item].initial_ocr_texts[0]
    finalized = [{} for _ in items]
    finalized[image_item] = {"ocr0": ocr_default}  # same as current OCR

    frame, _ = _build_frame(root, items, initial_finalized_texts=finalized)
    key = (image_item, "ocr0")

    assert frame._checkbox_checked[key] is False
    assert frame._checkbox_vars[key].get() is False


def test_finalized_edit_survives_page_out_and_back_in(root, sample_image):
    """Composition test (per ARCHITECTURE.md's "test where features compose"
    heuristic): a box pre-populated from a finalized edit must still show
    that edit after its row is paged out and rebuilt, with no further typing
    in between - the same scenario that exposed the UndoLog.baseline
    data-loss bug (see that section in ARCHITECTURE.md)."""
    items = _items(sample_image, count=40)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    assert items[text_item].initial_message_text != "finalized edit"
    finalized = [{} for _ in items]
    finalized[text_item] = {"message": "finalized edit"}

    frame, _ = _build_frame(root, items, initial_finalized_texts=finalized)
    key = (text_item, "message")
    assert frame._text_widgets[key].get("1.0", "end-1c") == "finalized edit"

    # Page far away (tears the row down with no further edits) and back.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    assert key in frame._text_widgets
    assert frame._text_widgets[key].get("1.0", "end-1c") == "finalized edit"


def test_spacer_finalized_edit_pre_populates_spacer_box(root, sample_image):
    """A spacer_end finalized edit must pre-populate the spacer box with
    the stored token string rather than the default computed at build time."""
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    default_spacer = items[text_item].initial_spacer_texts["spacer_end"]
    custom_spacer = r"\n\n\n\n\n\n\n\n"  # more tokens than the default
    assert custom_spacer != default_spacer
    finalized = [{} for _ in items]
    finalized[text_item] = {"spacer_end": custom_spacer}

    frame, _ = _build_frame(root, items, initial_finalized_texts=finalized)

    spacer_widget = frame._text_widgets[(text_item, "spacer_end")]
    assert spacer_widget.get("1.0", "end-1c") == custom_spacer


def test_multiple_image_message_finalized_edits_populate_each_ocr_box_independently(root, sample_image):
    """A message with two images must have each image's OCR box independently
    pre-populated from the corresponding finalized edit (ocr0 ≠ ocr1)."""
    items = _two_image_items(sample_image)
    finalized = [{"ocr0": "finalized first image", "ocr1": "finalized second image"}]

    frame, _ = _build_frame(root, items, initial_finalized_texts=finalized)

    assert frame._text_widgets[(0, "ocr0")].get("1.0", "end-1c") == "finalized first image"
    assert frame._text_widgets[(0, "ocr1")].get("1.0", "end-1c") == "finalized second image"
    assert frame._checkbox_checked[(0, "ocr0")] is True
    assert frame._checkbox_checked[(0, "ocr1")] is True


def test_unchecking_then_rechecking_ocr_box_starting_from_finalized_edit(root, sample_image):
    """Full toggle cycle for an OCR box pre-populated from a finalized edit:
    unchecking must revert to the OCR default without discarding the
    finalized text, and rechecking must bring the finalized text back."""
    items = _items(sample_image, count=5)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    ocr_default = items[image_item].initial_ocr_texts[0]
    finalized = [{} for _ in items]
    finalized[image_item] = {"ocr0": "finalized ocr text"}
    assert finalized[image_item]["ocr0"] != ocr_default

    frame, _ = _build_frame(root, items, initial_finalized_texts=finalized)
    key = (image_item, "ocr0")
    widget = frame._text_widgets[key]
    assert widget.get("1.0", "end-1c") == "finalized ocr text"
    assert frame._checkbox_vars[key].get() is True

    frame._checkbox_vars[key].set(False)
    frame._on_ocr_checkbox_toggle(key)

    assert widget.get("1.0", "end-1c") == ocr_default
    assert frame._checkbox_checked[key] is False
    assert frame._user_edited_texts[key] == "finalized ocr text"  # not lost

    frame._checkbox_vars[key].set(True)
    frame._on_ocr_checkbox_toggle(key)

    assert widget.get("1.0", "end-1c") == "finalized ocr text"
    assert frame._checkbox_checked[key] is True


def test_collect_edited_texts_reports_finalized_text_for_checked_ocr_box(root, sample_image):
    """collect_edited_texts (what autosave and Finalize read) must return
    the finalized text for a box pre-populated from a finalized edit whose
    checkbox is checked - and None if that same box is then unchecked,
    since unchecked means "use the OCR default"."""
    items = _items(sample_image, count=5)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    finalized = [{} for _ in items]
    finalized[image_item] = {"ocr0": "finalized ocr text"}

    frame, _ = _build_frame(root, items, initial_finalized_texts=finalized)
    key = (image_item, "ocr0")

    collected_checked = frame.collect_edited_texts()
    assert collected_checked[image_item]["ocr0"] == "finalized ocr text"

    frame._checkbox_vars[key].set(False)
    frame._on_ocr_checkbox_toggle(key)

    collected_unchecked = frame.collect_edited_texts()
    assert collected_unchecked[image_item]["ocr0"] is None


def test_finalized_ocr_edit_and_checkbox_survive_page_out_and_back_in(root, sample_image):
    """Composition test for the OCR-box-specific case: an OCR box pre-
    populated from a finalized edit must still show the finalized text and
    have its checkbox correctly checked after its row is paged out and back
    in with no further edits. Combines finalized pre-population, row
    virtualization, and checkbox seeding - three separate features that
    must all hold their invariants together."""
    items = _items(sample_image, count=40)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    ocr_default = items[image_item].initial_ocr_texts[0]
    finalized = [{} for _ in items]
    finalized[image_item] = {"ocr0": "finalized ocr text"}
    assert finalized[image_item]["ocr0"] != ocr_default

    frame, _ = _build_frame(root, items, initial_finalized_texts=finalized)
    key = (image_item, "ocr0")
    assert frame._text_widgets[key].get("1.0", "end-1c") == "finalized ocr text"
    assert frame._checkbox_checked[key] is True

    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    assert key in frame._text_widgets
    assert frame._text_widgets[key].get("1.0", "end-1c") == "finalized ocr text"
    assert frame._checkbox_checked[key] is True
    assert frame._checkbox_vars[key].get() is True


# --- Regression tests for INVESTIGATION_shift_tab_reconcile_lockup.md -----
#
# A selection-delete (Tk's own Delete/Backspace-with-a-selection binding
# calls `delete sel.first sel.last` internally) used to get recorded
# verbatim, then crash with an uncaught TclError the next time that box's
# row was rebuilt on a fresh widget with nothing selected - wedging the
# whole review screen's virtualization for the rest of the session. See
# text_undo.py's docstring and app_tests/test_text_undo.py for the
# lower-level mechanics; these tests exercise the same failure shape
# end-to-end through the real ReviewFrame.


def test_selecting_and_deleting_text_survives_a_row_being_paged_out_and_back_in(root, sample_image):
    """The end-to-end regression test for the shift-tab reconcile lockup:
    selecting text (as double-click/drag-select/Shift+Arrow would) and then
    deleting it goes through Tk's own sel.first/sel.last-based delete, the
    same call a real Delete/Backspace keypress on a selection makes. Before
    text_undo.py resolved symbolic indices to absolute positions at record
    time, paging this row away and back in raised an uncaught TclError from
    inside _reconcile and never got this far."""
    items = _items(sample_image, count=40)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (text_item, "message")
    frame, _ = _build_frame(root, items)
    widget = frame._text_widgets[key]

    widget.insert("1.0", "PREFIX ")
    widget.tag_add("sel", "1.0", "1.7")
    widget.delete("sel.first", "sel.last")  # mirrors a real Delete-key-on-selection
    expected_text = widget.get("1.0", "end-1c")

    # Page far away (tears the row down) and back to the top again - this
    # is exactly where the crash used to happen, via _populate_text_box's
    # replay_onto call.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()  # must not raise

    assert key in frame._text_widgets
    assert frame._text_widgets[key].get("1.0", "end-1c") == expected_text


def test_unreplayable_op_recovers_last_saved_text_instead_of_crashing(root, sample_image):
    """Last-resort guard in _populate_text_box: even if a UndoLog somehow
    still ends up holding an op that can't be replayed (this test injects
    one directly, bypassing the now-fixed recording proxy, to exercise the
    guard in isolation), a rebuild must recover the box's actual
    last-known-good text (self._saved_texts, captured at the box's last
    teardown) rather than letting the exception propagate and wedge the
    rest of the reconcile batch."""
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (text_item, "message")
    frame, _ = _build_frame(root, items)

    widget = frame._text_widgets[key]
    widget.insert("end", " an edit")
    edited_text = widget.get("1.0", "end-1c")

    # Tear the row down normally first (captures edited_text into
    # self._saved_texts, exactly as a real page-away would), then poison
    # this box's UndoLog with an op that can never replay cleanly onto a
    # fresh widget - simulating whatever residual failure mode the
    # record-time fix might not cover.
    frame._destroy_row(text_item)
    assert frame._saved_texts[key] == edited_text
    log = frame._undo_logs[key]
    log.ops.append(("delete", ("sel.first", "sel.last")))

    frame._build_row(text_item)  # must not raise

    rebuilt = frame._text_widgets[key]
    assert rebuilt.get("1.0", "end-1c") == edited_text
    # Self-healed: the poisoned op must not still be sitting in the log,
    # or this exact crash would recur on the box's very next rebuild.
    assert frame._undo_logs[key].ops == []
    assert frame._undo_logs[key].baseline == edited_text


def test_replay_divergence_self_heals_onto_last_saved_text(root, sample_image, caplog):
    """Regression test for INVESTIGATION_undo_redo_replay_divergence.md's
    direction #2 (detection + recovery): if a rebuild's replay ever lands
    on text that disagrees with self._saved_texts[key] - the box's own
    content as of its last teardown, captured independently of replay -
    _populate_text_box must log loudly and self-heal onto that saved text,
    the same recovery already used for an unreplayable (TclError) op,
    rather than leaving the wrong (but not necessarily default-looking)
    text sitting in the box. Injects a "replace" op with mismatched text
    directly, since the actual record-time fix (keyboard_nav.py's
    _record_undo_replacement) makes a real divergence very hard to trigger
    end-to-end anymore - this exercises the detection/recovery backstop in
    isolation, the same way test_unreplayable_op_recovers_last_saved_text_
    instead_of_crashing does for the TclError guard right above it."""
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (text_item, "message")
    frame, _ = _build_frame(root, items)

    widget = frame._text_widgets[key]
    widget.insert("end", " an edit")
    edited_text = widget.get("1.0", "end-1c")

    frame._destroy_row(text_item)
    assert frame._saved_texts[key] == edited_text
    log = frame._undo_logs[key]
    log.ops.append(("replace", ("this text was never actually seen live",)))

    with caplog.at_level("ERROR"):
        frame._build_row(text_item)

    rebuilt = frame._text_widgets[key]
    assert rebuilt.get("1.0", "end-1c") == edited_text
    assert frame._undo_logs[key].ops == []
    assert frame._undo_logs[key].baseline == edited_text
    assert any(
        "possible silent replay divergence" in record.getMessage()
        for record in caplog.records
    )


def test_double_build_reclaims_the_orphaned_widgets_content_into_saved_texts(root, sample_image):
    """_build_row being called twice for the same index without an
    intervening _destroy_row should be impossible (see
    _reclaim_widget_if_present's docstring) - but a bookkeeping bug in the
    virtualization core could get here anyway, and before this fix it
    silently discarded whatever the live widget held. Directly exercises
    RowBuildingMixin._reclaim_widget_if_present's rescue path."""
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (text_item, "message")
    frame, _ = _build_frame(root, items)

    live_widget = frame._text_widgets[key]
    live_widget.insert("end", " typed but never torn down")
    live_text = live_widget.get("1.0", "end-1c")
    old_container = frame._text_containers[key]

    # Simulate the "should be impossible" double-build directly, without
    # going through _destroy_row first.
    right_column = live_widget.master.master  # text_container -> right column frame
    frame._build_editable_text_box(
        right_column, text_item, "message", items[text_item].initial_message_text, 20,
    )

    assert frame._saved_texts[key] == live_text
    assert frame._text_widgets[key] is not live_widget
    # The orphaned widget's container must be torn down, not leaked.
    assert str(old_container) not in root.tk.call("info", "commands")


def test_one_row_build_failure_does_not_abort_the_rest_of_the_reconcile_batch(root, sample_image):
    """Before _try_build_row existed, a single row raising partway through
    _sync_materialized_rows's build loop propagated out of _reconcile
    entirely - every later row in that same batch was silently left
    unbuilt (still listed in self._slots, missing from self._text_widgets),
    and self._materialized_range was never updated to match reality. This
    directly exercises that a poisoned row is skipped, logged, and does not
    prevent its neighbors from building."""
    items = _items(sample_image, count=40)
    frame, _ = _build_frame(root, items)
    poisoned_index = len(items) - 5  # inside the far-away jump's build batch

    real_build_row = frame._build_row

    def _poisoned_build_row(index, before=None):
        if index == poisoned_index:
            raise RuntimeError("simulated row build failure")
        return real_build_row(index, before=before)

    frame._build_row = _poisoned_build_row

    frame._ensure_materialized(len(items) - 1)  # must not raise

    assert poisoned_index not in frame._row_frames
    assert poisoned_index not in frame._text_widgets
    # Its neighbors in the same jump-triggered batch must still be built.
    assert (len(items) - 1) in frame._row_frames
    assert frame._materialized_range is not None


# --- Property-style composition tests ---------------------------------------
#
# ARCHITECTURE.md's "General heuristic: test where features compose, not just
# each feature alone" section calls out that every dedicated regression test
# above (the UndoLog.baseline bug, the sel.first/sel.last bug, the undo/redo
# replay divergence bug) was only ever found by hand-writing a test for the
# *exact* combination someone had already hit - and proposes, as the still-
# missing structurally stronger complement, a property-style test that
# applies a randomized sequence of this subsystem's interaction types to a
# box, tears its row down, rebuilds it, and asserts the rebuilt content
# matches whatever was live immediately before teardown - targeting "does
# replay reproduce reality" as an invariant directly, rather than waiting to
# discover the next specific combination that breaks it. The tests below are
# that complement.

_RANDOM_EDIT_SNIPPETS = [" x", " word", "\nnewline", " some more typed text", "!"]


def _random_edit_sequence(frame, root, key, rng, num_ops, allow_checkbox):
    """Applies a randomized sequence of every interaction type a review-
    screen text box supports - typing, select+delete, undo, redo, a
    paste-shaped delete-selection-then-insert (see the "paste" branch
    below for why this doesn't go through the real OS clipboard), and
    (only for an "ocr" box, which is the only role with one) checkbox
    toggle - to whatever widget currently backs `key`. Each action is
    followed by a real event-loop pump (root.update()) so the deferred
    <<Modified>> handling (checkbox auto-check, spellcheck scheduling) that
    a live user's keystrokes would trigger actually runs before the next
    action, the same as it would interleaved with real typing - not just
    the ops recorded in text_undo.UndoLog."""
    actions = ["type", "select_delete", "undo", "redo", "paste"]
    if allow_checkbox:
        actions.append("checkbox_toggle")
    for _ in range(num_ops):
        widget = frame._text_widgets[key]
        action = rng.choice(actions)
        content = widget.get("1.0", "end-1c")
        if action == "type":
            widget.insert("insert", rng.choice(_RANDOM_EDIT_SNIPPETS))
        elif action == "select_delete":
            if not content:
                continue
            start = rng.randrange(len(content))
            end = rng.randrange(start + 1, len(content) + 1)
            # Mirrors what a real Delete/Backspace-on-a-selection keypress
            # does at the Tcl level (see text_undo.py's _resolve_index
            # docstring) - not widget.delete(start, end) directly, since
            # sel.first/sel.last is exactly the symbolic-mark case that
            # bug was about.
            widget.tag_add("sel", f"1.0+{start}c", f"1.0+{end}c")
            widget.delete("sel.first", "sel.last")
        elif action == "undo":
            frame._undo_text(type("Event", (), {"widget": widget})())
        elif action == "redo":
            frame._redo_text(type("Event", (), {"widget": widget})())
        elif action == "paste":
            # Mirrors what Tk's own <<Paste>> binding (tk::TextPaste) does at
            # the Tcl level - delete any active selection, then insert at the
            # cursor - without actually going through the real event/
            # clipboard, which would clobber the *system* clipboard (Tk's
            # clipboard is the OS clipboard, not sandboxed per-widget or
            # per-process) and pollute the user's own clipboard history with
            # every random snippet a test run happens to pick.
            if widget.tag_ranges("sel"):
                widget.delete("sel.first", "sel.last")
            widget.insert("insert", rng.choice(_RANDOM_EDIT_SNIPPETS))
        elif action == "checkbox_toggle":
            var = frame._checkbox_vars[key]
            var.set(not var.get())
            frame._on_ocr_checkbox_toggle(key)
        root.update()


def _assert_no_replay_self_heal_logged(caplog):
    """The dedicated regression tests above (test_unreplayable_op_recovers_
    last_saved_text_instead_of_crashing, test_replay_divergence_self_heals_
    onto_last_saved_text) already prove self-heal *works* by injecting a
    poisoned op directly - but self-heal recovers onto self._saved_texts,
    which is itself always kept correct independently of replay, so a
    property test that only checks final content would pass even if replay
    were badly broken and silently falling back to self-heal on every single
    rebuild. Asserting these ERROR-level log lines never fired makes sure
    replay actually reproduced the content on its own merits, not via the
    safety net catching it."""
    assert not any(
        "replay divergence" in r.getMessage() or "raised a TclError" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("role", ["message", "ocr0", "spacer_end"])
def test_random_interaction_sequence_survives_a_row_teardown_and_rebuild(
    root, sample_image, role, seed, caplog
):
    """For every role shape a box can have (a plain "message" box, an "ocr"
    box with its checkbox, and a checkbox-less spacer box), a randomized
    sequence of edits/undo/redo/paste/checkbox-toggle must survive that
    box's row being torn down and rebuilt - reproducing not just the same
    text, but the same cursor position and (for an "ocr" box) the same
    checked state, with replay never needing its self-heal backstop."""
    rng = random.Random(seed)
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    if role == "ocr0":
        index = next(i for i, item in enumerate(items) if item.image_paths)
    else:
        index = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (index, role)
    widget = frame._text_widgets[key]
    widget.focus_force()
    root.update_idletasks()

    _random_edit_sequence(frame, root, key, rng, num_ops=12, allow_checkbox=(role == "ocr0"))

    live_widget = frame._text_widgets[key]
    live_text = live_widget.get("1.0", "end-1c")
    live_cursor = live_widget.index("insert")
    live_checked = frame._checkbox_checked.get(key)

    with caplog.at_level("ERROR"):
        frame._destroy_row(index)
        frame._build_row(index)
    root.update()

    rebuilt = frame._text_widgets[key]
    assert rebuilt.get("1.0", "end-1c") == live_text
    assert rebuilt.index("insert") == live_cursor
    assert frame._checkbox_checked.get(key) == live_checked
    _assert_no_replay_self_heal_logged(caplog)


def test_random_interaction_sequence_survives_two_consecutive_teardown_rebuild_cycles(
    root, sample_image, caplog
):
    """Extends the single-cycle property test above to two consecutive
    teardown/rebuild cycles with further random edits in between. The
    UndoLog.baseline bug this whole section responds to specifically needed
    a *second* rebuild - one whose starting point was itself a replay result,
    not a fresh baseline - to surface at all (see ARCHITECTURE.md's account
    of it); a single cycle can't exercise that compounding."""
    rng = random.Random(20260720)
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")
    widget = frame._text_widgets[key]
    widget.focus_force()
    root.update_idletasks()

    _random_edit_sequence(frame, root, key, rng, num_ops=8, allow_checkbox=True)
    with caplog.at_level("ERROR"):
        frame._destroy_row(image_item)
        frame._build_row(image_item)
    root.update()
    frame._text_widgets[key].focus_force()
    root.update_idletasks()

    _random_edit_sequence(frame, root, key, rng, num_ops=8, allow_checkbox=True)
    live_widget = frame._text_widgets[key]
    live_text = live_widget.get("1.0", "end-1c")
    live_checked = frame._checkbox_checked.get(key)

    with caplog.at_level("ERROR"):
        frame._destroy_row(image_item)
        frame._build_row(image_item)
    root.update()

    rebuilt = frame._text_widgets[key]
    assert rebuilt.get("1.0", "end-1c") == live_text
    assert frame._checkbox_checked.get(key) == live_checked
    _assert_no_replay_self_heal_logged(caplog)


@pytest.mark.parametrize("seed_source", ["resumed_session", "finalized_edit"])
def test_random_edits_after_a_seeded_baseline_survive_a_further_teardown_and_rebuild(
    root, sample_image, seed_source, caplog
):
    """Every existing resumed-edit/finalized-edit composition test (test_
    resumed_edit_survives_being_paged_out_and_back_in_with_no_further_edits,
    test_finalized_edit_survives_page_out_and_back_in) covers a box's first
    rebuild with *zero* further edits after the resumed/finalized text
    seeded it - exactly the gap the UndoLog.baseline bug lived in. This
    closes the still-missing combination: real further edits (not just an
    empty op log) on top of a resumed/finalized baseline, then a rebuild -
    UndoLog.baseline must still be the seeded text, not the item's raw
    initial_message_text, for the ops on top of it to replay onto the right
    starting point."""
    rng = random.Random(42)
    items = _items(sample_image, count=40)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    assert items[text_item].initial_message_text != "seeded baseline text"
    seeded = [{} for _ in items]
    seeded[text_item] = {"message": "seeded baseline text"}
    kwargs = (
        {"initial_saved_texts": seeded} if seed_source == "resumed_session"
        else {"initial_finalized_texts": seeded}
    )

    frame, _ = _build_frame(root, items, **kwargs)
    key = (text_item, "message")
    assert frame._text_widgets[key].get("1.0", "end-1c") == "seeded baseline text"
    widget = frame._text_widgets[key]
    widget.focus_force()
    root.update_idletasks()

    _random_edit_sequence(frame, root, key, rng, num_ops=10, allow_checkbox=False)
    live_text = frame._text_widgets[key].get("1.0", "end-1c")

    # Page far enough away that this row is actually torn down (not just
    # kept alive by the virtualization buffer), then back.
    with caplog.at_level("ERROR"):
        frame._ensure_materialized(len(items) - 1)
        frame._canvas.yview_moveto(0.0)
        frame._reconcile()
    root.update()

    assert key in frame._text_widgets
    assert frame._text_widgets[key].get("1.0", "end-1c") == live_text
    _assert_no_replay_self_heal_logged(caplog)
