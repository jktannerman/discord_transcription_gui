# Architecture & internals

Deeper technical detail than the README needs for "how do I run this." Split
by topic so a change in one area doesn't pull in unrelated history. Read
"General heuristic" below no matter what you're touching; jump to a topic
doc for the rest.

## Topic docs

- **[ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md)** - the
  review screen's internals (`app/gui/review_view.py` and friends): row
  virtualization, focus/scroll correction, undo/redo replay, the OCR
  checkbox, spellcheck, the image context menu. The most architecturally
  involved part of the app, and the one most bugs have shipped in - read
  before changing anything under `app/gui/`.
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

Three real bugs have now shipped at the exact same seam - `_populate_text_box`'s
replay branch (`row_building.py`), which reconstructs a torn-down review-screen
text box from `UndoLog.ops` rather than rebuilding it fresh:

1. **`UndoLog.baseline`** (see `ARCHITECTURE_REVIEW_SCREEN.md`'s "A box's
   `UndoLog` must record what it actually started from"): a rebuild re-based
   replay onto the item's static `initial_text`, silently discarding a
   resumed edit with zero further ops recorded on top of it.
2. **`sel.first`/`sel.last`** (see `ARCHITECTURE_REVIEW_SCREEN.md`'s
   "Symbolic marks recorded in a `UndoLog` must be resolved before they can
   drift", `archive/INVESTIGATION_shift_tab_reconcile_lockup.md`): a
   recorded `delete sel.first sel.last` call replayed onto a fresh widget
   with nothing selected, raising `TclError` and wedging the whole review
   screen's virtualization for the rest of the session.
3. **Undo/redo replay divergence** (see `ARCHITECTURE_REVIEW_SCREEN.md`'s "A
   recorded undo/redo must not be replayed by calling
   `edit_undo()`/`edit_redo()` again", `INVESTIGATION_undo_redo_replay_divergence.md`):
   replaying a bare `"undo"`/`"redo"` marker by calling
   `edit_undo()`/`edit_redo()` again silently reverted a *different amount*
   of text than the live press did, with no exception and no log line.

All three are the same failure wearing different clothes: **replay assumed
the reconstructed widget was equivalent to the live one it stood in for, and
something true of the live widget - its actual starting point, a mark's live
resolution, the undo stack's grouping history - hadn't actually been
captured in the log.** The log looked like a complete causal description of
"what happened to this box," but each time it was missing a piece of context
that only lived on the live widget instance, so replay silently filled the
gap with a wrong default. None of the three needed rapid Tab/Shift-Tab to be
*possible* - a single ordinary teardown/rebuild is enough - but rapid
navigation is what reliably produces many teardown/rebuild cycles in a short
window, turning a latent bug into a frequent one. All three were also found
by forensic reconstruction from production logs after the fact, not by the
existing test suite, for the reason described below.

The general failure mode: a new feature gets layered on top of code that
already has its own internal state machine (here, `_populate_text_box`'s
build-fresh/rebuild-replay branching). It's not enough to ask "does my new
feature work" - the question that would have caught this is "does my new
feature still hold every invariant the *existing* state machine depends on."
That only has a useful answer once you've identified what those invariants
are and written them down somewhere other than the original author's head.

Concrete habits this argues for here:

- When a feature touches state another feature already manages (session
  resume touching the same per-box dicts virtualization owns), write at
  least one test exercising *both* together, in the order a real user would
  hit them - not just one test per feature with the other absent. If two
  such tests already exist separately (as they did for the `UndoLog.baseline`
  bug), that's a sign the combined test is still missing, not that coverage
  is adequate.
- When code's correctness depends on an assumption about how it got into its
  current state (e.g. "this log's ops are deltas from `initial_text`"),
  encode that assumption as actual stored data (`UndoLog.baseline`) rather
  than leaving it implicit in which branch happened to run. An assumption
  living only in a comment can drift out of sync with the code the moment a
  new caller shows up; one recorded as data the code reads back can't.
- **Resolve ambiguous/context-dependent things at record time, not replay
  time.** Every fix here converged on the same technique: turn something
  whose meaning depends on hidden or mutable context (a symbolic mark, an
  undo/redo call whose effect depends on the live widget's own grouping
  history) into something absolute and self-contained *before* it goes into
  the log, so replay never has to re-derive it. When adding a new kind of
  recordable interaction to these boxes (rich text tags, IME composition,
  drag-and-drop, ...), ask up front: does this op's effect depend on
  anything beyond its own literal arguments plus the box's current text? If
  yes, resolve that dependency before recording it, rather than finding out
  via a fourth incident.
- **Verify replay against independently-captured ground truth by default,
  not per-bug.** `_populate_text_box`'s replay branch now unconditionally
  compares its result against `self._saved_texts[key]` - the box's own
  content as of its last teardown, captured independently of whatever replay
  just produced - and self-heals on any mismatch. This started as a
  targeted fix for the undo/redo bug, but it's a generic property of *any*
  replay, and now runs for all of them - a standing structural guarantee of
  this subsystem, the same way `main.py`'s `root.report_callback_exception`
  is a blanket net for any uncaught Tk-callback exception, not just one
  revisited when a fifth bug of this shape turns up.
- **A dedicated regression test per discovered bug only covers combinations
  someone has already hit.** All three bugs above were invisible to the test
  suite until someone hand-wrote a test for the exact scenario each one
  turned out to require. The structurally stronger complement now exists:
  `test_review_view.py`'s `_random_edit_sequence` applies a randomized
  sequence of this subsystem's interaction types (type, select+delete via
  Tk's own `sel.first`/`sel.last`, undo, redo, a paste-shaped
  delete-selection-then-insert, and - for an "ocr" box - checkbox toggle) to
  a box in random order and count, tears its row down, rebuilds it, and
  asserts the rebuilt content, cursor position, and checked state all match
  what was live immediately before teardown - parametrized across every role
  shape a box can have ("message", "ocr{N}", a spacer role).
  `test_random_interaction_sequence_survives_two_consecutive_teardown_
  rebuild_cycles` extends this to two consecutive cycles (the
  `UndoLog.baseline` bug specifically needed a *second* rebuild, whose
  starting point was itself a replay result, to surface at all), and
  `test_random_edits_after_a_seeded_baseline_survive_a_further_teardown_
  and_rebuild` closes the gap every existing resumed/finalized composition
  test left open - real further edits on top of a resumed/finalized
  baseline, not just an empty op log, before the next rebuild. Every one of
  these asserts no `ERROR`-level replay-divergence/self-heal log line fired
  too - a property test that only checked final content would still pass if
  replay were badly broken and silently falling back to the self-heal
  backstop on every rebuild, since self-heal's recovery target
  (`self._saved_texts`) is kept correct independently of replay. This is
  randomized but seeded (fixed per-test seeds), so a failure is
  reproducible, not flaky - it isn't yet run with a broad seed sweep in CI
  the way true fuzzing would be, so it's a probabilistic net, not a proof.
