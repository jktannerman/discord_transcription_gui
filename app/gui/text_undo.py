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
from typing import Callable, List, Optional, Tuple

OpCallback = Callable[[str, tuple], None]


class UndoLog:
    """Records the ops applied to one text box's slot, across however many
    times that box's row has been torn down and rebuilt this session.

    `baseline` is the text the box actually started from the first time it
    was ever built this session - self._saved_texts.get(key) for a resumed
    edit, or the item's plain initial_text otherwise (see row_building.py's
    _populate_text_box). Every op in `ops` is a delta relative to whichever
    one that was, so a rebuild must replay onto *that* baseline, not onto
    the item's raw initial_text - re-basing onto initial_text instead would
    silently discard a resumed edit the moment its box is rebuilt, even
    with zero further ops to replay."""

    def __init__(self) -> None:
        self.ops: List[Tuple[str, tuple]] = []
        self.baseline: Optional[str] = None
        # Set True around a programmatic edit_undo()/edit_redo() call (see
        # keyboard_nav.py's _undo_text/_redo_text) to stop the recording
        # proxy below from also capturing that call's own internal
        # delete/insert side effects as if they were ordinary ops - despite
        # this module's own (incorrect, on at least this Tk version) belief
        # that edit_undo()/edit_redo() bypass the widget's Tcl command
        # entirely, they do not: they restore via the same renamed command
        # the proxy intercepts. Without this guard, an undo/redo gets
        # recorded twice - once as the delete/insert ops Tk performs to
        # carry it out, and again as the bare "undo"/"redo" marker appended
        # right after - so replaying the log applies it twice, undoing one
        # step further than the user actually did.
        self.suppress = False


def attach_undo_recording(
    text_widget: tk.Text, log: UndoLog, on_op: Optional[OpCallback] = None
) -> Callable[[], None]:
    """Start appending text_widget's insert/delete calls to `log`. Returns a
    detach() callback that must be called before the widget is destroyed,
    to restore its original Tcl command (otherwise the proxy command this
    installs is leaked, along with everything it closes over).

    `on_op`, if given, is called with the same (name, args) just appended
    to `log.ops`, after the append - purely a diagnostic hook (see
    row_building.py's _populate_text_box) for tracing exactly which ops a
    box's edit history is built from, since this proxy is the *only* place
    that observes edits made by Tk's own C-level key bindings rather than
    this codebase's Python calls."""
    widget_path = str(text_widget)
    shadow_path = widget_path + "_undo_shadow"
    tcl = text_widget.tk
    tcl.call("rename", widget_path, shadow_path)

    def _resolve_index(value):
        """Turn a possibly-symbolic index argument (`"sel.first"`,
        `"insert"`, `"end"`, `"1.0 lineend"`, ...) into the absolute
        `"line.column"` string it means *right now* - queried against the
        live widget, before whatever op is about to mutate it.

        This is the fix for a real, reproduced bug: Tk's own built-in
        Text bindings remove a selection via `delete sel.first sel.last`
        (e.g. Delete/Backspace/typing-over-a-selection), and recording
        that literally means replay_onto later calls
        `text_widget.delete("sel.first", "sel.last")` on a freshly-built
        widget that has never had anything selected - raising
        `_tkinter.TclError: text doesn't contain any characters tagged
        with "sel"` and wedging the whole review screen's virtualization
        (see INVESTIGATION_shift_tab_reconcile_lockup.md). Resolving to an
        absolute position at record time, while the mark is still live,
        makes every recorded op replay-safe by construction - not just
        `sel.*`, but any mark (`insert`, `end`, ...), since those are just
        as capable of meaning something different (or nothing at all) on a
        freshly-built widget as `sel.*` is.

        Queried via the shadow (real) widget's own Tcl command directly,
        not through `text_widget.index()` - the latter would route back
        through this same proxy (it calls the real widget path, which is
        this function), which works but is needlessly indirect. Best-
        effort: if the index can't be resolved at all (e.g. a genuinely
        nonexistent mark), the raw value is kept as a fallback - no worse
        than before this fix existed."""
        try:
            # str(...): tcl.call can hand back a Tcl_Obj ("textindex")
            # rather than a plain Python str - fine as a replay argument
            # either way, but this module's contract ("resolved to an
            # absolute line.column string") and every log/equality check
            # against a recorded op both assume a plain str.
            return str(tcl.call(shadow_path, "index", value))
        except tk.TclError:
            return value

    def _resolve_op_args(name, rest):
        if name == "delete":
            return tuple(_resolve_index(arg) for arg in rest)
        if name == "insert" and rest:
            # Only the leading index argument is a position - the rest are
            # (text, tags...) pairs and must be left untouched.
            return (_resolve_index(rest[0]),) + tuple(rest[1:])
        return rest

    def _proxy(*args):
        recordable = bool(args) and args[0] in ("insert", "delete") and not log.suppress
        # Resolved *before* the real call below actually performs the
        # insert/delete - a symbolic mark like "sel.first" only means
        # anything relative to the widget's state right before the
        # mutation, not after.
        resolved_rest = _resolve_op_args(args[0], args[1:]) if recordable else None
        try:
            result = tcl.call((shadow_path,) + args)
        except tk.TclError:
            # Renaming the widget's command this way intercepts *every*
            # subcommand sent to it, not just insert/delete - including
            # ones Tk's own internal bindings call expecting to fail
            # sometimes, e.g. `$widget index sel.first` when there's no
            # selection, normally wrapped in the binding's own
            # `catch {...}`. A native Tcl command failing there is no
            # problem - but here, the failure surfaces as a genuine Python
            # exception raised by *this* function, and tkinter's command
            # redirection propagates that exception all the way out of
            # mainloop() regardless of any surrounding Tcl-level catch
            # (a long-standing tkinter quirk, also hit by idlelib's
            # WidgetRedirector, which this module's docstring already
            # points to). Swallowing it and returning "" mirrors what a
            # plain Tcl catch around the real (un-redirected) command would
            # have done with the result anyway.
            return ""
        if recordable:
            op = (args[0], resolved_rest)
            log.ops.append(op)
            if on_op is not None:
                on_op(*op)
        return result

    tcl.createcommand(widget_path, _proxy)

    def detach() -> None:
        tcl.deletecommand(widget_path)
        tcl.call("rename", shadow_path, widget_path)

    return detach


def replay_onto(text_widget: tk.Text, log: UndoLog, on_op: Optional[OpCallback] = None) -> None:
    """Re-apply every op `log` has recorded so far onto `text_widget`,
    rebuilding both its visible content and its native undo/redo stack to
    match what they were just before this box's row was last torn down.
    Must run before attach_undo_recording, on a widget whose own undo
    stack is otherwise empty (see row_building.py's _populate_text_box) -
    rerecording this replay into `log` itself would duplicate every op on
    the next rebuild.

    `on_op`, if given, is called with each (name, args) as it's replayed -
    a diagnostic hook for tracing exactly what a rebuilt box's content was
    reconstructed from."""
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
        if on_op is not None:
            on_op(name, args)
