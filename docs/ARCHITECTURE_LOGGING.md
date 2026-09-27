# Logging

Part of [ARCHITECTURE.md](ARCHITECTURE.md).

Every module logs through `discord_transcription/logging_config.py`, which
writes single-line JSON records to a rotating log file at
`~/.discord_transcription_gui/app.log` (2MB x 3 backups) - `~` is Python's
`Path.home()`, so this is `C:\Users\<you>\.discord_transcription_gui\app.log`
on Windows. The directory and filename are
`config.APP_DATA_DIR`/`config.LOG_FILE`. Its level is `config.LOG_LEVEL`
(INFO by default), overridable for one launch with the
`DISCORD_TRANSCRIPTION_LOG_LEVEL` environment variable
(`logging_config.resolve_log_level`) - DEBUG adds per-image OCR and
per-message parsing detail. Warnings and errors are also printed to the
console (`config.CONSOLE_LOG_LEVEL`). It covers run start/exit, OCR batch
progress, HTML parsing summaries (with skip-reason counts), review-screen
build/finalize events, session saves, and caught exceptions. Every line
also carries a `run_id` (generated once per process start), so one run's
lines can be picked out of either log file.

The review screen's much higher-frequency tracing (reconcile, debounce,
remeasure, scroll input, image load/unload and text-box events, all emitted
via `VirtualRows.log_event`) goes to a separate logger and file instead of
`app.log`: `~/.discord_transcription_gui/scroll_trace.log`
(`config.SCROLL_TRACE_LOG_FILE`, via `logging_config.get_trace_logger()`),
10MB x 3 backups by default (`config.SCROLL_TRACE_MAX_BYTES`/
`SCROLL_TRACE_BACKUP_COUNT`; `SCROLL_TRACE_ENABLED = False` turns it off).
That keeps it from crowding out or rotating away `app.log`'s lifecycle
events. Every trace event, including image loads and unloads from
`image_loading.py`, goes through `log_event`, so each carries the same
`seq` number and scroll-state fields and any two events can be ordered
exactly.

Each text box's lifecycle is traced to `scroll_trace.log` with the box's
content fingerprint (`logging_config.text_fingerprint` - length + short
hash): `box_build` when a widget is filled from its SlotState,
`box_teardown` when its row is destroyed, and `box_modified` for every
`<<Modified>>` event (with `changed` saying whether it was a user edit the
SlotState didn't have yet). Checkbox toggles are traced too
(`ocr_checkbox_toggled`), and undo/redo presses are logged to `app.log`
with the resulting fingerprint and the undo/redo stack depths.

Each `input_mousewheel` trace event records the raw Tk event's `num` and
`state` alongside the normalised `delta`, so a misread wheel event (such
as the X11 horizontal-scroll-as-Shift+Button-4 case in
[ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md)) shows up
directly in the trace. `scroll_box_into_view` events record
`model_real_discrepancy_px`, the difference between where the row-height
model and the real widgets put the focused box (see
[ARCHITECTURE_ROW_GEOMETRY.md](ARCHITECTURE_ROW_GEOMETRY.md)).
