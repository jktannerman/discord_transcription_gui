"""Unit-level coverage for text_undo.py's recording proxy - specifically the
fix for a real, reproduced bug (see
INVESTIGATION_shift_tab_reconcile_lockup.md): Tk's own built-in Text
bindings remove a selection via the literal call `delete sel.first
sel.last`, not absolute positions. Recording that call verbatim and later
replaying it onto a freshly-built widget (which has never had anything
selected) used to raise `_tkinter.TclError: text doesn't contain any
characters tagged with "sel"` - silently wedging the whole review screen's
virtualization for the rest of the session. `attach_undo_recording`'s proxy
now resolves every insert/delete index argument to an absolute
`"line.column"` string *before* forwarding the call, so a recorded op can't
carry a mark that's meaningless on a different widget instance.

These tests build a real (if withdrawn) tk.Text widget - not a display-heavy
window, but still real Tk - so they're marked `gui` like every other test
file here that needs one.
"""
import tkinter as tk

import pytest

from gui_transcription.app.gui.text_undo import UndoLog, attach_undo_recording, replay_onto

pytestmark = pytest.mark.gui


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    r.withdraw()
    yield r
    r.destroy()


def _make_recorded_widget(root, initial_text=""):
    widget = tk.Text(root)
    if initial_text:
        widget.insert("1.0", initial_text)
        widget.edit_reset()
    log = UndoLog()
    log.baseline = initial_text
    detach = attach_undo_recording(widget, log)
    return widget, log, detach


def test_selection_delete_is_recorded_with_absolute_indices_not_sel_marks(root):
    widget, log, detach = _make_recorded_widget(root, "hello world")

    # Mirrors exactly what Tk's own Delete/Backspace-with-a-selection
    # binding does internally: select a range, then delete it via the
    # symbolic sel.first/sel.last marks rather than absolute positions.
    widget.tag_add("sel", "1.0", "1.5")
    widget.delete("sel.first", "sel.last")
    detach()

    delete_ops = [op for op in log.ops if op[0] == "delete"]
    assert len(delete_ops) == 1
    _, args = delete_ops[0]
    assert args == ("1.0", "1.5")
    assert "sel.first" not in args
    assert "sel.last" not in args


def test_insert_at_the_insert_mark_is_recorded_with_an_absolute_index(root):
    widget, log, detach = _make_recorded_widget(root, "hello world")

    widget.mark_set("insert", "1.5")
    widget.insert("insert", "!")
    detach()

    insert_ops = [op for op in log.ops if op[0] == "insert"]
    assert len(insert_ops) == 1
    _, args = insert_ops[0]
    assert args[0] == "1.5"
    assert args[1] == "!"


def test_recorded_selection_delete_replays_onto_a_fresh_widget_without_raising(root):
    """The actual regression this fix exists for: a selection-delete
    recorded on one widget must be replayable onto a brand new widget with
    nothing selected - the exact situation a torn-down-and-rebuilt review
    row is in."""
    original, log, detach = _make_recorded_widget(root, "hello world")
    original.tag_add("sel", "1.0", "1.5")
    original.delete("sel.first", "sel.last")
    expected_text = original.get("1.0", "end-1c")
    detach()

    fresh = tk.Text(root)
    fresh.insert("1.0", log.baseline)
    fresh.edit_reset()

    replay_onto(fresh, log)  # must not raise TclError

    assert fresh.get("1.0", "end-1c") == expected_text == " world"


def test_ordinary_typing_still_replays_correctly_after_the_index_resolution_change(root):
    """The resolution added to the recording proxy must not change what an
    ordinary (non-symbolic) insert/delete replays to."""
    original, log, detach = _make_recorded_widget(root, "abc")
    original.insert("1.3", "def")
    original.delete("1.1", "1.2")
    expected_text = original.get("1.0", "end-1c")
    detach()

    fresh = tk.Text(root)
    fresh.insert("1.0", log.baseline)
    fresh.edit_reset()

    replay_onto(fresh, log)

    assert fresh.get("1.0", "end-1c") == expected_text


def test_replace_op_replays_as_one_atomic_undo_step(root):
    """Regression test for INVESTIGATION_undo_redo_replay_divergence.md: a
    successful Ctrl+Z/Ctrl+Shift+Z is recorded as a "replace" op
    (keyboard_nav.py's _record_undo_replacement), not a bare "undo"/"redo"
    marker replayed via edit_undo()/edit_redo() again - see this module's
    docstring for why that used to silently diverge. This checks the two
    properties that matter: replay lands on the exact recorded text, and a
    single further edit_undo() reverts the whole "replace" in one step
    (landing back on what preceded it) rather than splitting into a
    delete-only intermediate state, which is what replay_onto's
    edit_separator()/autoseparators bracketing around the "replace" case
    exists to guarantee."""
    log = UndoLog()
    log.baseline = "hello"
    log.ops = [
        ("insert", ("end", " world")),
        ("replace", ("goodbye",)),
    ]

    # undo=True: the real text boxes this replays onto always set this (see
    # row_building.py's _build_editable_text_box/_build_spacer_text_box) -
    # a plain tk.Text() defaults to undo *disabled*, which would make
    # edit_undo() below silently do nothing rather than exercise what this
    # test is actually checking.
    fresh = tk.Text(root, undo=True)
    fresh.insert("1.0", log.baseline)
    fresh.edit_reset()

    replay_onto(fresh, log)
    assert fresh.get("1.0", "end-1c") == "goodbye"

    fresh.edit_undo()
    assert fresh.get("1.0", "end-1c") == "hello world"
