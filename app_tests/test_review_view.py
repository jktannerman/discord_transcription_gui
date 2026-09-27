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
import copy
import random
import time
import tkinter as tk
from pathlib import Path

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


@pytest.fixture(scope="module")
def root():
    # Module-scoped rather than one Tk() per test: with ~130 test cases in
    # this file, a fresh real (see below) window per test made the suite
    # both slow (each Tk()/destroy() pays a real window-manager handshake)
    # and visibly flicker the whole time it ran. One window is created for
    # the whole module and reused - see _destroy_test_widgets below for how
    # each test's state still gets torn down just as thoroughly as
    # root.destroy() used to do it.
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


@pytest.fixture(autouse=True)
def _destroy_test_widgets(root):
    """Every test builds its ReviewFrame straight onto the shared `root`
    and never tears it down itself - before this fixture existed, that was
    fine because root.destroy() ran at the end of every single test and
    cascaded a <Destroy> event down to the frame, which is what actually
    unbinds its canvas.bind_all("<MouseWheel>"/"<Prior>"/"<Next>") handlers
    and cancels its pending after() jobs (review_view.py's _on_destroy).
    Now that root outlives the test, this explicitly destroys every widget
    the test created instead, so that same <Destroy>-triggered cleanup still
    fires once per test - otherwise a leftover global binding or a stale
    after() callback from one test could fire during the next one."""
    yield
    for child in list(root.children.values()):
        child.destroy()
    root.update_idletasks()


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
    kwargs.setdefault("html_path", Path("dummy_chatlog.html"))
    frame = ReviewFrame(root, items, finalized.append, **kwargs)
    frame.pack(fill="both", expand=True)
    # _apply_initial_position is scheduled via after_idle, and polls itself
    # via self.after(20, ...) until the canvas reports a real height - drive
    # the event loop (not just update_idletasks, which skips timer events)
    # until that settles, the same way a real mainloop tick would. On a
    # brand-new toplevel this settles within a handful of tight-loop
    # root.update() calls, but once `root` is shared across tests (see the
    # root fixture) it's already mapped by the time later tests run, so a
    # newly-packed canvas's real size comes from the OS's own WM_SIZE
    # message rather than the toplevel's initial synchronous creation -
    # that needs actual wall-clock idle time to arrive, not just repeated
    # immediate update() calls, hence the small sleep and higher retry cap.
    for _ in range(100):
        root.update()
        if frame._materialized_range is not None:
            break
        time.sleep(0.01)
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
    widget = frame._slot_views[key].text_widget

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
    widget = frame._slot_views[key].text_widget

    widget.delete("1.0", "end")
    widget.insert("1.0", "this is a perfectly normal sentence")
    frame._run_spellcheck(key, widget)

    assert widget.tag_ranges("misspelled") == ()


def test_spacer_box_never_gets_the_misspelled_tag_configured(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    spacer_key = next(k for k in frame._slot_views if k[1].startswith("spacer"))
    widget = frame._slot_views[spacer_key].text_widget

    assert "misspelled" not in widget.tag_names()
    assert frame._slot_views[spacer_key].spellcheck_after_id is None


def test_spellcheck_tag_is_reapplied_after_a_row_is_paged_out_and_back_in(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    first_text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (first_text_item, "message")
    widget = frame._slot_views[key].text_widget
    widget.delete("1.0", "end")
    widget.insert("1.0", "definitly misspelled")
    frame._run_spellcheck(key, widget)
    assert widget.tag_ranges("misspelled") != ()

    # Page far away (tears the row down, destroying that Text widget - tags
    # live on the widget instance, not the SlotState, so they don't survive
    # this the way edited text/undo history do) and back to the top.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    rebuilt_widget = frame._slot_views[key].text_widget
    assert rebuilt_widget is not widget
    # The rebuild schedules its own debounced pass (_build_editable_text_box)
    # rather than applying immediately - run it directly, as above.
    frame._run_spellcheck(key, rebuilt_widget)
    assert rebuilt_widget.tag_ranges("misspelled") != ()


def test_destroying_a_row_cancels_its_pending_spellcheck_timer(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    key = next(k for k in frame._slot_views if k[1] == "message")
    after_id = frame._slot_views[key].spellcheck_after_id
    assert after_id is not None

    frame._destroy_row(key[0])

    assert key not in frame._slot_views
    assert after_id not in root.tk.splitlist(root.tk.call("after", "info"))


def test_edited_text_survives_a_row_being_paged_out_and_back_in(root, sample_image):
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    first_text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)

    widget = frame._slot_views[(first_text_item, "message")].text_widget
    widget.delete("1.0", "end")
    widget.insert("1.0", "an edit the user made")

    # Page far away (tears the edited row down) and back to the top again.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    assert (first_text_item, "message") in frame._slot_views
    restored = frame._slot_views[(first_text_item, "message")].text_widget.get("1.0", "end-1c")
    assert restored == "an edit the user made"


def test_undo_history_survives_a_row_being_paged_out_and_back_in(root, sample_image):
    """Undo history lives in the box's SlotState, not the Text widget, which
    is destroyed and rebuilt fresh on every page out/in - so Ctrl+Z on the
    rebuilt widget still reaches the edit made before the teardown."""
    items = _items(sample_image)
    frame, _ = _build_frame(root, items)
    first_text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (first_text_item, "message")
    original = items[first_text_item].initial_message_text

    widget = frame._slot_views[key].text_widget
    widget.insert("end", " edited")

    # Page far away (tears the edited row down) and back to the top again.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    rebuilt = frame._slot_views[key].text_widget
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
    widget = frame._slot_views[key].text_widget
    widget.focus_force()
    root.update_idletasks()
    widget.insert("end", " typed")
    root.update()
    edited_text = widget.get("1.0", "end-1c")

    var = frame._slot_views[key].checkbox_var
    var.set(False)
    frame._on_ocr_checkbox_toggle(key)
    assert widget.get("1.0", "end-1c") == default_text
    assert frame._slot_states[key].checked is False

    frame._undo_text(type("Event", (), {"widget": widget})())

    assert widget.get("1.0", "end-1c") == edited_text
    assert frame._slot_states[key].checked is True
    assert frame._slot_views[key].checkbox_var.get() is True


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


def test_collect_edited_texts_reports_none_for_untouched_items(root, sample_image):
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)

    collected = frame.collect_edited_texts()

    assert len(collected) == len(items)
    for edited, item in zip(collected, items):
        for role in item.slot_roles:
            # A box still at its default isn't an edit - even though every
            # row here has been built (so has live widgets) - and an
            # untouched OCR box's checkbox starts unchecked anyway. None
            # means "use the default" (see ReviewFrame._get_box_text).
            assert edited[role] is None
    assert frame.get_touched_slots() == set()


def test_ocr_checkbox_starts_unchecked_for_an_untouched_box(root, sample_image):
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")

    assert frame._slot_states[key].checked is False
    assert frame._slot_views[key].checkbox_var.get() is False
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == items[image_item].initial_ocr_texts[0]


def test_ocr_checkbox_starts_checked_for_a_resumed_edit_differing_from_default(root, sample_image):
    items = _items(sample_image, count=5)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    saved_texts = [{} for _ in items]
    saved_texts[image_item] = {"ocr0": "a resumed ocr edit"}

    frame, _ = _build_frame(root, items, initial_saved_texts=saved_texts)
    key = (image_item, "ocr0")

    assert frame._slot_states[key].checked is True
    assert frame._slot_views[key].checkbox_var.get() is True
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == "a resumed ocr edit"


def test_typing_into_an_ocr_box_checks_its_checkbox(root, sample_image):
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")
    widget = frame._slot_views[key].text_widget
    widget.focus_force()  # focus_set() alone doesn't reliably win real OS focus in a test run
    root.update_idletasks()

    widget.insert("end", " typed")
    root.update()  # let the queued <<Modified>> event fire

    assert frame._slot_states[key].checked is True
    assert frame._slot_views[key].checkbox_var.get() is True


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
    widget = frame._slot_views[key].text_widget
    widget.focus_force()
    root.update_idletasks()
    widget.insert("end", " typed")
    root.update()
    edited_text = widget.get("1.0", "end-1c")
    assert edited_text != default_text
    assert frame._slot_views[key].checkbox_var.get() is True

    var = frame._slot_views[key].checkbox_var
    var.set(False)
    frame._on_ocr_checkbox_toggle(key)

    assert widget.get("1.0", "end-1c") == default_text
    assert frame._slot_states[key].checked is False
    assert frame._slot_states[key].user_edit == edited_text  # not discarded

    var.set(True)
    frame._on_ocr_checkbox_toggle(key)

    assert widget.get("1.0", "end-1c") == edited_text
    assert frame._slot_states[key].checked is True


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
    widget = frame._slot_views[key].text_widget
    widget.focus_force()
    root.update_idletasks()
    widget.insert("end", " typed")
    root.update()
    var = frame._slot_views[key].checkbox_var
    var.set(False)
    frame._on_ocr_checkbox_toggle(key)

    collected = frame.collect_edited_texts()

    assert collected[image_item]["ocr0"] is None


def test_rows_paged_out_and_back_in_still_report_no_edits(root, sample_image):
    """Scrolling past a box used to save its (default) text, which then
    counted as an edit - and was stored as a finalized edit at Finalize."""
    items = _items(sample_image, count=40)
    frame, _ = _build_frame(root, items)
    first_row = frame._row_frames[0]
    frame._canvas.yview_moveto(1.0)
    frame._reconcile()
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()
    assert frame._row_frames[0] is not first_row  # rows really were torn down and rebuilt

    collected = frame.collect_edited_texts()

    assert all(text is None for edited in collected for text in edited.values())


def test_typing_marks_a_box_touched_and_reverting_by_hand_reports_none(root, sample_image):
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (text_item, "message")
    widget = frame._slot_views[key].text_widget
    widget.focus_force()
    root.update_idletasks()
    widget.insert("end", "!")
    root.update()

    assert key in frame.get_touched_slots()
    assert frame.collect_edited_texts()[text_item]["message"] == items[text_item].initial_message_text + "!"

    widget.delete("end-2c", "end-1c")
    root.update()

    assert frame.collect_edited_texts()[text_item]["message"] is None
    assert key in frame.get_touched_slots()


def test_unticking_an_ocr_box_marks_it_touched(root, sample_image):
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")
    frame._slot_views[key].checkbox_var.set(True)
    frame._on_ocr_checkbox_toggle(key)
    frame._slot_views[key].checkbox_var.set(False)
    frame._on_ocr_checkbox_toggle(key)

    assert key in frame.get_touched_slots()


def test_building_rows_alone_touches_nothing(root, sample_image):
    """Only deliberate actions may mark a slot touched - Finalize relies on
    that to never remove a stored edit the user didn't act on."""
    items = _items(sample_image, count=40)
    frame, _ = _build_frame(root, items)
    frame._canvas.yview_moveto(1.0)
    frame._reconcile()
    root.update()

    assert frame.get_touched_slots() == set()


def test_initial_touched_slots_are_kept(root, sample_image):
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items, initial_touched_slots={(1, "spacer_end")})

    assert frame.get_touched_slots() == {(1, "spacer_end")}


def test_ocr_checkbox_state_and_both_versions_survive_paging_out_and_back_in(root, sample_image):
    items = _items(sample_image, count=40)
    frame, _ = _build_frame(root, items)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")
    default_text = items[image_item].initial_ocr_texts[0]
    widget = frame._slot_views[key].text_widget
    widget.focus_force()
    root.update_idletasks()
    widget.insert("end", " typed")
    root.update()
    edited_text = widget.get("1.0", "end-1c")

    var = frame._slot_views[key].checkbox_var
    var.set(False)
    frame._on_ocr_checkbox_toggle(key)

    # Page far away (tears the row down) and back to the top again.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    assert key in frame._slot_views
    assert frame._slot_states[key].checked is False
    assert frame._slot_views[key].checkbox_var.get() is False
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == default_text
    assert frame._slot_states[key].user_edit == edited_text


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

    spacer_widget = frame._slot_views[(0, "spacer_msg_img")].text_widget
    assert int(spacer_widget.cget("height")) == 1
    assert spacer_widget.get("1.0", "end-1c") == "\\n\\n"

    end_widget = frame._slot_views[(0, "spacer_end")].text_widget
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
        html_path=Path("dummy_chatlog.html"),
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

    # See _build_frame's matching loop for why this needs more than a
    # handful of tight-loop update() calls once root is shared across tests.
    for _ in range(100):
        root.update()
        if frame._materialized_range is not None:
            break
        time.sleep(0.01)

    widget = frame._slot_views[(text_item, "message")].text_widget
    assert widget.get("1.0", "end-1c") == "a resumed edit"
    assert focus_calls == [(text_item, "message")]


def test_resumed_edit_survives_being_paged_out_and_back_in_with_no_further_edits(root, sample_image):
    """Regression test for a real data-loss bug (the old UndoLog.baseline
    bug): a resumed box showed its saved edit on first build, but reverted
    to the unedited default the next time its row was torn down and
    rebuilt, with no further edits in between. Combines the two scenarios
    test_resuming_session_restores_saved_edit_and_focus and test_edited_
    text_survives_a_row_being_paged_out_and_back_in each cover separately -
    neither alone caught this, since the bug only appeared once both were
    true at once."""
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
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == "a resumed edit"

    # Page far away (tears the row down with no edits made this build) and
    # back to the top again - no typing in between, matching the real
    # repro (edit made in an earlier session, just scrolled past in this
    # one).
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    assert key in frame._slot_views
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == "a resumed edit"


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
    container = frame._slot_views[(index, role)].container
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
    widget = frame._slot_views[key].text_widget
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
    widget = frame._slot_views[key].text_widget
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
    container = frame._slot_views[(target_index, target_role)].container
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
    container = frame._slot_views[(target_index, "message")].container
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
    widget = frame._slot_views[key].text_widget
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
    widget = frame._slot_views[key].text_widget
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
    assert frame._slot_states[key].cursor == "1.3"

    frame._build_row(text_item)
    root.update()  # let the after_idle-scheduled refocus run

    assert focus_calls == [key]
    assert frame._slot_views[key].text_widget.index("insert") == "1.3"


def test_destroying_an_unfocused_rows_box_then_rebuilding_does_not_steal_focus(root, sample_image):
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    frame, _ = _build_frame(root, items)
    key = (text_item, "message")
    # Deliberately not focused - _destroy_row should leave self._refocus_slot
    # untouched (None) for a row whose box never had focus.

    # Baseline instead of asserting focus_get() is None outright: on the
    # shared `root` (see the root fixture) an earlier test may have left
    # some other widget focused, which Tk's focus model can fall back to
    # even after that widget's destroyed - None isn't guaranteed here the
    # way it was when every test got its own never-before-focused Tk root.
    # What actually matters is that rebuilding this never-focused row
    # doesn't *change* who has focus, whatever it started as.
    baseline_focus = frame.focus_get()

    frame._destroy_row(text_item)
    assert frame._refocus_slot is None

    frame._build_row(text_item)
    root.update()

    assert frame.focus_get() == baseline_focus


def test_typing_in_a_focused_box_scrolled_offscreen_scrolls_its_row_back_into_view(root, sample_image):
    """Confirms _on_text_modified's existing snap-back-on-edit behavior:
    scrolling away (e.g. the mouse wheel) never touches Tk's keyboard focus
    by itself, so a still-focused, now-offscreen box should scroll its row
    back into view the moment the user types into it."""
    items = _items(sample_image, count=20)
    frame, _ = _build_frame(root, items)
    key = (0, "message")  # i % 3 == 0 -> text-only, per _items
    widget = frame._slot_views[key].text_widget
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
    assert key in frame._slot_views  # row 0 stays materialized (buffer covers it)

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

    assert frame._slot_views[(text_item, "message")].text_widget.get("1.0", "end-1c") == "finalized message text"


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

    assert frame._slot_views[(text_item, "message")].text_widget.get("1.0", "end-1c") == "session edit"


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

    assert frame._slot_views[(text_item, "message")].text_widget.get("1.0", "end-1c") == "session message"
    assert frame._slot_views[(image_item, "ocr0")].text_widget.get("1.0", "end-1c") == "finalized ocr"


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

    assert frame._slot_states[key].checked is True
    assert frame._slot_views[key].checkbox_var.get() is True
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == "finalized ocr different from default"
    assert frame._slot_states[key].user_edit == "finalized ocr different from default"


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

    assert frame._slot_states[key].checked is False
    assert frame._slot_views[key].checkbox_var.get() is False


def test_finalized_edit_survives_page_out_and_back_in(root, sample_image):
    """Composition test (per ARCHITECTURE.md's "test where features compose"
    heuristic): a box pre-populated from a finalized edit must still show
    that edit after its row is paged out and rebuilt, with no further typing
    in between - the same scenario that exposed the old UndoLog.baseline
    data-loss bug (see ARCHITECTURE.md)."""
    items = _items(sample_image, count=40)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    assert items[text_item].initial_message_text != "finalized edit"
    finalized = [{} for _ in items]
    finalized[text_item] = {"message": "finalized edit"}

    frame, _ = _build_frame(root, items, initial_finalized_texts=finalized)
    key = (text_item, "message")
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == "finalized edit"

    # Page far away (tears the row down with no further edits) and back.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    assert key in frame._slot_views
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == "finalized edit"


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

    spacer_widget = frame._slot_views[(text_item, "spacer_end")].text_widget
    assert spacer_widget.get("1.0", "end-1c") == custom_spacer


def test_multiple_image_message_finalized_edits_populate_each_ocr_box_independently(root, sample_image):
    """A message with two images must have each image's OCR box independently
    pre-populated from the corresponding finalized edit (ocr0 ≠ ocr1)."""
    items = _two_image_items(sample_image)
    finalized = [{"ocr0": "finalized first image", "ocr1": "finalized second image"}]

    frame, _ = _build_frame(root, items, initial_finalized_texts=finalized)

    assert frame._slot_views[(0, "ocr0")].text_widget.get("1.0", "end-1c") == "finalized first image"
    assert frame._slot_views[(0, "ocr1")].text_widget.get("1.0", "end-1c") == "finalized second image"
    assert frame._slot_states[(0, "ocr0")].checked is True
    assert frame._slot_states[(0, "ocr1")].checked is True


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
    widget = frame._slot_views[key].text_widget
    assert widget.get("1.0", "end-1c") == "finalized ocr text"
    assert frame._slot_views[key].checkbox_var.get() is True

    frame._slot_views[key].checkbox_var.set(False)
    frame._on_ocr_checkbox_toggle(key)

    assert widget.get("1.0", "end-1c") == ocr_default
    assert frame._slot_states[key].checked is False
    assert frame._slot_states[key].user_edit == "finalized ocr text"  # not lost

    frame._slot_views[key].checkbox_var.set(True)
    frame._on_ocr_checkbox_toggle(key)

    assert widget.get("1.0", "end-1c") == "finalized ocr text"
    assert frame._slot_states[key].checked is True


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

    frame._slot_views[key].checkbox_var.set(False)
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
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == "finalized ocr text"
    assert frame._slot_states[key].checked is True

    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()

    assert key in frame._slot_views
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == "finalized ocr text"
    assert frame._slot_states[key].checked is True
    assert frame._slot_views[key].checkbox_var.get() is True


# --- Regression tests for INVESTIGATION_shift_tab_reconcile_lockup.md -----
#
# A selection-delete (Tk's own Delete/Backspace-with-a-selection binding
# calls `delete sel.first sel.last` internally) used to get recorded
# verbatim, then crash with an uncaught TclError the next time that box's
# row was rebuilt on a fresh widget with nothing selected - wedging the
# whole review screen's virtualization for the rest of the session. Undo no
# longer records or replays widget calls at all, but these still exercise
# the same user-facing shape end-to-end through the real ReviewFrame.


def test_selecting_and_deleting_text_survives_a_row_being_paged_out_and_back_in(root, sample_image):
    """The end-to-end regression test for the shift-tab reconcile lockup:
    selecting text (as double-click/drag-select/Shift+Arrow would) and then
    deleting it goes through Tk's own sel.first/sel.last-based delete, the
    same call a real Delete/Backspace keypress on a selection makes. Under
    the old replay-based undo, paging this row away and back in raised an
    uncaught TclError from inside _reconcile and never got this far."""
    items = _items(sample_image, count=40)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (text_item, "message")
    frame, _ = _build_frame(root, items)
    widget = frame._slot_views[key].text_widget

    widget.insert("1.0", "PREFIX ")
    widget.tag_add("sel", "1.0", "1.7")
    widget.delete("sel.first", "sel.last")  # mirrors a real Delete-key-on-selection
    expected_text = widget.get("1.0", "end-1c")

    # Page far away (tears the row down) and back to the top again - this
    # is exactly where the crash used to happen.
    frame._ensure_materialized(len(items) - 1)
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()  # must not raise

    assert key in frame._slot_views
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == expected_text


def test_double_build_reclaims_the_orphaned_widgets_content(root, sample_image):
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

    live_widget = frame._slot_views[key].text_widget
    live_widget.insert("end", " typed but never torn down")
    live_text = live_widget.get("1.0", "end-1c")
    old_container = frame._slot_views[key].container

    # Simulate the "should be impossible" double-build directly, without
    # going through _destroy_row first.
    right_column = live_widget.master.master  # text_container -> right column frame
    frame._build_editable_text_box(right_column, text_item, "message", 20)

    assert frame._slot_states[key].text == live_text
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == live_text
    assert frame._slot_views[key].text_widget is not live_widget
    # The orphaned widget's container must be torn down, not leaked.
    assert str(old_container) not in root.tk.call("info", "commands")


def test_one_row_build_failure_does_not_abort_the_rest_of_the_reconcile_batch(root, sample_image):
    """Before _try_build_row existed, a single row raising partway through
    _sync_materialized_rows's build loop propagated out of _reconcile
    entirely - every later row in that same batch was silently left
    unbuilt (still listed in self._slots, missing from self._slot_views),
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
    assert poisoned_index not in frame._slot_views
    # Its neighbors in the same jump-triggered batch must still be built.
    assert (len(items) - 1) in frame._row_frames
    assert frame._materialized_range is not None


# --- Property-style composition tests ---------------------------------------
#
# ARCHITECTURE.md's "General heuristic: test where features compose, not just
# each feature alone" section: rather than one hand-written test per
# combination someone has already hit, apply a randomized sequence of this
# subsystem's interaction types to a box, tear its row down, rebuild it, and
# assert that nothing a user could observe changed - the text, the cursor,
# the checkbox, and the whole undo/redo history.

_RANDOM_EDIT_SNIPPETS = [" x", " word", "\nnewline", " some more typed text", "!"]


def _random_edit_sequence(frame, root, key, rng, num_ops, allow_checkbox):
    """Applies a randomized sequence of every interaction type a review-
    screen text box supports - typing, select+delete, undo, redo, a
    paste-shaped delete-selection-then-insert (see the "paste" branch
    below for why this doesn't go through the real OS clipboard), and
    (only for an "ocr" box, which is the only role with one) checkbox
    toggle - to whatever widget currently backs `key`. Each action is
    followed by a real event-loop pump (root.update()) so the deferred
    <<Modified>> handling (history recording, checkbox auto-check,
    spellcheck scheduling) that a live user's keystrokes would trigger
    actually runs before the next action."""
    actions = ["type", "select_delete", "undo", "redo", "paste"]
    if allow_checkbox:
        actions.append("checkbox_toggle")
    for _ in range(num_ops):
        widget = frame._slot_views[key].text_widget
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
            # does at the Tcl level.
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
            var = frame._slot_views[key].checkbox_var
            var.set(not var.get())
            frame._on_ocr_checkbox_toggle(key)
        root.update()


def _expected_undo_walk(frame, key):
    """The texts repeated Ctrl+Z would pass through from the box's current
    state, worked out on a copy of its history so the real one is left
    untouched."""
    state = frame._slot_states[key]
    history = copy.deepcopy(state.history)
    texts = []
    text = state.text
    while (text := history.undo(text)) is not None:
        texts.append(text)
    return texts


def _actual_undo_walk(frame, key):
    """Press Ctrl+Z on the box's live widget until there's nothing left to
    undo, collecting what the widget shows after each press."""
    texts = []
    while frame._slot_states[key].history.can_undo:
        widget = frame._slot_views[key].text_widget
        frame._undo_text(type("Event", (), {"widget": widget})())
        texts.append(widget.get("1.0", "end-1c"))
    return texts


def _freeze_clock(frame):
    """Stop EditHistory's pause rule depending on how fast the test runs,
    so a seed always produces the same undo steps."""
    frame._clock = lambda: 0.0


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("role", ["message", "ocr0", "spacer_end"])
def test_random_interaction_sequence_survives_a_row_teardown_and_rebuild(
    root, sample_image, role, seed
):
    """For every role shape a box can have (a plain "message" box, an "ocr"
    box with its checkbox, and a checkbox-less spacer box), a randomized
    sequence of edits/undo/redo/paste/checkbox-toggle must survive that
    box's row being torn down and rebuilt - the same text, cursor position,
    checked state, and a Ctrl+Z walk identical to the one the box would
    have given without the rebuild."""
    rng = random.Random(seed)
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    _freeze_clock(frame)
    if role == "ocr0":
        index = next(i for i, item in enumerate(items) if item.image_paths)
    else:
        index = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (index, role)
    widget = frame._slot_views[key].text_widget
    widget.focus_force()
    root.update_idletasks()

    _random_edit_sequence(frame, root, key, rng, num_ops=12, allow_checkbox=(role == "ocr0"))

    live_widget = frame._slot_views[key].text_widget
    frame._sync_slot_from_widget(key, live_widget)
    live_text = live_widget.get("1.0", "end-1c")
    live_cursor = live_widget.index("insert")
    live_checked = frame._slot_states[key].checked
    expected_walk = _expected_undo_walk(frame, key)

    frame._destroy_row(index)
    frame._build_row(index)
    root.update()

    rebuilt = frame._slot_views[key].text_widget
    assert rebuilt is not live_widget
    assert rebuilt.get("1.0", "end-1c") == live_text
    assert rebuilt.index("insert") == live_cursor
    assert frame._slot_states[key].checked == live_checked
    assert _actual_undo_walk(frame, key) == expected_walk


def test_random_interaction_sequence_survives_two_consecutive_teardown_rebuild_cycles(
    root, sample_image
):
    """Extends the single-cycle property test above to two consecutive
    teardown/rebuild cycles with further random edits in between. The old
    UndoLog.baseline bug specifically needed a *second* rebuild - one whose
    starting point was itself a rebuild - to surface at all (see
    ARCHITECTURE.md); a single cycle can't exercise that compounding."""
    rng = random.Random(20260720)
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    _freeze_clock(frame)
    image_item = next(i for i, item in enumerate(items) if item.image_paths)
    key = (image_item, "ocr0")
    widget = frame._slot_views[key].text_widget
    widget.focus_force()
    root.update_idletasks()

    _random_edit_sequence(frame, root, key, rng, num_ops=8, allow_checkbox=True)
    frame._destroy_row(image_item)
    frame._build_row(image_item)
    root.update()
    frame._slot_views[key].text_widget.focus_force()
    root.update_idletasks()

    _random_edit_sequence(frame, root, key, rng, num_ops=8, allow_checkbox=True)
    live_widget = frame._slot_views[key].text_widget
    frame._sync_slot_from_widget(key, live_widget)
    live_text = live_widget.get("1.0", "end-1c")
    live_checked = frame._slot_states[key].checked
    expected_walk = _expected_undo_walk(frame, key)

    frame._destroy_row(image_item)
    frame._build_row(image_item)
    root.update()

    rebuilt = frame._slot_views[key].text_widget
    assert rebuilt.get("1.0", "end-1c") == live_text
    assert frame._slot_states[key].checked == live_checked
    assert _actual_undo_walk(frame, key) == expected_walk


@pytest.mark.parametrize("seed_source", ["resumed_session", "finalized_edit"])
def test_random_edits_after_a_seeded_baseline_survive_a_further_teardown_and_rebuild(
    root, sample_image, seed_source
):
    """Real further edits on top of a resumed/finalized text, then a
    rebuild - the combination the old UndoLog.baseline bug lived in. Undoing
    everything must also land back on the seeded text, never the item's raw
    default: the seeded text is where this session's history starts."""
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
    _freeze_clock(frame)
    key = (text_item, "message")
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == "seeded baseline text"
    widget = frame._slot_views[key].text_widget
    widget.focus_force()
    root.update_idletasks()

    _random_edit_sequence(frame, root, key, rng, num_ops=10, allow_checkbox=False)
    live_text = frame._slot_views[key].text_widget.get("1.0", "end-1c")

    # Page far enough away that this row is actually torn down (not just
    # kept alive by the virtualization buffer), then back.
    frame._ensure_materialized(len(items) - 1)
    assert key not in frame._slot_views
    frame._canvas.yview_moveto(0.0)
    frame._reconcile()
    root.update()

    assert key in frame._slot_views
    assert frame._slot_views[key].text_widget.get("1.0", "end-1c") == live_text
    walk = _actual_undo_walk(frame, key)
    final = walk[-1] if walk else live_text
    assert final == "seeded baseline text"


def test_undo_steps_are_words_and_survive_a_rebuild(root, sample_image):
    """End-to-end check of the grouping rule through real <<Modified>>
    events: typing two words one keystroke at a time gives two undo
    steps, and a rebuild in between typing and undoing changes nothing."""
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)
    _freeze_clock(frame)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    key = (text_item, "message")
    original = items[text_item].initial_message_text
    widget = frame._slot_views[key].text_widget
    widget.focus_force()
    widget.mark_set("insert", "end")
    root.update()
    for char in "two words":
        widget.insert("insert", char)
        root.update()

    frame._destroy_row(text_item)
    frame._build_row(text_item)
    root.update()

    assert _actual_undo_walk(frame, key) == [original + "two ", original]
    rebuilt = frame._slot_views[key].text_widget
    frame._redo_text(type("Event", (), {"widget": rebuilt})())
    assert rebuilt.get("1.0", "end-1c") == original + "two "


# -- mouse wheel / keyboard events, sent as real Tk events ----------------------
#
# These deliberately go through event_generate rather than calling handler
# methods directly: both bugs below were in *which events* were bound, which
# a direct method call can't catch.


def _long_ocr_item(sample_image):
    """One image message whose OCR text is far taller than its box, so the
    box has its own internal scrollbar."""
    entries = [MessageEntry(message_id="long", text_lines=[], image_names=["sample.png"])]
    file_info = {"sample.png": [f"line {n}" for n in range(200)]}
    return build_review_items(entries, file_info, image_folder=sample_image.parent)


@pytest.mark.parametrize("sequence, kwargs", [
    ("<Button-5>", {}),                     # X11, Tk 8.6
    ("<MouseWheel>", {"delta": -120}),      # Windows/macOS
])
def test_wheel_down_over_the_canvas_scrolls_the_review_window(root, sample_image, sequence, kwargs):
    frame, _ = _build_frame(root, _items(sample_image, count=40))
    top_before, _ = frame._canvas.yview()

    frame._canvas.event_generate(sequence, **kwargs)
    root.update()

    top_after, _ = frame._canvas.yview()
    assert top_after > top_before


def test_wheel_up_on_x11_scrolls_the_review_window_back_up(root, sample_image):
    frame, _ = _build_frame(root, _items(sample_image, count=40))
    frame._canvas.yview_moveto(0.5)
    root.update()
    top_before, _ = frame._canvas.yview()

    frame._canvas.event_generate("<Button-4>")
    root.update()

    top_after, _ = frame._canvas.yview()
    assert top_after < top_before


def test_wheel_over_an_overflowing_box_scrolls_only_the_box_once(root, sample_image):
    items = _long_ocr_item(sample_image) + _items(sample_image, count=20)
    frame, _ = _build_frame(root, items)
    widget = frame._slot_views[(0, "ocr0")].text_widget
    root.update()
    box_top_before, _ = widget.yview()
    canvas_top_before, _ = frame._canvas.yview()
    assert box_top_before == 0.0

    widget.event_generate("<Button-5>")
    root.update()
    box_after_one, _ = widget.yview()
    widget.event_generate("<Button-5>")
    root.update()
    box_after_two, _ = widget.yview()

    assert frame._canvas.yview()[0] == canvas_top_before
    # Scrolled, and by the same amount each notch - Tk's own Text wheel
    # binding also running would double the first step.
    assert box_after_one > 0.0
    assert box_after_two - box_after_one == pytest.approx(box_after_one, rel=0.25)


def test_wheel_over_a_box_at_its_limit_scrolls_the_review_window(root, sample_image):
    items = _long_ocr_item(sample_image) + _items(sample_image, count=20)
    frame, _ = _build_frame(root, items)
    widget = frame._slot_views[(0, "ocr0")].text_widget
    widget.yview_moveto(1.0)
    root.update()
    canvas_top_before, _ = frame._canvas.yview()

    widget.event_generate("<Button-5>")
    root.update()

    assert frame._canvas.yview()[0] > canvas_top_before


@pytest.mark.parametrize("keysym, state", [
    ("ISO_Left_Tab", 0x1),   # what X11 sends for Shift+Tab
    ("Tab", 0x1),            # Shift held, as Windows sends it
])
def test_shift_tab_key_moves_focus_back_and_scrolls_it_into_view(root, sample_image, keysym, state):
    items = _items(sample_image, count=40)
    frame, _ = _build_frame(root, items)
    first_key = frame._slots[0]
    second_key = frame._slots[1]
    frame._focus_text_box(*second_key)
    widget = frame._slot_views[second_key].text_widget
    widget.focus_force()  # focus_set() alone doesn't reliably win real OS focus in a test run
    root.update()
    # Scroll one page down, the way the mouse/scrollbar can - within the
    # materialization buffer, so both boxes stay built but go off screen.
    frame._canvas.yview_scroll(1, "pages")
    frame._reconcile()
    root.update()
    first_container = frame._slot_views[first_key].container
    assert first_container.winfo_rooty() + first_container.winfo_height() <= frame._canvas.winfo_rooty()
    assert frame.focus_get() is widget

    widget.event_generate("<KeyPress>", keysym=keysym, state=state)
    root.update()

    assert frame._focused_slot() == first_key
    container = frame._slot_views[first_key].container
    box_top = container.winfo_rooty() - frame._canvas.winfo_rooty()
    assert 0 <= box_top < frame._canvas.winfo_height()
