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
import tkinter as tk

import pytest
from PIL import Image

from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.gui.layout_constants import ROW_PACK_PADY_PX
from gui_transcription.app.gui.review_view import ReviewFrame
from gui_transcription.app.pipeline import ReviewItem, build_review_items

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
            assert edited[role] == item.initial_text_for_role(role)


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
