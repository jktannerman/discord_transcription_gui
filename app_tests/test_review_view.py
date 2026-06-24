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
from gui_transcription.app.gui.review_view import ReviewFrame
from gui_transcription.app.pipeline import ReviewItem

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
    lay out differently from one another."""
    items = []
    for i in range(count):
        if i % 3 == 0:
            items.append(ReviewItem(
                entry=MessageEntry(message_id=str(i), text_lines=[f"text only {i}"], image_name=None),
                image_path=None, initial_message_text=f"text only {i}", initial_ocr_text=None,
            ))
        elif i % 3 == 1:
            items.append(ReviewItem(
                entry=MessageEntry(message_id=str(i), text_lines=[], image_name="sample.png"),
                image_path=sample_image, initial_message_text=None, initial_ocr_text=f"ocr {i}",
            ))
        else:
            items.append(ReviewItem(
                entry=MessageEntry(message_id=str(i), text_lines=[f"caption {i}"], image_name="sample.png"),
                image_path=sample_image, initial_message_text=f"caption {i}", initial_ocr_text=f"ocr {i}",
            ))
    return items


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


def test_collect_edited_texts_returns_initial_text_for_untouched_items(root, sample_image):
    items = _items(sample_image, count=5)
    frame, _ = _build_frame(root, items)

    collected = frame.collect_edited_texts()

    assert len(collected) == len(items)
    for (message_text, ocr_text), item in zip(collected, items):
        if item.initial_message_text is not None:
            assert message_text == item.initial_message_text
        if item.image_path is not None:
            assert ocr_text == (item.initial_ocr_text or "")


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


def test_resuming_session_restores_saved_edit_and_focus(root, sample_image):
    """Real OS/window-manager focus delivery is too flaky to assert on
    directly in an automated run (several Tk windows get created/destroyed
    across this test session) - so this checks the deterministic part
    instead: _apply_initial_position actually calls _focus_text_box with
    the resumed (index, role) slot, which is what would put real focus
    there in a live app."""
    items = _items(sample_image, count=5)
    text_item = next(i for i, item in enumerate(items) if item.initial_message_text is not None)
    saved_texts = [(None, None)] * len(items)
    saved_texts[text_item] = ("a resumed edit", None)

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
