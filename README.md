# Discord Transcription GUI Tool

A Tkinter rewrite of the CLI transcription script in
`original_transcription_program/`. It turns a DiscordChatExporter HTML
export + media folder into a corrected plain-text transcript, OCR'ing
attached images with Tesseract and letting you fix mistakes with real GUI
widgets instead of memorizing terminal text codes.

See `original_transcription_notes.md` for the full analysis of the original
script this was rebuilt from, including the design decisions and improvement
scope agreed on for this rewrite. See `ARCHITECTURE.md` for the review
screen's internals (the most architecturally involved part of the app) and
logging conventions - this README sticks to what it does and how to run it.

## Status

v1 is implemented, unit-tested, and has now been exercised end-to-end
against a real Discord export (OCR pass, review screen, finalize).

What's in scope for v1 (by design, agreed with the project owner):
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
   messages get kept, one per line in the form `123456789012345678 - Alice` 
   - only the leading digits (the actual Discord user ID) are used for 
   filtering, the rest is just a human-readable label. It's pre-filled with 
   whatever was used last run; individual entries you've typed before are also
   remembered and can be re-added via the "Known users" dropdown next to it
   without retyping the ID. A "Transcribe messages from all users" checkbox
   bypasses the filter entirely (and greys out the users box, since it's
   moot while checked). The window launches maximized.

   In-progress review sessions are saved per chatlog HTML file, indefinitely,
   not as a single global slot - so two different chatlogs can each be left
   mid-review (closing the app rather than clicking Finalize) and later
   resumed independently, without one evicting the other. The OCR cache is
   likewise kept per image folder rather than for only the most recent one,
   so switching between chatlogs never forces a re-OCR of a folder already
   done. Clicking Start checks whether the chosen HTML file has a saved
   session and, if so, prompts to resume it - accepting re-runs that
   session's saved inputs (OCR cache permitting) straight through to the
   review screen, with every saved edit, the focused text box, and the
   scroll position all restored. Declining discards that chatlog's saved
   session outright (other chatlogs' saved sessions are unaffected).

   Resuming re-runs HTML parsing/OCR from the saved inputs rather than
   serializing the parsed messages themselves, but saved edits are matched
   back onto the freshly-parsed messages by Discord's own per-message ID
   (read from the export's `data-message-id`, not by list position - see
   "HTML parsing" below) - so a chatlog re-exported with more messages
   appended or inserted anywhere in the transcript still has every prior
   edit land back on the right message. An edit whose message no longer
   appears (e.g. its author was later removed from the approved list) is
   simply dropped rather than misapplied to a different message, silently
   (logged, not surfaced as a popup, since this is an expected outcome of
   normal chatlog growth rather than an error) - similarly, a saved focus
   position whose message has disappeared just isn't restored rather than
   landing on the wrong box.
2. **OCR pass** (background thread, progress bar) — walks the image folder,
   skips non-image files and anything older than the start date, and runs
   Tesseract on the rest. Results are cached to disk as JSON so a re-run
   (e.g. to redo just the correction pass) doesn't repeat OCR work.
3. **HTML parsing** — parses the export, keeping only messages after the
   start date from the approved users entered on the setup screen (or every
   user, if "all users" was checked). DiscordChatExporter timestamps every
   message in the *exporting device's* local timezone by default, not UTC -
   but it also records exactly which offset that was as a "Timezone:
   UTC+H[:MM]" line in the export's postamble, which is read and used to
   convert every message's timestamp to a true UTC epoch (matching how the
   start date and the next run's recorded start date are both handled in
   UTC too). A chatlog export missing that postamble line raises a clear
   error rather than silently guessing a timezone. Each kept message also
   keeps Discord's own per-message ID (the export's `data-message-id`,
   read from its `chatlog__message-container` wrapper) - not used for
   filtering, only as the stable key resumed sessions match saved edits
   against (see "Setup screen" above). A message whose container is
   missing the ID raises a clear error rather than silently falling back
   to a less stable identity.
4. **Review screen** — an infinite-scroll window listing every approved
   message in order, mirroring the original chatlog. Every row has the same
   two-column shape, both columns stacked text-above-image when a message
   has both (matching Discord's own layout): an immutable left column (the
   message's own original text, its image, or both) paired with the
   matching editable box(es) on the right — a copy of the message's own
   text whenever it has any, and/or a box pre-filled with its image's OCR
   text (all paragraphs joined together, with empty paragraphs - including
   the trailing blank line Tesseract routinely leaves at the end of a
   page - dropped rather than left as stray blank lines in the box and the
   eventual output) whenever it has an image,
   independently editable, so a message with both a caption and an image
   gets two separate boxes rather than one covering both. Copy, paste, and
   arbitrary edits are all allowed in every text box, nothing is parsed or
   restricted. Only a bounded window of rows (12 by default) is
   ever built as actual widgets at once; scrolling near either edge of that
   window pages the next/previous half-window in and tears the opposite
   half down, so scrolling stays responsive no matter how long the
   transcript is. Each page transition pins a surviving row's on-screen
   position and compensates the scroll offset for whatever was
   added/removed above it ("scroll anchoring") — without that compensation,
   paging in more rows above the viewport made the next page-load trigger
   *more* likely rather than less, causing a runaway cascade of transitions
   back toward the start of the transcript. Edits survive a row being paged
   out and back in, and the focused text box keeps focus across a transition
   if it's still in the new window. Typing into a box that's still focused
   but has been scrolled off-screen (the mouse wheel/scrollbar can move the
   viewport without touching focus at all) scrolls its row back into view
   automatically, rather than leaving keystrokes landing somewhere the user
   can't see. Images within the materialized window
   are additionally decoded/loaded lazily as you scroll near them (and
   unloaded again once you scroll away). Nothing is written to disk while
   reviewing - but every edit, the focused text box, and the scroll position
   are autosaved to disk every 5 seconds, so closing the app at any point
   mid-review leaves a session that can be resumed from the setup screen's
   prompt next launch (see "Setup screen" above).

   Every left column is the same fixed width (so the image/label and
   text-box columns line up neatly across every row), but each image's
   *height* is its own aspect-preserving fit within that width — a wide
   (landscape) image, the common case, ends up much shorter than a tall
   (portrait) one, rather than every image being letterboxed inside a
   single fixed box-shaped slot. The immutable original-text label uses
   the same font/size as the editable boxes (it used to be smaller, before
   every message got an editable copy of its own text) — its background is
   left at the plain default, matching the image column's own background,
   so it still reads as visually distinct from the editable copy beside
   it. Each editable text box's height is fixed up front rather than
   resized to fit its content as you type: it matches its paired immutable
   element's own on-screen height — the label's, for a "message" box (a
   copy of the message's own text), or the image's, for an "ocr" box (an
   image's OCR text) — plus a small margin, capped at roughly 70% of the
   screen's height. It never grows past that fixed height for a long
   message or a lot of typing — it gets its own internal scrollbar instead
   (appearing/disappearing automatically based on whether the text actually
   overflows the box). Scrolling the mouse wheel while hovering over a
   text box that has its own scrollbar scrolls *that box* until it hits the
   end of its content, then further scrolling in the same direction falls
   through to scrolling the whole review window, same as if the box
   weren't there.

   Keyboard shortcuts on the review screen: **Ctrl+Backspace** deletes the
   previous word; **Tab**/**Shift+Tab** move between text boxes in
   transcript order (a message with both a message-text and an OCR box
   visits the message-text one first, matching their top-to-bottom order
   on screen; paging the window in if needed), landing on the Finalize
   button once there's no further box; **Page Up**/**Page Down** scroll
   the whole window, overriding Tk's default of scrolling within whichever
   text box has focus; **Ctrl+Z**/**Ctrl+Shift+Z** undo/redo within a
   single text box.
5. **Finalize** — a button that floats over the bottom of the review
   screen, but only once you've scrolled all the way to the end of the
   transcript (or the whole transcript fits on screen with nothing to
   scroll past) - it stays out of the way the rest of the time instead of
   permanently occupying its own strip below the review area. Clicking it
   writes every message's final lines (edited text if you changed it, original OCR/
   message text otherwise) to the output file in one pass, then runs the
   original regex cleanup pass, records the new run-end date, copies the
   newly-added text to the clipboard, appends a fresh `[BREAK]` marker as a
   bookmark for the next run, and clears the autosaved session - there's
   nothing left to resume once a run has actually been finalized.

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
    state.py             # JSON run-date log, OCR cache, approved-users
                          # state, in-progress session save/resume - every
                          # write goes through atomic write-then-replace
                          # with .bak rotation, every read falls back to
                          # the .bak if the primary file is missing/corrupt
    ocr.py                # Tesseract OCR behind a swappable backend interface
    chatlog.py            # HTML parsing + date/author filtering
    cleanup.py            # post-run regex cleanup pass
    pipeline.py           # OCR batch runner, review-item building,
                          # bulk output writing, finalization
    logging_config.py     # JSON file + console logging setup
    gui/
      main_window.py      # run orchestration + session persistence on the
                          # Tk side - constructs setup_view.py/progress_view.py/
                          # review_view.py in turn as each stage starts
      setup_view.py        # setup screen: file/folder pickers, start date,
                          # cache checkbox, approved-users list
      progress_view.py    # OCR progress bar
      review_view.py       # the review screen: virtualized row window,
                          # Finalize button (delegates layout/images/
                          # keyboard nav to the three modules below)
      virtualization.py    # pure row-height/visible-range math (no Tk)
      image_loading.py     # lazy image load/unload for review rows
      keyboard_nav.py      # Tab/Page Up-Down/undo keyboard shortcuts
      theme.py             # dark theme colors/fonts + ttk Style setup
  app_tests/              # pytest unit tests for all the non-GUI logic
  requirements.txt
  original_transcription_program/   # the original CLI script, kept as reference
  original_transcription_notes.md   # analysis + decisions behind this rewrite
  ARCHITECTURE.md         # review-screen internals + logging conventions
```

## Running it

Install it as an editable package (once), which registers the `app` package
on Python's path globally and adds a console-script entry point:

```powershell
py -3.13 -m pip install -e gui_transcription
```

Then, from any directory:

```powershell
discord-transcription-gui
```

Without installing, it can also be run directly from inside `gui_transcription/`:

```powershell
py -3.13 -m pip install -r requirements.txt
py -3.13 -m app.main
```

## Testing

```powershell
py -3.13 -m pytest gui_transcription\app_tests -v
```

111 tests cover the cleanup regexes, HTML parsing/filtering (including the
export postamble's declared timezone being applied to every message
timestamp, the clear error raised when that timezone is missing or
unparseable, the per-message Discord ID extracted from each
`chatlog__message-container`'s `data-message-id`, and the clear error
raised when that container is missing), OCR paragraph splitting and
backend dispatch, the JSON log formatter, JSON state
persistence (run dates, OCR cache and in-progress sessions both kept
per-chatlog/per-folder indefinitely rather than as a single global slot,
recent-path history), start-date validation, review-item building/output-writing
(including a text-only message's editable spacing copy standing in for its
immutable original when written out, and a message with both a caption
and an image getting two independently-edited text blocks), the OCR batch
runner/cache short-circuit, the atomic-write-plus-backup-rotation/recovery
behavior of every state file (`app/state.py`), and the finalize pass (cleanup + run-date +
clipboard + BREAK-marker bookmarking), the review screen's slot-based
keyboard navigation (`_move_focus` stepping through `(item_index, role)`
slots in transcript order, message-before-ocr for a row with both), its
row-height estimation/visible-range math (`app/gui/virtualization.py`,
the part of the windowing logic that's pure enough to unit-test without a
display), and resume's message-id-based edit/focus matching
(`app/gui/main_window.py`'s `_match_saved_edits`/`_match_focus_slot` -
edits surviving messages appended or inserted mid-transcript in a
re-export, orphaned edits for now-filtered-out messages being dropped, and
a stale focus slot falling back to no restore). The GUI itself only has a
manual smoke test (window
construction, the review screen with synthetic text-only/image-only/
image-with-caption items, an edit-then-finalize pass against a temp
output file, and a resumed session's saved edits/focus restoring
correctly) — there's no automated test driving real Tk button clicks or a
live Tesseract install.

## Known gaps / next steps

- Other `config.py` constants (Tesseract path, skip-types, etc.) are still
  not editable from the UI (deferred, not an immediate priority) - only the
  approved-users list has been moved out of config.py so far.
- Paging back up to revisit an earlier page re-decodes its images from disk
  (no cross-page image cache); only the edited text itself is cached across
  a page being torn down and rebuilt.
- A text box that's actively growing while you type (see
  `ARCHITECTURE.md`'s "Review screen internals") only updates its own row's
  recorded height immediately - neighboring rows' positions are only
  corrected on the next scroll-driven reconcile, not instantly, though this
  has no visible effect since the row being typed in doesn't move on screen
  either way.
- Only one generation of backup is kept per state file (`*.bak`), not a
  full history - a crash can still lose up to one autosave interval's
  worth of review edits (5 seconds, `AUTOSAVE_INTERVAL_MS`) if it happens
  between two autosaves, since the .bak only protects the *previous*
  successful write, not the in-memory edits since then (low priority).
- There's no UI for resetting a message's editable copy back to its
  original OCR/message text once edited (deferred, not an immediate
  priority, per the original feature request).
