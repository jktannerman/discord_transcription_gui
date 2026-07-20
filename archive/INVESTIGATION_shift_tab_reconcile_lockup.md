# Investigation: Shift-Tab burst causes silent edit loss + "greyed out" screen

**Status: fixed.** Root cause was confirmed from an actual Python traceback
(not just log inference), and both the fix and defense-in-depth hardening
described in "Recommended fix" below have since been implemented - see
ARCHITECTURE.md's "Review screen internals" section (the entry titled
"Symbolic marks recorded in a UndoLog must be resolved before they can
drift") for what actually shipped and where, and this file's own "What's
still open" section at the bottom for the up-to-date status of each item.
This file is kept in full (not deleted) so a future session (human or
Claude) has the original failure analysis available without having to
re-derive it from scratch - the bug this documents is exactly the kind of
subtle, cross-feature interaction (virtualization + undo replay) that's
cheap to reintroduce by accident if the reasoning behind the fix isn't
still on record somewhere. Read "Root cause (confirmed)" first - it
supersedes an earlier, wrong hypothesis kept below under "Superseded
hypothesis" only because the trace analysis that produced it uncovered
real, still-relevant downstream mechanics.

## User-reported symptoms

1. Holding Tab/Shift-Tab to move rapidly through the review screen (e.g.
   Shift-Tabbing upward fast to reference something written earlier) can
   silently revert an already-edited text box back to its raw OCR text -
   discovered only much later, and the loss survives closing and resuming
   the session (i.e. it gets autosaved), so it isn't recoverable from
   in-app undo either.
2. The same kind of rapid-navigation burst has also produced a review
   screen where the top part of the window goes visually grey/blank -
   effectively a stuck/broken render, not a clean crash (no error dialog).

Both were reproduced/observed together in one real session.

## Root cause (confirmed)

**`text_undo.py`'s `UndoLog` records a text box's `delete` calls using
whatever raw Tcl arguments Tk itself passed - and when the user selects
text (double-click, drag-select, Ctrl+A, Shift+Arrow) and then removes it
(Delete/Backspace, typing over the selection, Ctrl+X), Tk's own built-in
Text-widget binding performs that removal via the symbolic marks
`sel.first`/`sel.last`, not absolute character positions.** Those marks are
only meaningful while the specific widget instance actually has a live
`"sel"`-tagged selection - they are not part of the text's content, and
have no meaning at all on a different, freshly-built widget with nothing
selected.

`attach_undo_recording`'s interception proxy (`text_undo.py:71-107`)
records every `insert`/`delete` subcommand sent to the widget's real Tcl
command verbatim:

```python
if args and args[0] in ("insert", "delete") and not log.suppress:
    op = (args[0], args[1:])
    log.ops.append(op)
```

So a selection-delete gets recorded as literally
`("delete", ("sel.first", "sel.last"))`.

When that box's row is later torn down and rebuilt (e.g. because it
scrolled out of the virtualized window and back in), `_populate_text_box`
(`row_building.py:358`) replays the box's whole `UndoLog` onto the **brand
new widget** via `replay_onto` (`text_undo.py:130-152`):

```python
else:
    getattr(text_widget, name)(*args)
```

For the recorded op above, this calls `text_widget.delete("sel.first",
"sel.last")` on a widget that has **no selection at all** (nothing has
ever been selected on it - it was just created). Tk raises:

```
_tkinter.TclError: text doesn't contain any characters tagged with "sel"
```

**`replay_onto` has no exception handling around this call.** Contrast with
the *recording* proxy, which already has a `try/except tk.TclError` around
its own forwarding call (`text_undo.py:88-104`) - explicitly because the
widget's own internal Tcl bindings routinely probe `sel.first` in ways
that are expected to fail and are normally wrapped in Tcl's own `catch{}`,
which tkinter's command-redirection breaks (the module docstring already
describes this as "a long-standing tkinter quirk, also hit by idlelib's
WidgetRedirector"). That existing guard protects the *forward*/recording
direction only - it does nothing for replay, which calls the widget's
`.delete()` Python method directly, not through the proxy.

### What this breaks, concretely

The exception propagates up through:

```
_populate_text_box (row_building.py:424, the replay_onto call)
  -> _build_editable_text_box / _build_spacer_text_box (row_building.py)
  -> _build_row (row_building.py:111)
  -> _sync_materialized_rows's build loop (review_view.py:578)
  -> _reconcile (review_view.py:772)
  -> whichever caller invoked _reconcile - confirmed to be BOTH:
       - _ensure_materialized, called synchronously from Tab/Shift-Tab's
         _move_focus/_goto_slot (keyboard_nav.py:436)
       - _run_scheduled_reconcile, the normal 80ms-debounced scroll-driven
         reconcile (review_view.py:854)
```

Every one of these is a Tk-bound callback. A Python exception raised
inside one only ever reaches Tk's own default `report_callback_exception`
(prints "Exception in Tkinter callback" + traceback to console/stderr) -
it is **not** caught by this app's own logger (nothing in `app.log`
reflects it at all), and Tk swallows it and continues its event loop as if
the callback had returned normally, mid-function. Consequences, all
confirmed against the real trace:

1. **The build loop dies wherever this box sits in the batch, permanently
   for the rest of the session for *that specific box*.** The poisoned
   `("delete", ("sel.first", "sel.last"))` op is a permanent part of that
   box's `UndoLog.ops` - every future rebuild replays the exact same
   sequence and hits the exact same crash. This is directly confirmed: the
   identical traceback recurs 4 times in the captured console output, from
   **both** trigger paths above (`_ensure_materialized` twice,
   `_run_scheduled_reconcile` twice) - proving this is not specific to
   keyboard-driven reentrancy, contrary to the earlier hypothesis below.
2. **`self._reconcile()`'s own bookkeeping (`self._materialized_range =
   (first_idx, last_idx)`, `review_view.py:773`) never runs for any crashed
   call**, since the crash happens inside `_sync_materialized_rows`, which
   is called *before* that assignment and never returns normally.
   `self._materialized_range` is therefore left pointing at a stale value
   that no longer matches the real, partially-mutated contents of
   `self._row_frames` - this is what causes every *subsequent* reconcile to
   miscalculate which rows are "already built" relative to a wrong
   baseline, producing repeated "already has a live widget - the old
   widget's content is about to be orphaned" warnings and the pre-existing
   "possible silent data loss" regression alarm (`row_building.py:449`) for
   *other*, unrelated rows in the same materialization batch (rows 2/3/4 in
   the captured incident) - **these are real, but are downstream fallout of
   this one crash, not a separate bug.**
3. **Every slot after the crash point in that build batch is permanently
   missing from `self._text_widgets`, while still listed in the static
   `self._slots` navigation list** (built once in `__init__`, never
   corrected afterward). Any later Tab/Shift-Tab attempt to focus one of
   those slots raises `KeyError` in `_focus_text_box`
   (`keyboard_nav.py:392`, `widget = self._text_widgets[(index, role)]`,
   no `.get()` guard) - repeating on *every* keypress for as long as the
   user keeps trying to navigate there. Confirmed: dozens of identical
   `KeyError: (1, 'spacer_end')` tracebacks in a row while the user held
   Shift-Tab.
4. Since a crashed reconcile never reaches its tail
   (`canvas.configure(scrollregion=...)`, `canvas.coords(...)`, the final
   `_log_event("reconcile", ...)` call), the canvas's on-screen geometry is
   left half-updated - consistent with the "top part of the screen greyed
   out" symptom.

### The exact traceback (from `gui_transcription/temp_console_output.txt`,
run `20260720T175505.003837`, console output that is normally lost since
nothing routes it into `app.log` - see "Why nothing shows up in app.log"
below)

First occurrence, triggered by holding Shift-Tab:

```
Exception in Tkinter callback
Traceback (most recent call last):
  File "...\tkinter\__init__.py", line 2074, in __call__
    return self.func(*args)
  File "...\app\gui\keyboard_nav.py", line 200, in _on_shift_tab
    return self._move_focus(-1)
  File "...\app\gui\keyboard_nav.py", line 431, in _move_focus
    self._goto_slot(slots[pos])
  File "...\app\gui\keyboard_nav.py", line 436, in _goto_slot
    self._ensure_materialized(index)
  File "...\app\gui\review_view.py", line 533, in _ensure_materialized
    self._reconcile()
  File "...\app\gui\review_view.py", line 772, in _reconcile
    newly_built = self._sync_materialized_rows(old_range, (first_idx, last_idx))
  File "...\app\gui\review_view.py", line 578, in _sync_materialized_rows
    anchor = self._build_row(idx, before=anchor)
  File "...\app\gui\row_building.py", line 111, in _build_row
    self._build_editable_text_box(
        right, index, role, item.initial_ocr_texts[image_index], image_h, pady_bottom=gap,
    )
  File "...\app\gui\row_building.py", line 307, in _build_editable_text_box
    self._populate_text_box(key, text_widget, initial_text)
  File "...\app\gui\row_building.py", line 424, in _populate_text_box
    replay_onto(text_widget, log, ...)
  File "...\app\gui\text_undo.py", line 150, in replay_onto
    getattr(text_widget, name)(*args)
  File "...\tkinter\__init__.py", line 3831, in delete
    self.tk.call(self._w, 'delete', index1, index2)
_tkinter.TclError: text doesn't contain any characters tagged with "sel"
```

Immediately followed by the same-shaped `KeyError` repeating on every
subsequent Shift-Tab keypress (over 30 times captured in the log excerpt):

```
Exception in Tkinter callback
Traceback (most recent call last):
  File "...\tkinter\__init__.py", line 2074, in __call__
    return self.func(*args)
  File "...\app\gui\keyboard_nav.py", line 200, in _on_shift_tab
    return self._move_focus(-1)
  File "...\app\gui\keyboard_nav.py", line 431, in _move_focus
    self._goto_slot(slots[pos])
  File "...\app\gui\keyboard_nav.py", line 437, in _goto_slot
    self._focus_text_box(index, role)
  File "...\app\gui\keyboard_nav.py", line 392, in _focus_text_box
    widget = self._text_widgets[(index, role)]
KeyError: (1, 'spacer_end')
```

Then, later in the same session, the *identical* `TclError` recurs three
more times - twice more via keyboard nav, and (critically) also via the
**scroll-debounced** path, proving the trigger is independent of keyboard
reentrancy:

```
Exception in Tkinter callback
Traceback (most recent call last):
  File "...\tkinter\__init__.py", line 2074, in __call__
    return self.func(*args)
  File "...\tkinter\__init__.py", line 862, in callit
    func(*args)
  File "...\app\gui\review_view.py", line 854, in _run_scheduled_reconcile
    self._reconcile()
  File "...\app\gui\review_view.py", line 772, in _reconcile
    newly_built = self._sync_materialized_rows(old_range, (first_idx, last_idx))
  File "...\app\gui\review_view.py", line 578, in _sync_materialized_rows
    anchor = self._build_row(idx, before=anchor)
  File "...\app\gui\row_building.py", line 111, in _build_row
    self._build_editable_text_box(...)
  File "...\app\gui\row_building.py", line 307, in _build_editable_text_box
    self._populate_text_box(key, text_widget, initial_text)
  File "...\app\gui\row_building.py", line 424, in _populate_text_box
    replay_onto(text_widget, log, ...)
  File "...\app\gui\text_undo.py", line 150, in replay_onto
    getattr(text_widget, name)(*args)
  File "...\tkinter\__init__.py", line 3831, in delete
    self.tk.call(self._w, 'delete', index1, index2)
_tkinter.TclError: text doesn't contain any characters tagged with "sel"
```

(Timestamps from the surrounding `app.log` lines: occurrences at
approximately 18:01:46.49, 18:01:49.52 [via `_run_scheduled_reconcile`],
18:01:50.42, and 18:01:54.84 - each immediately preceded in `app.log` by
the pre-existing "possible silent data loss" alarm for key `[2,
'spacer_end']`, which is the downstream symptom described in point 2
above, not a separate box crashing.)

### Why nothing shows up in `app.log`

Confirmed directly this time, not just inferred: this whole class of
failure only ever appears in the **console/stderr** output (what Tk's
default `report_callback_exception` prints), never in `app.log`. The app's
own JSON logger is never involved because nothing in the `_reconcile`/
`_build_row`/`replay_onto` call chain wraps these calls in a `try/except`
that logs - unlike `main_window.py:530-536`, which has an explicit comment
acknowledging this exact limitation for a *different* code path (chatlog
parsing errors inside a `root.after()` callback) and wraps it accordingly.
If a user's terminal isn't being captured/redirected, this failure is
**invisible** except for its downstream symptoms (silent data loss,
greyed-out screen) - which is exactly why this took a full log-forensics
pass to even suspect, before the actual console output
(`temp_console_output.txt`) surfaced and confirmed it directly.

### Recommended fix

Two complementary things, not either/or:

1. **Don't record selection-relative marks that can't be replayed.** In
   `attach_undo_recording`'s `_proxy` (`text_undo.py:96-102`), when
   recording a `delete` (or `insert`) op, resolve any symbolic index
   argument (`"sel.first"`, `"sel.last"`, `"insert"`, `"end"`, etc.) to an
   absolute `"line.column"` string *at record time*, via
   `text_widget.index(arg)`, before appending to `log.ops` - not just for
   `sel.*` but for `insert`/`end`/any other mark, since those are just as
   capable of drifting or being invalid relative to a freshly-built
   widget's state as `sel.*` is. This is the real fix: it makes every
   recorded op replay-safe by construction, not just crash-safe.
2. **Add defensive `try/except tk.TclError` around `replay_onto`'s
   `getattr(text_widget, name)(*args)` call** (`text_undo.py:150`) as a
   last-resort guard, logged loudly (this app already has a strong
   precedent for loud, non-fatal degradation - see `_populate_text_box`'s
   existing "possible silent data loss" alarm and `_remeasure_built_rows`'s
   `winfo_height()<=1` guard in `ARCHITECTURE.md`). This alone is *not*
   sufficient as the only fix - silently skipping a delete that can't be
   replayed would leave the replayed text wrong (still containing text that
   was supposed to be deleted), which is a quieter form of the same
   correctness bug - but it stops one bad op from wedging the *entire
   review screen* for the rest of the session, which is the more urgent
   half of the user-visible damage (points 2-4 above).

Fixing only the reconcile-lockup fallout (e.g. making `_reconcile` more
resilient to a crash mid-batch) without fixing #1 would leave the
underlying data-loss bug intact - the box that hits the `sel.first` crash
will *still* fail to replay its real content correctly every time it's
rebuilt, even once the fallout stops taking the rest of the screen down
with it.

## Superseded hypothesis (kept for its still-valid downstream analysis)

Before the console traceback surfaced, the investigation proceeded from
`app.log`/`scroll_trace.log` alone and landed on a *reentrancy* theory:
that holding Shift-Tab causes `_reconcile()` to call `canvas.update()`
(via `_settle_pending_geometry`'s escalation path,
`review_view.py:588-627`) which drains queued autorepeat keypresses and
re-enters `_reconcile()` before the outer call finishes, corrupting
`self._materialized_range` relative to `self._row_frames`.

**This turned out not to be the trigger** - the same `TclError` recurs
identically via the scroll-debounced path (`_run_scheduled_reconcile`),
which is not reentrant with keyboard nav at all, proving the crash is
independent of Shift-Tab-specific reentrancy. Holding Shift-Tab does still
matter, but only because it's what repeatedly *demands* a rebuild of
whichever row happens to contain the poisoned box, at a rate fast enough
to make the resulting cascade (points 2-4 above) obvious quickly - a
single, one-off rebuild of that row (e.g. an ordinary scroll past it) would
trigger the exact same crash and exact same lockup, just less
conspicuously and without the rapid-fire `KeyError` spam.

The downstream mechanics this hypothesis correctly identified from the log
evidence alone - `self._materialized_range` getting permanently stuck,
which then causes repeated double-builds/orphaned widgets and the
"possible silent data loss" alarm for *other* boxes in the same batch -
are real and are kept here since they explain *why* the blast radius of
one bad `sel.first` delete extends to several unrelated rows, not just the
one box that actually failed to replay. The original `scroll_trace.log`
evidence for this (materialized_range frozen at `[5, 18]` for the rest of
the session; only 6 total successful `reconcile` completions in 2838
traced events) is accurate and still useful for confirming *that* the
lockup happened, even though it doesn't show *why* - the `TclError` itself
never reaches `scroll_trace.log` or `app.log`, only the console.

### How to find this again

```powershell
# app.log: the two downstream-symptom messages, and which run_id(s) they cluster in
Select-String -Path $env:USERPROFILE\.discord_transcription_gui\app.log,$env:USERPROFILE\.discord_transcription_gui\app.log.* `
  -Pattern '"already has a live widget"|"possible silent data loss"'
```

This only finds the *fallout*, not the trigger - the actual `TclError`/
`KeyError` tracebacks only ever appear on stderr/console. **If this
recurs, capture the app's console output** (redirect stdout/stderr to a
file when launching, e.g. `discord-transcription-gui > console.log 2>&1`,
or run it from a terminal that keeps scrollback) - that is what actually
cracked this open, after a first pass through the JSON logs alone produced
a plausible-but-wrong theory. Cross-reference a captured traceback's
timestamp-adjacent `app.log`/`scroll_trace.log` lines the same way this
investigation did, to confirm which specific box/run is affected.

## What's still open

Nothing - both items below, and the report_callback_exception idea, have
since shipped:

- **Fixed.** `text_undo.py`'s recording proxy now resolves every
  insert/delete index argument (not just `sel.*` - `insert`, `end`, and any
  other mark) to an absolute `"line.column"` string at record time, before
  the mutating call runs - covering `insert` too, per the original "not yet
  confirmed" note here, even though no concrete failing `insert` case had
  been observed. See ARCHITECTURE.md's "Review screen internals" entry for
  the full writeup, and `app_tests/test_text_undo.py` for the regression
  tests (a selection-delete's recorded op holds absolute indices, not
  `sel.first`/`sel.last`, and replays cleanly onto a fresh widget) - the
  same shape as the existing `test_resumed_edit_survives_being_paged_
  out_and_back_in_with_no_further_edits` test this file originally pointed
  to as a model.
- **Fixed**, as a last-resort backstop rather than the primary fix (per the
  original "not sufficient as the only fix" caveat above, which still
  holds - this doesn't replace the record-time fix, it only stops a future
  unanticipated replay failure from wedging the whole screen): a `TclError`
  from `replay_onto` is now caught in `row_building.py`'s
  `_populate_text_box`, recovering the box's actual last-known-good text
  from `self._saved_texts` (not `baseline`/`initial_text`, either of which
  can be staler) and self-healing the `UndoLog` (clearing the poisoned ops,
  re-baselining on the recovered text) so the same box doesn't crash again
  on its next rebuild.
- **Also fixed, beyond the two items above** (closing the actual
  data-loss/cascade mechanics this investigation traced through points 1-4
  of "What this breaks, concretely", as defense in depth against *any*
  future per-row build failure, not just this specific `TclError`):
  - The double-build orphaning hole (`row_building.py`'s
    `_reclaim_widget_if_present`, called from both
    `_build_editable_text_box` and `_build_spacer_text_box`) - rebuilding
    an already-live key now captures its current content/cursor into
    `self._saved_texts`/`self._saved_cursor` and tears it down properly
    first, instead of just logging a warning and silently orphaning
    whatever was typed into it.
  - The all-or-nothing build batch (`review_view.py`'s new
    `_try_build_row`, used throughout `_sync_materialized_rows`) - one
    row's build exception no longer aborts the rest of that reconcile's
    batch or leaves `self._materialized_range` permanently stale; the
    failing row is torn down, logged, and skipped, and its neighbors still
    get built.
  - The `KeyError` cascade (`keyboard_nav.py`'s `_focus_text_box` now uses
    `self._text_widgets.get(...)` with a logged-and-skip fallback instead
    of a raw dict lookup) - a slot whose row failed to build no longer
    raises on every subsequent Tab/Shift-Tab press aimed at it.
- **Fixed.** `root.report_callback_exception` (`main.py`) now routes
  through this app's own logger (`_log_tk_callback_exception`), so any
  future uncaught Tk-callback exception - whatever its cause - lands in
  `app.log` immediately instead of requiring the kind of console-log
  archaeology this investigation originally needed.
