# Review screen internals

Part of [ARCHITECTURE.md](ARCHITECTURE.md) - see there for the general
testing heuristic this codebase follows, and for links to the other topic
docs (row geometry, spacer slots, testing, logging).

The review screen is the most architecturally involved part of this app.
`ReviewFrame` (`discord_transcription/gui/review_view.py`) doesn't do the
work itself: it creates one object per concern and wires them together,
each getting what it needs through its constructor. Each module's
docstring is the canonical explanation of that part; this is just enough to
orient a new contributor.

| Object | Module | Owns |
|---|---|---|
| `VirtualRows` | `virtual_rows.py` | The canvas and scrollbar, every row's height (`heights`), which rows are built (`reconcile`/`_sync_materialized_rows`/`_remeasure_built_rows`/`offset_of`/`ensure_materialized`/`destroy_row`), debouncing, the scroll freeze, and the scroll trace (`log_event`). Knows nothing about what a row contains. |
| `RowBuilder` | `row_building.py` | Building one row's widgets (`fill_row`), each content box's fixed height, the image column width, and the pre-build height estimates (`estimate_heights`). |
| `SlotBoxes` | `slot_boxes.py` | Every editable box's `SlotState` (`states`) and, while its row is built, its `SlotView` (`views`): building boxes, syncing edits, the OCR checkbox, spellcheck, undo/redo, and reporting edits for autosave/Finalize. |
| `FocusNavigator` | `keyboard_nav.py` | The slot order (`slots`), Tab/Shift-Tab, keeping the focused box on screen, and restoring focus across a row rebuild. |
| `ColumnDivider` | `column_divider.py` | The image/text divider, its drag, and re-laying out the rows at a new width. |
| `ImageContextMenu` | `image_context_menu.py` | The right-click image menu and its actions. |
| `ImageLoader` | `image_loading.py` | Lazily loading/unloading the built rows' images. |

`ReviewFrame` itself keeps the scroll input handlers (wheel, Page Up/Down),
the floating Finalize button, the first layout (`_apply_initial_position`),
and the methods `App` calls (`collect_edited_texts`, `get_focused_slot`,
...). Callbacks connect the pieces: `VirtualRows` asks `ReviewFrame` to fill
or release a row (`_fill_row`/`_on_row_destroying`), and `SlotBoxes` asks
it to bind navigation keys on a new box and to keep a focused box in view.

The windowing core stays together in one object: it's the state that once
disagreed with itself (see "Row virtualization" below), and splitting it
further would relocate that risk, not remove it.

- **Row virtualization.** Only a small window of rows (around the visible
  viewport) is ever built as real Tk widgets - `VirtualRows.reconcile`
  recomputes that window from scratch on every scroll tick as a pure
  function of scroll position and each row's recorded height
  (`VirtualRows.heights`), and reconciling is idempotent (calling it twice
  with no scroll movement is a no-op). That idempotency is deliberate - an
  earlier, stateful "step the window forward/backward" design could fall
  into a self-sustaining oscillation loop; see the module docstring for the
  full story. Pure layout math lives in `discord_transcription/gui/virtualization.py` so it's
  testable without a display.
- **Nothing may repaint while the materialized block is out of place.**
  All built rows are packed into one frame, positioned on the canvas at
  its first row's offset. Destroying rows above shifts everything left in
  the frame up by their height, and building rows above shifts it down, so
  `canvas.coords` has to move the frame to match *before* anything flushes
  Tk's idle queue - `update_idletasks()` repaints the screen. It's moved
  right after the teardown in `_sync_materialized_rows` and again right
  after the builds in `VirtualRows.reconcile`. Row building itself must not flush
  the idle queue either: `RowBuilder._build_message_label` used to call
  `update_idletasks()` to measure its label, which repainted the
  half-rebuilt screen once per message row and was the cause of the
  visible jumping/flicker when scrolling down.
- **The pre-build height estimate isn't just cosmetic.**
  `virtualization.estimate_row_height`'s guess for an image row used to
  assume the image filled the full `THUMBNAIL_SIZE` bounding box - but most
  images here are landscape (width-, not height-, constrained), so the real
  fitted height is usually far less, and the guess overestimated most rows
  by 500+px. That got corrected once a row was actually built
  (`VirtualRows._remeasure_built_rows` shifts the scroll offset to keep
  on-screen content stable when a row's real height differs from its
  estimate) - but the correction itself showed up as a scroll jump
  disconnected from the user's actual scroll input, worse the bigger the
  estimate error. `estimate_row_height` now calls the same cheap,
  header-only `fitted_image_size` read already used to size the real
  placeholder, instead of a flat constant.
- **The pre-build estimate also has to mirror the real layout's cap, not
  just its content-driven size.** A content box's real height
  (`RowBuilder.fixed_text_box_height`) is capped at
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
  before `VirtualRows.offset_of` ever needs their height again - a jump skips that
  remeasurement for whatever it jumps over, so any long row in the skipped
  range keeps contributing its uncapped overestimate to every later row's
  document-space offset indefinitely (until something eventually builds it).
  `estimate_row_height` now takes an optional `max_text_box_height_px` and
  caps each content role's right-column contribution the same way
  `RowBuilder.fixed_text_box_height` does; `RowBuilder.estimate_heights`
  passes its own `max_text_box_height_px()` - which is also why the row-heights list
  is built only *after* `VirtualRows.canvas` exists, not before, so that call's own
  not-yet-laid-out/`winfo_screenheight()` fallback applies the same way it
  would for any other premature call to it.
- **Text sizes are measured, not hardcoded.** How big the text font really
  draws depends on which font Tk resolves (Consolas is substituted on most
  Linux systems) and on the display's DPI scaling, so fixed pixel values
  can be off by 2x or more. `ReviewFrame.__init__` calls
  `row_building.measure_text_metrics` once, which reads the font's
  character width and line height and the requested heights of a
  throwaway original-text label and spacer box, built by the same helpers
  (`_make_original_text_label`, `slot_boxes.make_spacer_text_widget`) the real rows
  use. The resulting `TextMetrics` feeds `estimate_row_height` and sets
  every spacer box's fixed height, so a spacer box can't clip its own text
  (it did while that height was a hardcoded 30px). Measure with
  `tkfont.Font(family=..., size=...)`, not `tkfont.Font(font=(family,
  size))`: the latter over-scales on some displays and reports a larger font
  than the widgets draw.
  `test_jumping_focus_past_a_capped_long_message_row_lands_target_fully_in_view`
  (`app_tests/test_review_view.py`) is the regression test - it puts one
  long, capped-height row outside the window built at startup and jumps
  straight past it to a target further down, the same way a resumed
  session's saved focus slot would.
- **A row's first-ever build can measure as `winfo_height()==1` even right
  after `canvas.update_idletasks()`.** A real bug: resuming a session whose
  saved focus slot is deep in the transcript jumps straight there
  (`VirtualRows.ensure_materialized`), which materializes that whole window of rows in
  the session's very *first* `VirtualRows.reconcile` call - a deeply nested `ttk.Frame`
  tree, several levels deep, none of which have ever been mapped to the
  screen before. `update_idletasks()` only drains Tcl's idle queue (what
  pack's own size negotiation runs on), not the window-system `Map` event a
  widget needs before `winfo_height()` reports anything real - and that
  event doesn't always arrive within a single idle-queue pass for that much
  brand-new tree at once. `_remeasure_built_rows` used to take
  `winfo_height()`'s bogus `1` at face value, permanently writing
  `2*ROW_PACK_PADY_PX` (9px) into `VirtualRows.heights` for every row in that
  first window - and since a row already in `VirtualRows.row_frames` is never
  rebuilt (so never remeasured) just because a later `VirtualRows.reconcile` runs, that
  9px-per-row corruption then threw `VirtualRows.offset_of` off by hundreds of px
  for every row after it, for the rest of the session, with no further
  chance to self-correct - the user-visible symptom was Tab/Shift-Tab's
  scroll-into-view looking completely broken from the moment a deep resume
  opened, confirmed against real `scroll_trace.log` output (every row in
  that first reconcile's `remeasure_mismatch` events reading `real_height:
  9`, every later reconcile reading correctly). Every *later* reconcile
  measures correctly on the first try, since by then the canvas has already
  been mapped once - this is specifically a first-reconcile problem.

  `VirtualRows.reconcile` now calls `_settle_pending_geometry` (`virtual_rows.py`) right
  after `update_idletasks()` and before trusting any measurement: it retries
  `update_idletasks()` a bounded number of times first (cheap, no
  event-processing side effects, covers the ordinary "pack is still
  settling" case), then falls back to a bounded number of full `update()`
  calls if that wasn't enough. `update()` - unlike `update_idletasks()` -
  drains *all* pending events, not just idle callbacks, which is what
  actually unblocks the stuck `Map`; confirmed by a standalone repro that
  `update_idletasks()` alone never resolves it, no matter how many retries,
  while a single `update()` does. `update()` can call back into `VirtualRows.reconcile`
  itself before returning (e.g. an already-scheduled debounced reconcile
  from the canvas's first `<Configure>` event) - expected and *not* guarded
  against: an earlier version added a reentrancy flag that made such a
  nested call a no-op, on the theory it could rebuild/tear down rows out
  from under the in-progress outer call. That theory didn't survive contact
  with the repro - guarding the reentrancy back out reproduced the exact
  bug, because the nested call's own geometry-touching work (re-issuing
  `canvas.configure(scrollregion=...)`/`canvas.coords`) turned out to be
  what actually finishes flushing the stuck `Map`, not incidental to it.
  `VirtualRows.reconcile`/`_sync_materialized_rows` are already written to be
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
  `RowBuilder.fill_row` actually lays it out (left: label and/or images, stacked;
  right: message box and/or one OCR box per image, mirroring
  `RowBuilder.fixed_text_box_height`'s rules; every stacked element but the last plus
  `GAP_BETWEEN_STACKED_PX`) and takes the taller of the two, rather than a
  flat constant. The margin/gap/row-overhead constants both sides need
  (`TEXT_BOX_MARGIN_PX`, `GAP_BETWEEN_STACKED_PX`, `ROW_FRAME_OVERHEAD_PX`)
  live in their own Tk-free `discord_transcription/gui/layout_constants.py` module that both
  `row_building.py` (the real layout) and `virtualization.py` (the estimate)
  import - replacing an earlier design where each side hardcoded its own
  copy of the same numbers "kept in sync by hand," which is exactly the kind
  of drift that caused this mismatch in the first place.
- **`<<Modified>>` fires on a box's initial population, not just real user
  edits.** `SlotBoxes.build_content_box` inserts a box's initial text and
  immediately calls `edit_modified(False)`, intending to stop that insert
  from being treated as a user edit by `_on_text_modified` - but Tk queues
  `<<Modified>>` for the next idle tick rather than firing it synchronously,
  so the binding (attached a few lines later, before control returns to the
  event loop) still catches it. That meant every newly-built row - including
  ones built only because they entered the virtualization buffer
  (`SCROLL_BUFFER_VIEWPORTS`), not because the user actually scrolled them
  into view - triggered `_on_text_modified`'s `FocusNavigator.scroll_box_into_view`,
  yanking the canvas to reveal a row the user hadn't scrolled to.
  `_on_text_modified` now only calls `FocusNavigator.scroll_box_into_view` when the edited
  box actually has focus, which a phantom build-time event never does.
- **Focus/cursor survive a row being torn down, not just edits.** A fast
  Page Up/Page Down burst can move the canvas several viewports between
  `VirtualRows.reconcile` passes (each debounced - see `DEBOUNCE_MS` - so a held key
  doesn't reconcile on every event), easily skipping past
  `SCROLL_BUFFER_VIEWPORTS`'s buffer and tearing down a row whose box
  currently has focus. Before `VirtualRows.destroy_row` tears the row down,
  `ReviewFrame._on_row_destroying` records that box's slot
  (`FocusNavigator.refocus_slot`), and every box's cursor index goes into its
  `SlotState.cursor`; `FocusNavigator.restore_focus_after_build` restores
  focus via `after_idle` rather than inline, if/when that index is rebuilt
  later - deferred so the restore's own scroll-into-view isn't immediately
  clobbered by `VirtualRows.reconcile`'s still-pending `_remeasure_built_rows`
  correction (which runs after `_sync_materialized_rows`/`RowBuilder.fill_row`
  return, in the same `VirtualRows.reconcile` call, if this fired from inside it).
  Guarded on nothing else having since taken focus
  (`self._focused_slot() is None and self.focus_get() is not
  self._finalize_button`) - deliberately not a `self.focus_get() is None`
  check, since destroying a focused widget hands Tk's focus to an ancestor
  frame rather than clearing it, so it's never actually `None` by the time
  the guard runs. The cursor isn't part of the autosaved session format, so
  a resumed box's cursor still starts at `"1.0"`.
- **Autosave can't ask Tk what's focused.** Tk's `focus_get()` returns
  `None` whenever this app isn't the active window - and on the project
  owner's desktop that's already true by the time the window-close handler
  runs its final save. Asking it at save time recorded no focused box on 18
  of 22 closes in one day's `app.log`, so resuming rarely restored focus.
  `get_focused_slot` falls back to `FocusNavigator.last_focused_slot`, which every
  box's `<FocusIn>` (and `FocusNavigator.focus_text_box`, since FocusIn only arrives
  once the app is active) records, and the Finalize button's `<FocusIn>`
  clears. The resume's scroll-fraction fallback had its own trap: it called
  `yview_moveto` before the canvas had a scrollregion, which Tk silently
  ignores (the same trap `VirtualRows.ensure_materialized` documents), so it now sets
  the scrollregion first. Regression tests:
  `test_focus_survives_app_losing_focus_save_and_resume` and
  `test_saved_scroll_fraction_is_restored_when_no_box_was_focused`.
- **The per-box model lives outside the widgets.** (`discord_transcription/gui/slot_state.py`,
  `discord_transcription/gui/slot_view.py`, `discord_transcription/gui/edit_history.py`.) Each editable box has
  two halves, both keyed by `(item_index, role)`:
  - `SlotBoxes.states` holds one `SlotState` per box, built eagerly (in
    `SlotBoxes.__init__`) for every slot: its default text, current text,
    cursor, undo/redo history, whether the user touched it this session,
    and (for an "ocr" box) its checkbox state and hidden user edit.
  - `SlotBoxes.views` holds one `SlotView` per *currently built* box: its
    Text widget, container, checkbox variable and pending spellcheck timer.
    `SlotBoxes._release_view` is the one place a view is unregistered (row
    teardown and `SlotBoxes._reclaim_if_present` both use it): it syncs the
    SlotState from the widget, records the cursor and cancels the timer.

  The widgets are just a view of the SlotState. Three rules keep the two in
  step:
  - **A (re)build is always the same:** `SlotBoxes._populate` inserts
    `SlotState.text` and restores the cursor. There is no separate
    "first build" versus "rebuild" path, and nothing is replayed.
  - **Widget to model:** `SlotBoxes.sync_from_widget` compares the widget's
    text with `SlotState.text`. It runs on every `<<Modified>>` event, and
    also before anything reads or replaces a box's text (teardown,
    `SlotBoxes.reported_text`, undo/redo, the checkbox), because `<<Modified>>` arrives
    on a later idle tick than the edit itself. A difference is a user edit:
    it's recorded in the history, marks the slot touched, and ticks an
    "ocr" box's checkbox.
  - **Model to widget:** the app's own writes (undo/redo, the checkbox
    swap) go through `SlotBoxes.set_text`, which updates `SlotState.text`
    *before* touching the widget. The `<<Modified>>` event that write
    causes then finds no difference, so it is never mistaken for a user
    edit - no suppression flags needed.

  Undo/redo uses the box's own `EditHistory`, a stack of full-text
  snapshots capped at `MAX_UNDO_STEPS`, not Tk's (the widgets are created
  with `undo=False`). Because it never lives on a widget, a teardown and
  rebuild can't change it: the property tests in `test_review_view.py`
  check that repeated Ctrl+Z after a rebuild walks through exactly the
  texts it would have without one. `EditHistory` decides where undo steps
  start and end itself (by word, pause, insert/delete switch, cursor jump;
  paste/cut/checkbox toggles are always their own step - see its module
  docstring), and those rules are unit-tested without a display in
  `test_edit_history.py`. After an undo/redo the cursor goes to the change
  (`cursor_after_change`).

  This replaced a design that kept Tk's own undo stack alive by recording
  every insert/delete through a renamed Tcl command and replaying them onto
  each rebuilt widget. Three bugs shipped in that replay (a resumed edit
  lost on rebuild, a `sel.first` mark that crashed replay, and undo/redo
  replaying a different amount of text) - see ARCHITECTURE.md and
  `archive/INVESTIGATION_shift_tab_reconcile_lockup.md` /
  `archive/INVESTIGATION_undo_redo_replay_divergence.md`. None of them can
  happen with snapshots: there is nothing recorded from a widget that could
  mean something different on another widget.

  Two backstops from that era remain, since they guard the virtualization
  core rather than undo: `SlotBoxes._reclaim_if_present` (a box built while an
  old widget for the same key is still registered syncs the old one into
  its SlotState and destroys it, rather than orphaning it), and
  `virtual_rows.py`'s `_try_build_row` (one row's build failure is logged
  and skipped instead of aborting the rest of the reconcile batch).
  `main.py`'s `root.report_callback_exception` also still routes any
  uncaught Tk-callback exception into `app.log`.
- **Spellcheck tagging.** (`discord_transcription/spellcheck.py`, wired in via
  `slot_boxes._configure_spellcheck_tag`/
  `SlotBoxes.schedule_spellcheck`/`SlotBoxes.run_spellcheck`.) A misspelled word is underlined
  in red via a plain Tk text tag (`tag_configure("misspelled",
  underline=True, underlinefg=...)`) - a straight underline, since Tk has no
  wavy/squiggly underline primitive. First use of `tk.Text` tags anywhere in
  this codebase. Tags don't change a box's text, so they never show up as an
  edit or in undo history. The tradeoff: tags live on the `tk.Text`
  *instance*, not in the box's SlotState, so they don't survive a row being torn down and rebuilt
  (a fresh widget) - `SlotBoxes.build_content_box` schedules a fresh spellcheck
  pass on every (re)build, not just the first, to compensate. Applied only
  to "message"/"ocr{N}" boxes - `SlotBoxes.build_spacer_box` never calls into
  this, so a spacer box (holding nothing but `\n` tokens) is never a
  candidate for the tag.

  `find_misspelled_spans` also loads a second, complementary sidecar file -
  `spellcheck_blacklist.txt`, same one-word-per-line format and
  load-and-cache convention as the whitelist (`_get_blacklist`, mirroring
  `_get_whitelist`; both re-read their file when its modification time
  changes) - for real English words that the dictionary
  considers correctly spelled but that keep turning out to be OCR misreads
  or typos for something else in this transcript's context. A candidate word
  is flagged if it's either unrecognized by the dictionary *or* in the
  blacklist; a word in both the whitelist and the blacklist is never
  flagged, since the whitelist subtraction (`candidates = {...} -
  whitelist`) happens before the blacklist union is computed.

  Debounced per box (`SPELLCHECK_DEBOUNCE_MS`, via `text_widget.after`) so
  typing doesn't re-scan a box's text on every keystroke -
  `VirtualRows.destroy_row`/`SlotBoxes._reclaim_if_present` cancel a box's pending timer
  before tearing its widget down, the same defensive posture as everything
  else here that reaches back into an about-to-be-destroyed widget. That
  per-row cancellation isn't enough on its own, though: rows still
  materialized when the *whole* `ReviewFrame` goes away (screen switch, app
  close, or - in `test_review_view.py` - the per-test teardown fixture
  explicitly destroying that test's frame, since its `root` is now shared
  across the whole module rather than recreated per test) never go through
  `VirtualRows.destroy_row` at all, so their pending timers would otherwise leak. This surfaced
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
  `ReviewFrame`'s own `<Destroy>` handler now cancels every still-built
  box's pending timer (`SlotView.cancel_spellcheck`) too, not just per-row
  teardown.
- **Slot-addressed boxes.** Since a row can now have a "message" box (a copy
  of the message's own text) and any number of OCR boxes - one per attached
  image, since a single message can have more than one - a plain item index
  is no longer enough to identify one box. Every per-box structure
  (`SlotBoxes.states`, `SlotBoxes.views`)
  is keyed by `(item_index, role)` instead, where `role` is
  `"message"` or `"ocr{N}"` (the Nth attached image's OCR box, 0-indexed in
  attachment order) - encoding the image index into the role string this
  way, rather than widening every key to a 3-tuple, kept the change confined
  to how `role` strings are generated/parsed rather than touching every
  dict's key shape. `self._slots` is the flat, transcript-ordered list of
  every `(item_index, role)` pair that exists across all items - built once
  in `__init__` from each item's `initial_message_text`/`image_paths` (a
  "message" slot whenever the former isn't None, then one `"ocr{i}"` slot
  per entry in the latter), message before every image's OCR slot. This is
  what Tab/Shift-Tab navigate (`keyboard_nav.py`'s `FocusNavigator.move_focus`, stepping
  through `self._slots` by `self._slot_positions[slot]`) and what session
  resume's saved focus position addresses a box by - a plain item index
  couldn't disambiguate which of a row's boxes to refocus. `ImageLoader`
  mirrors this with its own `(item_index, image_index)`-keyed slots (see
  `discord_transcription/gui/image_loading.py`), since a row can likewise now load/unload more
  than one image.
- **Per-row left-column sizing.** (`row_building.RowBuilder`.) Every
  row's left column is the same width (`RowBuilder.image_column_width_px`,
  set by the column divider - see below), whether it holds an image, the immutable
  original-text label, or both stacked text-above-image - so every row's
  column pairs line up neatly across the whole transcript. An image's height
  is its own aspect-preserving fit within `image_bounding_box(width)`
  (`fitted_image_size`), not the full bounding box - otherwise a landscape
  image (the common case) gets letterboxed inside a box-shaped slot; this
  only reads the image file's header (cheap), separately from the actual
  lazy pixel decode in `ImageLoader._load_image` once a row scrolls near the
  viewport. The immutable label's height isn't known until the label exists,
  so `RowBuilder._build_message_label` reads the label's own
  `winfo_reqheight()` (valid as soon as it's configured) before pinning
  both dimensions, rather than computing it upfront the way
  `fitted_image_size` does for images.
- **The column divider re-lays out through the rebuild path.**
  (`column_divider.py`'s `ColumnDivider`.) The image column's width
  sets every row's height (images are fitted to it, and the original-text
  label wraps at it), so changing it can't be patched onto the built rows
  alone - rows that aren't built have estimated heights that depend on it
  too. `ColumnDivider.set_width` treats it like a far scroll jump: it
  records an anchor (the focused box's top edge if it's in view, otherwise
  the top row's), tears down every built row, re-estimates every row's
  height at the new width, scrolls so the anchor's *estimated* position is
  back at the same screen offset, reconciles, and then repeats that scroll
  against the rebuilt rows' *real* geometry and reconciles again. It adds
  no second sizing path: `RowBuilder.fill_row` and `estimate_row_height` both just
  take the width, and the per-box model carries edits, cursor, undo
  history and focus across the rebuild the same way it does for scrolling.
  Recomputing every row's estimate stays cheap because
  `image_loading._natural_size` caches each image's header size, so a new
  width is plain arithmetic per image.

  The divider is a `tk.Frame` `place()`d over the canvas (`in_=canvas`, so
  its x is in the rows' own coordinates) at `divider_x_for_width`, centered
  in the `2 * COLUMN_PADX_PX` gap between the columns. That position is
  derived from the same layout constants `RowBuilder.fill_row` packs with
  (`ROW_PACK_PADX_PX`, the row Frame's border/padding, `COLUMN_PADX_PX` -
  combined as `IMAGE_COLUMN_LEFT_PX`), and
  `test_column_divider_sits_in_the_gap_between_the_columns` checks it
  against the real widgets. Dragging only moves the divider; the relayout
  runs on release. The width is stored as a fraction of the canvas width,
  so a window resize (debounced `<Configure>`, `RESIZE_DEBOUNCE_MS`) keeps
  the proportion, and `ReviewFrame`'s caller saves it per chatlog
  (`state.save_image_column_fraction`). Clamping
  (`virtualization.clamp_image_column_width`) keeps both columns above
  their minimums, the image column's winning if the window is too narrow
  for both. A relayout costs about the same as a far scroll jump (roughly
  half a second at 1900x1000 in a profile, nearly all of it Tk laying out
  and painting the new rows), which is why a live-while-dragging relayout
  would need a cheaper path than a full rebuild.
- **Per-box text box sizing.** (`row_building.RowBuilder`.) Each
  editable text box lives in its own fixed-height container
  (`pack_propagate(False)`, same trick as the left column's placeholders) so
  it doesn't stretch to fill whatever space is left via Tk's `fill="both"`.
  `RowBuilder.fixed_text_box_height` decides that height once, at build time, from a
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
- **Horizontal wheel events must not scroll vertically.** Tk 8.6 on X11
  delivers horizontal scrolling (buttons 6/7, e.g. a touchpad's sideways
  drift during a two-finger scroll) as Shift+Button-4/5, and a plain
  `<Button-4>`/`<Button-5>` binding matches those too. Treated as vertical,
  that drift cancelled out most downward scrolling. `wheel.wheel_delta`
  returns 0 for any Shift-modified wheel event, and the `input_mousewheel`
  trace event records each event's raw `num`/`state` so this kind of
  problem is visible directly in `scroll_trace.log`.
- **Per-row scroll redirection.** The mouse wheel is bound globally
  (`canvas.bind_all` for every `wheel.WHEEL_EVENT_SEQUENCES` entry), but the bound callback still
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
  on screen with nothing to scroll past) - called from `VirtualRows.reconcile` on every
  scroll-driven update and from `FocusNavigator.scroll_box_into_view` so Tab'ing to the
  last box reveals it immediately rather than waiting on the next scroll
  event.
- **Per-box, not per-row, scroll-into-view.** `FocusNavigator.scroll_box_into_view`
  (`keyboard_nav.py`) replaced an earlier `_scroll_into_view` that checked
  only a row's outer bounds against the viewport. A row can stack more than
  one box - a message's text box, one OCR box per attached image, and a
  spacer box between/after each (`RowBuilder.fill_row`) - and can end up taller than
  the viewport itself, so the row-level check could find the row "already
  fully visible" (because some box within it was) while the specific box
  Tab/Shift-Tab had just focused, or the one the user was typing into, was
  still only partially onscreen - in the worst case almost entirely covered,
  with just a sliver poking into view, which the old check's row-level
  bounds didn't catch as a reason to scroll at all. Computed the same way
  `FocusNavigator.keep_cursor_in_viewport`'s box bounds already were: `VirtualRows.offset_of(index)`
  (the row's document-space offset) plus a `winfo_rooty()` delta for the
  box's offset *within* that row.
- **Per-OCR-box edited/checkbox state.** Every "ocr" box (one per attached
  image - never a "message" box, which was never OCR'd and so has no
  "original" to revert to) has a checkbox tracking "edited vs. not" and
  letting the user toggle between the original (regex-corrected) OCR
  transcription and their own edit without losing either. Its state is in
  the box's SlotState: `checked`, and `user_edit` (the last user-edited
  version, kept distinct from whatever the box currently *displays* - the
  OCR default, while unchecked). Like the rest of the SlotState, both
  survive a row being torn down and rebuilt with no extra plumbing.

  The seeding rule in `slot_boxes.initial_slot_states` - checked exactly
  when the seeded text (a resumed session's edit, else a finalized edit)
  differs from the OCR default - also implements the project owner's
  chosen resume behavior: `SlotBoxes.reported_text` (what `collect_edited_texts`,
  autosave and the session file read) reports `None` for an unchecked
  "ocr" box, so a saved, non-default value can only ever mean "there's a
  real edit to surface". An edit hidden behind an unchecked box is kept
  only while the app stays open.

  Three rules decide the checked state:
  - **Any user edit ticks it** (`_on_ocr_box_user_edit`, called from
    `SlotBoxes.sync_from_widget`), even if the new text happens to match the
    default again.
  - **Clicking it swaps the text** (`SlotBoxes.on_ocr_checkbox_toggle`): unchecking
    shows the OCR default, checking shows `user_edit` again. The swap is one
    undo step of its own (`EditHistory.record(..., standalone=True)`). The
    new checked state is read before syncing any pending edit, since that
    sync would otherwise tick the box straight back.
  - **Undo/redo re-derives it** (`keyboard_nav.py`'s
    `_resync_ocr_checkbox_after_undo`): checked exactly when the resulting
    text differs from the OCR default. So undoing an untick brings back both
    the edit and the tick, and undoing a box's only edit unticks it.

  Checkbox widgets themselves are `ttk.Checkbutton`/`tk.BooleanVar`,
  `takefocus=0` so Tab/Shift-Tab - which already only navigate
  `self._slots`, never anything Tk's own default focus traversal would
  otherwise reach - skip over them with no further change needed. The
  checkbox sits inside an otherwise-invisible `ttk.Frame` column packed
  `side="right"` into the box's `text_container`, built (and packed) before
  `text_widget` so it's earlier in the container's pack order and claims a
  slice off the right edge before `text_widget`'s `expand=True` claims
  everything still left - the same pack-order trick `_set_text_scrollbar`'s
  own `before=` argument already relies on for the scrollbar, just with one
  more widget in the chain: showing the scrollbar now has to insert it
  before the checkbox column, not just before `text_widget`, to land at the
  true right edge with the checkbox column directly to its left.
- **A popup `tk.Menu`'s close can't be detected via `<Unmap>` on Windows.**
  (`image_context_menu.py`'s `ImageContextMenu.show`.) The right-click
  context menu on a review row's image (Open Image/Open Image in
  Browser/Open Image Location/Open Chatlog at Message/Copy Image) freezes
  review-window scrolling
  (mousewheel/Page Up-Down/scrollbar - `VirtualRows.frozen`, checked
  by `VirtualRows`'s scrollbar handler and `ReviewFrame`'s
  mousewheel and `_on_page_up`/`_on_page_down` handlers) for as long as it's
  open, so scrolling can't move rows - and this menu's target image - out
  from under it. The first version unfroze via `menu.bind("<Unmap>", ...)`,
  assuming Tk would fire its ordinary widget-unmap event when the popup
  closed, the same way it does for a normal window being
  withdrawn/destroyed. A real, reported bug: that binding never fired for a
  real close on Windows, however it closed - clicking one of the three
  commands *did* unfreeze (each command's own callback ran, and the menu
  happening to close right after was incidental), but dismissing the menu
  with Escape or a click elsewhere left scrolling frozen forever, since
  nothing else ever reset `VirtualRows.frozen`. Root cause: on Windows,
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
  unfreezing in `ImageContextMenu.show`'s own `finally` - right after
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
  argument correctly) and launches the result directly via
  `subprocess.Popen` (not `run`, which would freeze the UI until the
  browser exits), bypassing file-type association entirely. Either registry step failing
  (no `UserChoice` set, or a `ProgId` left over from a since-uninstalled
  browser) makes `_default_browser_command` return `None` rather than
  raise, which `_open_image_in_browser` turns into a `RuntimeError` -
  letting `_run_image_menu_action`'s existing try/except log it as an
  ordinary action failure rather than needing its own special-cased
  handling.

  Linux has the same file-vs-browser split (`xdg-open` on a `file://` URI
  follows the file's MIME association), so `desktop_linux.py` does the
  freedesktop equivalent: `xdg-settings get default-web-browser` names the
  browser's `.desktop` file, found in XDG precedence order
  (`$XDG_DATA_HOME` first, so a user override wins), and its `Exec` line
  is expanded with the URL in place of `%u`/`%U`/`%f`/`%F`. Open Image
  Location uses the `org.freedesktop.FileManager1.ShowItems` D-Bus call
  (Nemo, Nautilus, Dolphin, ... all implement it) as the equivalent of
  Explorer's `/select`. Copy Image pipes PNG bytes to xclip/wl-copy with
  stdout/stderr *not* captured: both fork a background process to keep
  serving the clipboard, which would inherit a captured pipe and make
  `subprocess.run` wait until the clipboard changed hands.
