# Architecture & internals

Deeper technical detail than the README needs for "how do I run this" -
review-screen internals (the most architecturally involved part of the app)
and logging conventions, for whoever's about to change either.

## General heuristic: test where features compose, not just each feature alone

Three separate real bugs have now shipped at the exact same seam -
`_populate_text_box`'s replay branch (`row_building.py`), the code that
reconstructs a torn-down review-screen text box from `UndoLog.ops` rather
than rebuilding it fresh:

1. **`UndoLog.baseline`** (see "A box's `UndoLog` must record what it
   actually started from" below): a rebuild re-based replay onto the
   item's static `initial_text`, silently discarding a resumed edit that
   had zero further ops recorded on top of it.
2. **`sel.first`/`sel.last`** (see "Symbolic marks recorded in a `UndoLog`
   must be resolved before they can drift" below,
   `archive/INVESTIGATION_shift_tab_reconcile_lockup.md`): a recorded
   `delete sel.first sel.last` call replayed onto a fresh widget with
   nothing selected, raising `TclError` and wedging the whole review
   screen's virtualization for the rest of the session.
3. **Undo/redo replay divergence** (see "A recorded undo/redo must not be
   replayed by calling edit_undo()/edit_redo() again" below,
   `INVESTIGATION_undo_redo_replay_divergence.md`): replaying a bare
   `"undo"`/`"redo"` marker by calling `edit_undo()`/`edit_redo()` again
   silently reverted a *different amount* of text than the live press did,
   with no exception and no log line.

All three are the same failure shape wearing different clothes: **replay
implicitly assumed the reconstructed widget was equivalent to the live one
it's standing in for, and something true of the live widget - its actual
starting point, a mark's live resolution, the undo stack's grouping
history - wasn't actually captured in the log.** The log looks like a
complete causal description of "what happened to this box," but each time
it turned out to be missing a piece of context that only lived on the live
widget instance, so replay silently filled the gap with a wrong default.
None of these three needed rapid Tab/Shift-Tab to be *possible* - a single
ordinary teardown/rebuild is enough - but virtualization's whole design is
"destroy and reconstruct widget state from a compact model," and rapid
navigation is what reliably produces many teardown/rebuild cycles in a
short window, which is what turns a latent bug into a frequent, real one.
All three were also found by forensic reconstruction from production
logs after the fact, not by the existing test suite, for the same
underlying reason described below.

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

Concrete habits this argues for in this codebase specifically:

- When adding a feature that touches state another feature already
  manages (session resume touching the same per-box dicts virtualization
  owns), write at least one test that exercises *both* in the same test,
  in the order a real user would actually hit them - not just one test
  per feature with the other feature absent. If two such tests already
  exist separately (as they did for the `UndoLog.baseline` bug), that's a
  sign the combined test is still missing, not that coverage is already
  adequate.
- When a piece of code's correctness depends on an assumption about how
  it got into its current state (e.g. "this log's ops are deltas from
  `initial_text`"), encode that assumption as actual stored data (`UndoLog.
  baseline`) rather than leaving it implicit in which branch happened to
  run. An assumption that only lives in a comment or a docstring can drift
  silently out of sync with the code the moment a new caller is added that
  the original author didn't have in mind; an assumption recorded as data
  the code itself reads back can't.
- **Resolve ambiguous/context-dependent things at record time, not replay
  time.** Every fix here converged on the same technique: turn something
  whose meaning depends on hidden or mutable context (a symbolic mark, an
  undo/redo call whose effect depends on the live widget's own grouping
  history) into something absolute and self-contained *before* it goes
  into the log, so replay never has to re-derive it. When adding a new
  kind of recordable interaction to these boxes (rich text tags, IME
  composition, drag-and-drop, ...), ask up front: does this op's effect
  depend on anything beyond its own literal arguments plus the box's
  current text? If yes, resolve that dependency before recording it,
  rather than finding out via a fourth production incident.
- **Verify replay against independently-captured ground truth by default,
  not per-bug.** `_populate_text_box`'s replay branch now unconditionally
  compares its result against `self._saved_texts[key]` - the box's own
  content as of its last teardown, captured completely independently of
  whatever replay just produced - and self-heals on any mismatch. This
  started as a targeted fix for the undo/redo bug specifically, but it's
  actually a generic property of *any* replay, and now runs for all of
  them. Treat it as a standing structural guarantee of this subsystem, the
  same way `main.py`'s `root.report_callback_exception` is a blanket net
  for *any* uncaught Tk-callback exception rather than a fix for one - not
  as a mechanism that only needs revisiting when a fifth bug of this shape
  turns up.
- **A dedicated regression test per discovered bug only covers
  combinations someone has already hit.** All three bugs above were
  invisible to the test suite until someone hand-wrote a test for the
  exact scenario each one turned out to require. The structurally stronger
  complement this used to call out as not yet done now exists:
  `test_review_view.py`'s `_random_edit_sequence` applies a randomized
  sequence of this subsystem's interaction types (type, select+delete via
  Tk's own `sel.first`/`sel.last`, undo, redo, a paste-shaped delete-
  selection-then-insert, and - for an "ocr" box - checkbox toggle) to a box
  in random order and count, tears its row down, rebuilds it, and asserts the rebuilt
  content, cursor position, and (for an "ocr" box) checked state all match
  whatever was live immediately before teardown - targeting "does replay
  reproduce reality" as an invariant directly, parametrized across every
  role shape a box can have ("message", "ocr{N}", a spacer role).
  `test_random_interaction_sequence_survives_two_consecutive_teardown_
  rebuild_cycles` extends this to two consecutive cycles (the
  `UndoLog.baseline` bug specifically needed a *second* rebuild, one whose
  starting point was itself a replay result, to surface at all), and
  `test_random_edits_after_a_seeded_baseline_survive_a_further_teardown_
  and_rebuild` closes the specific gap every existing resumed/finalized
  composition test left open - real further edits on top of a
  resumed/finalized baseline, not just an empty op log, before the next
  rebuild. Every one of these asserts no `ERROR`-level replay-divergence/
  self-heal log line fired too - a property test that only checked final
  content would still pass if replay were badly broken and silently
  falling back to the self-heal backstop (see below) on every single
  rebuild, since self-heal's own recovery target (`self._saved_texts`) is
  kept correct independently of replay. This is randomized but seeded
  (fixed per-test seeds), so a failure is reproducible, not flaky - it
  isn't yet run with a broad seed sweep in CI the way a true fuzzing setup
  would be, so it's still a probabilistic net, not a proof.

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
- **Symbolic marks recorded in a `UndoLog` must be resolved before they can
  drift.** (`text_undo.py`'s `attach_undo_recording`.) A real, reproduced
  bug: Tk's own built-in Text bindings remove a selection via the literal
  call `delete sel.first sel.last` (Delete/Backspace/typing-over-a-
  selection/Ctrl+X), not absolute positions - and the recording proxy used
  to log that call's raw Tcl arguments verbatim, so a box with a
  selection-delete in its history got `("delete", ("sel.first",
  "sel.last"))` permanently written into `UndoLog.ops`. `sel.first`/
  `sel.last` only mean anything while *that specific widget instance* has
  a live selection - replaying that op onto a freshly-built widget (row
  paged out and back in, or torn down and rebuilt for any other reason)
  with nothing selected raised `_tkinter.TclError: text doesn't contain
  any characters tagged with "sel"`, uncaught, from inside a Tk-bound
  callback (`_reconcile`, reached from both keyboard nav and the scroll-
  debounced path). Full analysis in
  `archive/INVESTIGATION_shift_tab_reconcile_lockup.md` - kept in full even
  though fixed, since the cross-feature interaction it traces
  (virtualization's row rebuild + undo replay) is exactly the kind of thing
  worth having on record rather than re-deriving if a similar bug
  resurfaces. Every other in-code reference to this filename (row_building.py,
  review_view.py, text_undo.py, keyboard_nav.py, main.py, and their tests)
  cites it by bare filename only, without the `archive/` prefix - still
  unambiguous to grep for, and not worth touching that many call sites just
  to spell out a path.

  Fixed at the source: `_proxy`'s recording now resolves *every*
  insert/delete index argument - not just `sel.*`, since `insert`/`end`/
  any other mark is just as capable of meaning something different (or
  nothing) on a freshly-built widget - to an absolute `"line.column"`
  string via `tcl.call(shadow_path, "index", value)`, called *before* the
  real mutating call runs (a mark's meaning is only well-defined relative
  to the widget's state right before the mutation, not after). This makes
  every recorded op replay-safe by construction, the same way `UndoLog.
  baseline` (above) made a rebuild's *starting point* trustworthy by
  construction rather than by convention.

  A record-time fix alone can't guarantee there's no other, still-unknown
  way for a replay to fail - so three complementary, independently-useful
  hardenings shipped alongside it, all backstops rather than substitutes
  for the record-time fix (see `INVESTIGATION_shift_tab_reconcile_
  lockup.md`'s "Recommended fix"/"What's still open" sections):
  - `_populate_text_box`'s replay is wrapped in `try/except tk.TclError`.
    On failure, it recovers the box's actual last-known-good text from
    `self._saved_texts` (captured independently, at the box's last
    teardown - never `log.baseline`/`initial_text`, either of which can be
    staler than what the user actually left in the box) and *self-heals*:
    wipes the poisoned `log.ops` and re-baselines on the recovered text, so
    the same box doesn't crash again on its next rebuild.
  - `_reclaim_widget_if_present` (called from both
    `_build_editable_text_box` and `_build_spacer_text_box`, replacing
    what used to be just a logged warning) closes a real data-loss
    mechanism this investigation traced precisely: once
    `self._materialized_range` gets stuck (as fallout from an uncaught
    build exception), a later reconcile could call `_build_row` for an
    index that's *already* live in `self._text_widgets` - silently
    orphaning that widget's content, since `_destroy_row` (the only place
    that captures `text_widget.get()` into `self._saved_texts`) never runs
    for it. Now it does, every time, before the key is overwritten.
  - `review_view.py`'s `_try_build_row` (used throughout
    `_sync_materialized_rows` in place of calling `_build_row` directly)
    catches any exception from a single row's build, tears down whatever
    partially got built (via `_destroy_row`, capturing anything that did
    succeed), logs loudly, and lets the rest of that reconcile's batch
    continue - a backstop against *any* future per-row build failure, not
    just this specific `TclError`, since before this fix one bad row
    silently killed every row after it in the same batch and left
    `self._materialized_range` permanently disagreeing with
    `self._row_frames`'s real contents. `keyboard_nav.py`'s
    `_focus_text_box` correspondingly uses `self._text_widgets.get(...)`
    with a logged-and-skip fallback instead of a raw dict lookup, so a slot
    whose row failed to build logs once instead of raising `KeyError` on
    every subsequent Tab/Shift-Tab press aimed at it.

  Separately, `main.py` now installs `root.report_callback_exception =
  _log_tk_callback_exception`, routing *any* uncaught Tk-callback exception
  through this app's own logger - this specific bug was invisible in
  `app.log` and only ever reached an unlogged console, which is what made
  it take a full log-forensics-then-console-capture pass to even find (see
  the investigation doc's "Why nothing shows up in app.log"). This isn't
  specific to the `sel.first` bug either - it's a blanket safety net for
  whatever the *next* uncaught Tk-callback exception turns out to be.

- **A recorded undo/redo must not be replayed by calling `edit_undo()`/
  `edit_redo()` again.** (`text_undo.py`.) A real, reproduced bug, distinct
  from the `sel.first` one above despite sharing the same file - see
  `INVESTIGATION_undo_redo_replay_divergence.md` for the full log forensics.
  `keyboard_nav.py`'s `_undo_text`/`_redo_text` used to append a bare
  `("undo", ())`/`("redo", ())` marker to `UndoLog.ops`, and `replay_onto`
  replayed it by literally calling `text_widget.edit_undo()`/`.edit_redo()`
  again on the rebuilt widget - on the theory that replaying the same
  insert/delete call sequence would make Tk's own autoseparator logic
  re-derive the same undo-step grouping it used live, since that grouping
  is a deterministic function of the call sequence. False in practice:
  grouping also depends on things that never make it into `log.ops` at all
  - e.g. `_on_ocr_checkbox_toggle`'s own `edit_separator()` calls, which
  aren't insert/delete calls and so are invisible to this log - so a
  rebuilt widget's replay-time grouping isn't guaranteed to match the live
  grouping, and `edit_undo()` against a differently-grouped stack can
  revert a different *amount* of text than it did live. Worse than the
  `sel.first` bug in one way: that one crashed loudly (`TclError`), which -
  while bad - is at least conspicuous; this one raised nothing and logged
  nothing, so the box just silently ended up holding different content
  than it held at teardown, which then got autosaved and could reach
  Finalize unnoticed.

  Fixed at the source, the same way `sel.first` was: a successful
  `edit_undo()`/`edit_redo()` is now recorded as a `"replace"` op -
  `widget`'s exact resulting text, captured live right after the call -
  instead of a bare marker, so replay never touches Tk's undo stack for
  this step at all; it's just another content mutation through the same
  insert/delete primitives every other op already uses. `replay_onto`
  brackets the replayed delete+insert with `edit_separator()`/
  `autoseparators` suppression, the same trick `_on_ocr_checkbox_toggle`
  already uses, so it lands as one atomic step on the rebuilt widget's own
  stack rather than splitting into two. The tradeoff this leaves: a
  *further* Ctrl+Z pressed after such a rebuild isn't guaranteed to have
  the same step boundaries it would have live (e.g. one Ctrl+Z landing on
  an intermediate empty state instead of a real prior one, recoverable with
  a second Ctrl+Z or a Redo) - a user-visible granularity difference, not
  silent content corruption.

  `_populate_text_box`'s replay branch also gained a second, broader
  regression check alongside the pre-existing "landed back on the item's
  bare default" alarm (left completely unchanged, including its exact
  message text, so `archive/INVESTIGATION_shift_tab_reconcile_lockup.md`'s
  grep instructions still work): it compares the replay's `result_text`
  directly against `self._saved_texts[key]` - the box's own content as of
  its last teardown (`review_view.py`'s `_destroy_row`, captured
  independently of whatever replay just produced) - and, on any mismatch,
  logs `ERROR` (`"replay result doesn't match this box's content as of its
  last teardown - possible silent replay divergence"`) and *self-heals*:
  overwrites the widget onto `self._saved_texts[key]` and re-baselines
  `log`, the same recovery `_populate_text_box`'s existing `TclError`
  guard already does. This is a detection-and-recovery backstop, not a
  substitute for the record-time fix above - it exists for whatever future
  replay-divergence mechanism this doesn't anticipate, the same
  relationship the `sel.first` fix's own defense-in-depth backstops have to
  its record-time fix. Like the `TclError` recovery it mirrors, self-
  healing discards `log.ops`, so a Ctrl+Z pressed immediately after a
  self-heal event finds nothing to undo - preferable to silently wrong
  content, but still a real, visible difference from an ordinary rebuild.

- **Spellcheck tagging.** (`app/spellcheck.py`, wired in via
  `row_building.RowBuildingMixin._configure_spellcheck_tag`/
  `_schedule_spellcheck`/`_run_spellcheck`.) A misspelled word is underlined
  in red via a plain Tk text tag (`tag_configure("misspelled", underline=True,
  underlinefg=...)`) - a straight underline, since Tk has no wavy/squiggly
  underline primitive. This was the first use of `tk.Text` tags anywhere in
  this codebase, which mattered for one reason: `text_undo.py`'s recording
  proxy (see "A box's UndoLog must record what it actually started from"
  above) only records `insert`/`delete` calls - `tag_add`/`tag_remove` pass
  through unrecorded, so spellcheck tagging can't corrupt or interact with
  undo history the way a naive approach touching the widget's content might.
  The tradeoff: tags live on the `tk.Text` *instance*, not in any per-box
  bookkeeping dict, so they don't survive a row being torn down and rebuilt
  (a fresh widget) - `_build_editable_text_box` schedules a fresh spellcheck
  pass on every (re)build, not just the first, to compensate. Applied only to
  "message"/"ocr{N}" boxes - `_build_spacer_text_box` never calls into this
  at all, so a spacer box (holding nothing but `\n` tokens) is never even
  candidate for the tag.

  `find_misspelled_spans` also loads a second, complementary sidecar file -
  `spellcheck_blacklist.txt`, same one-word-per-line format and lazy-load-
  and-cache convention as the whitelist (`_get_blacklist`/`_blacklist`/
  `_blacklist_loaded`, mirroring `_get_whitelist`/`_whitelist`/
  `_whitelist_loaded`) - for real English words that the dictionary
  considers correctly spelled but that keep turning out to be OCR misreads
  or typos for something else in this transcript's context. A candidate
  word is flagged if it's either unrecognized by the dictionary *or* in the
  blacklist; a word in both the whitelist and the blacklist is never
  flagged, since the whitelist subtraction (`candidates = {...} -
  whitelist`) happens before the blacklist union is computed, so a
  whitelisted word is never even a blacklist candidate.

  Debounced per box (`SPELLCHECK_DEBOUNCE_MS`, via `text_widget.after`) so
  typing doesn't re-scan a box's text on every keystroke - `_destroy_row`/
  `_reclaim_widget_if_present` cancel a box's pending timer before tearing
  its widget down, the same defensive posture as everything else here that
  reaches back into an about-to-be-destroyed widget. That per-row
  cancellation isn't enough on its own, though: rows still materialized when
  the *whole* `ReviewFrame` goes away (screen switch, app close, or a test's
  `root.destroy()`) never go through `_destroy_row` at all, so their pending
  timers would otherwise leak. This surfaced immediately as a real,
  reproduced test failure - not a hypothetical - once spellcheck shipped:
  `test_review_view.py`'s GUI tests build and tear down many `ReviewFrame`s
  (and their many text boxes) back to back in the same process, and Tcl's
  `after` timer queue turned out to be shared across every `tk.Tk()`
  interpreter in that process (it's per-thread, not per-interpreter) - so
  leaked timers from earlier tests piled up and measurably slowed a later
  test's own `update()`/`update_idletasks()` calls, enough to occasionally
  exhaust `_settle_pending_geometry`'s bounded retry count (see "A row's
  first-ever build can measure as winfo_height()==1" above) and leave that
  test's own first row never actually built. Fixed the same way
  `self._update_job`/`self._initial_position_job` already were: the
  `ReviewFrame`'s own `<Destroy>` handler now cancels every remaining entry
  in `self._spellcheck_after_ids` too, not just per-row teardown.
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
- **A popup `tk.Menu`'s close can't be detected via `<Unmap>` on Windows.**
  (`image_context_menu.py`'s `_show_image_context_menu`.) The right-click
  context menu on a review row's image (Open Image in Browser/Open Image
  Location/Copy Image) freezes review-window scrolling (mousewheel/Page
  Up-Down/scrollbar - `ReviewFrame._scroll_frozen`, checked in
  `review_view.py`'s mousewheel/scrollbar handlers and `keyboard_nav.py`'s
  `_on_page_up`/`_on_page_down`) for as long as it's open, so scrolling
  can't move rows - and this menu's target image - out from under it. The
  first version unfroze via `menu.bind("<Unmap>", ...)`, on the assumption
  that Tk would fire its ordinary widget-unmap event when the popup closed,
  the same way it does for a normal window being withdrawn/destroyed. A
  real, reported bug: that binding never fired for a real close on Windows,
  however it closed - clicking one of the three commands *did* unfreeze
  (each command's own callback ran, and the menu happening to close right
  after was incidental), but dismissing the menu with Escape or a click
  elsewhere left scrolling frozen forever, since nothing else ever reset
  `_scroll_frozen`. Root cause: on Windows, `tk.Menu`'s popup is implemented
  via the native `TrackPopupMenu` API rather than as an ordinary
  Tk-managed toplevel - so it never generates the `Unmap` event Tk's own
  binding machinery depends on, regardless of how it's dismissed.

  Fixed by not depending on any event at all: `TrackPopupMenu` blocks the
  call that posts it - `menu.tk_popup(...)` doesn't return until a person
  has actually dismissed the menu, one way or another - confirmed by hand,
  not just inferred, since this exact blocking is also what made an
  earlier, unattended version of this feature's own test suite hang with
  the real popup menu visible on screen until force-closed (see
  `test_image_context_menu.py`, which mocks `tk.Menu.tk_popup` for exactly
  this reason rather than ever calling the real thing). That blocking
  makes unfreezing in `_show_image_context_menu`'s own `finally` - right
  after `tk_popup(...)` returns - deterministic: by the time control gets
  there, the menu is already gone, whichever of the three ways it closed.
  `_on_image_context_menu_closed` (the unfreeze itself, plus its own log
  line) is a real bound method rather than a nested closure specifically so
  a test can call it directly without needing a real popup close to trigger
  it.

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

## Test coverage

312 tests total: 213 run by default, plus 99 marked `gui` (build a real,
withdrawn Tk window - see the README's "Testing" section) that are skipped
unless run with `-m gui` or `-m ""`.

### Review screen

The most architecturally involved and historically bug-prone part of the
app (see "Review screen internals" and "Row geometry" above), so it has
the deepest coverage:

- `ReviewFrame._reconcile`'s windowing core - idempotency, paging to the
  end of a long transcript, a far-away Tab/resume target materializing
  correctly, edits surviving a row being paged out and back in, and the
  Finalize button's visibility toggle.
- Row-height estimation/visible-range math (`app/gui/virtualization.py`,
  the part of the windowing logic pure enough to unit-test without a
  display) - including a row with multiple images estimating taller than
  one with a single image.
- Slot-based keyboard navigation (`_move_focus` stepping through
  `(item_index, role)` slots in transcript order, message before one
  `"ocrN"` slot per attached image).
- Per-box undo/redo history (`text_undo.py`) surviving a row being paged
  out and rebuilt.
- `text_undo.py`'s recording proxy resolving symbolic index arguments
  (`sel.first`/`sel.last`, the `insert` mark) to absolute positions at
  record time (`app_tests/test_text_undo.py`), plus the end-to-end shape of
  the bug this fixes: selecting and deleting text, then paging that row out
  and back in, must not raise and must land on the correct final text
  (`app_tests/test_review_view.py`). See "Symbolic marks recorded in a
  UndoLog must be resolved before they can drift" above.
- The three defense-in-depth backstops that shipped alongside that fix
  (same section above): a replay failure that still somehow occurs recovers
  the box's last-known-good text and self-heals its `UndoLog` instead of
  crashing; rebuilding an already-live box (a "should be impossible"
  double-build) reclaims its content into `self._saved_texts` instead of
  orphaning it; and one row's build exception no longer aborts the rest of
  its reconcile batch or leaves a dangling `KeyError` trap on later
  Tab/Shift-Tab navigation to a row that failed to build.
- A focused box always scrolling fully into view, not just its row, and a
  far-away Tab/resume target landing fully within the *real* canvas
  viewport rather than just the document-space model's own idea of where
  it is (a since-fixed row-height accounting bug could get this wrong).
- Each OCR box's checkbox (see "Per-OCR-box edited/checkbox state" above):
  starting checked/unchecked correctly for an untouched vs. a
  resumed-and-differing-from-default box, typing checking it
  automatically, unchecking/rechecking round-tripping both the OCR
  default and the edited version without either being discarded,
  `collect_edited_texts` reporting `None` for an unchecked box despite its
  edited version still being cached, both the checkbox state and both
  text versions surviving a row being paged out and back in, and Ctrl+Z
  re-deriving the right checked state after undoing a toggle.
- The editable text box height rule (`_fixed_text_box_height`'s capping
  logic).
- Spellcheck tagging (`test_spellcheck.py`, pure logic - flagged/not-flagged
  words, short-word and ALL-CAPS skipping, whitelist loading/caching - plus
  `test_review_view.py`'s GUI tests for the real `tk.Text` tag behavior: a
  misspelled word getting tagged, a correctly-spelled box getting no tag, a
  spacer box never having the tag configured at all, the tag being
  recomputed after a row is paged out and rebuilt onto a fresh widget, and a
  torn-down row's pending debounce timer actually getting cancelled).
- Image preview sizing/visibility (`app/gui/image_loading.py`'s
  aspect-fit math and its load/unload viewport-boundary decision, plus
  real load/failure/unload behavior against actual Tk widgets).
- The right-click image context menu (`app/gui/image_context_menu.py`,
  `test_image_context_menu.py`): each action's success/failure logging;
  `_open_image_in_browser`/`_open_image_location`/`_copy_image_to_clipboard`
  themselves (mocking `webbrowser`/`subprocess`/`win32clipboard` rather than
  really opening a browser, a real Explorer window, or touching the real
  clipboard); scrolling (mousewheel, Page Up/Down, the scrollbar) freezing
  while the menu is open and unfreezing once it closes. The real
  `tk.Menu.tk_popup()` call is never made in any of these - see "A popup
  `tk.Menu`'s close can't be detected via `<Unmap>` on Windows" above for
  why it blocks until a person dismisses it, which hangs an unattended
  test - `tk_popup` is mocked out instead, so these test what
  `_show_image_context_menu` itself controls (state before/after the
  call) rather than the real OS-level popup/dismissal.

### Session resume

- Message-id-based edit/focus matching (`_match_saved_edits`/
  `_match_focus_slot`) - edits surviving messages appended or inserted
  mid-transcript in a re-export, orphaned edits for now-filtered-out
  messages being dropped, a saved message's per-image OCR edits being
  aligned back onto its current images by position, and a stale focus
  slot falling back to no restore.
- `App._on_start`'s validation branches (missing fields, an invalid start
  date, an empty approved-users list) and its pending-session resume
  prompt, including `_resume_session` always forcing `use_cache=True`
  regardless of what the saved session originally recorded, so resuming
  never redoes OCR.
- The malformed-chatlog error path (`App._on_ocr_done` surfacing a clear
  dialog instead of letting the error escape uncaught from a background
  Tk callback).

### HTML parsing / OCR pipeline

- HTML parsing/filtering - the export postamble's declared timezone
  applied to every message timestamp, the clear error raised when that
  timezone is missing or unparseable, the per-message Discord ID
  extracted from each `chatlog__message-container`'s `data-message-id`,
  the clear error raised when that container is missing, and - run
  against a real DiscordChatExporter export fixture,
  `example_inputs/short_test_input.html` - every image attachment a
  message has being picked up rather than just the first.
- OCR paragraph splitting and backend dispatch.
- The OCR-misread corrections pass (`ocr_corrections.py`'s file
  parsing/validation and regex application) and its wiring into
  `build_review_items` - applied to OCR text only, never to a message's
  own text.
- The OCR batch runner/cache short-circuit.
- The cleanup regexes.
- Review-item building/output-writing - a text-only message's editable
  spacing copy standing in for its immutable original when written out, a
  message with both a caption and an image getting two
  independently-edited text blocks, and a message with multiple images
  getting one independently-edited OCR block per image, each falling back
  to its own original OCR text when not edited.
- The finalize pass (cleanup + run-date + clipboard + BREAK-marker
  bookmarking).

### Persistence

- JSON state persistence - run dates, OCR cache and in-progress sessions
  both kept per-chatlog/per-folder indefinitely rather than as a single
  global slot, and recent-path history.
- The atomic-write-plus-backup-rotation/recovery behavior of every state
  file (`app/state.py`).
- The JSON log formatter.
- Start-date validation.

### What's not covered

- No automated test drives real Tk button *clicks* - only direct method
  calls standing in for them - or a live Tesseract install.
- `setup_view.py`'s widget wiring is still only covered by manual
  smoke-testing: window construction, the review screen with synthetic
  text-only/image-only/image-with-caption/multiple-images-on-one-message
  items, an edit-then-finalize pass against a temp output file, and a
  resumed session's saved edits/focus restoring correctly.

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
(`logging_config.get_trace_logger()`, 40MB x 6 backups - sized generously,
see the comment at its `RotatingFileHandler` call, so debugging a rare bug
isn't also a race against this file rotating the relevant session away) -
so it doesn't compete with, or evict, `app.log`'s much lower-volume
lifecycle events under rotation. Every event in that category, including
ones originating in `image_loading.py` (image load/unload) rather than
`review_view.py` itself, goes through the same `_log_event` call and so
carries the same `seq`/scroll-state fields, making any two events in the
trace directly correlatable without falling back to timestamp ordering.

Every undo/redo-capable text box's edit history (`app/gui/text_undo.py`'s
`UndoLog`) is fully traced on both the recording and replay side - every op
appended to `log.ops`, whether via `attach_undo_recording`'s Tcl-command-
interception proxy (ordinary insert/delete) or `keyboard_nav.py`'s
`_record_undo_replacement` (the `"replace"` op a successful Ctrl+Z/
Ctrl+Shift+Z appends directly to `log.ops`, bypassing that proxy entirely -
see "A recorded undo/redo must not be replayed by calling edit_undo()/
edit_redo() again" above), logs a
`box_op_recorded` event to `scroll_trace.log` carrying the widget's content
fingerprint (`logging_config.text_fingerprint` - length + short hash)
*immediately after* that op took effect. `_populate_text_box`'s replay
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
check beyond the pre-existing "landed back on the item's bare default"
alarm (`row_building.py`, still present unchanged, together with its
associated grep pattern in `archive/INVESTIGATION_shift_tab_reconcile_
lockup.md`): it compares the replay's `result_text` directly against
`self._saved_texts[key]` - the box's own content as of its last teardown
(`review_view.py`'s `_destroy_row`) - and, on any mismatch, logs an
`ERROR`-level "replay result doesn't match this box's content as of its
last teardown - possible silent replay divergence" and self-heals onto
`self._saved_texts[key]` (same recovery as the `TclError` guard just above
it), regardless of whether the wrong result happens to look like the bare
default or like some other, still-edited-looking text. The bare-default
check only ever covered the former; this covers both, and is exactly the
check `INVESTIGATION_undo_redo_replay_divergence.md`'s reproduced case
would have tripped and corrected immediately instead of requiring a manual
cross-session log reconstruction to notice at all.
