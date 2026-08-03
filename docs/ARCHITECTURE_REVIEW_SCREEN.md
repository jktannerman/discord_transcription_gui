# Review screen internals

Part of [ARCHITECTURE.md](ARCHITECTURE.md) - see there for the general
testing heuristic this codebase follows, and for links to the other topic
docs (row geometry, spacer slots, testing, logging).

The review screen (`app/gui/review_view.py`) is the most architecturally
involved part of this app. Its module docstring is the canonical explanation
and worth reading in full before changing it; this is just enough to orient
a new contributor.

Building a single row's widgets (`_build_row` and the label/image-placeholder/
editable-text-box helpers it calls) lives in `app/gui/row_building.py`'s
`RowBuildingMixin`, mixed into `ReviewFrame` the same way `keyboard_nav.py`'s
`KeyboardNavMixin` already is - it doesn't carry the same "disagreed with
itself across files" risk the windowing core below does, since each row's
widgets are self-contained once built. `review_view.py` itself keeps only
that windowing core (`_reconcile`/`_sync_materialized_rows`/
`_remeasure_built_rows`/`_offset_of`/`_ensure_materialized`/`_destroy_row`)
plus the scroll/debounce/Finalize-button machinery:

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
- **The pre-build height estimate isn't just cosmetic.**
  `virtualization.estimate_row_height`'s guess for an image row used to
  assume the image filled the full `THUMBNAIL_SIZE` bounding box - but most
  images here are landscape (width-, not height-, constrained), so the real
  fitted height is usually far less, and the guess overestimated most rows
  by 500+px. That got corrected once a row was actually built
  (`ReviewFrame._remeasure_built_rows` shifts the scroll offset to keep
  on-screen content stable when a row's real height differs from its
  estimate) - but the correction itself showed up as a scroll jump
  disconnected from the user's actual scroll input, worse the bigger the
  estimate error. `estimate_row_height` now calls the same cheap,
  header-only `fitted_image_size` read already used to size the real
  placeholder, instead of a flat constant.
- **The pre-build estimate also has to mirror the real layout's cap, not
  just its content-driven size.** A content box's real height
  (`RowBuildingMixin._fixed_text_box_height`) is capped at
  `TEXT_BOX_MAX_HEIGHT_FRACTION` of the canvas - a long message/OCR text
  gets an internal scrollbar past that point rather than growing the row
  further - but `estimate_row_height` had no matching cap, so a long row's
  *estimated* height could run far past what its real, capped box would
  ever be. Harmless for a row built and remeasured soon after - the actual
  symptom was a focused box landing partially or fully off the real canvas
  viewport after a discontinuous jump straight to a deep slot (a resumed
  session's saved focus, or a scrollbar drag far down the document), while
  continuously walking there via Tab/Page Down from the top always looked
  fine. The difference: walking through remeasures every row along the way
  before `_offset_of` ever needs their height again - a jump skips that
  remeasurement for whatever it jumps over, so any long row in the skipped
  range keeps contributing its uncapped overestimate to every later row's
  document-space offset indefinitely (until something eventually builds it).
  `estimate_row_height` now takes an optional `max_text_box_height_px` and
  caps each content role's right-column contribution the same way
  `_fixed_text_box_height` does; `ReviewFrame.__init__` passes
  `self._max_text_box_height_px()` - which is also why the row-heights list
  is now built *after* `self._canvas` exists, not before, so that call's own
  not-yet-laid-out/`winfo_screenheight()` fallback applies the same way it
  would for any other premature call to it.
  `test_jumping_focus_past_a_capped_long_message_row_lands_target_fully_in_view`
  (`app_tests/test_review_view.py`) is the regression test - it puts one
  long, capped-height row outside the window built at startup and jumps
  straight past it to a target further down, the same way a resumed
  session's saved focus slot would.
- **A row's first-ever build can measure as `winfo_height()==1` even right
  after `canvas.update_idletasks()`.** A real bug: resuming a session whose
  saved focus slot is deep in the transcript jumps straight there
  (`_ensure_materialized`), which materializes that whole window of rows in
  the session's very *first* `_reconcile` call - a deeply nested `ttk.Frame`
  tree, several levels deep, none of which have ever been mapped to the
  screen before. `update_idletasks()` only drains Tcl's idle queue (what
  pack's own size negotiation runs on), not the window-system `Map` event a
  widget needs before `winfo_height()` reports anything real - and that
  event doesn't always arrive within a single idle-queue pass for that much
  brand-new tree at once. `_remeasure_built_rows` used to take
  `winfo_height()`'s bogus `1` at face value, permanently writing
  `2*ROW_PACK_PADY_PX` (9px) into `self._row_heights` for every row in that
  first window - and since a row already in `self._row_frames` is never
  rebuilt (so never remeasured) just because a later `_reconcile` runs, that
  9px-per-row corruption then threw `self._offset_of` off by hundreds of px
  for every row after it, for the rest of the session, with no further
  chance to self-correct - the user-visible symptom was Tab/Shift-Tab's
  scroll-into-view looking completely broken from the moment a deep resume
  opened, confirmed against real `scroll_trace.log` output (every row in
  that first reconcile's `remeasure_mismatch` events reading `real_height:
  9`, every later reconcile reading correctly). Every *later* reconcile
  measures correctly on the first try, since by then the canvas has already
  been mapped once - this is specifically a first-reconcile problem.

  `_reconcile` now calls `_settle_pending_geometry` (`review_view.py`) right
  after `update_idletasks()` and before trusting any measurement: it retries
  `update_idletasks()` a bounded number of times first (cheap, no
  event-processing side effects, covers the ordinary "pack is still
  settling" case), then falls back to a bounded number of full `update()`
  calls if that wasn't enough. `update()` - unlike `update_idletasks()` -
  drains *all* pending events, not just idle callbacks, which is what
  actually unblocks the stuck `Map`; confirmed by a standalone repro that
  `update_idletasks()` alone never resolves it, no matter how many retries,
  while a single `update()` does. `update()` can call back into `_reconcile`
  itself before returning (e.g. an already-scheduled debounced reconcile
  from the canvas's first `<Configure>` event) - expected and *not* guarded
  against: an earlier version added a reentrancy flag that made such a
  nested call a no-op, on the theory it could rebuild/tear down rows out
  from under the in-progress outer call. That theory didn't survive contact
  with the repro - guarding the reentrancy back out reproduced the exact
  bug, because the nested call's own geometry-touching work (re-issuing
  `canvas.configure(scrollregion=...)`/`canvas.coords`) turned out to be
  what actually finishes flushing the stuck `Map`, not incidental to it.
  `_reconcile`/`_sync_materialized_rows` are already written to be
  idempotent and safe to re-enter (see the module docstring), so trusting
  that existing guarantee - rather than adding a new one - is what makes
  this safe. `_remeasure_built_rows` itself also gained a direct
  `winfo_height()<=1` guard as a last-resort fallback (skip recording that
  row's height at all, leaving its previous estimate in place) for if
  `_settle_pending_geometry`'s bound is ever actually hit.
  `test_resuming_deep_in_a_long_transcript_remeasures_rows_correctly_on_first_build`
  (`app_tests/test_review_view.py`) is the regression test - it resumes with
  a saved focus slot deep enough that jumping there is that session's very
  first reconcile, then checks every materialized row's recorded height
  against its real, current `winfo_height()`.

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
  `_fixed_text_box_height`'s rules; every stacked element but the last plus
  `GAP_BETWEEN_STACKED_PX`) and takes the taller of the two, rather than a
  flat constant. The margin/gap/row-overhead constants both sides need
  (`TEXT_BOX_MARGIN_PX`, `GAP_BETWEEN_STACKED_PX`, `ROW_FRAME_OVERHEAD_PX`)
  live in their own Tk-free `app/gui/layout_constants.py` module that both
  `row_building.py` (the real layout) and `virtualization.py` (the estimate)
  import - replacing an earlier design where each side hardcoded its own
  copy of the same numbers "kept in sync by hand," which is exactly the kind
  of drift that caused this mismatch in the first place.
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
  `_on_text_modified` now only calls `_scroll_box_into_view` when the edited
  box actually has focus, which a phantom build-time event never does.
- **Focus/cursor survive a row being torn down, not just edits.** A fast
  Page Up/Page Down burst can move the canvas several viewports between
  `_reconcile` passes (each debounced - see `DEBOUNCE_MS` - so a held key
  doesn't reconcile on every event), easily skipping past
  `SCROLL_BUFFER_VIEWPORTS`'s buffer and tearing down a row whose box
  currently has focus. `_destroy_row` now records that box's slot
  (`self._refocus_slot`) and exact cursor index (`self._saved_cursor`,
  alongside the existing `self._saved_texts`) before tearing it down;
  `_build_row` restores both, via `after_idle` rather than inline, if/when
  that index is rebuilt later - deferred so the restore's own
  scroll-into-view isn't immediately clobbered by `_reconcile`'s still-
  pending `_remeasure_built_rows` correction (which runs after
  `_sync_materialized_rows`/`_build_row` return, in the same `_reconcile`
  call, if this fired from inside it). Guarded on nothing else having since
  taken focus (`self._focused_slot() is None and self.focus_get() is not
  self._finalize_button`) - deliberately not a `self.focus_get() is None`
  check, since destroying a focused widget hands Tk's focus to an ancestor
  frame rather than clearing it, so it's never actually `None` by the time
  the guard runs. `self._saved_cursor` isn't part of the autosaved session
  format - only `self._saved_texts` is - so a resumed box's cursor still
  starts at `"1.0"`, same as before this.
- **A box's `UndoLog` must record what it actually started from, not assume
  it was `initial_text`.** (`text_undo.py`'s `UndoLog.baseline`, set in
  `row_building.py`'s `_populate_text_box`.) `_populate_text_box` has always
  had two branches: a box's *first* build this session (no `UndoLog` for its
  key yet) inserts `self._saved_texts.get(key)` if a resumed/in-session edit
  exists, else the item's plain `initial_text`; any *later* rebuild (the row
  was torn down and is being paged back in) replays the recorded ops onto a
  fresh widget instead. For a long time the rebuild branch re-based that
  replay on `initial_text` directly, silently assuming a fresh `UndoLog`'s
  ops were always deltas from `initial_text` - true for a box that started
  untouched, **false** for one that started from a resumed edit. That box's
  *very next* rebuild (an ordinary scroll-away-and-back, no further typing
  needed) discarded the resumed edit and replayed onto the bare default
  instead - with zero ops to replay in the common case, this reverted the
  box to its unedited OCR text with no trace, and the next autosave tick
  persisted that loss to disk. Real data loss, not theoretical: see the
  project owner's transcription work for a confirmed instance. Fixed by
  having `UndoLog` itself record `baseline` - whichever text the box's first
  build this session actually used - and having the rebuild branch replay
  onto `log.baseline`, never onto `initial_text` directly. Any future change
  to this method must preserve that: the only thing a rebuild may assume
  about a box's prior state is whatever the box's own `UndoLog` recorded,
  never the item's static default.
  `test_resumed_edit_survives_being_paged_out_and_back_in_with_no_further_
  edits` (`app_tests/test_review_view.py`) is the regression test - notably,
  the two narrower scenarios it combines (resume-then-use,
  edit-then-page-away-and-back) each already had their own passing test
  beforehand, and neither caught this: the bug only exists where both are
  true at once, which is exactly the gap a single new test combining them
  had to close, rather than expecting either existing test to generalize on
  its own.
- **Symbolic marks recorded in a `UndoLog` must be resolved before they can
  drift.** (`text_undo.py`'s `attach_undo_recording`.) A real bug: Tk's own
  built-in Text bindings remove a selection via the literal call `delete
  sel.first sel.last` (Delete/Backspace/typing-over-a-selection/Ctrl+X), not
  absolute positions - and the recording proxy used to log that call's raw
  Tcl arguments verbatim, so a box with a selection-delete in its history got
  `("delete", ("sel.first", "sel.last"))` permanently written into
  `UndoLog.ops`. `sel.first`/`sel.last` only mean anything while *that
  specific widget instance* has a live selection - replaying that op onto a
  freshly-built widget (row paged out and back in, or torn down and rebuilt
  for any other reason) with nothing selected raised `_tkinter.TclError:
  text doesn't contain any characters tagged with "sel"`, uncaught, from
  inside a Tk-bound callback (`_reconcile`, reached from both keyboard nav
  and the scroll-debounced path). Full analysis in
  `archive/INVESTIGATION_shift_tab_reconcile_lockup.md` - kept in full even
  though fixed, since the cross-feature interaction it traces
  (virtualization's row rebuild + undo replay) is worth having on record
  rather than re-deriving if a similar bug resurfaces. Every other in-code
  reference to this filename (row_building.py, review_view.py, text_undo.py,
  keyboard_nav.py, main.py, and their tests) cites it by bare filename only,
  without the `archive/` prefix - still unambiguous to grep for, and not
  worth touching that many call sites just to spell out a path.

  Fixed at the source: `_proxy`'s recording now resolves *every*
  insert/delete index argument - not just `sel.*`, since `insert`/`end`/any
  other mark is just as capable of meaning something different (or nothing)
  on a freshly-built widget - to an absolute `"line.column"` string via
  `tcl.call(shadow_path, "index", value)`, called *before* the real mutating
  call runs (a mark's meaning is only well-defined relative to the widget's
  state right before the mutation, not after). This makes every recorded op
  replay-safe by construction, the same way `UndoLog.baseline` (above) made
  a rebuild's *starting point* trustworthy by construction rather than by
  convention.

  A record-time fix alone can't guarantee there's no other, still-unknown
  way for a replay to fail - so three complementary hardenings shipped
  alongside it, all backstops rather than substitutes for the record-time
  fix (see `INVESTIGATION_shift_tab_reconcile_lockup.md`'s "Recommended
  fix"/"What's still open" sections):
  - `_populate_text_box`'s replay is wrapped in `try/except tk.TclError`. On
    failure, it recovers the box's actual last-known-good text from
    `self._saved_texts` (captured independently, at the box's last
    teardown - never `log.baseline`/`initial_text`, either of which can be
    staler than what the user actually left in the box) and *self-heals*:
    wipes the poisoned `log.ops` and re-baselines on the recovered text, so
    the same box doesn't crash again on its next rebuild.
  - `_reclaim_widget_if_present` (called from both
    `_build_editable_text_box` and `_build_spacer_text_box`, replacing what
    used to be just a logged warning) closes a real data-loss mechanism this
    investigation traced precisely: once `self._materialized_range` gets
    stuck (fallout from an uncaught build exception), a later reconcile
    could call `_build_row` for an index already live in
    `self._text_widgets`, silently orphaning that widget's content, since
    `_destroy_row` (the only place that captures `text_widget.get()` into
    `self._saved_texts`) never runs for it. Now it does, every time, before
    the key is overwritten.
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
  `app.log` and only ever reached an unlogged console, which is what made it
  take a full log-forensics-then-console-capture pass to even find (see the
  investigation doc's "Why nothing shows up in app.log"). Not specific to
  the `sel.first` bug either - it's a blanket safety net for whatever the
  *next* uncaught Tk-callback exception turns out to be.

- **A recorded undo/redo must not be replayed by calling `edit_undo()`/
  `edit_redo()` again.** (`text_undo.py`.) A real bug, distinct from the
  `sel.first` one above despite sharing the same file - see
  `INVESTIGATION_undo_redo_replay_divergence.md` for the full log forensics.
  `keyboard_nav.py`'s `_undo_text`/`_redo_text` used to append a bare
  `("undo", ())`/`("redo", ())` marker to `UndoLog.ops`, and `replay_onto`
  replayed it by literally calling `text_widget.edit_undo()`/`.edit_redo()`
  again on the rebuilt widget - on the theory that replaying the same
  insert/delete sequence would make Tk's own autoseparator logic re-derive
  the same undo-step grouping it used live, since that grouping is a
  deterministic function of the call sequence. False in practice: grouping
  also depends on things that never make it into `log.ops` at all - e.g.
  `_on_ocr_checkbox_toggle`'s own `edit_separator()` calls, which aren't
  insert/delete calls and so are invisible to this log - so a rebuilt
  widget's replay-time grouping isn't guaranteed to match the live grouping,
  and `edit_undo()` against a differently-grouped stack can revert a
  different *amount* of text than it did live. Worse than the `sel.first`
  bug in one way: that one crashed loudly (`TclError`), which - while bad -
  is at least conspicuous; this one raised and logged nothing, so the box
  just silently ended up holding different content than it held at
  teardown, which then got autosaved and could reach Finalize unnoticed.

  Fixed at the source, the same way `sel.first` was: a successful
  `edit_undo()`/`edit_redo()` is now recorded as a `"replace"` op -
  `widget`'s exact resulting text, captured live right after the call -
  instead of a bare marker, so replay never touches Tk's undo stack for this
  step at all; it's just another content mutation through the same
  insert/delete primitives every other op already uses. `replay_onto`
  brackets the replayed delete+insert with `edit_separator()`/
  `autoseparators` suppression, the same trick `_on_ocr_checkbox_toggle`
  already uses, so it lands as one atomic step on the rebuilt widget's own
  stack rather than splitting into two. The tradeoff: a *further* Ctrl+Z
  pressed after such a rebuild isn't guaranteed to have the same step
  boundaries it would have live (e.g. one Ctrl+Z landing on an intermediate
  empty state instead of a real prior one, recoverable with a second Ctrl+Z
  or a Redo) - a user-visible granularity difference, not silent content
  corruption.

  `_populate_text_box`'s replay branch also gained a second, broader
  regression check alongside the pre-existing "landed back on the item's
  bare default" alarm (left unchanged, including its exact message text, so
  `archive/INVESTIGATION_shift_tab_reconcile_lockup.md`'s grep instructions
  still work): it compares the replay's `result_text` directly against
  `self._saved_texts[key]` - the box's own content as of its last teardown
  (`review_view.py`'s `_destroy_row`, captured independently of whatever
  replay just produced) - and, on any mismatch, logs `ERROR`
  (`"replay result doesn't match this box's content as of its last
  teardown - possible silent replay divergence"`) and *self-heals*:
  overwrites the widget onto `self._saved_texts[key]` and re-baselines
  `log`, the same recovery `_populate_text_box`'s existing `TclError` guard
  already does. This is a detection-and-recovery backstop, not a substitute
  for the record-time fix above - it exists for whatever future
  replay-divergence mechanism this doesn't anticipate, the same relationship
  the `sel.first` fix's own backstops have to its record-time fix. Like the
  `TclError` recovery it mirrors, self-healing discards `log.ops`, so a
  Ctrl+Z pressed immediately after a self-heal event finds nothing to
  undo - preferable to silently wrong content, but still a real, visible
  difference from an ordinary rebuild.

- **Spellcheck tagging.** (`app/spellcheck.py`, wired in via
  `row_building.RowBuildingMixin._configure_spellcheck_tag`/
  `_schedule_spellcheck`/`_run_spellcheck`.) A misspelled word is underlined
  in red via a plain Tk text tag (`tag_configure("misspelled",
  underline=True, underlinefg=...)`) - a straight underline, since Tk has no
  wavy/squiggly underline primitive. First use of `tk.Text` tags anywhere in
  this codebase, which mattered for one reason: `text_undo.py`'s recording
  proxy (see "A box's UndoLog must record what it actually started from"
  above) only records `insert`/`delete` calls - `tag_add`/`tag_remove` pass
  through unrecorded, so spellcheck tagging can't corrupt or interact with
  undo history the way a naive content-touching approach might. The
  tradeoff: tags live on the `tk.Text` *instance*, not in any per-box
  bookkeeping dict, so they don't survive a row being torn down and rebuilt
  (a fresh widget) - `_build_editable_text_box` schedules a fresh spellcheck
  pass on every (re)build, not just the first, to compensate. Applied only
  to "message"/"ocr{N}" boxes - `_build_spacer_text_box` never calls into
  this, so a spacer box (holding nothing but `\n` tokens) is never a
  candidate for the tag.

  `find_misspelled_spans` also loads a second, complementary sidecar file -
  `spellcheck_blacklist.txt`, same one-word-per-line format and
  lazy-load-and-cache convention as the whitelist (`_get_blacklist`/
  `_blacklist`/`_blacklist_loaded`, mirroring `_get_whitelist`/`_whitelist`/
  `_whitelist_loaded`) - for real English words that the dictionary
  considers correctly spelled but that keep turning out to be OCR misreads
  or typos for something else in this transcript's context. A candidate word
  is flagged if it's either unrecognized by the dictionary *or* in the
  blacklist; a word in both the whitelist and the blacklist is never
  flagged, since the whitelist subtraction (`candidates = {...} -
  whitelist`) happens before the blacklist union is computed.

  Debounced per box (`SPELLCHECK_DEBOUNCE_MS`, via `text_widget.after`) so
  typing doesn't re-scan a box's text on every keystroke -
  `_destroy_row`/`_reclaim_widget_if_present` cancel a box's pending timer
  before tearing its widget down, the same defensive posture as everything
  else here that reaches back into an about-to-be-destroyed widget. That
  per-row cancellation isn't enough on its own, though: rows still
  materialized when the *whole* `ReviewFrame` goes away (screen switch, app
  close, or a test's `root.destroy()`) never go through `_destroy_row` at
  all, so their pending timers would otherwise leak. This surfaced
  immediately as a real, reproduced test failure once spellcheck shipped:
  `test_review_view.py`'s GUI tests build and tear down many `ReviewFrame`s
  (and their many text boxes) back to back in the same process, and Tcl's
  `after` timer queue turned out to be shared across every `tk.Tk()`
  interpreter in that process (per-thread, not per-interpreter) - so leaked
  timers from earlier tests piled up and measurably slowed a later test's
  own `update()`/`update_idletasks()` calls, enough to occasionally exhaust
  `_settle_pending_geometry`'s bounded retry count (see "A row's first-ever
  build can measure as winfo_height()==1" above) and leave that test's own
  first row never actually built. Fixed the same way
  `self._update_job`/`self._initial_position_job` already were: the
  `ReviewFrame`'s own `<Destroy>` handler now cancels every remaining entry
  in `self._spellcheck_after_ids` too, not just per-row teardown.
- **Slot-addressed boxes.** Since a row can now have a "message" box (a copy
  of the message's own text) and any number of OCR boxes - one per attached
  image, since a single message can have more than one - a plain item index
  is no longer enough to identify one box. Every per-box dict in
  `ReviewFrame` (`_text_widgets`, `_text_containers`, `_box_floor_px`,
  `_saved_texts`) is keyed by `(item_index, role)` instead, where `role` is
  `"message"` or `"ocr{N}"` (the Nth attached image's OCR box, 0-indexed in
  attachment order) - encoding the image index into the role string this
  way, rather than widening every key to a 3-tuple, kept the change confined
  to how `role` strings are generated/parsed rather than touching every
  dict's key shape. `self._slots` is the flat, transcript-ordered list of
  every `(item_index, role)` pair that exists across all items - built once
  in `__init__` from each item's `initial_message_text`/`image_paths` (a
  "message" slot whenever the former isn't None, then one `"ocr{i}"` slot
  per entry in the latter), message before every image's OCR slot. This is
  what Tab/Shift-Tab navigate (`keyboard_nav.py`'s `_move_focus`, stepping
  through `self._slots` by `self._slot_positions[slot]`) and what session
  resume's saved focus position addresses a box by - a plain item index
  couldn't disambiguate which of a row's boxes to refocus. `ImageLoader`
  mirrors this with its own `(item_index, image_index)`-keyed slots (see
  `app/gui/image_loading.py`), since a row can likewise now load/unload more
  than one image.
- **Per-row left-column sizing.** (`row_building.RowBuildingMixin`.) Every
  row's left column is the same fixed width (`THUMBNAIL_SIZE[0]` in
  `app/gui/image_loading.py`), whether it holds an image, the immutable
  original-text label, or both stacked text-above-image - so every row's
  column pairs line up neatly across the whole transcript. An image's height
  is its own aspect-preserving fit within `THUMBNAIL_SIZE`
  (`fitted_image_size`), not the full bounding box - otherwise a landscape
  image (the common case) gets letterboxed inside a box-shaped slot; this
  only reads the image file's header (cheap), separately from the actual
  lazy pixel decode in `ImageLoader._load_image` once a row scrolls near the
  viewport. The immutable label's height isn't known until the label exists,
  so `_build_immutable_message_label` measures it with the container's
  `pack_propagate` left on before pinning both dimensions, rather than
  computing it upfront the way `fitted_image_size` does for images.
- **Per-box text box sizing.** (`row_building.RowBuildingMixin`.) Each
  editable text box lives in its own fixed-height container
  (`pack_propagate(False)`, same trick as the left column's placeholders) so
  it doesn't stretch to fill whatever space is left via Tk's `fill="both"`.
  `_fixed_text_box_height` decides that height once, at build time, from a
  fixed rule rather than measuring the text's actual wrapped line count: its
  paired immutable element's own on-screen height (the label's, for a
  "message" box; the image's, for an "ocr" box) plus `TEXT_BOX_MARGIN_PX`,
  either way capped at `TEXT_BOX_MAX_HEIGHT_FRACTION` of the screen. An
  earlier version gave a "message" box a flat 3-line minimum instead,
  assuming most messages here are short text - in practice many ran to
  several lines, so that assumption is gone and both roles now use the same
  rule. A box gets an internal scrollbar that shows/hides itself
  automatically (`_set_text_scrollbar`, driven by the box's own
  `yscrollcommand`) whenever its content overflows that fixed height,
  whether from a long original message or from typing past it - the box
  itself never grows. This replaced an earlier design (`_size_text_container`,
  removed) that measured the text's current wrapped line count and resized
  the box to fit, re-running on every keystroke (`_on_text_modified`) - that
  made a row's true height unknowable until it was built and typed in,
  exactly the gap `_remeasure_built_rows` existed to correct, and a repeated
  source of this screen's scroll-position bugs. Fixing height to something
  knowable upfront - the same way an image's height already was, via
  `fitted_image_size`'s cheap header read - removes that correction's reason
  to exist instead of just estimating it more carefully.
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
  true end of the scrollregion, or trivially true for a transcript that fits
  on screen with nothing to scroll past) - called from `_reconcile` on every
  scroll-driven update and from `_scroll_box_into_view` so Tab'ing to the
  last box reveals it immediately rather than waiting on the next scroll
  event.
- **Per-box, not per-row, scroll-into-view.** `_scroll_box_into_view`
  (`keyboard_nav.py`) replaced an earlier `_scroll_into_view` that checked
  only a row's outer bounds against the viewport. A row can stack more than
  one box - a message's text box, one OCR box per attached image, and a
  spacer box between/after each (`_build_row`) - and can end up taller than
  the viewport itself, so the row-level check could find the row "already
  fully visible" (because some box within it was) while the specific box
  Tab/Shift-Tab had just focused, or the one the user was typing into, was
  still only partially onscreen - in the worst case almost entirely covered,
  with just a sliver poking into view, which the old check's row-level
  bounds didn't catch as a reason to scroll at all. Computed the same way
  `_keep_cursor_in_viewport`'s box bounds already were: `self._offset_of(index)`
  (the row's document-space offset) plus a `winfo_rooty()` delta for the
  box's offset *within* that row.
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
  row being torn down and rebuilt with no teardown/rebuild-specific plumbing
  of their own - they're written to only from live editing code (typing,
  the checkbox's own toggle, undo/redo), never read back from an
  about-to-be-destroyed widget the way `_saved_texts` is.

  Both dicts are seeded **eagerly in `ReviewFrame.__init__`**, for every
  `(idx, "ocr{i}")` slot across all items, not lazily the first time a row
  is built - `collect_edited_texts`/autosave loop over every item regardless
  of whether its row has ever been materialized this session (e.g. right
  after a resume far from that row), so the checked-state has to be knowable
  without requiring a build. The seeding rule - `checked = saved is not None
  and saved != default`, where `saved` comes from `self._saved_texts`
  (itself already seeded from a resumed session's `edited_texts` field) -
  also implements the project owner's chosen resume behavior for free: a
  resumed box with an edited version differing from its default is always
  shown checked, regardless of whether it happened to be checked or
  unchecked at the moment the session was last saved. This works because
  `ReviewFrame._get_box_text` (what `collect_edited_texts`/autosave/the
  session file actually read) reports `None` - not the box's live,
  OCR-default-matching content - for an `ocr*` role while unchecked, so a
  saved, non-default value can only ever mean "there's a real edit to
  surface." This doesn't change what Finalize ever writes (`None` already
  falls back to the same default text in `review_item.lines_for_item`),
  only what gets *persisted/reported* for an unchecked box.

  `_populate_text_box`'s existing baseline+replay logic (`row_building.py`)
  needed no changes to support any of this: a checkbox toggle's content swap
  (`RowBuildingMixin._on_ocr_checkbox_toggle`) is just another recorded
  delete/insert through the same undo-recording proxy every other edit goes
  through, so a row torn down mid-toggle and rebuilt later replays back to
  the right content automatically, the same way a resumed edit already did
  (see "A box's `UndoLog` must record what it actually started from"
  above).

  Two things needed new code, both reusing an existing mechanism rather than
  inventing a new one:
  - **Telling a real edit apart from a programmatic one.** `<<Modified>>`
    fires for the checkbox's own swap exactly like it does for typing (see
    "`<<Modified>>` fires on a box's initial population" above) - left
    unguarded, unchecking a box would immediately re-check itself via the
    same "any change checks the box" reaction real typing needs.
    `_on_text_modified`'s existing `had_focus` gate (already there to skip a
    build-time insert, which also fires this event) turns out to cover this
    case too for free: clicking the checkbox normally moves focus to it, not
    the text widget, so the swap's deferred `<<Modified>>` arrives with
    `had_focus` false. `_on_ocr_checkbox_toggle` additionally adds its key
    to `self._suppress_ocr_auto_check` around the swap as defense in depth,
    in case focus ever doesn't move as expected - and `keyboard_nav.py`'s
    `_undo_text`/`_redo_text` rely on that same set for real, since Ctrl+Z
    *does* run with the box focused: undo/redo always re-derives
    checked/unchecked by comparing the resulting text to the OCR default
    (`_resync_ocr_checkbox_after_undo`) rather than the unconditional "any
    change checks the box" rule ordinary typing uses, so undoing a toggle
    that lands exactly back on the OCR default correctly un-checks the box
    again instead of leaving it stuck checked.
  - **Making a toggle's delete+insert undo as one step.** A `tk.Text`
    widget's default `autoseparators` behavior inserts a separator on every
    insert-delete type transition - left alone, a toggle's `delete("1.0",
    "end")` followed by `insert("1.0", ...)` becomes *two* undo groups
    instead of one, so a single Ctrl+Z only reversed the insert half,
    landing on the empty post-delete/pre-reinsert text rather than back on
    whatever the toggle swapped away from. Worse, naively disabling
    `autoseparators` only around the delete+insert pair (with no boundary
    *before* it either) merged the toggle into whatever undo group preceded
    it, so a single Ctrl+Z undid the toggle *and* the user's last real edit
    together. `_on_ocr_checkbox_toggle` calls `edit_separator()` once
    *before* turning `autoseparators` off (sealing off whatever came
    before), then again right after re-inserting (sealing off whatever comes
    after) before turning `autoseparators` back on - bounding the
    delete+insert pair as exactly one atomic undo/redo step.

  Checkbox widgets themselves are plain `tk.Checkbutton`/`tk.BooleanVar` (not
  `ttk`, so each can be colored to blend into its own text box's background
  rather than sharing one global `ttk.Style`), `takefocus=0` so Tab/Shift-Tab -
  which already only navigate `self._slots`, never anything Tk's own default
  focus traversal would otherwise reach - skip over them with no further
  change needed. The checkbox sits inside an otherwise-invisible `tk.Frame`
  column packed `side="right"` into the box's `text_container`, built (and
  packed) before `text_widget` so it's earlier in the container's pack order
  and claims a slice off the right edge before `text_widget`'s
  `expand=True` claims everything still left - the same pack-order trick
  `_set_text_scrollbar`'s own `before=` argument already relies on for the
  scrollbar, just with one more widget in the chain: showing the scrollbar
  now has to insert it before the checkbox column, not just before
  `text_widget`, to land at the true right edge with the checkbox column
  directly to its left.
- **A popup `tk.Menu`'s close can't be detected via `<Unmap>` on Windows.**
  (`image_context_menu.py`'s `_show_image_context_menu`.) The right-click
  context menu on a review row's image (Open Image/Open Image in
  Browser/Open Image Location/Copy Image) freezes review-window scrolling
  (mousewheel/Page Up-Down/scrollbar - `ReviewFrame._scroll_frozen`, checked
  in `review_view.py`'s mousewheel/scrollbar handlers and
  `keyboard_nav.py`'s `_on_page_up`/`_on_page_down`) for as long as it's
  open, so scrolling can't move rows - and this menu's target image - out
  from under it. The first version unfroze via `menu.bind("<Unmap>", ...)`,
  assuming Tk would fire its ordinary widget-unmap event when the popup
  closed, the same way it does for a normal window being
  withdrawn/destroyed. A real, reported bug: that binding never fired for a
  real close on Windows, however it closed - clicking one of the three
  commands *did* unfreeze (each command's own callback ran, and the menu
  happening to close right after was incidental), but dismissing the menu
  with Escape or a click elsewhere left scrolling frozen forever, since
  nothing else ever reset `_scroll_frozen`. Root cause: on Windows,
  `tk.Menu`'s popup is implemented via the native `TrackPopupMenu` API
  rather than as an ordinary Tk-managed toplevel - so it never generates the
  `Unmap` event Tk's own binding machinery depends on, regardless of how
  it's dismissed.

  Fixed by not depending on any event at all: `TrackPopupMenu` blocks the
  call that posts it - `menu.tk_popup(...)` doesn't return until a person
  has actually dismissed the menu, one way or another - confirmed by hand,
  not just inferred, since this exact blocking is also what made an
  earlier, unattended version of this feature's own test suite hang with
  the real popup menu visible on screen until force-closed (see
  `test_image_context_menu.py`, which mocks `tk.Menu.tk_popup` for exactly
  this reason rather than ever calling the real thing). That blocking makes
  unfreezing in `_show_image_context_menu`'s own `finally` - right after
  `tk_popup(...)` returns - deterministic: by the time control gets there,
  the menu is already gone, whichever of the three ways it closed.
  `_on_image_context_menu_closed` (the unfreeze itself, plus its own log
  line) is a real bound method rather than a nested closure specifically so
  a test can call it directly without needing a real popup close to trigger
  it.
- **"Open in browser" needs the http protocol's default handler, not the
  image's own file-type association.** (`image_context_menu.py`'s
  `_open_image_in_browser`/`_default_browser_command`.) A real, reported
  bug: the first version opened a local `file://` URI via
  `webbrowser.open()`, assuming that would launch the user's browser -
  instead it opened Windows Photos, confirmed by hand. Root cause: neither
  `webbrowser.open()` nor a plain `os.startfile()` on a local path/`file://`
  URI actually consult "what's the default browser" - both resolve through
  the file's *extension* association instead (`.png` → Photos), the same
  lookup `os.startfile()` alone already does for the separate, deliberately
  plain **Open Image** action added alongside this fix. The default
  *browser* is a completely different piece of registry state (the http
  protocol's own `UserChoice`), which never gets consulted by either API for
  a local file path no matter how it's phrased.

  Fixed by reading that association directly: `_default_browser_command`
  does the same two-step registry lookup Explorer itself does to resolve
  "open with default browser" -
  `HKEY_CURRENT_USER\...\UrlAssociations\http\UserChoice`'s `ProgId` value,
  then that `ProgId`'s own `shell\open\command` value - and returns the
  resulting raw command line (e.g. `"C:\Program Files\Mozilla
  Firefox\firefox.exe" -osint -url "%1"`). `_open_image_in_browser`
  substitutes the image's `file://` URI for the literal `%1` placeholder
  (`shlex.split` first, so a quoted path containing spaces splits into one
  argument correctly) and launches the result directly via `subprocess.run`,
  bypassing file-type association entirely. Either registry step failing
  (no `UserChoice` set, or a `ProgId` left over from a since-uninstalled
  browser) makes `_default_browser_command` return `None` rather than
  raise, which `_open_image_in_browser` turns into a `RuntimeError` -
  letting `_run_image_menu_action`'s existing try/except log it as an
  ordinary action failure rather than needing its own special-cased
  handling.
