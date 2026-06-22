# Discord Transcription GUI Tool

A Tkinter rewrite of the CLI transcription script in
`original_transcription_program/`. It turns a DiscordChatExporter HTML
export + media folder into a corrected plain-text transcript, OCR'ing
attached images with Tesseract and letting you fix mistakes with real GUI
widgets instead of memorizing terminal text codes.

See `original_transcription_notes.md` for the full analysis of the original
script this was rebuilt from, including the design decisions and improvement
scope agreed on for this rewrite.

## Status

v1 is implemented and unit-tested, but **not yet exercised against a real
Discord export** — there's no sample HTML/media fixture in this repo, so the
next step is a real end-to-end run with your own exported data.

What's in scope for v1 (by design, agreed with the project owner):
- Just the core transcription flow — no Google Doc integration, no
  settings UI, no multi-project support, no driving the
  `DiscordChatExporter.Cli.exe` export step (that stays a separate manual
  step before this tool runs).
- Tesseract is the only OCR backend, but it's called through a small
  swappable interface (`app/ocr.py`) so an EasyOCR backend could be added
  later without touching calling code.
- Most config values are hardcoded constants in `app/config.py` (isolated
  there for an eventual settings screen, but not yet exposed in the UI). The
  approved-author list is the one exception - it's entered and cached from
  the setup screen, the same way the file/folder pickers are.

## What it does

1. **Setup screen** — pick the chatlog HTML file, the exported image folder,
   and the output `.txt` file, each a combo box pre-filled with the most
   recently used value and offering your last several picks as a dropdown
   (cached to disk per field, most-recent-first). The start date field is
   pre-filled from the last recorded run; a "use cached OCR data" checkbox
   starts checked and can always be toggled - if it's checked but no
   matching cache exists for the selected image folder, OCR just runs
   normally. An "Approved users" multi-line box lists which Discord users'
   messages get kept, one per line in the form `123456789 - Alice` - only
   the leading digits (the actual Discord user ID) are used for filtering,
   the rest is just a human-readable label. It's pre-filled with whatever
   was used last run; individual entries you've typed before are also
   remembered and can be re-added via the "Known users" dropdown next to it
   without retyping the ID. A "Transcribe messages from all users" checkbox
   bypasses the filter entirely (and greys out the users box, since it's
   moot while checked). The window launches maximized.
2. **OCR pass** (background thread, progress bar) — walks the image folder,
   skips non-image files and anything older than the start date, and runs
   Tesseract on the rest. Results are cached to disk as JSON so a re-run
   (e.g. to redo just the correction pass) doesn't repeat OCR work.
3. **HTML parsing** — parses the export, keeping only messages after the
   start date from the approved users entered on the setup screen (or every
   user, if "all users" was checked).
4. **Review screen** — an infinite-scroll window listing every approved
   message in order, mirroring the original chatlog. Text-only messages are
   shown for context; messages with an attached image show that image
   (large — roughly two-thirds of the window's width) next to a single
   freely-editable text box pre-filled with its OCR text (all paragraphs
   joined together) — copy, paste, and arbitrary edits are all allowed,
   nothing is parsed or restricted. Only a bounded window of rows (12 by
   default) is ever built as actual widgets at once; scrolling near either
   edge of that window pages the next/previous half-window in and tears the
   opposite half down, so scrolling stays responsive no matter how long the
   transcript is. Each page transition pins a surviving row's on-screen
   position and compensates the scroll offset for whatever was added/removed
   above it ("scroll anchoring") — without that compensation, paging in more
   rows above the viewport made the next page-load trigger *more* likely
   rather than less, causing a runaway cascade of transitions back toward
   the start of the transcript. Edits survive a row being paged out and back
   in, and the focused text box keeps focus across a transition if it's
   still in the new window. Images within the materialized window are
   additionally decoded/loaded lazily as you scroll near them (and unloaded
   again once you scroll away). Nothing is written to disk while reviewing.

   Keyboard shortcuts on the review screen: **Ctrl+Backspace** deletes the
   previous word; **Tab**/**Shift+Tab** move between text boxes in
   transcript order (paging the window in if needed), landing on the
   Finalize button once there's no further text box; **Page Up**/**Page
   Down** scroll the whole window, overriding Tk's default of scrolling
   within whichever text box has focus; **Ctrl+Z**/**Ctrl+Shift+Z** undo/redo
   within a single text box.
5. **Finalize** — a single button at the bottom of the review screen writes
   every message's final lines (edited text if you changed it, original OCR
   text otherwise) to the output file in one pass, then runs the original
   regex cleanup pass, records the new run-end date, copies the newly-added
   text to the clipboard, and appends a fresh `[BREAK]` marker as a bookmark
   for the next run.

## Appearance

The whole app uses a dark theme (`app/gui/theme.py`), palette and dark-title-bar
trick borrowed from `song_folder_player/gui.py`, with the message-text/input-box
colors and font instead matched to `multi_file_search/multi_file_search.py`
(darker background, larger monospace font, brighter text cursor) since that read
more clearly for dense transcript text than the general UI palette. On Windows,
the title bar's dark mode is forced to repaint immediately on launch via a
`SetWindowPos(SWP_FRAMECHANGED)` call, since `DwmSetWindowAttribute` alone left
it light until the window was next resized.

## Project layout

```
gui_transcription/
  app/
    main.py              # entry point
    config.py            # constants: paths, markers, default approved users
    state.py             # JSON run-date log, OCR cache, approved-users state
    ocr.py                # Tesseract OCR behind a swappable backend interface
    chatlog.py            # HTML parsing + date/author filtering
    cleanup.py            # post-run regex cleanup pass
    pipeline.py           # OCR batch runner, review-item building,
                          # bulk output writing, finalization
    logging_config.py     # JSON file + console logging setup
    gui/
      main_window.py      # setup screen + run orchestration on the Tk side
      progress_view.py    # OCR progress bar
      review_view.py       # the full scrollable review screen + Finalize button
                          # (lazy image loading, large thumbnails)
      theme.py             # dark theme colors/fonts + ttk Style setup
  app_tests/              # pytest unit tests for all the non-GUI logic
  requirements.txt
  original_transcription_program/   # the original CLI script, kept as reference
  original_transcription_notes.md   # analysis + decisions behind this rewrite
```

## Running it

```powershell
py -3.13 -m pip install -r gui_transcription\requirements.txt
py -3.13 -m gui_transcription.app.main
```

(Run from the directory containing `gui_transcription/`, i.e. the repo root.)

## Testing

```powershell
py -3.13 -m pytest gui_transcription\app_tests -v
```

33 tests cover the cleanup regexes, HTML parsing/filtering, OCR paragraph
splitting, JSON state persistence (run dates, OCR cache, recent-path
history), start-date validation, and review-item building/output-writing.
The GUI itself only has a manual smoke test (window construction, the
review screen with synthetic data, an edit-then-finalize pass against a
temp output file) — there's no automated test driving real Tk button
clicks or a live Tesseract install.

## Logging

Every module logs through `app/logging_config.py`, which writes single-line
JSON records to both the console and a rotating log file at
`~/.discord_transcription_gui/app.log` (2MB x 3 backups). Covers run
start/exit, OCR batch progress, HTML parsing summaries (with skip-reason
counts), review-screen build/finalize events, and caught exceptions.

## Known gaps / next steps

- No real-data end-to-end test yet (see Status above).
- Other `config.py` constants (Tesseract path, skip-types, etc.) are still
  not editable from the UI (deferred, not an immediate priority) - only the
  approved-users list has been moved out of config.py so far.
- The review screen is a single long scroll of stacked rows (image +
  editable text box per message) rather than two independently-scrolling
  columns; this was the simpler, more robust layout to keep image and text
  vertically locked together while scrolling.
- Paging back up to revisit an earlier page re-decodes its images from disk
  (no cross-page image cache); only the edited text itself is cached across
  a page being torn down and rebuilt.
