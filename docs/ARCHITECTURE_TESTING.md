# Test coverage

Part of [ARCHITECTURE.md](ARCHITECTURE.md).

317 tests total: 218 run by default, plus 99 marked `gui` (build a real,
withdrawn Tk window - see the README's "Testing" section) that are skipped
unless run with `-m gui` or `-m ""`.

## Review screen

The most architecturally involved and historically bug-prone part of the
app (see [ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md) and
[ARCHITECTURE_ROW_GEOMETRY.md](ARCHITECTURE_ROW_GEOMETRY.md)), so it has the
deepest coverage:

- `ReviewFrame._reconcile`'s windowing core - idempotency, paging to the end
  of a long transcript, a far-away Tab/resume target materializing
  correctly, edits surviving a row being paged out and back in, and the
  Finalize button's visibility toggle.
- Row-height estimation/visible-range math (`app/gui/virtualization.py`, the
  part of the windowing logic pure enough to unit-test without a
  display) - including a row with multiple images estimating taller than
  one with a single image.
- Slot-based keyboard navigation (`_move_focus` stepping through
  `(item_index, role)` slots in transcript order, message before one
  `"ocrN"` slot per attached image).
- Per-box undo/redo history (`text_undo.py`) surviving a row being paged out
  and rebuilt.
- `text_undo.py`'s recording proxy resolving symbolic index arguments
  (`sel.first`/`sel.last`, the `insert` mark) to absolute positions at
  record time (`app_tests/test_text_undo.py`), plus the end-to-end shape of
  the bug this fixes: selecting and deleting text, then paging that row out
  and back in, must not raise and must land on the correct final text
  (`app_tests/test_review_view.py`). See
  [ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md)'s "Symbolic
  marks recorded in a UndoLog must be resolved before they can drift".
- The three defense-in-depth backstops that shipped alongside that fix (same
  doc): a replay failure that still somehow occurs recovers the box's
  last-known-good text and self-heals its `UndoLog` instead of crashing;
  rebuilding an already-live box (a "should be impossible" double-build)
  reclaims its content into `self._saved_texts` instead of orphaning it; and
  one row's build exception no longer aborts the rest of its reconcile
  batch or leaves a dangling `KeyError` trap on later Tab/Shift-Tab
  navigation to a row that failed to build.
- A focused box always scrolling fully into view, not just its row, and a
  far-away Tab/resume target landing fully within the *real* canvas
  viewport rather than just the document-space model's own idea of where it
  is (a since-fixed row-height accounting bug could get this wrong).
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
- The editable text box height rule (`_fixed_text_box_height`'s capping
  logic).
- Spellcheck tagging (`test_spellcheck.py`, pure logic - flagged/not-flagged
  words, short-word and ALL-CAPS skipping, whitelist loading/caching - plus
  `test_review_view.py`'s GUI tests for the real `tk.Text` tag behavior: a
  misspelled word getting tagged, a correctly-spelled box getting no tag, a
  spacer box never having the tag configured at all, the tag being
  recomputed after a row is paged out and rebuilt onto a fresh widget, and a
  torn-down row's pending debounce timer actually getting cancelled).
- Image preview sizing/visibility (`app/gui/image_loading.py`'s aspect-fit
  math and its load/unload viewport-boundary decision, plus real
  load/failure/unload behavior against actual Tk widgets).
- The right-click image context menu (`app/gui/image_context_menu.py`,
  `test_image_context_menu.py`): each action's success/failure logging;
  `_open_image`/`_open_image_in_browser`/`_open_image_location`/
  `_open_chatlog_at_message`/`_copy_image_to_clipboard` themselves (mocking
  `os`/`subprocess`/`win32clipboard` rather than really opening a viewer/browser, a real
  Explorer window, or touching the real clipboard);
  `_default_browser_command`'s two-step registry lookup (mocked
  `winreg.OpenKey`/`QueryValueEx`) succeeding, and returning `None` when
  either step fails (an unset `UserChoice`, or a `ProgId` left over from a
  since-uninstalled browser); scrolling (mousewheel, Page Up/Down, the
  scrollbar) freezing while the menu is open and unfreezing once it closes.
  The real `tk.Menu.tk_popup()` call is never made in any of these - see
  [ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md)'s "A popup
  `tk.Menu`'s close can't be detected via `<Unmap>` on Windows" for why it
  blocks until a person dismisses it, which hangs an unattended
  test - `tk_popup` is mocked out instead, so these test what
  `_show_image_context_menu` itself controls (state before/after the call)
  rather than the real OS-level popup/dismissal.

## Session resume

- Message-id-based edit/focus matching (`_match_saved_edits`/
  `_match_focus_slot`) - edits surviving messages appended or inserted
  mid-transcript in a re-export, orphaned edits for now-filtered-out
  messages being dropped, a saved message's per-image OCR edits being
  aligned back onto its current images by position, and a stale focus slot
  falling back to no restore.
- `App._on_start`'s validation branches (missing fields, an invalid start
  date, an empty approved-users list) and its pending-session resume
  prompt, including `_resume_session` always forcing `use_cache=True`
  regardless of what the saved session originally recorded, so resuming
  never redoes OCR.
- The chatlog read/parse error paths (`_prepare_run` turning an unreadable
  file, a malformed export or an unexpected exception into a `_RunError`
  with a user-facing message), `App._on_ocr_done` reporting a
  review-building failure instead of letting it escape uncaught from a Tk
  callback, and its missing-images warning.

## HTML parsing / OCR pipeline

- HTML parsing/filtering - the export postamble's declared timezone applied
  to every message timestamp, the clear error raised when that timezone is
  missing or unparseable, the per-message Discord ID extracted from each
  `chatlog__message-container`'s `data-message-id`, the clear error raised
  when that container is missing, and - run against a real
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
- The cleanup regexes.
- Review-item building/output-writing - a text-only message's editable
  spacing copy standing in for its immutable original when written out, a
  message with both a caption and an image getting two independently-edited
  text blocks, and a message with multiple images getting one
  independently-edited OCR block per image, each falling back to its own
  original OCR text when not edited.
- The finalize pass (cleanup + run-date + clipboard + BREAK-marker
  bookmarking).

## Persistence

- JSON state persistence - run dates, OCR cache (including reading the
  version 1 format) and in-progress sessions
  both kept per-chatlog/per-folder indefinitely rather than as a single
  global slot, and recent-path history.
- The atomic-write-plus-backup-rotation/recovery behavior of every state
  file (`app/state.py`).
- The JSON log formatter.
- Start-date validation.

## What's not covered

- No automated test drives real Tk button *clicks* - only direct method
  calls standing in for them - or a live Tesseract install.
- `setup_view.py`'s widget wiring is still only covered by manual
  smoke-testing: window construction, the review screen with synthetic
  text-only/image-only/image-with-caption/multiple-images-on-one-message
  items, an edit-then-finalize pass against a temp output file, and a
  resumed session's saved edits/focus restoring correctly.
