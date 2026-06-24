"""Lets a review-screen text box's undo/redo history survive its row being
torn down and rebuilt by the virtualization scheme (ReviewFrame._destroy_row/
_build_row in review_view.py) - Tk's own undo stack lives on the Text widget
instance itself, so it's destroyed along with the widget rather than the box
it represents. Kept in memory only, per the "don't persist across sessions"
requirement - ReviewFrame._undo_logs starts empty on every launch, the same
as an unedited box.

Works by renaming the Tk Text widget's own Tcl command and substituting a
thin proxy that records every insert/delete call - including the ones Tk's
own keyboard bindings make directly via the widget's Tcl command, not just
calls made through tkinter's Python methods - before forwarding it
unchanged to the real command. This is the same "command interception"
technique idlelib's WidgetRedirector uses, since it's the only way to
observe edits made by Tk's C-level key bindings rather than by this
codebase's own Python calls.

edit_undo()/edit_redo() are recorded too (as bare markers, via
keyboard_nav.py's _undo_text/_redo_text), even though they don't go through
the Tcl command above (Tk performs them directly on its internal undo
stack, not by re-invoking the widget's "insert"/"delete" subcommands) -
without that, a box torn down partway through an undone edit would replay
back to the *un-undone* text, since the recorded insert/delete ops alone
don't capture that an undo happened in between.

Replaying the recorded ops back onto a freshly built widget (in the same
order, via plain .insert()/.delete()/.edit_undo()/.edit_redo() calls) lets
Tk's own autoseparator logic re-derive the same undo-step grouping it used
originally, since that grouping is a deterministic function of the
insert/delete call sequence - so there's no need to separately record where
Tk decided to place a separator.
"""

import tkinter as tk
from typing import Callable, List, Tuple


class UndoLog:
    """Records the ops applied to one text box's slot, across however many
    times that box's row has been torn down and rebuilt this session."""

    def __init__(self) -> None:
        self.ops: List[Tuple[str, tuple]] = []


def attach_undo_recording(text_widget: tk.Text, log: UndoLog) -> Callable[[], None]:
    """Start appending text_widget's insert/delete calls to `log`. Returns a
    detach() callback that must be called before the widget is destroyed,
    to restore its original Tcl command (otherwise the proxy command this
    installs is leaked, along with everything it closes over)."""
    widget_path = str(text_widget)
    shadow_path = widget_path + "_undo_shadow"
    tcl = text_widget.tk
    tcl.call("rename", widget_path, shadow_path)

    def _proxy(*args):
        result = tcl.call((shadow_path,) + args)
        if args and args[0] in ("insert", "delete"):
            log.ops.append((args[0], args[1:]))
        return result

    tcl.createcommand(widget_path, _proxy)

    def detach() -> None:
        tcl.deletecommand(widget_path)
        tcl.call("rename", shadow_path, widget_path)

    return detach


def replay_onto(text_widget: tk.Text, log: UndoLog) -> None:
    """Re-apply every op `log` has recorded so far onto `text_widget`,
    rebuilding both its visible content and its native undo/redo stack to
    match what they were just before this box's row was last torn down.
    Must run before attach_undo_recording, on a widget whose own undo
    stack is otherwise empty (see row_building.py's _populate_text_box) -
    rerecording this replay into `log` itself would duplicate every op on
    the next rebuild."""
    for name, args in log.ops:
        if name == "undo":
            try:
                text_widget.edit_undo()
            except tk.TclError:
                pass  # nothing to undo - shouldn't happen on a faithful replay
        elif name == "redo":
            try:
                text_widget.edit_redo()
            except tk.TclError:
                pass  # nothing to redo - shouldn't happen on a faithful replay
        else:
            getattr(text_widget, name)(*args)
