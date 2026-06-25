# Architecture & internals

Deeper technical detail than the README needs for "how do I run this" -
review-screen internals (the most architecturally involved part of the app)
and logging conventions, for whoever's about to change either.

## General heuristic: test where features compose, not just each feature alone

A real data-loss bug (see "A box's `UndoLog` must record what it actually
started from" below) shipped, with passing tests, because two features -
session resume and the review screen's virtualized row rebuild - were each
tested thoroughly in isolation, but never *together*. Each test's author
reasonably treated their own feature as the unit under test and used the
simplest setup that exercised it; neither setup happened to also exercise
the other feature, so the one combination where they interacted badly
(a resumed box, rebuilt with no further edits) went unexercised by either.

The general failure mode: a new feature gets layered on top of existing
code that already has its own internal state machine (here, `_populate_
text_box`'s build-fresh/rebuild-replay branching). It's not enough to ask
"does my new feature work" - the question that actually would have caught
this is "does my new feature still hold every invariant the *existing*
state machine depends on." That second question only has a useful answer
once you've identified what those invariants actually are (here: a
rebuild may only assume what the box's own history recorded, never the
item's static default) and written them down somewhere other than in the
original author's head.

Two concrete habits this argues for in this codebase specifically:

- When adding a feature that touches state another feature already
  manages (session resume touching the same per-box dicts virtualization
  owns), write at least one test that exercises *both* in the same test,
  in the order a real user would actually hit them - not just one test
  per feature with the other feature absent. If two such tests already
  exist separately (as they did here), that's a sign the combined test is
  still missing, not that coverage is already adequate.
- When a piece of code's correctness depends on an assumption about how
  it got into its current state (e.g. "this log's ops are deltas from
  `initial_text`"), encode that assumption as actual stored data (`UndoLog.
  baseline`) rather than leaving it implicit in which branch happened to
  run. An assumption that only lives in a comment or a docstring can drift
  silently out of sync with the code the moment a new caller is added that
  the original author didn't have in mind; an assumption recorded as data
  the code itself reads back can't.

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
- **The pre-build estimate also has to mirror the real layout's cap, not
  just its content-driven size.** A content box's real height
  (`RowBuildingMixin._fixed_text_box_height`) is capped at
  `TEXT_BOX_MAX_HEIGHT_FRACTION` of the canvas - a long message/OCR text
  gets an internal scrollbar past that point rather than growing the row
  further - but `estimate_row_height` had no matching cap, so a long
  row's *estimated* height could run far past what its real, capped box
  would ever be. That overestimate is harmless for a row that gets built
  and remeasured soon after (`_remeasure_built_rows` corrects it away like
  any other estimate/reality mismatch) - the actual symptom was a focused
  box landing partially or fully off the real canvas viewport after a
  discontinuous jump straight to a deep slot (a resumed session's saved
  focus, or a scrollbar drag far down the document), while continuously
  walking there via Tab/Page Down from the top always looked fine. The
  difference: walking through remeasures every row along the way,
  including any long ones, before `_offset_of` ever needs their height
  again - a jump skips that remeasurement entirely for whatever it jumps
  over, so any long row in that skipped range keeps contributing its
  uncapped overestimate to every later row's document-space offset
  indefinitely (until something eventually builds it). `estimate_row_height`
  now takes an optional `max_text_box_height_px` and caps each content
  role's right-column contribution the same way `_fixed_text_box_height`
  does; `ReviewFrame.__init__` passes `self._max_text_box_height_px()` -
  which is also why the row-heights list is now built *after*
  `self._canvas` exists, not before, so that call's own
  not-yet-laid-out/`winfo_screenheight()` fallback applies the same way it
  would for any other premature call to it.
  `test_jumping_focus_past_a_capped_long_message_row_lands_target_fully_in_view`
  (`app_tests/test_review_view.py`) is the regression test - it puts one
  long, capped-height row outside the window built at startup and jumps
  straight past it to a target further down, the same way a resumed
  session's saved focus slot would.
- **A row's first-ever build can measure as winfo_height()==1 even right
  after `canvas.update_idletasks()`.** This was the actual cause behind a
  real, reproduced bug: resuming a session whose saved focus slot is deep
  in the transcript jumps straight there (`_ensure_materialized`), which
  materializes that whole window of rows in the session's very *first*
  `_reconcile` call - a deeply nested `ttk.Frame` tree, several levels
  deep, none of which have ever been mapped to the screen before.
  `update_idletasks()` only drains Tcl's idle queue (which is what pack's
  own size negotiation runs on), not the window-system `Map` event a
  widget needs before `winfo_height()` reports anything real - and that
  event apparently doesn't always arrive within a single idle-queue pass
  for that much brand-new tree at once. `_remeasure_built_rows` used to
  take `winfo_height()`'s bogus `1` at face value, permanently writing
  `2*ROW_PACK_PADY_PX` (9px) into `self._row_heights` for every row in that
  first window - and since a row already in `self._row_frames` is never
  rebuilt (so never remeasured) just because a later `_reconcile` runs,
  that 9px-per-row corruption then threw `self._offset_of` off by hundreds
  of px for every row after it, for the rest of the session, with no
  further chance to self-correct - the user-visible symptom was Tab/
  Shift-Tab's scroll-into-view looking completely broken from the moment a
  deep resume opened, confirmed against real `scroll_trace.log` output
  (every row in that first reconcile's `remeasure_mismatch` events reading
  `real_height: 9`, every later reconcile reading correctly). Every
  *later* reconcile measures correctly on the first try, since by then the
  canvas has already been mapped at least once - this is specifically a
  first-reconcile problem.

  `_reconcile` now calls `_settle_pending_geometry` (`review_view.py`)
  right after `update_idletasks()` and before trusting any measurement:
  it retries `update_idletasks()` a bounded number of times first (cheap,
  no event-processing side effects, covers the ordinary "pack is still
  settling" case), then falls back to a bounded number of full `update()`
  calls if that wasn't enough. `update()` - unlike `update_idletasks()` -
  drains *all* pending events, not just idle callbacks, which is what
  actually unblocks the stuck `Map`; confirmed by a standalone repro that
  `update_idletasks()` alone never resolves it, no matter how many times
  it's retried, while a single `update()` does. `update()` can call back
  into `_reconcile` itself before returning (e.g. an already-scheduled
  debounced reconcile from the canvas's first `<Configure>` event) - this
  is expected and *not* guarded against: an earlier version of this fix
  added a reentrancy flag that made such a nested call a no-op, on the
  theory that it could rebuild/tear down rows out from under the in-
  progress outer call. That theory didn't survive contact with the actual
  repro - guarding the reentrancy back out reproduced the exact bug this
  fix exists to remove, because the nested call's own geometry-touching
  work (re-issuing `canvas.configure(scrollregion=...)`/`canvas.coords`)
  turned out to be what actually finishes flushing the stuck `Map`, not
  incidental to it. `_reconcile`/`_sync_materialized_rows` are already
  written to be idempotent and safe to re-enter (see the module
  docstring), so trusting that existing guarantee - rather than adding a
  new one - is what makes this safe. `_remeasure_built_rows` itself also
  gained a direct `winfo_height()<=1` guard as a last-resort fallback
  (skip recording that row's height at all, leaving its previous estimate
  in place, rather than recording the bogus near-zero value) for if
  `_settle_pending_geometry`'s bound is ever actually hit.
  `test_resuming_deep_in_a_long_transcript_remeasures_rows_correctly_on_first_build`
  (`app_tests/test_review_view.py`) is the regression test - it resumes
  with a saved focus slot deep enough into a long transcript that jumping
  there is that session's very first reconcile, then checks every
  materialized row's recorded height against its real, current
  `winfo_height()`.

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
  `_build_row` actually lays it out (left: label and/or images, stacked;
  right: message box and/or one OCR box per image, mirroring
  `_fixed_text_box_height`'s rules; every stacked element but the last -
  caption, then each image - plus `GAP_BETWEEN_STACKED_PX`) and takes the
  taller of the two, rather than a flat constant. The margin/gap/row-
  overhead constants both sides need (`TEXT_BOX_MARGIN_PX`,
  `GAP_BETWEEN_STACKED_PX`, `ROW_FRAME_OVERHEAD_PX`) live in their own
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
  into view - triggered `_on_text_modified`'s `_scroll_box_into_view`,
  yanking the canvas to reveal a row the user hadn't scrolled to.
  `_on_text_modified` now only calls `_scroll_box_into_view` when the
  edited box actually has focus, which a phantom build-time event never
  does.
- **Focus/cursor survive a row being torn down, not just edits.** A fast
  Page Up/Page Down burst can move the canvas several viewports between
  `_reconcile` passes (each one debounced - see `DEBOUNCE_MS` - so a held
  key doesn't reconcile on every event), easily skipping past
  `SCROLL_BUFFER_VIEWPORTS`'s buffer and tearing down a row whose box
  currently has the focus. `_destroy_row` now records that box's slot
  (`self._refocus_slot`) and exact cursor index (`self._saved_cursor`,
  alongside the existing `self._saved_texts`) before tearing it down;
  `_build_row` restores both, via `after_idle` rather than inline, if/when
  that same index is rebuilt later - deferred so the restore's own
  scroll-into-view isn't immediately clobbered by `_reconcile`'s still-
  pending `_remeasure_built_rows` correction (which runs after
  `_sync_materialized_rows`/`_build_row` return, in the same `_reconcile`
  call, if this fired from inside it). Guarded on nothing else having
  since taken focus (`self._focused_slot() is None and self.focus_get() is
  not self._finalize_button`) - deliberately not a `self.focus_get() is
  None` check, since destroying a focused widget hands Tk's focus to an
  ancestor frame rather than clearing it, so it's never actually `None` by
  the time the guard runs. `self._saved_cursor` isn't part of the
  autosaved session format - only `self._saved_texts` is - so a resumed
  box's cursor still starts at `"1.0"`, same as before this.
- **A box's `UndoLog` must record what it actually started from, not
  assume it was `initial_text`.** (`text_undo.py`'s `UndoLog.baseline`,
  set in `row_building.py`'s `_populate_text_box`.) `_populate_text_box`
  has always had two branches: a box's *first* build this session (no
  `UndoLog` for its key yet) inserts `self._saved_texts.get(key)` if a
  resumed/in-session edit exists, else the item's plain `initial_text`;
  any *later* rebuild (the row was torn down and is being paged back in)
  replays the recorded ops onto a fresh widget instead. For a long time
  the rebuild branch re-based that replay on `initial_text` directly,
  silently assuming a fresh `UndoLog`'s ops were always deltas from
  `initial_text` - true for a box that started untouched, **false** for
  one that started from a resumed edit. That box's *very next* rebuild
  (an ordinary scroll-away-and-back, no further typing needed) discarded
  the resumed edit and replayed onto the bare default instead - with zero
  ops to replay in the common case, this reverted the box to its
  unedited OCR text with no trace, and the next autosave tick persisted
  that loss to disk. Real data loss, not theoretical: see the project
  owner's transcription work for a confirmed instance. Fixed by having
  `UndoLog` itself record `baseline` - whichever text the box's first
  build this session actually used - and having the rebuild branch
  replay onto `log.baseline`, never onto `initial_text` directly. Any
  future change to this method must preserve that: the only thing a
  rebuild may assume about a box's prior state is whatever the box's own
  `UndoLog` recorded, never the item's static default.
  `test_resumed_edit_survives_being_paged_out_and_back_in_with_no_further_
  edits` (`app_tests/test_review_view.py`) is the regression test -
  notably, the two narrower scenarios it combines (resume-then-use,
  edit-then-page-away-and-back) each already had their own passing test
  beforehand, and neither caught this: the bug only exists where both are
  true at once, which is exactly the gap a single new test combining them
  had to be added to close, rather than expecting either existing test to
  generalize to it on its own.
- **Slot-addressed boxes.** Since a row can now have a "message" box (a
  copy of the message's own text) and any number of OCR boxes - one per
  attached image, since a single message can have more than one - a
  plain item index is no longer enough to identify one box. Every per-box
  dict in `ReviewFrame` (`_text_widgets`, `_text_containers`,
  `_box_floor_px`, `_saved_texts`) is keyed by `(item_index, role)`
  instead, where `role` is `"message"` or `"ocr{N}"` (the Nth attached
  image's OCR box, 0-indexed in attachment order) - encoding the image
  index into the role string this way, rather than widening every key to
  a 3-tuple, kept the change confined to how `role` strings are
  generated/parsed rather than touching every dict's key shape.
  `self._slots` is the flat, transcript-ordered list of every
  `(item_index, role)` pair that exists across all items - built once in
  `__init__` from each item's `initial_message_text`/`image_paths` (a
  "message" slot whenever the former isn't None, then one `"ocr{i}"` slot
  per entry in the latter), message before every image's OCR slot. This
  is what Tab/Shift-Tab navigate (`keyboard_nav.py`'s `_move_focus`,
  stepping through `self._slots` by `self._slot_positions[slot]`) and what
  session resume's saved focus position addresses a box by (see the
  README's "Setup screen" description) - a plain item index couldn't
  disambiguate which of a row's boxes to refocus. `ImageLoader` mirrors
  this with its own `(item_index, image_index)`-keyed slots (see
  `app/gui/image_loading.py`), since a row can likewise now load/unload
  more than one image.
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
  on every scroll-driven update and from `_scroll_box_into_view` so
  Tab'ing to the last box reveals it immediately rather than waiting on
  the next scroll event.
- **Per-box, not per-row, scroll-into-view.** `_scroll_box_into_view`
  (`keyboard_nav.py`) replaced an earlier `_scroll_into_view` that checked
  only a row's outer bounds against the viewport. A row can stack more
  than one box - a message's text box, one OCR box per attached image, and
  a spacer box between/after each (`_build_row`) - and can end up taller
  than the viewport itself, so the row-level check could find the row
  "already fully visible" (because some box within it was) while the
  specific box Tab/Shift-Tab had just focused, or the one the user was
  typing into, was still only partially onscreen - in the worst case
  almost entirely covered, with just a sliver poking into view, which the
  old check's row-level bounds didn't catch as a reason to scroll at all.
  Computed the same way `_keep_cursor_in_viewport`'s box bounds already
  were: `self._offset_of(index)` (the row's document-space offset) plus a
  `winfo_rooty()` delta for the box's offset *within* that row.
- **Per-OCR-box edited/checkbox state.** Every "ocr" box (one per attached
  image - never a "message" box, which was never OCR'd and so has no
  "original" to revert to) has a checkbox tracking "edited vs. not" and
  letting the user toggle between the original (regex-corrected) OCR
  transcription and their own edit without losing either. Two
  `ReviewFrame`-level dicts hold this, keyed by `(item_index, role)` like
  every other per-box dict here (`_saved_texts`, `_saved_cursor`, ...):
  `self._checkbox_checked` (current checked state) and
  `self._user_edited_texts` (the last user-edited version, kept distinct
  from whatever the box currently *displays* - the OCR default, while
  unchecked). Neither is ever cleared by `_destroy_row`, so both survive a
  row being torn down and rebuilt with no teardown/rebuild-specific
  plumbing of their own - they're written to only from live editing code
  (typing, the checkbox's own toggle, undo/redo), never read back from a
  about-to-be-destroyed widget the way `_saved_texts` is.

  Both dicts are seeded **eagerly in `ReviewFrame.__init__`**, for every
  `(idx, "ocr{i}")` slot across all items, not lazily the first time a row
  is built - `collect_edited_texts`/autosave loop over every item
  regardless of whether its row has ever been materialized this session
  (e.g. right after a resume far from that row), so the checked-state has
  to be knowable without requiring a build. The seeding rule - `checked =
  saved is not None and saved != default`, where `saved` comes from
  `self._saved_texts` (itself already seeded from a resumed session's
  `edited_texts` field) - also implements the project owner's chosen resume
  behavior for free: a resumed box with an edited version differing from
  its default is always shown checked, regardless of whether it happened to
  be checked or unchecked at the moment the session was last saved. This
  works because `ReviewFrame._get_box_text` (what `collect_edited_texts`/
  autosave/the session file actually read) reports `None` - not the box's
  live, OCR-default-matching content - for an `ocr*` role while unchecked,
  so a saved, non-default value can only ever mean "there's a real edit to
  surface." This doesn't change what Finalize ever writes (`None` already
  falls back to the same default text in `review_item.lines_for_item`),
  only what gets *persisted/reported* for an unchecked box.

  `_populate_text_box`'s existing baseline+replay logic
  (`row_building.py`) needed no changes to support any of this: a checkbox
  toggle's content swap (`RowBuildingMixin._on_ocr_checkbox_toggle`) is
  just another recorded delete/insert through the same undo-recording proxy
  every other edit goes through, so a row torn down mid-toggle and rebuilt
  later replays back to the right content automatically, the same way a
  resumed edit already did (see "A box's `UndoLog` must record what it
  actually started from" above).

  Two things needed new code, both reusing an existing mechanism rather
  than inventing a new one:
  - **Telling a real edit apart from a programmatic one.** `<<Modified>>`
    fires for the checkbox's own swap exactly like it does for typing (see
    "`<<Modified>>` fires on a box's initial population" above) - left
    unguarded, unchecking a box would immediately re-check itself via the
    same "any change checks the box" reaction real typing needs.
    `_on_text_modified`'s existing `had_focus` gate (already there to skip
    a build-time insert, which also fires this event) turns out to cover
    this case too for free: clicking the checkbox normally moves focus to
    it, not the text widget, so the swap's deferred `<<Modified>>` arrives
    with `had_focus` false. `_on_ocr_checkbox_toggle` additionally adds its
    key to `self._suppress_ocr_auto_check` around the swap as defense in
    depth, in case focus ever doesn't move the way expected - and
    `keyboard_nav.py`'s `_undo_text`/`_redo_text` rely on that same set for
    real, since Ctrl+Z *does* run with the box focused: undo/redo always
    re-derives checked/unchecked by comparing the resulting text to the OCR
    default (`_resync_ocr_checkbox_after_undo`) rather than the
    unconditional "any change checks the box" rule ordinary typing uses, so
    undoing a toggle that lands exactly back on the OCR default correctly
    un-checks the box again instead of leaving it stuck checked.
  - **Making a toggle's delete+insert undo as one step.** A `tk.Text`
    widget's default `autoseparators` behavior inserts a separator on every
    insert↔delete type transition - left alone, a toggle's `delete("1.0",
    "end")` followed by `insert("1.0", ...)` becomes *two* undo groups
    instead of one, so a single Ctrl+Z only reversed the insert half,
    landing on the empty post-delete/pre-reinsert text rather than back on
    whatever the toggle swapped away from. Worse, naively disabling
    `autoseparators` only around the delete+insert pair (with no boundary
    *before* it either) merged the toggle into whatever undo group preceded
    it, so a single Ctrl+Z undid the toggle *and* the user's last real edit
    together. `_on_ocr_checkbox_toggle` calls `edit_separator()` once
    *before* turning `autoseparators` off (sealing off whatever came
    before), then again right after re-inserting (sealing off whatever
    comes after) before turning `autoseparators` back on - bounding the
    delete+insert pair as exactly one atomic undo/redo step.

  Checkbox widgets themselves are plain `tk.Checkbutton`/`tk.BooleanVar`
  (not `ttk`, so each can be colored to blend into its own text box's
  background rather than sharing one global `ttk.Style`), `takefocus=0` so
  Tab/Shift-Tab - which already only navigate `self._slots`, never anything
  Tk's own default focus traversal would otherwise reach - skip over them
  with no further change needed. The checkbox sits inside an otherwise-
  invisible `tk.Frame` column packed `side="right"` into the box's
  `text_container`, built (and packed) before `text_widget` so it's earlier
  in the container's pack order and claims a slice off the right edge
  before `text_widget`'s `expand=True` claims everything still left - this
  is the same pack-order trick `_set_text_scrollbar`'s own `before=`
  argument already relied on for the scrollbar (see that method's
  docstring), just with one more widget in the chain: showing the
  scrollbar now has to insert it before the checkbox column, not just
  before `text_widget`, to land at the true right edge with the checkbox
  column directly to its left.

## Row geometry: the document-space spacing model

`self._row_heights` (estimated pre-build, real post-build) is the single
source of truth `_offset_of`, the canvas `scrollregion`, and every
scroll-into-view calculation in `keyboard_nav.py` derive their numbers
from. All of that math implicitly assumes **`self._row_heights[idx]` is
the full vertical screen space row `idx` consumes, including everything
`_build_row` puts around it that the row's own `winfo_height()` can't
see** - not just the row `ttk.Frame`'s own size. Any real on-screen
spacing left out of that number doesn't show up as a one-off glitch: it
silently shifts every row after it by the missed amount, compounding
row over row, until a Tab/Shift-Tab session far enough into a long
transcript scrolls to entirely the wrong place (or decides, wrongly,
that no scroll is needed at all) - exactly the bug this section exists
to stop from recurring. The canonical list of what has to be accounted
for, and where:

| Constant (`layout_constants.py`) | Where it lives on screen | Inside the row's own `Frame` (`winfo_height()` sees it)? | Has to be added by hand in |
|---|---|---|---|
| `ROW_FRAME_PADDING_PX`/`ROW_FRAME_BORDERWIDTH_PX` (`ROW_FRAME_OVERHEAD_PX`) | The row `Frame`'s own `padding=`/`borderwidth=` chrome | Yes | `estimate_row_height` only |
| `GAP_BETWEEN_STACKED_PX` | Between stacked elements *within* a row's left/right column | Yes | `estimate_row_height` only |
| `TEXT_BOX_MARGIN_PX` | Extra headroom baked into a text box's own fixed height | Yes | `estimate_row_height` only |
| `ROW_PACK_PADY_PX` (counted ×2: above *and* below) | `_build_row`'s `row.pack(pady=ROW_PACK_PADY_PX)` - the gap *outside* the row's `Frame`, between it and its neighbors in `_scroll_frame` | **No** | `estimate_row_height`, `ReviewFrame._remeasure_built_rows` (`real = row.winfo_height() + 2*ROW_PACK_PADY_PX`), *and* every place in `keyboard_nav.py` that turns `self._offset_of(index)` into a real screen comparison (`self._offset_of(index) + ROW_PACK_PADY_PX + ...`) |

`ROW_PACK_PADY_PX` is the one entry that lives *outside* the row's own
bounding box, which is what made it easy to miss: every other constant
above is chrome the row `Frame` itself contains, so once
`_remeasure_built_rows` captures `row.winfo_height()`, that chrome is
automatically included - there was nothing to add by hand. The pack
gap is invisible to `winfo_height()` by construction (it's the *parent*
geometry manager's doing, not the row's own size), so it had to be
added back explicitly in three places: `estimate_row_height` (the
pre-build guess), `_remeasure_built_rows` (the real height, captured
once a row is actually built), and - easy to overlook even after fixing
the first two - everywhere `keyboard_nav.py` derives a real screen
y-coordinate from `self._offset_of(index)`. That last one is subtle for
a second reason beyond just "remember to add it": `self._offset_of(index)`
is defined as where row `index`'s full pack-allocated *slot* starts
(`sum(self._row_heights[:index])`), not where its `Frame`'s own visible
top edge sits - the `Frame` starts `ROW_PACK_PADY_PX` further down, past
its own leading pady. `_scroll_box_into_view`/`_keep_cursor_in_viewport`
both anchor a box's position off `row.winfo_rooty()` (the `Frame`'s real
top edge), so they need that `+ ROW_PACK_PADY_PX` correction explicitly,
on top of `_row_heights` already including it in the row's *total*
height.

If a future change adds another constant here - more outer padding, a
border on `_scroll_frame` itself, anything `_build_row` packs around a
row rather than inside it - it needs the same three-way treatment, not
just a bump to `ROW_FRAME_OVERHEAD_PX` (which is for chrome *inside* the
row only). The fastest way to catch a future regression of this kind:
focus a box several hundred rows into a long transcript (jumping there,
not scrolling incrementally) and compare its container's real
`winfo_rooty()` against the canvas viewport's real screen bounds - any
nonzero, *constant* (not growing) offset points at a missing one-time
correction like the `ROW_PACK_PADY_PX` one in `keyboard_nav.py`; a
*growing* offset (worse the further you've scrolled) points at a missing
per-row term in `estimate_row_height`/`_remeasure_built_rows` instead.

## Spacer slots

Blank-line spacing between/within review items used to be produced by a
post-run regex pass (`cleanup.py`) that collapsed excess newlines to a
fixed cap and special-cased die-roll commands - it couldn't express
anything finer than its hardcoded rules, and silently clobbered a
deliberately larger gap back down to its cap. Spacing is now fully owned
by dedicated "spacer" text boxes on the review screen: one-line-tall,
right-column-only editable boxes between every adjacent pair of
transcription elements, holding literal `\n` *tokens* (the two characters
`\` and `n`, not real newlines) that the user can freely edit. Nothing
else in a spacer box has any effect - see "Finalize-time parsing" below.

### Slot ordering (`review_item.ReviewItem.slot_roles`)

Four different places used to each independently re-derive "message, then
ocr0, ocr1, ..." from `item.initial_message_text`/`item.image_paths` -
row building, height estimation, the keyboard-navigable slot list, and
output writing. Adding spacer slots meant inserting new roles into that
sequence, so `ReviewItem.slot_roles` now computes the full ordered list
once and every one of those four places (`row_building.RowBuildingMixin
._build_row`, `virtualization.estimate_row_height`, `review_view
.ReviewFrame.__init__`'s `self._slots`, `review_item.lines_for_item`) just
walks it, rather than each re-deriving its own copy that could drift out
of sync with the others.

For an item with a message and N images, `slot_roles` is:
`["message", "spacer_msg_img", "ocr0", "spacer_img0", "ocr1", ...,
"ocr{N-1}", "spacer_end"]` - `"spacer_msg_img"` only appears when the item
has both a message and at least one image; `"spacer_img{i}"` appears
between every pair of images (omitted after the last one); `"spacer_end"`
(the gap before the next message) always appears, even for an item with
no images at all. A spacer role has no left-column counterpart - row
building puts nothing in the left column for it, and it's sized to
exactly one Tk text line (`SPACER_BOX_HEIGHT_PX`/`height=1`, see
`row_building.RowBuildingMixin._build_spacer_text_box`) rather than via
`_fixed_text_box_height`'s paired-height rule.

Tab/Shift-Tab visit spacer slots the same as any content slot (per the
project owner's decision) - `self._slots` already generalizes to any
role string, so `keyboard_nav.py`'s `_move_focus` needed no changes at all.

### Default newline counts

Computed once in `build_review_items`, from each message's *original*
text - not whatever the user later edits it to, so editing a message's
transcribed text never changes its own default spacing:

| Gap | Default |
|---|---|
| Message text → its first image (`spacer_msg_img`) | 1 empty line (2 tokens) |
| Between two images on the same message (`spacer_img{i}`) | 2 empty lines (3 tokens) |
| End of a normal message (text-only or image-ending) → next message | 3 empty lines (4 tokens) |
| Die-roll command (`config.DICE_COMMAND_RE`: `%roll \d*(d\|l\|h)\d+` or `%draw \d+ \d+`) → its result | 0 empty lines (1 token) |
| Die-roll result → next message that is itself a die-roll command | 1 empty line (2 tokens) |
| Die-roll result → next message that is *not* a die-roll command | 3 empty lines (4 tokens), same as normal |

A die-roll command's result is simply "the next approved message" -
there's no separate regex identifying a result; `build_review_items`
classifies every entry once via `_is_dice_command` and looks at each
item's immediate predecessor/successor in the (already author/date-
filtered) entries list. Die-roll messages are assumed to never have
images, so they only ever get a `"message"` slot plus a single trailing
`"spacer_end"` slot. The token count written into a spacer box's default
content (`review_item._spacer_default`) is always the empty-line count plus
one, since the gap also includes the newline that terminates the line
right before it - see "Finalize-time parsing" below for why that one
extra newline isn't *also* added by the preceding content box.

### Finalize-time parsing (`review_item.lines_for_item`)

- **Content roles** (`"message"`/`"ocr{i}"`): only *trailing* real
  newline/carriage-return characters are stripped from the box's text; the
  rest (including any internal newlines from a multi-line message) is
  written verbatim, with no newline forced onto the end. The spacer role
  that always immediately follows a content role in `slot_roles` supplies
  that terminator, plus however many blank lines the box was left with -
  this is why a spacer's default token count is the empty-line count plus
  one rather than just the empty-line count on its own.
- **Spacer roles**: every real newline/carriage-return character anywhere
  in the box - leading, trailing, or mixed through the middle - is
  discarded first, then the remaining literal `"\n"` tokens are counted
  (`review_item._count_spacer_tokens`) and that many real newline characters
  are written. Any other stray character typed into a spacer box is
  ignored, never written - nothing but backslash/`n` characters has any
  effect there.
- `write_message_lines`/`write_all_items` no longer add any fixed padding
  around an item's chunks (the old unconditional `"\n\n\n\n"` prefix /
  `"\n\n"` suffix was exactly the behavior spacer slots replace) - each
  item's own `"spacer_end"` chunk now supplies the entire gap before the
  next item. The gap before the very first item of a run is already
  provided by the previous run's trailing `\n\n\n{BREAK_MARKER}\n\n\n`
  (written by `finalize_run`), so no leading padding is needed there either.

### Session format

`ReviewFrame.collect_edited_texts`/the autosaved `edited_texts` session
field changed shape from a fixed per-item `(message_text, ocr_texts)`
tuple to a per-item `{role: text}` dict (covering every role in that
item's `slot_roles`, content and spacer alike) - the old shape had no way
to address a spacer slot at all. `_show_review` discarded a saved session
in the old shape (detected structurally, via a saved per-message edit dict
containing the literal key `"ocr"`) for a transition period after this
change shipped; that detection has since been removed now that no
pre-spacer-slot session is expected to still be on disk.

### Finalized edit persistence

When the user clicks Finalize and the pipeline succeeds, `_on_finalize_clicked`
saves every non-`None` slot value to `finalized_edits.json` (via
`state.save_finalized_edits`), keyed by the HTML path and each message's
Discord `message_id`. On a subsequent fresh run of the same chatlog,
`_show_review` loads these via `state.load_finalized_edits` and passes them
to `ReviewFrame` as `initial_finalized_texts`.

**Priority order** inside `ReviewFrame.__init__`: `_saved_texts` is seeded
first from the in-progress session (`initial_saved_texts`), then from
finalized edits for any slot not already covered. The existing checkbox-
seeding loop (`checked = saved is not None and saved != default`) runs last,
so a finalized edit that differs from the OCR default starts its checkbox
checked automatically with no special-case code.

**Merge semantics** (`state.save_finalized_edits`): new non-`None` values are
merged into whatever was already stored for this chatlog; `None` values
(unchecked OCR boxes) are skipped, leaving the prior finalized text for that
slot intact. A finalized edit is never deleted — once stored it persists until
overwritten by a subsequent finalize that supplies a non-`None` value for
that slot.

**Matching** (`_match_finalized_edits`): stored `{message_id: {role: text}}`
data is aligned to the current item list by Discord `message_id` (not by
position) using the same approach as `_match_saved_edits` for session resume
— orphaned message IDs are dropped silently, and roles that no longer appear
in an item's `slot_roles` (e.g. because the chatlog was re-exported with
fewer images) are also dropped. All `slot_roles` including `spacer_*` are
eligible for storage and pre-population.

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
