# Logging

Part of [ARCHITECTURE.md](ARCHITECTURE.md).

Every module logs through `app/logging_config.py`, which writes single-line
JSON records to a rotating log file at
`~/.discord_transcription_gui/app.log` (2MB x 3 backups) - `~` is Python's
`Path.home()`, so this is `C:\Users\<you>\.discord_transcription_gui\app.log`
on Windows, not a literal path. The directory and filename are
`config.APP_DATA_DIR`/`config.LOG_FILE`; change them there, not in
`logging_config.py`, if this ever needs to move. Its level is
`config.LOG_LEVEL` (INFO by default), overridable for one launch with the
`DISCORD_TRANSCRIPTION_LOG_LEVEL` environment variable
(`logging_config.resolve_log_level`) - DEBUG adds per-image OCR and
per-message parsing detail. Warnings and errors are also printed to the
console (`config.CONSOLE_LOG_LEVEL`). Covers run start/exit, OCR
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
10MB x 3 backups by default (`config.SCROLL_TRACE_MAX_BYTES`/
`SCROLL_TRACE_BACKUP_COUNT`; `SCROLL_TRACE_ENABLED = False` turns it off) -
enough for several busy review sessions) - so it doesn't compete with, or evict, `app.log`'s much lower-volume lifecycle
events under rotation. Every event in that category, including ones
originating in `image_loading.py` (image load/unload) rather than
`review_view.py` itself, goes through the same `_log_event` call and so
carries the same `seq`/scroll-state fields, making any two events in the
trace directly correlatable without falling back to timestamp ordering.

Each text box's lifecycle is traced to `scroll_trace.log` with the box's
content fingerprint (`logging_config.text_fingerprint` - length + short
hash): `box_build` when a widget is filled from its SlotState,
`box_teardown` when its row is destroyed, and `box_modified` for every
`<<Modified>>` event (with `changed` saying whether it was a user edit the
SlotState didn't have yet). Undo/redo presses and checkbox toggles are
logged too, with the resulting fingerprint and the undo/redo stack depths.

The much heavier per-op tracing the old replay-based undo needed (a
`box_op_recorded` event per recorded insert/delete, a `box_replay_op` event
per replayed one, and the replay-divergence self-heal alarm) went away with
it: nothing is replayed any more, so there is nothing to diff.

Each `input_mousewheel` trace event records the raw Tk event's `num` and
`state` alongside the normalised `delta`, so a misread wheel event (such
as the X11 horizontal-scroll-as-Shift+Button-4 case in
[ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md)) shows up
directly in the trace instead of having to be inferred.
