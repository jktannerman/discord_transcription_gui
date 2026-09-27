# Code review findings (2026-09-27)

A full review of the code and docs, done with a fresh pair of eyes. Each item
has an ID so we can work through them one at a time. Tick an item off once
it's fixed, or strike it through if we decide against it.

How sure each finding is:
- **[confirmed]** - reproduced, or seen directly on disk or in the test run.
- **[code-read]** - clear from reading the code, but not reproduced.
- **[likely]** - probable, but needs checking on a real machine or export.

---

## A. Bugs (do these first)

- [x] **A1. Tests write into the real app-data folder** [confirmed] - fixed: autouse fixture in `app_tests/conftest.py`; test data removed from the real data folder
  The paths in `app/config.py` are worked out once, when the module is
  imported. `app_tests/test_state.py` overrides `APP_DATA_DIR` and one file
  path per test, but `state.clear_session()` also calls
  `archive_session_backup()`. That writes to `config.SESSION_BACKUPS_FILE`,
  which is never overridden, so it lands in the real
  `~/.discord_transcription_gui/`. The real `session_backups.bak` currently
  holds test data (`"chat.html"`, `"chat_a.html"`).
  *Fix:* add an autouse fixture in `app_tests/conftest.py` that points every
  `config.*_FILE` path, and `APP_DATA_DIR`, at `tmp_path`. Then clean the test
  data out of the real `session_backups.json`/`.bak`.

- [x] **A2. A failed Finalize can lose edits, and retrying it duplicates output** [code-read] - fixed: confirmation prompt, session saved first, output built in memory and written atomically (commit point), post-write failures shown as warnings on the done screen, review screen kept if the write fails
  In `main_window._on_finalize_clicked`, any exception goes through
  `_on_run_error` to `show_setup()`. That throws away the review screen
  without a final save, so edits made since the last autosave (up to 5s) are
  lost. By then `write_all_items` may already have appended the output, and
  `finalize_run` may already have recorded the run date. The session is not
  cleared, so resuming and finalizing again appends the whole run a second
  time.
  A likely trigger on Linux: `pyperclip.copy` raises when there's no
  `xclip`/`xsel`. That happens *after* the output has been rewritten and the
  run date appended, but *before* the `[BREAK]` marker is written.
  *Fix:*
  - Save the session before doing anything else.
  - Build all the output in memory, then write the file atomically in one go.
  - Treat a clipboard failure as a warning, not an error.
  - Stay on the review screen if it fails.
  - Ask for confirmation before Finalize, since it can't be undone.

- [ ] **A3. The OCR cache is all-or-nothing per image folder** [code-read]
  In `pipeline.run_ocr_batch`, if the cache is used, the whole cached dict
  for the folder is returned. Images added since then get
  `file_info.get(name, [])`, which is an empty OCR box with no warning.
  Resuming a session always forces the cache on, so it hits this too. A
  non-cached run redoes OCR on everything and then *overwrites* the folder's
  cache with only this run's subset.
  *Fix:* keep a cache per image and only run OCR on images that are missing
  from it. This fits well with C3.

- [x] **A4. Every box you scroll past counts as edited** [code-read] - fixed: a box is only an edit if it differs from its default; a stored edit is only removed after a deliberate revert (recorded user action, persisted in the session), and every replaced/removed stored edit is archived to `finalized_edits_history.json`
  When a row is scrolled out of view, `ReviewFrame._destroy_row` saves every
  box's text unconditionally. `_get_box_text` then returns the live or saved
  text for message and spacer boxes whether or not you changed them. So
  `collect_edited_texts` reports "edits" for every message and spacer box
  that was ever built. This means:
  - `finalized_edits.json` stores copies of default text for everything you
    scrolled past.
  - On future runs, those stale copies win over new defaults (spacer-count
    changes in `config.py`, changes to the dice command/result context).
  - Session files get bigger than they need to be.
  OCR boxes aren't affected, because their checkbox controls this.
  *Fix:* only report or save a value when it differs from the default, or
  when the box really was edited.

- [ ] **A5. Mouse wheel over the review canvas probably does nothing on Linux** [likely]
  `review_view.py` only binds `<MouseWheel>`. Tk 8.6 on X11 (the installed
  version is 8.6) sends `<Button-4>`/`<Button-5>` for the wheel; the two were
  only unified in Tk 8.7. So the wheel probably only scrolls inside
  individual text boxes, using Tk's own class bindings, and the "scroll the
  box, then fall through to the page" logic never runs.
  *Fix:* bind Button-4/5 too, and turn them into the same delta handling.
  Please check on your machine first.

- [ ] **A6. The context-menu browser/Explorer launch blocks the UI (Windows)** [code-read]
  `image_context_menu._launch_url_in_default_browser` and
  `_open_image_location` use `subprocess.run`, which waits for the process to
  exit. If the browser wasn't already running, the app freezes until you
  close the browser.
  *Fix:* use `subprocess.Popen`.

- [ ] **A7. A few unexpected parse errors leave the app stuck on the progress screen** [code-read]
  `main_window._on_ocr_done` only catches `OSError`/`ValueError`. Anything
  else, for example a `KeyError` from an `<img>` with no `src` in
  `chatlog._parse_message`, only gets logged by
  `report_callback_exception`, and the UI stays on the progress screen.
  *Fix:* catch everything there and route it to `_on_run_error`.

- [ ] **A8. Small setup-screen bugs** [code-read]
  - "Known users → Add" inserts at `"end"` without adding a newline first.
    If the last line has no trailing newline, the new entry gets joined onto
    it.
  - The start-date label says `YYYY-MM-DD`, but the pre-filled value is
    `YYYY-MM-DD-HH-MM-SS` and is read as **UTC**. The screen never says so.

- [x] **A9. An edit behind an unchecked OCR box is lost on restart** [code-read] - decided: intended (unchecking = use OCR); README corrected
  For an unchecked OCR box, `_get_box_text` returns `None`, so
  `_user_edited_texts` for that box is never autosaved. Re-checking the box
  after a restart can't bring the edit back. The README suggests it can (see
  E3). Decide which behaviour you want, then fix either the code or the
  README.

---

## B. Structural / design risks

- [ ] **B1. The undo-history replay (`app/gui/text_undo.py`) is the biggest liability**
  All of this exists only so Tk's built-in undo survives a row being rebuilt.
  It works like this:
  - It renames each Text widget's Tcl command and records every insert and
    delete.
  - It replays all of them when the row is rebuilt.
  - It needs a self-heal step, plus per-keystroke fingerprint logging, to
    catch replay mistakes.
  Three bugs have already shipped here (see `docs/ARCHITECTURE.md`). Other
  problems:
  - The proxy swallows **every** `TclError` from **any** subcommand and
    returns `""`, including errors from the app's own Python calls.
  - `UndoLog.ops` grows without limit, and replay cost grows with the number
    of edits every time the row is loaded again.
  *Alternatives, simplest first:*
  - (a) An undo stack per box that stores text snapshots, grouped by pause
    or word boundary, applied by replacing the text. It doesn't depend on
    the widget still existing.
  - (b) Never destroy a Text widget once it has been visited; just
    `pack_forget`/re-pack it.
  Either one removes replay, the self-heal step, and most of the forensic
  logging.

- [ ] **B2. Per-box state is spread across about 12 parallel dicts on one class split over several files**
  `ReviewFrame` plus 3 mixins (`RowBuildingMixin`, `KeyboardNavMixin`,
  `ImageContextMenuMixin`) all share state keyed by `(idx, role)`:
  `_saved_texts`, `_saved_cursor`, `_checkbox_checked`, `_user_edited_texts`,
  `_undo_logs`, `_undo_detach`, `_text_widgets`, `_text_containers`,
  `_checkbox_vars`, `_spellcheck_after_ids`, `_suppress_ocr_auto_check`,
  `_refocus_slot`. Most of the recorded bugs came from these getting out of
  step with each other.
  *Fix:* one `SlotState` dataclass per slot for the model (text, default,
  cursor, checked, user edit, undo history), plus a thin view layer holding
  the live widgets. Separately, `App` reaches into the frame's private
  `_materialized_range`.

- [ ] **B3. Fragile row-height estimation and correction**
  `virtualization.estimate_row_height` uses hardcoded pixel constants
  (`~7px/char`, `18px` line, `SPACER_BOX_HEIGHT_PX = 30`) and assumes the
  `Consolas` font, which doesn't exist on Linux. Rows are then measured again
  after they're built, and the scroll position is corrected. The correction
  step, `_settle_pending_geometry`, calls `canvas.update()` and relies on
  `_reconcile` being called again from inside itself. This breaks under
  HiDPI scaling or font substitution.
  *Fix:* get the constants from real `tkinter.font.Font` metrics, and pick a
  font that exists on each platform (e.g. `TkFixedFont` as a fallback).

- [ ] **B4. Choose OCR images from the chatlog, not by file date**
  `run_ocr_batch` picks images by `os.path.getctime >= start_time`. On Linux
  that's the inode-change time, not the creation time. It also runs OCR on
  images from unapproved authors.
  *Fix:* parse the HTML first, then run OCR on exactly the images the kept
  messages reference. That makes the incremental cache in A3 easy, and makes
  it easy to run OCR in parallel, since Tesseract runs as a separate
  process.

- [ ] **B5. The output file is the only file that isn't written safely** - partly done in A2: now atomic; still no backup of the previous version
  Everything in `state.py` is written atomically with a `.bak` copy, but
  `finalize_run` reads the *whole* output file (including past runs), runs a
  regex over it, and rewrites it in place with no backup.

- [ ] **B6. `cleanup.clean_transcript` deletes every literal `\n` in the whole output file**
  It also hits past runs and any genuine text containing a backslash-n.
  Spacer tokens are already parsed in `review_item.lines_for_item`, so this
  step is probably redundant now. Check, then remove it or limit it to the
  text added this run.

- [ ] **B7. Background thread calls `root.after()` directly**
  The OCR worker in `main_window._begin_run` calls Tk from a non-main thread.
  This usually works with a threaded Tcl build, but it isn't guaranteed.
  *Fix:* use the standard pattern, a `queue.Queue` that the main thread
  polls.

- [ ] **B8. Timestamps and DST** [likely]
  `chatlog._parse_export_timezone` reads a single UTC offset from the
  postamble. If DiscordChatExporter writes local time *including DST as it
  was at message time*, messages from the other side of a DST change are off
  by 1h. That only matters for the start-date cutoff. Check against an
  export that spans a DST change. Separately, `TIMESTAMP_FORMAT` is
  hardcoded, and DCE's date format depends on locale and options.

---

## C. Persistence / state

- [ ] **C1. State keys are raw path strings**
  `str(Path(p))` isn't resolved or normalised. The same chatlog reached
  through a different spelling, a symlink, or different case on Windows
  misses its session, finalized edits and cache. Moving or renaming a
  chatlog silently orphans all of its finalized edits.

- [ ] **C2. No version field in any state JSON file**
  This makes future format changes hard to migrate. The old-session
  detection code that was removed earlier is an example of why it's needed.

- [ ] **C3. Whole-file read-modify-write on every save**
  The autosave rewrites **every** chatlog's session (with fsync) every 5s.
  `ocr_cache.json` holds every folder and grows forever. That's fine at
  current scale, but it's worth splitting into one file per chatlog or
  folder, or using SQLite.

- [ ] **C4. No protection against two running instances**
  Two copies of the app save over each other, and the last write wins. A
  simple lockfile would prevent this.

---

## D. Tests / tooling / packaging

- [ ] **D1. The suite is never green on Linux**
  - 213 pass, 9 fail.
  - 6 of the failures are Windows-only tests in `test_image_context_menu.py`,
    which should have `skipif(sys.platform != "win32")`.
  - 1 is `test_chatlog.py::test_real_export_with_multiple_attachments_per_message`,
    which needs the private `example_inputs/`. It should be skipped when the
    file is missing.
  - The other 2 are the state tests from A1.

- [ ] **D2. No CI, no coverage, no lint or type-check config**
  Dev dependencies (`pytest`, `ruff`) aren't declared anywhere. Ruff mostly
  reports style drift (`List` mixed with `list`, `Optional` mixed with `|`),
  plus a few unused variables in the tests.

- [ ] **D3. Dependencies are pinned in two places**
  `requirements.txt` repeats `pyproject.toml` and can drift from it. Keep
  `pyproject.toml` as the single source, with a `[project.optional-dependencies] dev`
  group.

- [ ] **D4. Packaging**
  - The top-level package is named `app`, which is very generic.
  - The user-editable `.txt` files aren't package data, so only an editable
    install works.
  - Tests import `gui_transcription.app.*`, while the installed app uses
    `app.*`. That's two identities for the same code, and logger names differ
    between them.
  *Fix:* rename the package (e.g. `discord_transcription`). Either declare
  the `.txt` files as package-data, or move the user-editable ones to
  `APP_DATA_DIR`, seeded from defaults on first run.

- [ ] **D5. Docstring style**
  `CLAUDE.md` asks for Google-style docstrings (Args/Returns). The code uses
  long narrative prose instead. There are also a few unannotated defs, e.g.
  `state._read_json_with_backup`, `ImageLoader.__init__`, and the inner
  handlers in `ReviewFrame.__init__`.

---

## E. Documentation vs code

- [ ] **E1. README describes the old scrolling design**
  The review-screen section says "(12 by default)", "pages the next/previous
  half-window in and tears the opposite half down", and "pins a surviving
  row's on-screen position (scroll anchoring)". That's the design the
  `review_view.py` module docstring says was *replaced*. The real behaviour:
  keep one viewport of rows loaded on each side, recompute the range from
  scratch each time, and correct the scroll position after measuring.

- [x] **E2. The README's claim about which edits are saved at Finalize is wrong** - fixed with A4
  It says "every box that had a user edit" is stored. In practice it's every
  loaded message/spacer box (see A4).

- [x] **E3. The README's claim about resuming checkbox edits is wrong** - fixed with A9
  It says resuming shows your edited version "regardless of whether it
  happened to be checked or unchecked" at close. That isn't true for
  unchecked boxes (see A9).

- [ ] **E4. Stale section references in code comments (9 places)**
  They point to `ARCHITECTURE.md`'s "Spacer slots", "Row geometry" and
  "<<Modified>> fires on a box's initial population" sections, which have
  moved to the topic docs. Found in `cleanup.py`, `config.py`,
  `review_item.py`, `review_view.py` and `keyboard_nav.py`.

- [ ] **E5. Small inaccuracies**
  - The `main.py` docstring says `py -3.13`.
  - The README layout says "delegates ... to the five modules below" but
    lists eight.
  - The README says "~70 tests" in `test_review_view.py`; there are 54 test
    functions.
  - README "Known gaps" says only one backup generation is kept, but
    `session_backups.json` now keeps 3.
  - Several docs cite `INVESTIGATION_*.md` without the `archive/` prefix.

- [ ] **E6. Too much history in code comments**
  Many docstrings are change histories ("an earlier version did...",
  "this used to..."). That belongs in git history and `archive/`. In the
  code it hides the logic and keeps producing stale references like E4.
  Trim them down to what the code does now and why.

---

## F. Minor / polish

- [ ] **F1. Logging defaults**
  Logging is hardcoded to `DEBUG`, and JSON goes to stdout. The scroll-trace
  log can reach 40MB x 7, about 280MB. Make the level configurable and
  shrink the defaults once B1 removes the need for forensic logging.
- [ ] **F2. `keyboard_nav._focused_slot` has an unreachable `return None`.**
- [ ] **F3. Redo fires with Caps Lock on**
  `<Control-Z>` is bound for redo, so with Caps Lock on, Ctrl+Z redoes
  instead of undoing.
- [ ] **F4. Image loading details**
  `image_loading.fitted_image_size` opens every image's header when the
  review screen is built. There's also no EXIF orientation handling for
  phone photos.
- [ ] **F5. Spellcheck word lists need a restart**
  The whitelist and blacklist are cached for the life of the process, so
  edits to them only take effect after a restart.
- [ ] **F6. The `\|` → `I` OCR correction replaces every pipe character**
  That includes genuine ones. It's a design choice; noting it in case it
  isn't what you want.
- [ ] **F7. Linux context-menu actions**
  `xdg-open` covers Open Image, Open Image in Browser, and Open Chatlog at
  Message. For Copy Image, pipe PNG bytes to
  `xclip -selection clipboard -t image/png`.

---

## Suggested order

1. A1: test isolation, and clean the polluted backup file.
2. A2: make Finalize safe to retry (save first, atomic write, confirmation).
3. A4 + A9 + E2/E3: only store real edits; decide how unchecked edits should
   behave.
4. A3 + B4: build the image list from the chatlog, with an incremental cache.
5. A5: Linux mouse wheel.
6. D1: make the suite green on Linux (skip markers).
7. E1-E5: README and comment fixes.
8. B1 + B2: snapshot-based undo and a per-slot state object (the big one).
