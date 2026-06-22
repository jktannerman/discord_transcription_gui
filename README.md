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
- The approved-author allow-list and other config values are hardcoded
  constants in `app/config.py` (isolated there for an eventual settings
  screen, but not yet exposed in the UI).

## What it does

1. **Setup screen** — pick the chatlog HTML file, the exported image folder,
   and the output `.txt` file. The start date field is pre-filled from the
   last recorded run; a "use cached OCR data" checkbox is enabled only when
   a matching cache already exists for the selected image folder.
2. **OCR pass** (background thread, progress bar) — walks the image folder,
   skips non-image files and anything older than the start date, and runs
   Tesseract on the rest. Results are cached to disk as JSON so a re-run
   (e.g. to redo just the correction pass) doesn't repeat OCR work.
3. **HTML parsing** — parses the export, keeping only messages after the
   start date from the approved author IDs (the GM + dice-roller bot).
4. **Correction screen** — for each approved message with an attached image,
   shows the image alongside an editable text box, one OCR paragraph at a
   time, with buttons for: Accept, Back, Retry, Accept-all-remaining,
   Skip-rest-of-image, Skip-this-paragraph, and Accept-&-stop. These map
   directly onto the original script's `bbb/ccc/ddd/eee/fff/ggg` terminal
   codes. Each message's lines are appended to the output file as soon as
   it's resolved (no separate "save" step).
5. **Finishing up** — runs the original regex cleanup pass over the whole
   output file, records the new run-end date, copies the newly-added text
   to the clipboard, and appends a fresh `[BREAK]` marker as a bookmark for
   the next run.

## Project layout

```
gui_transcription/
  app/
    main.py              # entry point
    config.py            # constants: paths, allow-list, markers
    state.py             # JSON run-date log + OCR cache (replaces pickle)
    ocr.py                # Tesseract OCR behind a swappable backend interface
    chatlog.py            # HTML parsing + date/author filtering
    cleanup.py            # post-run regex cleanup pass
    pipeline.py           # OCR batch runner, correction state machine,
                          # run orchestration, finalization
    gui/
      main_window.py      # setup screen + run orchestration on the Tk side
      progress_view.py    # OCR progress bar
      correction_view.py  # one-paragraph-at-a-time correction screen
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

27 tests cover the cleanup regexes, HTML parsing/filtering, OCR paragraph
splitting, JSON state persistence, start-date validation, and the full
paragraph-correction state machine. The GUI itself only has a manual smoke
test (window construction, validation paths) — there's no automated test
driving real Tk button clicks or a live Tesseract install.

## Known gaps / next steps

- No real-data end-to-end test yet (see Status above).
- Author allow-list and other `config.py` constants are not yet editable
  from the UI (deferred, not an immediate priority).
- No "undo across messages" — once a message is submitted, only the
  in-progress message's paragraphs can be revisited (via Back), matching
  the original script's per-message-append behavior.
