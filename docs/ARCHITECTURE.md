# Architecture & internals

Deeper technical detail than the README needs for "how do I run this." Split
by topic so a change in one area doesn't pull in unrelated history. Read
"General heuristic" below no matter what you're touching; jump to a topic
doc for the rest.

## Topic docs

- **[ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md)** - the
  review screen's internals (`discord_transcription/gui/review_view.py` and friends): row
  virtualization, focus/scroll correction, the per-box model and undo, the OCR
  checkbox, spellcheck, the image context menu. The most architecturally
  involved part of the app, and the one most bugs have shipped in - read
  before changing anything under `discord_transcription/gui/`.
- **[ARCHITECTURE_ROW_GEOMETRY.md](ARCHITECTURE_ROW_GEOMETRY.md)** - the
  document-space spacing model `_offset_of` and every scroll-into-view
  calculation depend on, and the one constant (`ROW_PACK_PADY_PX`) that's
  easy to leave out of it. Read before touching row height/offset
  accounting specifically.
- **[ARCHITECTURE_SPACER_SLOTS.md](ARCHITECTURE_SPACER_SLOTS.md)** - how
  blank-line spacing between transcript elements is modeled as editable
  "spacer" text boxes, default newline counts, and how Finalize parses them.
  Read before touching spacing, `review_item.py`, or finalized-edit
  persistence.
- **[ARCHITECTURE_TESTING.md](ARCHITECTURE_TESTING.md)** - what's covered
  where, and what's still only manually smoke-tested. Check here before
  adding a feature or fixing a bug, to find its existing tests (or confirm
  it has none).
- **[ARCHITECTURE_LOGGING.md](ARCHITECTURE_LOGGING.md)** - the two log
  files, what each carries, and the undo/redo trace format. Read before
  adding a log line or debugging from `app.log`/`scroll_trace.log`.

## General heuristic: test where features compose, not just each feature alone

Three real bugs shipped at the exact same seam - the old replay-based undo
(`text_undo.py`, since replaced), which rebuilt a torn-down review-screen
text box by replaying every recorded insert/delete onto a fresh widget:

1. **`UndoLog.baseline`**: a rebuild re-based replay onto the item's static
   default text, silently discarding a resumed edit with zero further ops
   recorded on top of it.
2. **`sel.first`/`sel.last`** (`archive/INVESTIGATION_shift_tab_reconcile_lockup.md`):
   a recorded `delete sel.first sel.last` call replayed onto a fresh widget
   with nothing selected, raising `TclError` and wedging the whole review
   screen's virtualization for the rest of the session.
3. **Undo/redo replay divergence**
   (`archive/INVESTIGATION_undo_redo_replay_divergence.md`): replaying a
   recorded undo by calling `edit_undo()` again silently reverted a
   *different amount* of text than the live press did, with no exception and
   no log line.

All three are the same failure wearing different clothes: **replay assumed
the reconstructed widget was equivalent to the live one it stood in for, and
something true of the live widget - its actual starting point, a mark's live
resolution, the undo stack's grouping history - hadn't actually been
captured in the log.** None of the three needed rapid Tab/Shift-Tab to be
*possible* - a single ordinary teardown/rebuild is enough - but rapid
navigation reliably produces many teardown/rebuild cycles in a short window,
turning a latent bug into a frequent one. All three were found by forensic
reconstruction from production logs, not by the test suite.

The eventual fix was structural rather than a fourth patch: the box's text,
cursor and undo history now live in a per-box model (`SlotState`, with a
snapshot-based `EditHistory`) that never depends on a widget at all, so a
rebuild just fills a fresh widget from it (see
`ARCHITECTURE_REVIEW_SCREEN.md`'s "The per-box model lives outside the
widgets"). When patches keep landing on the same seam, question the seam.

The general failure mode: a new feature gets layered on top of code that
already has its own internal state machine. It's not enough to ask "does my
new feature work" - the question that would have caught these is "does my
new feature still hold every invariant the *existing* state machine depends
on." That only has a useful answer once you've identified what those
invariants are and written them down somewhere other than the original
author's head.

Concrete habits this argues for here:

- When a feature touches state another feature already manages (session
  resume touching the same per-box state virtualization rebuilds from),
  write at least one test exercising *both* together, in the order a real
  user would hit them - not just one test per feature with the other absent.
  If two such tests already exist separately (as they did for the
  `UndoLog.baseline` bug), that's a sign the combined test is still missing,
  not that coverage is adequate.
- When code's correctness depends on an assumption about how it got into its
  current state, encode that assumption as actual stored data rather than
  leaving it implicit in which branch happened to run - or better, remove
  the branch, as the move to `SlotState` did.
- **Resolve context-dependent things when you record them, not when you use
  them.** Anything whose meaning depends on hidden or mutable context (a
  symbolic mark, the live widget's undo grouping) should be turned into
  something absolute and self-contained before it's stored. Full-text
  snapshots are the extreme case: they depend on nothing.
- **A dedicated regression test per discovered bug only covers combinations
  someone has already hit.** The structurally stronger complement:
  `test_review_view.py`'s `_random_edit_sequence` applies a randomized
  sequence of this subsystem's interaction types (type, select+delete via
  Tk's own `sel.first`/`sel.last`, undo, redo, a paste-shaped
  delete-selection-then-insert, and - for an "ocr" box - checkbox toggle) to
  a box in random order and count, tears its row down, rebuilds it, and
  asserts the rebuilt text, cursor position, checked state and the whole
  Ctrl+Z walk all match what they were immediately before teardown -
  parametrized across every role shape a box can have ("message", "ocr{N}",
  a spacer role).
  `test_random_interaction_sequence_survives_two_consecutive_teardown_rebuild_cycles`
  extends this to two consecutive cycles (the `UndoLog.baseline` bug
  specifically needed a *second* rebuild to surface at all), and
  `test_random_edits_after_a_seeded_baseline_survive_a_further_teardown_and_rebuild`
  covers real further edits on top of a resumed/finalized starting text.
  These are randomized but seeded (fixed per-test seeds, with the pause
  rule's clock frozen), so a failure is reproducible, not flaky - they
  aren't yet run with a broad seed sweep the way true fuzzing would be, so
  they're a probabilistic net, not a proof.
