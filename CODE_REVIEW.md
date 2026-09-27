# Code review findings (2026-09-27, second pass)

A second full review of the code, tests and docs. It builds on the earlier
`REVIEW_FINDINGS.md` (most of which was fixed) and only repeats that
review's open items where something new was found. Each item has an ID so
it can be ticked off, or struck through if we decide against it.

How sure each finding is:
- **[confirmed]** - reproduced by running code, or seen directly on disk or
  in a sample export.
- **[code-read]** - clear from reading the code, but not reproduced.
- **[check]** - probable, but needs checking on a real machine or export.

State at the time of the review: the default test suite passed (335 passed,
11 Windows-only skipped, 143 `gui` tests not run).

---

## A. Bugs

- [x] **A1. Message times are an hour off during summer time** [confirmed] - fixed with A2
  In `example_inputs/short_test_input.html`, the visible timestamps read
  `17:49` and the postamble says `Timezone: UTC+0`, but the timestamps
  encoded in the message IDs say 16:49 UTC (the export was made on BST).
  DiscordChatExporter seems to write the standard offset without the DST
  hour. `chatlog.parse_message_groups` therefore places every message an
  hour late while DST is in effect, and messages from the last hour before
  each export are transcribed again in the next run (the start date is the
  previous export's file mtime, which is true UTC). This was the earlier
  review's B8 ([likely]).
  *Fix:* see A2.

- [x] **A2. Messages continuing a group across an export boundary are silently skipped** [confirmed] - fixed: `chatlog.snowflake_timestamp_ms` gives each message's UTC send time from its ID, and the cutoff is checked per message; the postamble timezone and `config.TIMESTAMP_FORMAT` are no longer used (a non-numeric ID now raises a clear error). Regression tests in `test_chatlog.py`, including one against the sample export
  The start-date cutoff only checks each `chatlog__message-group`'s first
  timestamp, so a group is kept or dropped as a whole. The sample export is
  one group of 8 messages; with a cutoff between the 4th and 5th message,
  4 messages are really after it and the parser keeps 0. Anyone mid-group
  at export time loses their later messages in that group for good (the
  reverse, re-including earlier messages of a kept group, also happens).
  *Fix (A1 and A2):* take each message's time from its Discord message ID
  (a snowflake: `(id >> 22) + 1420070400000` ms), which is already read and
  required. Exact per-message UTC time, no dependence on the postamble,
  the locale-dependent `TIMESTAMP_FORMAT`, DST, or minute rounding.

- [x] **A3. The `\b([A-Z]),` OCR correction damages ordinary text** [confirmed] - fixed: rule is now `\b([A-HJ-Z]),`, with a comment; tests for both cases in `test_ocr_corrections.py`
  `"I, for one, agree."` becomes `"I. for one, agree."`, and
  `"Plan A, then B."` becomes `"Plan A. then B."`. The rule was meant for
  A/B/C lists, but `I,` is very common in chat. It also has no comment,
  though the README says the file's comments explain each rule.
  *Fix:* exclude `I` (e.g. `[A-HJ-Z]`) or require list-like context.

- [ ] **A4. Empty messages add extra blank lines** [confirmed] - deferred: not an issue in practice for now
  A message with no text and no images (video, file or sticker only) still
  gets an empty "message" box plus its `spacer_end`, so the output has 7
  blank lines at that point instead of 3.
  *Fix:* skip such messages in `build_review_items`, or give them no
  spacer.

- [ ] **A5. Spellcheck false positives** [confirmed] - deferred for now
  - Words in straight quotes are flagged with the quotes (`'hello'`),
    because `_WORD_RE` is `[A-Za-z']+`.
  - Accented words are split: `cafés` is flagged as `caf`.
  - British spellings (`colour`) are always flagged; the whitelist is
    accumulating `centre`, `recognise`, `jewellery`, ...
  *Fix:* strip leading/trailing apostrophes, use a Unicode letter class,
  and consider seeding British variants.

- [x] **A6. Autosave stops for good after one failure** [code-read] - fixed: always rescheduled, a warning shown once per run of failures, recovery logged. Also fixed: a failed final save on window close skipped `root.destroy()`, so the window couldn't be closed - it now asks whether to close anyway. Tests in `test_main_window_autosave.py`
  If `_snapshot_and_save` raises inside `App._run_autosave` (disk full,
  permissions), the `root.after` that schedules the next save is never
  reached. Autosave is off for the rest of the session, and only `app.log`
  says so.
  *Fix:* reschedule in a `finally`, and show the failure on screen.

- [x] **A7. The pre-filled start date ignores which chatlog is chosen** [code-read] - fixed: `run_dates.json` is now {chatlog: [dates]}; the setup screen pre-fills the chosen chatlog's date and updates it when the HTML path changes (empty if never finalized). The old shared list is assigned once to the most recently used chatlog. Tests in `test_state.py`, `test_pipeline_finalize.py`, `test_setup_view.py`
  `run_dates.json` is one global list, so the setup screen pre-fills the
  date of whichever chatlog was finalized last. The README explicitly
  supports alternating chatlogs; doing so gives a wrong start date (gaps or
  duplicates).
  *Fix:* record run dates per chatlog (`state.path_key`) and update the
  field when the HTML path changes.

- [x] **A8. Resume ignores the setup screen** [code-read] - fixed: `pipeline.check_output_path` runs when Start is clicked, before the resume prompt (folder exists and is writable; an existing file is a readable, writable file); a resumed session uses the setup screen's output path, and keeps its other saved inputs, since they decide which messages its edits belong to. Tests in `test_pipeline.py`, `test_main_window_on_start.py`
  `_resume_session` uses the session's saved output path, image folder,
  start date and users, whatever the setup screen shows. A mistyped output
  folder can only be fixed by creating that folder, or by declining the
  resume and losing the session's edits. Paths aren't validated before the
  run, so this surfaces only at Finalize.

- [ ] **A9. Smaller issues**
  - [x] [code-read] (fixed: `just_added` is now the part of the cleaned output after the existing content; tests in `test_pipeline_finalize.py`) If a run adds no text, Finalize copies the *previous* run's
    text to the clipboard (`cleaned.split(BREAK_MARKER)[-1]` after the
    trailing marker is stripped). Text containing `[BREAK]` also truncates
    the clipboard copy.
  - [x] [code-read] (fixed: `UNREADABLE_IMAGE_HEIGHT_PX` strip instead) A missing/unreadable image gets a full 950px-tall empty
    placeholder (`fitted_image_size` falls back to the bounding box).
  - [x] [code-read] (fixed: `ImageLoader` decodes on 2 worker threads, polled from the Tk thread; stale results dropped) Images are decoded and resized on the Tk thread inside
    `_reconcile`; large screenshots can stall scrolling.
  - [x] [code-read] (fixed: deletes the selection, like Backspace) Ctrl+Backspace ignores an active selection.
  - [check] With several images on one message, image N probably sits
    progressively higher than its OCR box: spacer boxes exist only in the
    right column, and right boxes are `TEXT_BOX_MARGIN_PX` taller.
  - [check] The Windows default-browser lookup splits the registry command
    with POSIX `shlex` rules, and newer Windows 11 builds may keep the http
    association under `UserChoiceLatest`.

---

## B. Structure

- [x] **B1. `ReviewFrame` is one ~3,000-line class across five files** - fixed: `ReviewFrame` now composes `VirtualRows` (new `virtual_rows.py`), `RowBuilder`, `SlotBoxes` (new `slot_boxes.py`), `FocusNavigator`, `ColumnDivider` and `ImageContextMenu`, each given its collaborators explicitly; no mixins left. Tests and docs updated; all default and `gui` tests pass
  `ReviewFrame` plus `KeyboardNavMixin`, `RowBuildingMixin`,
  `ImageContextMenuMixin` and `ColumnDividerMixin` share ~45 attributes with
  no declared interface; type checkers can't follow it. Natural split:
  the windowing core (knows nothing of slots), a per-box controller
  (states, views, undo, spellcheck), and the image context menu as a
  standalone object. Not urgent; the main structural debt.

- [x] **B2. Session logic lives in the GUI module** - fixed: new `session.py` (`SavedSession` with `capture`/`to_json`/`from_json`/`restore_onto`, the `match_*` helpers, `build_finalized_updates`, `log_edit_changes`); `RunContext`/`prepare_run`/`RunError` moved to `pipeline.py`; the on-disk format is unchanged. Tests in `test_session.py` (was `test_main_window_resume.py`) and `test_pipeline_run.py`
  ~275 lines of pure persistence logic in `gui/main_window.py`
  (`_match_*`, `_build_finalized_updates`, `_prepare_run`). The session
  format is an implicit dict built in `_snapshot_and_save` and taken apart
  in `_resume_session`/`_show_review`. A non-GUI `session.py` with a
  dataclass (to/from JSON, validated once) would be testable without Tk.

- [ ] **B3. Box roles are stringly typed**
  `"ocr3"`, `"spacer_img0"` etc. are parsed with `startswith` /
  `int(role[3:])` in 13 places across 6 files. A small helper type (kind,
  index, to/from string) would keep the parsing in one place; roles stay
  strings in the saved JSON.

---

## C. Fragile or overcomplicated areas

- [ ] **C1. Nested event processing mid-reconcile.** `_reconcile` →
  `_settle_pending_geometry` can call `canvas.update()`, which runs *any*
  pending event (including a Finalize click) partway through rebuilding
  rows. Documented as safe for nested reconciles, but it's a hazard.
- [ ] **C2. The height estimate duplicates the layout by hand.**
  `virtualization.estimate_row_height` mirrors `_build_row`'s sizing with a
  "change both" warning. Remeasuring hides a mismatch, but as a scroll jump.
- [ ] **C3. Row offsets are re-summed on every call.** `_offset_of`,
  `sum(self._row_heights)` and `compute_visible_range` each re-add the
  height list, including inside loops (`_remeasure_built_rows`,
  `ImageLoader.update_visible`). One cached prefix-sum array would serve
  all three.
- [ ] **C4. Permanent compatibility code in `state.py`.** Two versioning
  schemes (`format_version` wrapper vs the OCR cache's own `version`), and
  `_matching_key`/`_pop_matching` scan every key (with a `resolve()` per
  key) on every autosave to find legacy un-normalised keys. A one-time
  migration at startup would let that code go. Reader validation is also
  inconsistent (`read_last_run_date`/`load_recent_paths` trust the shape).
- [x] **C5. Comments and docstrings are mostly history** (earlier E6) - fixed: history and bug narratives trimmed from source docstrings/comments (now ~2,100 docstring + ~400 comment lines against ~4,000 code lines, from ~2,230 + ~500), `ARCHITECTURE_REVIEW_SCREEN.md` rewritten as a description of the current design, and the other topic docs trimmed the same way. Test docstrings weren't touched.
  ~2,300 docstring + ~700 comment lines against ~3,700 code lines;
  `review_view.py` has more doc than code; 24 "used to / previously /
  replaced an earlier design" passages. `ARCHITECTURE_REVIEW_SCREEN.md`
  reads as a bug diary. Trim to what the code does now and why.

---

## D. Docs vs code

- [x] **D1.** (fixed) README "The `gui` marker": says `test_main_window_ocr_error.py`
  is `gui` in full (only 1 test is), omits `test_setup_view.py` and
  `test_select_all.py` (fully `gui`) and 3 tests in
  `test_image_context_menu.py`.
- [x] **D2.** (fixed; now 528 / 384 / 144) `ARCHITECTURE_TESTING.md`: "317 tests (218 default, 99 gui)";
  actually 489 / 346 / 143.
- [x] **D3.** (fixed with B1) `ARCHITECTURE_REVIEW_SCREEN.md` (spellcheck section) names
  `_whitelist_loaded`/`_blacklist_loaded`, which no longer exist (now the
  mtime-keyed `_wordlists` cache).
- [x] **D4.** (fixed) `ARCHITECTURE_LOGGING.md`, `config.py` and
  `logging_config.get_trace_logger` mention "box-resize" events, removed
  with fixed-height boxes.
- [x] **D5.** (fixed) `virtualization.wrapped_line_count`'s docstring cites text-box
  auto-sizing that no longer exists; an orphan comment block in the same
  file and `layout_constants.py` still point at `review_view.py` for code
  now in `row_building.py`.
- [x] **D6.** README project layout lists `original_transcription_program/`
  "kept as reference", but it's gitignored, so a checkout lacks it.
  `sketch_improvement_ideas.txt` is gitignored but tracked (the rule has no
  effect). Fixed: the original program is no longer relevant, so the README
  no longer mentions it; the ideas file is untracked (`git rm --cached`,
  still on disk).
- [x] **D7.** (fixed) README says previously typed users are remembered; the list is
  capped at `MAX_RECENT_PATHS` (8). It never says the run-end date is the
  chatlog file's modification time.

---

## E. Compared with a professional project

- [ ] **E1. Tooling:** (partly done: mypy added to the `dev` extras and configured in `pyproject.toml`; ruff and mypy run once - ruff's 2 findings, unused test variables, fixed; mypy reports ~70 errors, none a live bug: mostly `Optional` attributes set before use, tkinter's `Misc` widget type, BeautifulSoup's loose types, and Windows-only modules on Linux. CI still to do) no CI; ruff is configured but installed nowhere, so
  the lint never runs; no type checker despite mandatory type hints; no
  coverage. (Earlier D2.)
- [ ] **E2. Tests:** gaps line up with the bugs - every chatlog test was one
  message per group, none covered a DST export (both now covered, with
  A1/A2). The sample-export tests are now skipped when the private file is
  missing (earlier D1). GUI tests could run
  under Xvfb instead of stealing focus (earlier D6).
- [ ] **E3. User data inside the package:** the OCR rules and spellcheck
  word lists are user-edited data living in the source tree.
- [ ] **E4. Error visibility and UX:** some failures (autosave) only reach
  the log; no way to cancel the OCR pass; OCR runs one image at a time
  (Tesseract is a subprocess, so a small thread pool would be easy).
- [ ] **E5. Housekeeping:** no LICENSE or CHANGELOG; version fixed at
  1.0.0; a partial `.venv` in the project (7 packages, no pytest) although
  the project deliberately uses pipx; the earlier `REVIEW_FINDINGS.md`
  deletion is uncommitted.

---

## Suggested order

Done so far: ~~A1 + A2~~, ~~A3~~, ~~A6~~, ~~A7~~, ~~A8~~, ~~A9 clipboard~~,
~~B1~~, ~~B2~~, ~~C5~~, ~~Section D~~. Deferred: A4, A5.

Next, in order (revised 2026-09-27):

1. ~~**A9, first item: the Finalize clipboard copy.**~~ (done) A run that adds nothing
   copies the *previous* run's text, and `[BREAK]` in the new text cuts
   the copy short. The only open issue that silently produces wrong
   output. Small fix: take `just_added` from the rendered text rather than
   splitting the combined file.
2. ~~**A8: resume ignores the setup screen; paths aren't validated.**~~ (done) A
   mistyped output folder only shows up at Finalize, after the whole
   review. Check the output folder exists and is writable when Start is
   clicked, and let a resumed session use the setup screen's current
   output path. Small; do together with 1, since both protect the output
   file and the user's edits.
3. ~~**E1: tooling.**~~ (done, apart from CI and fixing mypy's findings) Add ruff and mypy to the `dev` extras and run them
   once (as pytest is run, via the pipx env's site-packages on
   `PYTHONPATH` - no project `.venv`). CI later.
4. **A9, remaining items**: ~~the 950px placeholder for a missing image~~,
   ~~Ctrl+Backspace ignoring a selection~~, ~~image decoding on the Tk
   thread~~ (done); still open: (check on a real export first) multi-image
   rows drifting out of line with their OCR boxes.
5. **B3: typed box roles.** A medium refactor; fixes nothing by itself but
   makes later review-screen changes safer.

Lower priority: C3 (a perf gain only for very long transcripts), C1 (a
theoretical hazard, no reported bug), C4 (cleanup only), C2 (stays open by
design - the estimate has to stay Tk-free), E2-E5 (nice to have).
