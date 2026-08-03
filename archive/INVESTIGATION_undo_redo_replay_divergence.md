# Investigation: `edit_undo()`/`edit_redo()` replay can silently reproduce the *wrong* text after a rapid Tab/Shift-Tab burst

Status as of this writeup: **root cause identified and reproduced from real
production logs (`app.log`/`scroll_trace.log`); fixed.** This file exists so
a future session (human or Claude) does not have to re-derive the analysis
below from scratch - the log forensics alone took a full pass through two
session logs to pin down. Read "Root cause" first, then "What's still open"
at the bottom for exactly what shipped and what's left.

**Update, later session:** direction #1 below ("recommended fix direction")
has since shipped - `keyboard_nav.py` no longer records a bare `"undo"`/
`"redo"` marker; a successful Ctrl+Z/Ctrl+Shift+Z is recorded as a
`"replace"` op (the box's exact resulting text) instead, and `replay_onto`
no longer calls `edit_undo()`/`edit_redo()` during replay at all. Direction
#2's detection also gained the self-heal recovery step that was previously
only sketched as "still open" (see below). See `ARCHITECTURE.md`'s "A
recorded undo/redo must not be replayed by calling edit_undo()/edit_redo()
again" for the shipped design and its own tradeoff (a further Ctrl+Z after
a rebuild can land on an intermediate state rather than the exact live step
boundary - visible, not silent). Everything below this point is kept as
originally written, as the historical record of how the bug was found and
what was considered - only "What's still open" at the very end has been
updated to reflect current status.

**Important - do not confuse this with `archive/INVESTIGATION_shift_tab_
reconcile_lockup.md`.** That investigation covers a *different*, already-
fixed bug in the same file (`text_undo.py`): a recorded `("delete",
("sel.first", "sel.last"))` op crashing on replay with `TclError: text
doesn't contain any characters tagged with "sel"`. That fix (resolving
symbolic index marks to absolute `"line.column"` positions at record time)
is real, shipped, and working - the case documented below occurs
**downstream of that fix, in logs from *after* it shipped**, and involves no
`sel.*` marks and no crash of any kind. Initial review of this session's
logs briefly misattributed one old, pre-fix incident (see "A red herring
worth recording" below) to this bug before checking timestamps against the
other investigation doc - worth reading that section before assuming any
new-looking `app.log` alarm is this bug rather than the old one.

## User-reported symptoms

1. Holding Shift-Tab to move rapidly upward through the review screen (e.g.
   to reference something written earlier) can leave an already-edited text
   box's content silently changed back toward its original OCR text -
   discovered only later, after the fact.
2. The change survives closing and resuming the session (it's autosaved),
   so it isn't recoverable from in-app undo either - the user has to notice
   the discrepancy by rereading and manually redo the edit.

## Root cause

`text_undo.py`'s `replay_onto` (and the design note in its module
docstring) rests on an explicit, stated assumption:

> "Replaying the recorded ops back onto a freshly built widget ... lets
> Tk's own autoseparator logic re-derive the same undo-step grouping it
> used originally, since that grouping is a deterministic function of the
> insert/delete call sequence - so there's no need to separately record
> where Tk decided to place a separator."

`keyboard_nav.py`'s `_undo_text`/`_redo_text` additionally append a bare
`("undo", ())` / `("redo", ())` marker to the box's `UndoLog.ops` whenever
the user presses Ctrl+Z/Ctrl+Shift+Z, and `replay_onto` replays that marker
by literally calling `text_widget.edit_undo()` / `.edit_redo()` again on the
freshly-built widget:

```python
def replay_onto(text_widget: tk.Text, log: UndoLog, on_op=None) -> None:
    for name, args in log.ops:
        if name == "undo":
            try:
                text_widget.edit_undo()
            except tk.TclError:
                pass  # nothing to undo - shouldn't happen on a faithful replay
        ...
```

**This is the false assumption.** Whether `edit_undo()` reverts one insert,
several coalesced inserts, or something else again depends on how Tk's
`autoseparators` option grouped the *preceding* inserts/deletes into undo
steps - and that grouping is not guaranteed to come out identically between
a live session (edits arriving one at a time, spread over real seconds,
interleaved with cursor moves, focus changes, and everything else a live Tk
mainloop does) and a replay (the same ops fired back-to-back in a tight
Python loop with nothing else happening). When the two diverge, `edit_undo()`
undoes a *different amount* of text during replay than it did live, and the
box silently ends up holding different content than it held at teardown -
no exception, no log line, nothing to signal that anything went wrong. This
is worse than the already-fixed `sel.first` bug in one specific way: that
bug crashed loudly (`TclError`) and froze the whole screen, which - while
bad - is at least *conspicuous*. This one produces no error and no crash;
the box just quietly ends up wrong, and it looks exactly like an
intentional edit from that point on (it gets autosaved, and appears
"finalized" if the user clicks through without noticing).

## The reproduced case (`app/gui/text_undo.py`'s `replay_onto`, `undo` branch)

Chatlog `kq_wm_0001_p1.html`, item index 9's OCR box (`key = (9, "ocr0")`),
across two consecutive real runs.

### 1. The edit, live (run `20260720T210355.882917`, `scroll_trace.log`)

Between 21:04:01 and 21:04:39 the user builds up a real edit on this box: 25
ops total, including one deliberate Ctrl+Z partway through (at 21:04:16.263,
reverting a large accidental paste back to the 249-char state from just
before it - this itself replayed correctly, it's not where the bug bites).
By 21:04:39.598 the box holds its final, correct edited text:

```json
{"key": [9, "ocr0"], "hash": "7d3a2c95", "len": 278, "message": "box_modified", ...}
```

### 2. Ordinary Tab paging tears the row down (correct, unremarkable)

```json
{"key": [9, "ocr0"], "hash": "7d3a2c95", "len": 278, "message": "box_teardown",
 "timestamp": "2026-07-20T21:05:41.060747+00:00", ...}
```

This teardown is a normal, single-step forward Tab (not part of a rapid
burst) paging the row out of the ~12-row materialized window. The correct,
final edited text (`hash 7d3a2c95`, 278 chars) is captured into
`self._saved_texts` at this point, as designed.

### 3. A held Shift-Tab burst pages back up through it

From `21:05:41.441` to `21:05:45.568` - about 4.1 seconds - `scroll_trace.log`
shows `move_focus_start` events roughly 20-220ms apart, almost all
`delta: -1` (a handful of `delta: 1` mixed in, consistent with OS key-repeat
jitter), walking the focus slot backward through items 16 → 15 → 14 → ... →
9. This is the held-Shift-Tab auto-repeat the user described. It drags item
9's row back into the materialized window, which means rebuilding it -
and since this box already has a non-empty `UndoLog` (`op_count: 25`), that
rebuild goes through `_populate_text_box`'s **replay** branch, not a fresh
build:

```json
{"key": [9, "ocr0"], "hash": "c4752eab", "len": 247, "message": "box_build_replay_start",
 "op_count": 25, "timestamp": "2026-07-20T21:05:45.587894+00:00", ...}
```

(`c4752eab`/247 chars is `log.baseline` - the box's pristine, un-edited OCR
text, correctly used as the replay starting point per the already-fixed
"`UndoLog` must record what it actually started from" rule in
ARCHITECTURE.md.)

### 4. Op 4 of 25 is the recorded `undo` marker

```json
{"key": [9, "ocr0"], "op": "undo", "args": "()", "message": "box_replay_op",
 "timestamp": "2026-07-20T21:05:45.589627+00:00", ...}
```

This is the replay of the Ctrl+Z from step 1 above. Ops 1-3 (two `\n`
inserts plus the big paste) replay identically up to this point - the
divergence happens *at* this `edit_undo()` call or in how it interacts with
the 21 ops that follow it, all of which reference **absolute** `line.column`
positions that were resolved against the *live* session's post-undo text.

### 5. The replay finishes on the wrong text

```json
{"key": [9, "ocr0"], "hash": "c740250b", "len": 279, "message": "box_build_replay_done",
 "timestamp": "2026-07-20T21:05:45.593085+00:00", ...}
```

`c740250b`/279 chars, not `7d3a2c95`/278 chars. No exception was raised (the
`try/except tk.TclError` around `replay_onto` never fires - this isn't a
crash, it's a silent wrong answer), so nothing in `_populate_text_box`'s
existing "possible silent data loss" regression alarm fires either: that
alarm only checks for a full revert to the item's bare default
(`result_text == initial_text`), and this landed on a different-but-still-
edited-looking 279-char string, not on the 247-char default. **This specific
corruption is invisible to the app's own existing safety net.**

### 6. The corruption is what gets autosaved and reloaded

The very next launch, `20260720T210609.390875`, resumes this session. Its
first build of this box loads straight from the corrupted autosave:

```json
{"key": [9, "ocr0"], "hash": "c740250b", "len": 279, "message": "box_build_fresh",
 "source": "saved_texts", "timestamp": "2026-07-20T21:06:13.347901+00:00", ...}
```

The trace for this run then shows the user immediately re-editing this exact
box by hand starting at 21:06:24 (a `delete`/`insert` sequence around
position `3.0`-`3.33`, re-adding a quote mark) - consistent with them
noticing the text was wrong and manually fixing it again, i.e. the "requiring
work to be redone" the user described.

## Why rapid Tab/Shift-Tab specifically

A box that's never torn down never goes through `replay_onto` at all - only
the initial "fresh build" branch, which just inserts text directly and is
not susceptible to this. Only a box that gets torn down (paged out of the
small materialized window) and later paged back in goes through the replay
path. Holding Shift-Tab (or Tab) causes many rows to be torn down and
rebuilt in a short burst as the focus/scroll window jumps backward or
forward repeatedly - so it's not that rapid input *causes* the divergence
mechanically, it's that rapid input is what makes a *rebuild* of a box that
happens to have `undo`/`redo` in its history likely to occur at all, and to
occur repeatedly across a session. A box that's edited but never has
Ctrl+Z/Ctrl+Shift+Z pressed on it, or that simply never gets paged out, is
not exposed to this specific defect (though see "Open questions" below on
whether the same class of divergence can occur without an explicit
undo/redo in the log).

## A red herring worth recording

Early in this investigation, `app.log` was grepped for `ERROR`/`WARNING`
entries and turned up five occurrences of `row_building.py`'s existing
`"box rebuilt back to its bare default despite a different saved edit on
record - possible silent data loss"` alarm, clustered at 18:01:46-18:01:54
in run `20260720T175505.003837`, alongside a burst of `"building a box for a
key that already has a live widget"` warnings. These looked, at first
glance, like they could be this same bug. **They are not** - cross-checking
against `archive/INVESTIGATION_shift_tab_reconcile_lockup.md` shows these
are the exact same timestamps that investigation already attributes to the
(now-fixed) `sel.first`/`sel.last` `TclError` crash: "each immediately
preceded in `app.log` by the pre-existing 'possible silent data loss' alarm
for key `[2, 'spacer_end']`". That whole incident predates the `text_undo.py`
record-time index-resolution fix (file timestamps put the fix's landing
somewhere between 18:01 and ~20:06-20:25 the same evening). Confirmed by
also checking: every `ERROR`/`WARNING` line anywhere in the current
`app.log` falls inside that 18:01 window - there is no logged error or
warning anywhere near the `21:03`-`21:06` sessions this document's actual
evidence comes from, which is itself notable (see "Root cause" above on why
this bug produces none).

**Lesson for next time:** when `app.log` shows an alarm that matches an
old investigation's known signature, check that investigation doc's exact
timestamps before assuming it's evidence of something new - two different
bugs in the same file, at different points in the fix history, can produce
identical-looking alarms.

## Recommended fix direction (not yet implemented)

The core problem is relying on Tk's own `edit_undo()`/`edit_redo()` to
"re-derive" grouping during replay, when that re-derivation isn't actually
guaranteed to match. Two directions, not mutually exclusive:

1. **Don't replay `undo`/`redo` as native Tk calls at all.** Instead of
   recording a bare `("undo", ())` marker and calling `edit_undo()` again
   during replay, resolve what the undo *actually did* at record time (the
   text state immediately before and after it) and either (a) skip
   recording the ops it reverted in the first place - collapsing them out of
   `log.ops` entirely rather than recording-then-undoing them - or (b)
   record the undo's effect as an ordinary `delete`+`insert` pair (absolute
   positions, same as every other recorded op), so replay never needs to
   call `edit_undo()`/`edit_redo()` on the fresh widget at all and isn't at
   the mercy of its from-scratch autoseparator grouping. Option (a) is
   probably cleaner - it keeps `log.ops` as a minimal, already-reconciled
   description of the box's *current* content, rather than a literal replay
   transcript of every keystroke including ones that got undone.
2. **Verify replay results, not just replay-time crashes.** The existing
   `_populate_text_box` alarm only catches a full revert to the item's bare
   default. Given `self._saved_texts[key]` already holds the box's
   last-known-good content (captured at every teardown), the replay's
   `result_text` could be compared against `self._saved_texts.get(key)`
   whenever that's available (not just against `initial_text`) - a mismatch
   there is unambiguous evidence of exactly this bug, for *any* box, not
   just ones that happen to land back on the bare default. This wouldn't
   fix the underlying divergence, but it would at least surface it loudly
   (matching this codebase's existing "loud, non-fatal degradation"
   precedent) instead of silently corrupting the box - and would let a
   recovery path (e.g. falling back to `self._saved_texts[key]`, the same
   way the existing `TclError` handler already does) actually run.
   **Done** - see "Logging/detection coverage added as a result of this
   investigation" below. Note this alarm still only *detects* the
   divergence after replay has already produced the wrong text; it doesn't
   yet fall back to `self._saved_texts[key]` the way the `TclError` handler
   does, so the box is still left showing the wrong content, just now
   loudly rather than silently. That recovery step is still open - see
   "What's still open".

Fixing only #2 without #1 would still silently *attempt* the wrong replay
first before catching it - probably fine given #2's recovery path, but #1 is
the real fix; #2 is the safety net, the same relationship the sel.first
investigation drew between its own two fix components. (Historical note:
at the time this was written, #1 was not yet implemented and only #2's
detection had shipped - see the "Update, later session" note at the top of
this document and "What's still open" at the bottom for current status;
both #1 and #2's recovery step have since shipped.)

## Logging/detection coverage added as a result of this investigation

Prompted by a review of how usable the logs actually were while tracing the
case above (several real friction points, not just hypothetical nice-to-
haves), the following shipped alongside this write-up - all logging-only,
no behavior change to the app itself:

- **`keyboard_nav.py`'s `_record_undo_marker`** now emits a `box_op_recorded`
  trace event (with a content fingerprint) at the moment an `"undo"`/`"redo"`
  marker is appended to `log.ops`, mirroring what `attach_undo_recording`'s
  proxy already did for ordinary insert/delete ops. Previously this was
  **the single biggest blind spot** in reconstructing this bug: a Ctrl+Z/
  Ctrl+Shift+Z press was invisible in `scroll_trace.log` until whatever
  *later* rebuild replayed it - this investigation had to infer "the user
  must have pressed Ctrl+Z here" from a `box_replay_op` entry showing up
  during a *subsequent* replay, rather than seeing it recorded live. Now it
  logs immediately, at the same point the marker is appended.
- **Both `box_op_recorded` (record-time) and `box_replay_op` (replay-time)**
  now carry a `logging_config.text_fingerprint` of the widget's content
  *immediately after* that specific op - previously only the final result
  was fingerprinted (`box_build_replay_done`), so a divergence between live
  and replayed execution could only be proven to exist, not localized to
  the specific op where it first appeared. The two event streams are now
  directly diffable op-by-op for the same `key`.
- Both of those events also now log `args_full_len` alongside the existing
  `args=repr(args)[:200]` - removes the ambiguity (previously had to be
  reasoned about, not read directly) over whether a long insert/delete
  payload (e.g. a pasted paragraph) was actually truncated in the log line
  versus genuinely that short.
- **`_populate_text_box`'s replay branch** now runs a second, broader
  regression alarm alongside the pre-existing "landed back on the item's
  bare default" one (left completely unchanged, including its exact message
  text, so `archive/INVESTIGATION_shift_tab_reconcile_lockup.md`'s existing
  grep instructions still work): `"replay result doesn't match this box's
  content as of its last teardown - possible silent replay divergence"`,
  logged at `ERROR` whenever `result_text != self._saved_texts.get(key)`
  (when a saved value exists), regardless of what the wrong result actually
  looks like. This is precisely direction #2 from "Recommended fix
  direction" above, and would have caught the reproduced case in this
  document immediately rather than requiring the manual cross-session log
  reconstruction that actually found it.
- **`scroll_trace.log`'s rotation budget** grew from 10MB x 3 backups to
  40MB x 6 (see the comment at its `RotatingFileHandler` call in
  `logging_config.py`) - the per-op fingerprinting above makes each op
  noisier still, and 10MB x 3 was already found to work out to only a
  session or two of headroom in practice, which matters specifically for a
  bug like this one where the relevant window needs to still be on disk by
  the time anyone thinks to go looking for it.

See `ARCHITECTURE.md`'s "Logging" section for where these fit into the
review screen's overall trace-logging story.

## How to find this again

```powershell
# scroll_trace.log: every replay whose result hash doesn't match what was
# captured at the most recent teardown for the same key - this is the
# general-purpose check this investigation used to find the case above,
# not specific to any one run or key.
```

As of this investigation, `_populate_text_box` now runs exactly this check
itself and logs an `ERROR` when it fails (see "Logging/detection coverage
added as a result of this investigation" above) - so the fastest way to find
a recurrence is now:

```powershell
Select-String -Path $env:USERPROFILE\.discord_transcription_gui\app.log,$env:USERPROFILE\.discord_transcription_gui\app.log.* `
  -Pattern "possible silent replay divergence"
```

Before that alarm existed, this required pairing each key's `box_teardown`
hash with the next `box_build_replay_done` hash for the same key and
comparing by hand, since (unlike the sel.first bug) there was no
distinctive log message or exception to search for directly - a short
Python pass over `scroll_trace.log` (track last `box_modified` hash per
key, record it at each `box_teardown`, compare against the next
`box_build_replay_start`/`_done` hash for that key) is still the way to
*reproduce this specific historical analysis* against an old capture from
before the alarm shipped, or to narrow down *which op* diverged (now
possible directly via the per-op fingerprints on `box_op_recorded`/
`box_replay_op`, diffed op-by-op, rather than needing to re-derive it).
`box_replay_op`/`box_op_recorded` entries with `"op": "undo"` or `"op":
"redo"` are worth specifically flagging in either kind of pass, since both
confirmed occurrences of this bug class (the reproduced one here, and -
per re-reading its own "op_count": 2 detail - likely also at least one of
the five 18:01 alarms, independent of the sel.first crash that also hit
that same run) involve one.

## Open questions

- Whether the divergence is specifically about `edit_undo()`/`edit_redo()`
  replay, or whether ordinary insert/delete sequences (no undo/redo
  involved at all) can also replay to a different result if Tk's
  autoseparator grouping depends on something replay doesn't reproduce
  (real-time gaps, focus events, etc.) - not observed in this investigation,
  but not ruled out either, since every confirmed case so far happens to
  involve an `undo` op. Worth specifically testing a long edit sequence
  with *no* undo/redo, torn down and rebuilt, diffed byte-for-byte against
  the pre-teardown text - not just checked for "close enough."
- Exactly what the live `edit_undo()` at 21:04:16.263 vs. the replayed
  `edit_undo()` at 21:05:45.589627 each actually reverted at the Tk level -
  this would require a live/instrumented repro (e.g. a standalone script
  reproducing the exact same op sequence against a real `tk.Text` widget,
  comparing a "live-paced" run against a "replay-paced" run) rather than
  log inference, the same way the sel.first bug ultimately needed a captured
  console traceback rather than log inference alone. Not yet attempted.
- Whether a regression test can be written without a live Tk display (per
  ARCHITECTURE.md's `gui`-marked test convention) - likely needs one, since
  the bug is specifically about real Tk `Text` widget/undo-stack behavior,
  not the pure-Python parts of this codebase.

## What's still open

- **Fixed.** Both directions from "Recommended fix direction" above have
  shipped:
  - Direction #1 (the actual fix): a successful Ctrl+Z/Ctrl+Shift+Z is now
    recorded as a `"replace"` op (`keyboard_nav.py`'s
    `_record_undo_replacement`) - the box's exact resulting text, captured
    live right after `edit_undo()`/`edit_redo()` ran - instead of a bare
    `("undo", ())`/`("redo", ())` marker. `replay_onto` (`text_undo.py`)
    replays a `"replace"` op as a plain, bracketed delete+insert and never
    calls `edit_undo()`/`edit_redo()` during replay at all, so this step's
    *content* is no longer at the mercy of whether replay's own native
    undo stack happens to group the same way the live one did. Residual,
    accepted tradeoff: the replayed step's own undo-*granularity* isn't
    guaranteed to match live exactly - a further Ctrl+Z pressed after such
    a rebuild can land on an intermediate state (e.g. empty, if the
    replace's delete and insert ever aren't bracketed as one step) rather
    than the same state one Ctrl+Z reached live - recoverable with a
    second Ctrl+Z or a Redo, and visibly wrong rather than silently wrong.
    See `ARCHITECTURE.md`'s "A recorded undo/redo must not be replayed by
    calling edit_undo()/edit_redo() again" for the full design.
  - Direction #2 (detection): as before, plus the self-heal recovery step
    that was previously called out as still open - a detected mismatch now
    overwrites the box onto `self._saved_texts[key]` and re-baselines its
    `UndoLog`, the same recovery `_populate_text_box`'s existing `TclError`
    guard already used, rather than only logging and leaving the wrong
    text in place.
- Two copies of `archive/INVESTIGATION_shift_tab_reconcile_lockup.md` exist
  in the repo: the correct, up-to-date "Status: fixed" version in
  `archive/`, and a stale, pre-fix "not yet fixed" duplicate still sitting
  at the top level (`gui_transcription/INVESTIGATION_shift_tab_reconcile_
  lockup.md`, file timestamp 20:25 vs. the archived copy's 20:06 - so the
  stale one is not simply an untouched leftover, something wrote to it
  *after* the archived copy was finalized). Noticed as a side effect of this
  investigation, not otherwise addressed here - worth cleaning up
  separately so a future reader doesn't land on the stale copy and think
  the sel.first bug is still open.
