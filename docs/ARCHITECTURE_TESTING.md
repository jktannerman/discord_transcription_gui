# Test coverage

Part of [ARCHITECTURE.md](ARCHITECTURE.md).

547 tests in all (as of 2026-09-27): 396 run by default, plus 151 marked
`gui` (they build a real Tk window - see the README's "Testing" section)
that only run with `-m gui` or `-m ""`. Of the default ones, 11 test
Windows-only code and are skipped elsewhere. `python -m pytest --co -q
-m ""` gives the current count.

## Review screen

The most architecturally involved and bug-prone part of the app (see [ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md) and
[ARCHITECTURE_ROW_GEOMETRY.md](ARCHITECTURE_ROW_GEOMETRY.md)), so it has the
deepest coverage:

- `VirtualRows.reconcile`'s windowing core - idempotency, paging to the end
  of a long transcript, a far-away Tab/resume target materializing
  correctly, edits surviving a row being paged out and back in, and the
  Finalize button's visibility toggle.
- Row-height estimation/visible-range math (`discord_transcription/gui/virtualization.py`, the
  part of the windowing logic pure enough to unit-test without a
  display) - including a row with multiple images estimating taller than
  one with a single image.
- Slot-based keyboard navigation (`FocusNavigator.move_focus` stepping through
  `(item_index, role)` slots in transcript order, message before one
  `"ocrN"` slot per attached image).
- Undo-step grouping and the undo/redo stacks (`discord_transcription/gui/edit_history.py`,
  `test_edit_history.py`, no display needed): word boundaries, the pause
  rule, insert/delete switches, cursor jumps, paste/cut/standalone steps,
  redo clearing, and the history cap.
- Per-box undo/redo history surviving a row being paged out and rebuilt,
  plus the randomized property tests (text, cursor, checkbox and the whole
  Ctrl+Z walk unchanged by a teardown/rebuild) - see ARCHITECTURE.md's
  general heuristic.
- Selecting and deleting text, then paging that row out and back in, must
  not raise and must land on the correct final text
  (`app_tests/test_review_view.py`).
- Two backstops in the virtualization core: rebuilding an already-live box
  (a "should be impossible" double-build) syncs its content into its
  SlotState instead of orphaning it, and one row's build exception doesn't
  abort the rest of its reconcile batch or leave a dangling `KeyError` trap
  on later Tab/Shift-Tab navigation to a row that failed to build.
- A focused box always scrolling fully into view, not just its row, and a
  far-away Tab/resume target landing fully within the *real* canvas
  viewport, not just where the row-height model puts it (see
  [ARCHITECTURE_ROW_GEOMETRY.md](ARCHITECTURE_ROW_GEOMETRY.md)).
- Each OCR box's checkbox (see "Per-OCR-box edited/checkbox state" in
  [ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md)): starting
  checked/unchecked correctly for an untouched vs. a
  resumed-and-differing-from-default box, typing checking it automatically,
  unchecking/rechecking round-tripping both the OCR default and the edited
  version without either being discarded, `collect_edited_texts` reporting
  `None` for an unchecked box despite its edited version still being
  cached, both the checkbox state and both text versions surviving a row
  being paged out and back in, and Ctrl+Z re-deriving the right checked
  state after undoing a toggle.
- The editable text box height rule (`RowBuilder.fixed_text_box_height`
  and its cap, `test_row_building.py`).
- Spellcheck tagging (`test_spellcheck.py`, pure logic - flagged/not-flagged
  words, short-word and ALL-CAPS skipping, whitelist loading/caching - plus
  `test_review_view.py`'s GUI tests for the real `tk.Text` tag behavior: a
  misspelled word getting tagged, a correctly-spelled box getting no tag, a
  spacer box never having the tag configured at all, the tag being
  recomputed after a row is paged out and rebuilt onto a fresh widget, and a
  torn-down row's pending debounce timer actually getting cancelled).
- Image preview sizing/visibility (`discord_transcription/gui/image_loading.py`'s aspect-fit
  math and its load/unload viewport-boundary decision; background decoding
  with a fake poll widget, including a decode finishing after its image was
  unloaded or its row torn down; plus real load/failure/unload behavior
  against actual Tk widgets, and one review-screen test that an image in
  view is actually shown).
- The image/text column divider: width clamping, fraction-to-width and
  width-dependent row estimates (`test_virtualization.py`), divider position
  math (`test_column_divider.py`), per-chatlog persistence
  (`test_state.py`), and in `test_review_view.py`'s GUI tests: rows rebuilt
  at the new width with heights matching their real ones, the divider sitting
  in the real gap between the columns, the focused box or top row staying
  put on screen, an edit and its undo surviving a resize *then* a scroll
  teardown/rebuild, a drag applying only on release and reporting the new
  fraction, a saved fraction applied at startup, and a width change keeping
  the proportion. Not covered: a real mouse drag or a real window resize
  (the tests call the handlers directly), and the second, exact anchoring
  pass - the test rows' estimates are exact, so it never has anything to
  correct there.
- The right-click image context menu (`discord_transcription/gui/image_context_menu.py`,
  `test_image_context_menu.py`): each action's success/failure logging;
  `_open_image`/`_open_image_in_browser`/`_open_image_location`/
  `_open_chatlog_at_message`/`_copy_image_to_clipboard` themselves (mocking
  `os`/`subprocess`/`win32clipboard` rather than really opening a viewer/browser, a real
  Explorer window, or touching the real clipboard);
  `_default_browser_command`'s two-step registry lookup (mocked
  `winreg.OpenKey`/`QueryValueEx`) succeeding, and returning `None` when
  either step fails (an unset `UserChoice`, or a `ProgId` left by an
  uninstalled browser); scrolling (mousewheel, Page Up/Down, the
  scrollbar) freezing while the menu is open and unfreezing once it closes.
  The real `tk.Menu.tk_popup()` call is never made in any of these: on
  Windows it blocks until a person dismisses the menu (see
  [ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md)'s "Image
  context menu"), so it's mocked, and these test what
  `ImageContextMenu.show` itself controls (state before/after the call)
  rather than the real OS-level popup/dismissal. The tests of Windows-only
  code (the real `winreg` lookup, Explorer's `/select`, the Win32
  clipboard) are marked `windows_only` and skipped on other platforms.
- Wheel event normalization (`test_wheel.py`, no display needed): X11
  Button-4/5 and `<MouseWheel>` deltas map to the same vertical units, and
  Shift-modified (horizontal) events are ignored.
- The Linux context-menu helpers (`test_desktop_linux.py`, every external
  command mocked): Desktop Entry `Exec` parsing, XDG application-directory
  precedence, the `xdg-settings` default-browser lookup, FileManager1
  `ShowItems` with its open-the-folder fallback, and xclip/wl-copy
  selection; plus the Linux branch of each menu action
  (`test_image_context_menu.py`, marked `not_windows`).
- Ctrl+Z/Ctrl+Shift+Z dispatch depending on Shift, not Caps Lock
  (`test_keyboard_nav.py`), EXIF-rotated images being sized and decoded the
  right way up (`test_image_loading.py`), Ctrl+A selecting all in every
  kind of text field (`test_select_all.py`), and Ctrl+Backspace deleting a
  word or the selection (`test_delete_word_backward.py`).

## Session resume

- The saved session format (`session.SavedSession`, `test_session.py`, no
  display needed): capturing the review screen's state by message ID, the
  exact JSON written, a JSON round trip, rejecting a session whose run
  inputs are missing or unusable, and dropping only the malformed parts of
  the rest.
- Message-id-based edit/focus matching (`session.match_saved_edits`/
  `match_focus_slot`, and `SavedSession.restore_onto`) - edits surviving messages appended or inserted
  mid-transcript in a re-export, orphaned edits for now-filtered-out
  messages being dropped, a saved message's per-image OCR edits being
  aligned back onto its current images by position, and a stale focus slot
  falling back to no restore.
- Autosave rescheduling after a failed save, warning once per run of
  failures, and the close prompt after a failed final save
  (`test_main_window_autosave.py`).
- `App._on_start`'s validation branches (missing fields, an invalid start
  date, an empty approved-users list) and its pending-session resume
  prompt, including `_resume_session` always forcing `use_cache=True`
  regardless of what the saved session originally recorded, so resuming
  never redoes OCR.
- The chatlog read/parse error paths (`pipeline.prepare_run` turning an
  unreadable file, a malformed export or an unexpected exception into a
  `RunError` with a user-facing message, in `test_pipeline_run.py`),
  `App._on_ocr_done` reporting a
  review-building failure instead of letting it escape uncaught from a Tk
  callback, and its missing-images warning.

## HTML parsing / OCR pipeline

- HTML parsing/filtering - each message's send time decoded from its
  snowflake ID, the start-date cutoff applied per message (splitting a
  message group that straddles it, and ignoring the export's visible
  timestamps and postamble timezone), the per-message Discord ID extracted
  from each `chatlog__message-container`'s `data-message-id`, the clear
  error raised when that container is missing or the ID isn't a snowflake,
  and - run against a real
  DiscordChatExporter export fixture,
  `example_inputs/short_test_input.html` - every image attachment a message
  has being picked up rather than just the first.
- OCR paragraph splitting and backend dispatch.
- The OCR-misread corrections pass (`ocr_corrections.py`'s file
  parsing/validation and regex application) and its wiring into
  `build_review_items` - applied to OCR text only, never to a message's own
  text.
- The OCR batch runner: only chatlog-referenced images are OCR'd, missing
  files are reported, cached results are reused unless the file's
  size/mtime changed, the cache is saved periodically and survives an OCR
  failure partway through, and version 1 cache entries are trusted and
  upgraded.
- The cleanup regexes, including that past runs' text is left untouched.
- Review-item building/output-writing - a text-only message's editable
  spacing copy standing in for its immutable original when written out, a
  message with both a caption and an image getting two independently-edited
  text blocks, and a message with multiple images getting one
  independently-edited OCR block per image, each falling back to its own
  original OCR text when not edited.
- The finalize pass (cleanup of this run's text only + backup of the
  previous output + run-date + clipboard + BREAK-marker bookmarking).
- Spellcheck word lists reloading when their file changes
  (`test_spellcheck.py`), the OCR worker's event queue being drained on the
  Tk thread until its final callback (`test_main_window_ocr_error.py`),
  and log-level resolution (`test_logging_config.py`).

## Persistence

- JSON state persistence - run dates per chatlog (including converting the
  older shared list), OCR cache (including reading the
  version 1 format), the `format_version` wrapper on every other state file
  (and reading files written before it), path-key normalisation (the same
  chatlog/folder reached through different spellings or a symlink, and
  entries saved under a pre-normalisation key being found and migrated),
  the single-instance lock, and in-progress sessions
  both kept per-chatlog/per-folder indefinitely rather than as a single
  global slot, and recent-path history.
- The atomic-write-plus-backup-rotation/recovery behavior of every state
  file (`discord_transcription/state.py`).
- The JSON log formatter, and uncaught Tk callback exceptions reaching the
  log (`test_main.py`).
- Start-date and approved-users parsing (`test_pipeline.py`).

## What's not covered

- No automated test drives real Tk button *clicks* - only direct method
  calls standing in for them - or a live Tesseract install.
- Nothing checks for repaints of the review screen mid-reconcile (see
  "Nothing may repaint while the built block is out of place" in
  [ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md)); the
  trace log doesn't record paints, so this was verified by hand.
- `setup_view.py` is tested only for the "Known users" Add newline handling
  and the start date following the chosen chatlog (`test_setup_view.py`).
  The rest of its widget wiring, and the end-to-end flow (setup, OCR,
  review, an edit, Finalize against a real output file, and resuming), are
  smoke-tested by hand.
