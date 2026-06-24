# Architecture & internals

Deeper technical detail than the README needs for "how do I run this" -
review-screen internals (the most architecturally involved part of the app)
and logging conventions, for whoever's about to change either.

## Review screen internals

The review screen (`app/gui/review_view.py`) is the most architecturally
involved part of this app. Its module docstring is the canonical
explanation and worth reading in full before changing it; the summary here
is just enough to orient a new contributor.

Building a single row's widgets (`_build_row` and the label/image
placeholder/editable-text-box helpers it calls) lives in
`app/gui/row_building.py`'s `RowBuildingMixin`, mixed into `ReviewFrame`
the same way `keyboard_nav.py`'s `KeyboardNavMixin` already is - it
doesn't carry the same "disagreed with itself across files" risk the
windowing core below does, since each row's widgets are self-contained
once built. `review_view.py` itself keeps only that windowing core
(`_reconcile`/`_sync_materialized_rows`/`_remeasure_built_rows`/
`_offset_of`/`_ensure_materialized`/`_destroy_row`) plus the scroll/
debounce/Finalize-button machinery:

- **Row virtualization.** Only a small window of rows (around the visible
  viewport) is ever built as real Tk widgets - `ReviewFrame._reconcile`
  recomputes that window from scratch on every scroll tick as a pure
  function of scroll position and each row's recorded height
  (`self._row_heights`), and reconciling is idempotent (calling it twice
  with no scroll movement is a no-op). That idempotency is deliberate - an
  earlier, stateful "step the window forward/backward" design could fall
  into a self-sustaining oscillation loop; see the module docstring for the
  full story. Pure layout math lives in `app/gui/virtualization.py` so it's
  testable without a display.
- **The pre-build height estimate isn't just a cosmetic detail.**
  `virtualization.estimate_row_height`'s guess for an image row used to
  assume the image filled the full `THUMBNAIL_SIZE` bounding box - but most
  images here are landscape (width-, not height-, constrained), so the real
  fitted height is usually far less, and the guess was overestimating most
  rows by 500+px. That overestimate got corrected once a row was actually
  built (`ReviewFrame._remeasure_built_rows` shifts the scroll offset to
  keep on-screen content stable when a row's real height turns out
  different from its estimate) - but the correction itself was what showed
  up as a scroll jump disconnected from the user's actual scroll input, the
  bigger the estimate error, the bigger the jump. `estimate_row_height` now
  calls the same cheap, header-only `fitted_image_size` read already used to
  size the real placeholder, instead of a flat constant, so there's far
  less left for `_remeasure_built_rows` to ever need to correct.

  The fixed-height text box policy (see "Per-box text box sizing" below)
  removed most of what was left to estimate, but also moved the goalposts:
  once a box's height stopped being a content-driven floor and became the
  dominant term in an image row's total height, `estimate_row_height`'s old
  flat per-row overhead constant (sized for the previous auto-growing box's
  typical chrome) became a large, *systematic* overestimate instead of a
  cosmetic one - every image row over by the same ~190px, visible as
  occasional counterintuitive scroll jumps in `scroll_trace.log`'s
  `remeasure_mismatch` events even after the text-box-height change.
  `estimate_row_height` now computes each column's height the same way
  `_build_row` actually lays it out (left: label and/or image, stacked;
  right: message and/or ocr box, mirroring `_fixed_text_box_height`'s
  rules; both plus `GAP_BELOW_MESSAGE_PX` when a caption sits above an
  image) and takes the taller of the two, rather than a flat constant.
  The margin/gap/row-overhead constants both sides need (`TEXT_BOX_MARGIN_
  PX`, `GAP_BELOW_MESSAGE_PX`, `ROW_FRAME_OVERHEAD_PX`) live in their own
  Tk-free `app/gui/layout_constants.py` module that both `row_building.py`
  (the real layout) and `virtualization.py` (the estimate) import - this
  replaced an earlier design where each side hardcoded its own copy of the
  same numbers "kept in sync by hand," which is exactly the kind of drift
  that caused this estimate/reality mismatch in the first place.
- **`<<Modified>>` fires on a box's initial population, not just real user
  edits.** `_build_editable_text_box` inserts a box's initial text and
  immediately calls `edit_modified(False)`, intending to stop that insert
  from being treated as a user edit by `_on_text_modified` - but Tk queues
  `<<Modified>>` for the next idle tick rather than firing it synchronously,
  so the binding (attached a few lines later, before control returns to the
  event loop) still catches it. That meant every newly-built row - including
  ones built only because they entered the virtualization buffer
  (`SCROLL_BUFFER_VIEWPORTS`), not because the user actually scrolled them
  into view - triggered `_on_text_modified`'s `_scroll_into_view`, yanking
  the canvas to reveal a row the user hadn't scrolled to. `_on_text_modified`
  now only calls `_scroll_into_view` when the edited box actually has focus,
  which a phantom build-time event never does.
- **Slot-addressed boxes.** Since a row can now have a "message" box (a
  copy of the message's own text), an "ocr" box (an image's OCR text), or
  both, a single item index is no longer enough to identify one box.
  Every per-box dict in `ReviewFrame` (`_text_widgets`, `_text_containers`,
  `_box_floor_px`, `_saved_texts`) is keyed by `(item_index, role)` instead,
  and `self._slots` is the flat, transcript-ordered list of every
  `(item_index, role)` pair that exists across all items - built once in
  `__init__` from each item's `initial_message_text`/`image_path` (a
  "message" slot whenever the former isn't None, an "ocr" slot whenever
  the latter isn't), message before ocr. This is what Tab/Shift-Tab
  navigate (`keyboard_nav.py`'s `_move_focus`, stepping through
  `self._slots` by `self._slot_positions[slot]`) and what session
  resume's saved focus position addresses a box by (see the README's
  "Setup screen" description) - a plain item index couldn't disambiguate
  which of a row's two boxes to refocus.
- **Per-row left-column sizing.** (`row_building.RowBuildingMixin`.) Every row's left column is the same fixed
  width (`THUMBNAIL_SIZE[0]` in `app/gui/image_loading.py`), whether it
  holds an image, the immutable original-text label, or both stacked
  text-above-image - so every row's column pairs line up neatly across the
  whole transcript. An image's height is its own aspect-preserving fit
  within `THUMBNAIL_SIZE` (`fitted_image_size`), not the full bounding box
  - otherwise a landscape image (the common case) gets letterboxed inside
  a box-shaped slot; this only reads the image file's header (cheap),
  separately from the actual lazy pixel decode in `ImageLoader._load_image`
  once a row scrolls near the viewport. The immutable label's height isn't
  known until the label exists, so `_build_immutable_message_label`
  measures it with the container's `pack_propagate` left on before pinning
  both dimensions, rather than computing it upfront the way
  `fitted_image_size` does for images.
- **Per-box text box sizing.** (`row_building.RowBuildingMixin`, mixed into
  `ReviewFrame` - see the note at the top of this section.) Each editable
  text box lives in its own fixed-height container (`pack_propagate(False)`,
  same trick as the left column's placeholders) so it doesn't stretch to
  fill whatever space is left via Tk's `fill="both"`. `_fixed_text_box_height` decides
  that height once, at build time, from a fixed rule rather than measuring
  the text's actual wrapped line count: its paired immutable element's own
  on-screen height (the label's, for a "message" box; the image's, for an
  "ocr" box) plus `TEXT_BOX_MARGIN_PX`, either way capped at
  `TEXT_BOX_MAX_HEIGHT_FRACTION` of the screen. An earlier version gave a
  "message" box a flat 3-line minimum instead, assuming most messages here
  are short text - in practice many ran to several lines, so that
  assumption is gone and both roles now use the same rule. A box gets an internal
  scrollbar that shows/hides itself automatically (`_set_text_scrollbar`,
  driven by the box's own `yscrollcommand`) whenever its content overflows
  that fixed height, whether from a long original message or from typing
  past it - the box itself never grows. This replaced an earlier design
  (`_size_text_container`, removed) that measured the text's current
  wrapped line count and resized the box to fit, re-running on every
  keystroke (`_on_text_modified`) - that made a row's true height
  unknowable until it was built and typed in, which is exactly the gap
  `_remeasure_built_rows` existed to correct, and a repeated source of this
  screen's scroll-position bugs. Fixing height to something knowable
  upfront - the same way an image's height already was, via
  `fitted_image_size`'s cheap header read - removes that correction's
  reason to exist instead of just estimating it more carefully.
- **Per-row scroll redirection.** The mouse wheel is bound globally
  (`canvas.bind_all("<MouseWheel>", ...)`), but the bound callback still
  receives the specific widget under the cursor as `event.widget` - so
  hovering a text box that has its own scrollbar scrolls that box first
  (`_scroll_text_widget`), only falling through to scrolling the whole
  review window once the box is scrolled as far as it can go in that
  direction (or has nothing to scroll at all).
- **Floating Finalize button.** Unlike every other widget here, the
  Finalize button's container is never packed/gridded into the frame's own
  layout - it's positioned with `place(relx=0.5, rely=1.0, anchor="s")`
  relative to `self` (the static window), not the canvas's scrolling
  document, so it floats pinned to the bottom of the viewport without ever
  claiming a permanent slice of vertical space the way a packed row would.
  `_update_finalize_button_visibility` calls `place()`/`place_forget()` to
  show it only once `canvas.yview()`'s bottom fraction reaches `1.0` (the
  true end of the scrollregion, or trivially true for a transcript that
  fits on screen with nothing to scroll past) - called from `_reconcile`
  on every scroll-driven update and from `_scroll_into_view` so Tab'ing to
  the last box reveals it immediately rather than waiting on the next
  scroll event.

## Logging

Every module logs through `app/logging_config.py`, which writes single-line
JSON records to both the console and a rotating log file at
`~/.discord_transcription_gui/app.log` (2MB x 3 backups). Covers run
start/exit, OCR batch progress, HTML parsing summaries (with skip-reason
counts), review-screen build/finalize events, and caught exceptions. Every
line also carries a `run_id` (generated once per process start), so one
run's lines can be isolated without re-deriving line offsets from an
`application starting` marker by hand.

The review screen's much higher-frequency per-scroll-tick tracing
(reconcile/debounce/remeasure/image-load/box-resize events, all emitted via
`ReviewFrame._log_event`) is routed to a separate logger/file instead of
`app.log` - `~/.discord_transcription_gui/scroll_trace.log`
(`logging_config.get_trace_logger()`, 10MB x 3 backups) - so it doesn't
compete with, or evict, `app.log`'s much lower-volume lifecycle events
under rotation. Every event in that category, including ones originating in
`image_loading.py` (image load/unload) rather than `review_view.py` itself,
goes through the same `_log_event` call and so carries the same `seq`/
scroll-state fields, making any two events in the trace directly
correlatable without falling back to timestamp ordering.
