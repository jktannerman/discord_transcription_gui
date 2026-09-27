# Discord Transcription GUI Tool

A Tkinter rewrite of the CLI transcription script in
`original_transcription_program/`. It turns a DiscordChatExporter HTML
export + media folder into a corrected plain-text transcript, OCR'ing
attached images with Tesseract and letting you fix mistakes with real GUI
widgets instead of memorizing terminal text codes.

See `docs/original_transcription_notes.md` for the full analysis of the
original script this was rebuilt from, including the design decisions and
improvement scope agreed on for this rewrite. See `docs/ARCHITECTURE.md` and
the topic docs it links to for the review screen's internals (the most
architecturally involved part of the app), spacer/geometry design, test
coverage, and logging conventions - this README sticks to what it does and
how to run it.

## Status

v1 is implemented, unit-tested, and has now been exercised end-to-end
against a real Discord export (OCR pass, review screen, finalize).

What's in scope for v1:
- Tesseract is the only OCR backend, but it's called through a small
  swappable interface (`app/ocr.py`) so an EasyOCR backend could be added
  later without touching calling code.
- Most config values are hardcoded constants in `app/config.py` (isolated
  there for an eventual settings screen, but not yet exposed in the UI). The
  approved-author list is the one exception - it's entered and cached from
  the setup screen, the same way the file/folder pickers are.
- It runs on Windows and Linux (X11) with Python 3.12 or newer. The core
  workflow (setup, OCR, review, finalize) works the same on both. The
  image right-click menu actions and the dark title bar are still
  Windows-only - see "Known gaps" below.

## What it does

1. **Setup screen** — pick the chatlog HTML file, the exported image folder,
   and the output `.txt` file, each a combo box pre-filled with the most
   recently used value and offering your last several picks as a dropdown
   (cached to disk per field, most-recent-first). The start date field is
   pre-filled from the last recorded run, and is read as UTC. A "Re-run OCR
   on all images (ignore cache)" checkbox starts unticked; normally
   previously OCR'd images are reused from the cache and only new or
   changed ones are OCR'd, and ticking it forces every image in this run
   to be OCR'd again. An "Approved users" multi-line box lists which
   Discord users' messages get kept, one per line in the form
   `123456789012345678 - Alice`. Only the leading digits (the actual
   Discord user ID) are used for filtering; the rest is just a
   human-readable label. It's pre-filled with whatever was used last run; individual entries you've typed before are also
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

   In addition, every text box that had a user edit when a run was Finalized
   is stored permanently per chatlog, so starting a **fresh run** of the same
   chatlog pre-populates each box with the previously-finalized edit rather
   than raw OCR. This means noticing a mistake later requires only a re-run
   to fix - previous edits are already there, not lost. On a **resumed
   session** the most recent in-session edit takes priority; the stored
   finalized edit fills in any box the session did not cover.

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
2. **OCR pass** (background thread, progress bar) — after the chatlog is
   parsed (see "HTML parsing" below), runs Tesseract on exactly the images
   the kept messages reference, not on everything in the image folder. The
   results are cached per image as JSON, together with each file's size and
   modification time, so a later run only OCRs images that are new or whose
   file changed. The cache is saved every 10 images during the pass, so
   closing the app or a crash partway through keeps most of the work
   done. Any referenced image that isn't in the image folder (e.g. an
   incomplete media download) is listed in a warning before the review
   screen opens, and its OCR box starts empty.
3. **HTML parsing** — parses the export, keeping only messages after the
   start date from the approved users entered on the setup screen (or every
   user, if "all users" was checked). DiscordChatExporter timestamps every
   message in the *exporting device's* local timezone by default, not UTC -
   but it also records exactly which offset that was as a "Timezone:
   UTC+H[:MM]" line in the export's postamble, which is read and used to
   convert every message's timestamp to a true UTC epoch (matching how the
   start date and the next run's recorded start date are both handled in
   UTC too). A chatlog export missing that postamble line raises a clear
   error rather than silently guessing a timezone. A message's images are
   read from every `chatlog__attachment` block it has (an uploaded file)
   *and* every `chatlog__embed` block (a pasted image URL/link that Discord
   unfurled, marked up as `chatlog__embed-generic-image` rather than
   `chatlog__attachment-media`) - Discord allows more than one image per
   message either way, and each gets its own OCR pass and its own editable
   box on the review screen (see "Review screen" below), in the same order
   they appear in the export.
   Each kept message also keeps Discord's own per-message ID (the export's
   `data-message-id`,
   read from its `chatlog__message-container` wrapper) - not used for
   filtering, only as the stable key resumed sessions match saved edits
   against (see "Setup screen" above). A message whose container is
   missing the ID raises a clear error rather than silently falling back
   to a less stable identity. Either error is caught where parsing runs
   (on the worker thread, before any OCR starts) and shown in the same
   error dialog OCR failures use, rather than leaving the app stuck on the
   progress screen with no indication anything went wrong.
4. **Review screen** — an infinite-scroll window listing every approved
   message in order, mirroring the original chatlog. Every row has the same
   two-column shape, both columns stacked text-above-images when a message
   has both (matching Discord's own layout): an immutable left column (the
   message's own original text, its image(s), or both) paired with the
   matching editable box(es) on the right — a copy of the message's own
   text whenever it has any, and one box per attached image pre-filled with
   that image's own OCR text (all paragraphs joined together, with empty
   paragraphs - including the trailing blank line Tesseract routinely
   leaves at the end of a page - dropped rather than left as stray blank
   lines in the box and the eventual output). A message can have more than
   one image attachment (Discord allows several per message) - each gets
   its own independently-editable OCR box, stacked in attachment order, the
   same way a caption and an image each get their own box rather than one
   covering both. Copy, paste, and arbitrary edits are all allowed in every
   text box, nothing is parsed or restricted. Only the rows on screen, plus
   about one screen's worth on either side, are ever built as actual
   widgets at once, so scrolling stays responsive no matter how long the
   transcript is. Shortly after scrolling pauses, the app works out from
   scratch which rows should be built for the current scroll position,
   builds the missing ones and tears down the rest, then measures the new
   rows and corrects the scroll position for any difference from their
   estimated height, so the content on screen doesn't jump. Edits survive a
   row being torn down and rebuilt, and the focused text box keeps focus
   if its row is still built - and even if it isn't (e.g. a fast Page
   Up/Page Down burst that skips straight past the built range), focus and
   the exact cursor position are restored once that row is built again,
   rather than just staying lost for the rest of the session.
   Typing into a box that's still focused
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
   single fixed box-shaped slot. Photos carrying an EXIF rotation tag
   (common from phones) are shown the right way up. The immutable original-text label uses
   the same font/size as the editable boxes (it used to be smaller, before
   every message got an editable copy of its own text) — its background is
   left at the plain default, matching the image column's own background,
   so it still reads as visually distinct from the editable copy beside
   it. Each editable text box's height is fixed up front rather than
   resized to fit its content as you type: it matches its paired immutable
   element's own on-screen height — the label's, for the "message" box (a
   copy of the message's own text), or that image's, for one of the "ocr"
   boxes (an image's OCR text, one per attachment) — plus a small margin,
   capped at roughly 70% of the screen's height. It never grows past that
   fixed height for a long message or a lot of typing — it gets its own
   internal scrollbar instead (appearing/disappearing automatically based
   on whether the text actually overflows the box). Scrolling the mouse
   wheel while hovering over a text box that has its own scrollbar scrolls
   *that box* until it hits the end of its content, then further scrolling
   in the same direction falls through to scrolling the whole review
   window, same as if the box weren't there. Horizontal scrolling (Shift +
   wheel, or a touchpad's sideways movement during a two-finger scroll) is
   ignored, since nothing on the review screen scrolls sideways.

   Right-clicking an image (loaded or not yet scrolled into view) pops up a
   standard context menu with five actions: **Open Image** (opens the image
   file with the system's default *image viewer*, the same program a
   double-click in the file manager would use), **Open Image in Browser**
   (opens it with the system's default *browser* specifically -
   Firefox/Chrome/Edge/etc., looked up the same way the OS itself resolves
   "open with default browser", independently of whatever program is the
   default image viewer), **Open Image Location** (opens its containing
   folder in the file manager with the file pre-selected), **Open Chatlog
   at Message** (opens
   the original chatlog HTML export, in the same default browser as "Open
   Image in Browser", scrolled straight to that image's message - handy for
   checking the surrounding conversation for context an OCR'd image alone
   doesn't give), and **Copy Image** (copies the actual picture - not a
   file path - to the clipboard, so it can be pasted into another app).
   "Open Chatlog at Message" targets the message's own
   `chatlog__message-container-<id>` element id, which DiscordChatExporter
   writes onto the same container element as its `data-message-id`
   attribute (see "HTML parsing" below) - a plain HTML fragment
   (`#chatlog__message-container-<id>`) anchor on the file's own `file://`
   URI, so the browser jumps straight there the same way it would for any
   in-page anchor link. Like any right-click menu, it closes on Escape or a
   click anywhere else; review-window scrolling (mouse wheel, Page Up/Down,
   dragging the scrollbar) is frozen for as long as it's open. Every open,
   close, and action click is logged, along with whether the action itself
   succeeded or failed.

   On Windows these use the registry (default browser), Explorer and the
   Win32 clipboard. On Linux they use freedesktop.org standards instead
   (`app/gui/desktop_linux.py`): `xdg-open` for Open Image, the browser
   `xdg-settings` reports as default (launched from its `.desktop` entry)
   for the two browser actions, the `org.freedesktop.FileManager1` D-Bus
   interface for Open Image Location (falling back to opening the folder
   without a selection if no file manager provides it), and `xclip` (X11)
   or `wl-copy` (Wayland) for Copy Image. A failed action is logged in
   `app.log` rather than crashing the app.

   Each "ocr" box additionally has a checkbox in an otherwise-invisible
   column at its own top-right corner - inside the box, compressing its text
   leftward, and to the left of that box's own scrollbar if/when one
   appears (a "message" box has no such checkbox, since it was never OCR'd
   and so has no "original" to track). Unchecked means the box's current
   text still matches the original (regex-corrected) OCR transcription;
   typing anything into the box checks it automatically. Unchecking a
   checked box snaps its text back to that original OCR transcription
   without discarding whatever you'd typed - re-checking it brings your
   edit straight back. Both versions, and the checkbox's own state, survive
   a row being scrolled out of the materialized window and back in, the
   same as an ordinary edit does. Unchecking means "use the OCR text": an
   edit hidden behind an unchecked box is kept only while the app stays
   open, so re-checking can bring it back within a session, but it isn't
   saved - after closing and resuming, that box is just the OCR text.
   Tab/Shift-Tab never land on the checkbox itself - only on the
   text boxes, same as before this existed.

   When a box is pre-populated from a previously-finalized edit (see
   "Setup screen" above), its checkbox starts **checked** if that finalized
   text differs from the current OCR default, so unchecking still reverts
   to OCR and re-checking brings back the finalized text. If the finalized
   text happens to match the current OCR (e.g. OCR corrections were not
   changed and the original was already correct), the checkbox starts
   unchecked and the box is visually indistinguishable from an ordinary
   fresh OCR result.

   Every "message"/"ocr" box also gets a basic spellcheck: a word not found
   in a standard English dictionary is underlined in red, the same way a
   word processor flags one - checked shortly after you stop typing (not on
   every keystroke), against a small user-editable whitelist
   (`app/spellcheck_whitelist.txt`, one word per line) for Discord
   usernames/slang that would otherwise be flagged every time, and skipping
   short words and ALL-CAPS acronyms to keep obvious false positives down.
   A complementary user-editable blacklist (`app/spellcheck_blacklist.txt`,
   same one-word-per-line format) does the opposite - it flags a word even
   though the dictionary considers it a real word, for real English words
   that keep turning out to be OCR misreads or typos for something else in
   this transcript's context. A word listed in both files is never
   flagged: the whitelist wins.
   Edits to either file take effect on the next spellcheck, without
   restarting the app.
   It's a plain dictionary lookup, not a language model, so it's meant to
   catch obvious OCR garbling rather than to be a correctness oracle for
   informal chat text - and it never applies to a spacer box (see below),
   which holds nothing but `\n` tokens anyway.

   Between every text/image piece - text and its first image, one image
   and the next, and the gap before the next message - there's also a
   **spacer box**: a one-line-tall, editable box holding nothing but
   literal `\n` characters (typed as backslash-n, not real line breaks),
   pre-filled with a default count that you can freely add to, remove
   from, or otherwise edit. Anything else typed into one is ignored. This replaces the original
   script's fixed regex-based spacing, which couldn't express anything
   finer than its own hardcoded rules. See `docs/ARCHITECTURE_SPACER_SLOTS.md`
   for the full default-spacing table and exactly how a spacer's content is
   parsed at Finalize.

   Keyboard shortcuts on the review screen: **Ctrl+Backspace** deletes the
   previous word; **Tab**/**Shift+Tab** move between text boxes in
   transcript order (a message visits its message-text box first, if it
   has one, then one OCR box per attached image, in attachment order,
   matching their top-to-bottom order on screen, with a spacer box visited
   between/after each of those too; paging the window in if needed),
   landing on the Finalize button once there's no further box;
   **Page Up**/**Page Down** scroll the whole window, overriding Tk's
   default of scrolling within whichever text box has focus; **Up**/**Down**
   move the cursor within a box as usual, but also scroll the review window
   itself if that would otherwise leave the cursor offscreen - holding
   **Down** at the bottom of a box's own view aligns that box's bottom edge
   with the bottom of the window (and **Up** the top edge with the top),
   rather than only the cursor's own line peeking into view;
   **Ctrl+Z**/**Ctrl+Shift+Z** undo/redo within a single text box (Caps
   Lock doesn't swap them) - history
   is kept separately per box and survives that box's row being paged out
   and back in, though (like everything else not written to the output
   file) not a full app restart.
5. **Finalize** — a button that floats over the bottom of the review
   screen, but only once you've scrolled all the way to the end of the
   transcript (or the whole transcript fits on screen with nothing to
   scroll past) - it stays out of the way the rest of the time instead of
   permanently occupying its own strip below the review area. Clicking it
   asks for confirmation, saves the session, then builds the new output in
   memory: the existing file, plus every message's final lines (edited
   text if you changed it, original OCR/message text otherwise, with each
   spacer box's blank-line count written out as real newlines in between),
   run through the remaining post-run cleanup (leftover literal `\n`s and
   trailing `[BREAK]` markers - blank-line spacing is no longer touched
   here, since spacer boxes already wrote exactly what you left in them;
   common OCR misreads are now fixed earlier, before you ever see the
   text; see "OCR corrections" below), plus a fresh `[BREAK]` marker as a
   bookmark for the next run. The previous version of the output file is
   first copied to `<output name>.bak` beside it (e.g. `transcript.txt.bak`),
   then the new output is written in one atomic replace, so a failure never
   leaves it half-written. Only this run's new text is cleaned up; text
   from earlier runs is never rewritten. If the write
   fails, nothing has changed on disk and you stay on the review screen to
   fix the problem and try again. Once it succeeds, the run counts as
   finalized: it records the new run-end date, copies the newly-added text
   to the clipboard, and clears the autosaved session, since there's
   nothing left to resume. A failure in any of those later steps (e.g. no
   clipboard tool installed) is shown as a warning on the done screen
   rather than undoing the run.

   Finalize also stores every box whose text differs from its default
   (an OCR box only while checked) into `finalized_edits.json`, keyed by
   this chatlog's HTML path and each message's Discord message ID, so
   running again on the same chatlog - e.g. to fix a noticed mistake -
   starts with every prior edit already in place. Spacer-box edits are
   included. Just scrolling past a box doesn't count as an edit.

   A stored edit is only removed when you deliberately revert that box:
   unchecking its OCR checkbox, or editing it back to its default text,
   during the session you finalize (the session remembers these actions
   across closing and resuming). A box that is at its default with no such
   action behind it keeps its stored edit, so a bug that unticked a box or
   reset its text could never erase a finalized edit. Anything a Finalize
   replaces or removes is first copied into `finalized_edits_history.json`
   (in the app's data folder), which is only ever appended to - so no
   finalized edit is ever lost for good; recovering one currently means
   copying its `old_text` out of that file by hand.

## OCR corrections

`app/ocr_corrections.txt` is a user-editable, plain-text list of regex
find/replace rules for common Tesseract misreads (e.g. a stray `|` instead
of a capital `I`) - not Python code, so it can be tuned by hand without
touching the app itself. Entries are blank-line-separated blocks of:

```
<find regex>
<replacement - \1 etc. refer to the find regex's capture groups>
# any number of comment lines (must start with "#")
```

Each entry is applied, in file order, to an image's freshly-OCR'd text
exactly once - right when it becomes that image's starting OCR-box content
on the review screen - never to a box once you've edited it, and never to
a message's own original text (which was never OCR'd in the first place).
A missing or empty file just means no corrections run. See the comments
in `app/ocr_corrections.txt` itself for the current rule set. Not yet
exposed in the GUI - per the "Known gaps" section below, that's deferred,
same as the rest of `config.py`'s settings.

## Appearance

The whole app uses a dark theme (`app/gui/theme.py`). On Windows,
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
                          # state, in-progress session save/resume, and
                          # finalized-edit persistence - every write goes
                          # through atomic write-then-replace with .bak
                          # rotation, every read falls back to the .bak if
                          # the primary file is missing/corrupt; a session's
                          # last 3 end-of-session states are also kept in a
                          # separate rotating backup file
                          # (archive_session_backup/load_session_backups),
                          # so resuming and continuing to edit doesn't
                          # erase the previous session's final state the
                          # way the single write-time .bak would
    ocr.py                # Tesseract OCR behind a swappable backend interface
    ocr_corrections.py     # loads/applies ocr_corrections.txt's regex fixes
    ocr_corrections.txt    # user-editable OCR-misread find/replace rules
    spellcheck.py          # dictionary-lookup spellcheck (misspelled word
                          # spans) + spellcheck_whitelist.txt/
                          # spellcheck_blacklist.txt loading
    spellcheck_whitelist.txt  # user-editable words never flagged as misspelled
    spellcheck_blacklist.txt  # user-editable words always flagged as misspelled
    chatlog.py            # HTML parsing + date/author filtering
    cleanup.py            # post-run regex cleanup pass (structural only -
                          # OCR-misread fixes moved to ocr_corrections.py)
    pipeline.py           # OCR batch runner, bulk output writing,
                          # finalization - orchestration glue only
    review_item.py        # ReviewItem domain model: slot-role ordering +
                          # spacer-token parsing shared by row building,
                          # height estimation, keyboard nav, output writing
    logging_config.py     # JSON file + console logging setup
    gui/
      main_window.py      # run orchestration + session persistence on the
                          # Tk side - constructs setup_view.py/progress_view.py/
                          # review_view.py in turn as each stage starts
      setup_view.py        # setup screen: file/folder pickers, start date,
                          # cache checkbox, approved-users list
      progress_view.py    # OCR progress bar
      review_view.py       # the review screen's windowing core (reconcile/
                          # paging/scroll-correction) + Finalize button -
                          # delegates row construction, images, and
                          # keyboard nav to the modules below
      row_building.py      # builds a single row's widgets (labels, image
                          # placeholders, editable text boxes/scrollbars)
      layout_constants.py  # row/text-box sizing constants shared by
                          # row_building.py and virtualization.py, so the
                          # real layout and its pre-build estimate can't
                          # drift out of sync with each other
      virtualization.py    # pure row-height/visible-range math (no Tk)
      image_loading.py     # lazy image load/unload for review rows
      image_context_menu.py  # right-click menu on a review row's image
                          # (open in browser/location, open chatlog at
                          # message, copy to clipboard) - freezes scrolling
                          # (mousewheel/Page Up-Down/scrollbar) while open
      desktop_linux.py     # Linux side of those actions: default browser,
                          # file manager, clipboard (freedesktop standards)
      keyboard_nav.py      # Tab/Page Up-Down/undo keyboard shortcuts
      wheel.py             # mouse wheel/touchpad events across platforms
                          # (<MouseWheel> vs X11's <Button-4>/<Button-5>;
                          # horizontal/Shift scrolls ignored)
      text_undo.py          # per-box undo/redo history that survives a
                          # row being paged out and rebuilt (in-memory only)
      theme.py             # dark theme colors/fonts + ttk Style setup
  app_tests/              # pytest unit tests for all the non-GUI logic
  requirements.txt
  original_transcription_program/   # the original CLI script, kept as reference
  docs/
    original_transcription_notes.md   # analysis + decisions behind this rewrite
    ARCHITECTURE.md         # architecture doc index + general testing heuristic
    ARCHITECTURE_REVIEW_SCREEN.md   # review screen internals (most involved part of the app)
    ARCHITECTURE_ROW_GEOMETRY.md    # document-space row spacing/offset accounting
    ARCHITECTURE_SPACER_SLOTS.md    # blank-line spacing model + finalize/session persistence
    ARCHITECTURE_TESTING.md         # what's covered where, what isn't
    ARCHITECTURE_LOGGING.md         # app.log/scroll_trace.log conventions
```

## Requirements

- **Python 3.12 or newer**, with Tkinter. On Windows, Tkinter comes with the
  python.org installer. On Debian/Ubuntu/Mint it's a separate package:
  `sudo apt install python3-tk`.
- **Tesseract OCR**, installed separately from the Python packages:
  - Windows: install it to the default location,
    `C:\Program Files\Tesseract-OCR\`. `app/config.py` points pytesseract
    there directly, since the installer doesn't add it to PATH.
  - Linux: `sudo apt install tesseract-ocr`. It only needs to be on PATH.
- **Linux only: a clipboard tool** for finalize's copy-to-clipboard step
  (pyperclip uses it) and the image menu's Copy Image: `sudo apt install
  xclip` on X11 (`xsel` also works for finalize, but not Copy Image), or
  `wl-clipboard` on Wayland.

## Running it

### Linux

Install it once with [pipx](https://pipx.pypa.io/). pipx creates and manages
an isolated virtual environment for the app and puts the
`discord-transcription-gui` command on your PATH:

```bash
pipx install --editable path/to/gui_transcription
```

Then, from any directory:

```bash
discord-transcription-gui
```

Install with `--editable`. The app reads `ocr_corrections.txt` and the
spellcheck whitelist/blacklist from next to its source files, and
`pyproject.toml` doesn't package those `.txt` files, so a normal
(non-editable) install would leave them out. An editable install also means
code and config changes take effect without reinstalling.

Don't `pip install` it into the system Python: most current distros mark it
as externally managed (PEP 668), and the package installs a top-level module
with the generic name `app`, which belongs in its own environment.

To run it without installing, use a virtual environment inside
`gui_transcription/`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app.main
```

### Windows

Install it as an editable package (once), which puts the `app` package on
Python's path and adds a console-script entry point:

```powershell
py -3 -m pip install -e gui_transcription
```

Then, from any directory:

```powershell
discord-transcription-gui
```

Without installing, it can also be run directly from inside `gui_transcription/`:

```powershell
py -3 -m pip install -r requirements.txt
py -3 -m app.main
```

Only one copy of the app can run at a time (it holds a lock on
`app.lock` in its data folder), since two copies would overwrite each
other's saved sessions and settings; starting a second one just shows an
"Already running" message.

### Logs

The app writes `app.log` and `scroll_trace.log` to its data folder
(`~/.discord_transcription_gui/`; see `docs/ARCHITECTURE_LOGGING.md`).
`app.log`'s level is `config.LOG_LEVEL` (DEBUG by default), and can be
changed for a single launch with an environment variable, e.g.
`DISCORD_TRANSCRIPTION_LOG_LEVEL=INFO discord-transcription-gui`. The
scroll trace's size budget, and a switch to turn it off entirely, are the
`SCROLL_TRACE_*` settings in `app/config.py`.

## Testing

The tests import the package as `gui_transcription.app`, so run them from
the directory that *contains* `gui_transcription/`, not from inside it. Use
whichever Python environment has the requirements plus the `dev` extras
(`pytest`, `ruff`) installed, e.g. `python -m pip install -e ".[dev]"` in
the `.venv` above. pipx's app environment doesn't include them; to use it
anyway, run pytest from a Python that has it, with the app environment's
site-packages on `PYTHONPATH`.

Lint with `python -m ruff check gui_transcription` (configured in
`pyproject.toml`).

Run the default suite:

```bash
# Linux
python -m pytest gui_transcription/app_tests -v
```

```powershell
# Windows
py -3 -m pytest gui_transcription\app_tests -v
```

The tests for the Windows-only image context-menu actions in
`test_image_context_menu.py` (which exercise `winreg`, `win32clipboard`,
and Explorer directly) are skipped on other platforms. One test in
`test_chatlog.py` reads a private sample export from `example_inputs/`,
which is gitignored, so it fails on any checkout that doesn't have that
file.

### The `gui` marker

Some tests build a real (if withdrawn) Tk window - `test_review_view.py`,
`test_main_window_ocr_error.py`, and `test_main_window_on_start.py` in
full, plus three tests in `test_image_loading.py`. Even a withdrawn window
can briefly flash on screen, most noticeably on the very first `Tk()` call
in a process (which also runs Tcl/Tk's one-time subsystem init), so these
are marked `gui` in `pyproject.toml` and excluded by default:

`test_review_view.py` can't withdraw its window at all (see the module
docstring on its `root` fixture) - its ~80 tests (counting parametrized
cases) need real pixel geometry
to verify actual row layout, and a withdrawn/never-mapped window never
gets that. That file's `root` fixture is module-scoped and reused across
all of its tests rather than opened and closed per test, which is what
keeps running it from being a rapid-fire flash of ~80 separate windows;
each test still tears its own widgets down afterward (an autouse fixture)
so state can't leak between tests the way it would if the shared root were
never cleaned up.

```bash
# Just the GUI tests
python -m pytest gui_transcription/app_tests -v -m gui

# Everything, GUI tests included
python -m pytest gui_transcription/app_tests -v -m ""
```

(On Windows, use `py -3` and `gui_transcription\app_tests` as above.)

### What's covered where

See `docs/ARCHITECTURE_TESTING.md` for a breakdown of what each area of the
app is tested for, and what's still only covered by manual smoke-testing. The
review screen (`app/gui/review_view.py` and friends) is the most
architecturally involved and historically bug-prone part of the app, so it
gets the most detailed treatment there.

## Known gaps / next steps

- The dark title bar is Windows-only (it uses DWM). On Linux, the title bar
  follows the window manager's theme.
- Other `config.py` constants (Tesseract path, etc.) are still
  not editable from the UI (deferred, not an immediate priority) - only the
  approved-users list has been moved out of config.py so far.
- Paging back up to revisit an earlier page re-decodes its images from disk
  (no cross-page image cache); only the edited text itself is cached across
  a page being torn down and rebuilt.
- Images load a moment after their row appears while scrolling, so an
  image area can briefly show empty (lazy loading, by design).
- Only one generation of backup is kept per state file (`*.bak`), apart
  from sessions, whose last 3 end-of-session states are also kept in
  `session_backups.json`. A crash can still lose up to one autosave interval's
  worth of review edits (5 seconds, `AUTOSAVE_INTERVAL_MS`) if it happens
  between two autosaves, since the .bak only protects the *previous*
  successful write, not the in-memory edits since then (low priority).
- Each "ocr" box now has a checkbox for resetting it back to its original
  OCR transcription without losing the edit (see the "Review screen"
  section above) - but a "message" box (a copy of the message's own
  original text, never OCR'd) still has no equivalent reset-to-original UI
  (deferred, not an immediate priority, per the original feature request).
