# Logging

Part of [ARCHITECTURE.md](ARCHITECTURE.md).

Every module logs through `app/logging_config.py`, which writes single-line
JSON records to both the console and a rotating log file at
`~/.discord_transcription_gui/app.log` (2MB x 3 backups) - `~` is Python's
`Path.home()`, so this is `C:\Users\<you>\.discord_transcription_gui\app.log`
on Windows, not a literal path. The directory and filename are
`config.APP_DATA_DIR`/`config.LOG_FILE`; change them there, not in
`logging_config.py`, if this ever needs to move. Covers run start/exit, OCR
batch progress, HTML parsing summaries (with skip-reason counts),
review-screen build/finalize events, and caught exceptions. Every line also
carries a `run_id` (generated once per process start), so one run's lines
can be isolated without re-deriving line offsets from an `application
starting` marker by hand.

The review screen's much higher-frequency per-scroll-tick tracing
(reconcile/debounce/remeasure/image-load/box-resize events, all emitted via
`ReviewFrame._log_event`) is routed to a separate logger/file instead of
`app.log` - `~/.discord_transcription_gui/scroll_trace.log`
(`config.SCROLL_TRACE_LOG_FILE`, read via `logging_config.get_trace_logger()`,
40MB x 6 backups - sized generously,
see the comment at its `RotatingFileHandler` call, so debugging a rare bug
isn't also a race against this file rotating the relevant session away) - so
it doesn't compete with, or evict, `app.log`'s much lower-volume lifecycle
events under rotation. Every event in that category, including ones
originating in `image_loading.py` (image load/unload) rather than
`review_view.py` itself, goes through the same `_log_event` call and so
carries the same `seq`/scroll-state fields, making any two events in the
trace directly correlatable without falling back to timestamp ordering.

Every undo/redo-capable text box's edit history (`app/gui/text_undo.py`'s
`UndoLog`) is fully traced on both the recording and replay side - every op
appended to `log.ops`, whether via `attach_undo_recording`'s
Tcl-command-interception proxy (ordinary insert/delete) or
`keyboard_nav.py`'s `_record_undo_replacement` (the `"replace"` op a
successful Ctrl+Z/Ctrl+Shift+Z appends directly to `log.ops`, bypassing that
proxy entirely - see
[ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md)'s "A recorded
undo/redo must not be replayed by calling edit_undo()/edit_redo() again"),
logs a `box_op_recorded` event to `scroll_trace.log` carrying the widget's
content fingerprint (`logging_config.text_fingerprint` - length + short
hash) *immediately after* that op took effect. `_populate_text_box`'s replay
branch (`row_building.py`) mirrors this with a `box_replay_op` event per
replayed op, fingerprinted the same way. The two traces are directly
diffable op-by-op for the same key - `key`, `op`, and `total_ops`/op-index
line up - which is what lets a *replay* divergence from the original *live*
edit sequence be pinned to the exact op where they first disagree, rather
than only being provable from the two sequences' final results differing
(see `INVESTIGATION_undo_redo_replay_divergence.md`, which had to infer an
undo/redo's occurrence from a later `box_replay_op` entry and could only
show that a replay's *end* result was wrong, not identify which op caused
it - both gaps this closes). Both event kinds also log `args_full_len`
alongside the `args=repr(args)[:200]` truncation already used for a long
insert/delete payload (e.g. a pasted paragraph), so a log reader can tell
from the line itself whether `args` was actually truncated rather than
guessing from its length whether the real op was short.

`_populate_text_box`'s replay branch also runs a second, broader regression
check beyond the pre-existing "landed back on the item's bare default" alarm
(`row_building.py`, still present unchanged, together with its associated
grep pattern in
`archive/INVESTIGATION_shift_tab_reconcile_lockup.md`): it compares the
replay's `result_text` directly against `self._saved_texts[key]` - the
box's own content as of its last teardown (`review_view.py`'s
`_destroy_row`) - and, on any mismatch, logs an `ERROR`-level "replay result
doesn't match this box's content as of its last teardown - possible silent
replay divergence" and self-heals onto `self._saved_texts[key]` (same
recovery as the `TclError` guard just above it), regardless of whether the
wrong result happens to look like the bare default or like some other,
still-edited-looking text. The bare-default check only ever covered the
former; this covers both, and is exactly the check
`INVESTIGATION_undo_redo_replay_divergence.md`'s reproduced case would have
tripped and corrected immediately instead of requiring a manual
cross-session log reconstruction to notice at all.
