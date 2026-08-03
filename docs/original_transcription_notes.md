# Notes: `discord_tesseract_transcription_v3.py`

## Purpose

Takes a Discord chatlog HTML export (from DiscordChatExporter) plus the
exported media folder, and produces a plain-text transcript of a play-by-post
tabletop/interactive-fiction game. Messages contain both narration text and
images (item cards, dice-roll graphics, maps) that were OCR'd and need manual
correction before being appended to a running `.txt` transcript.

## Invocation (from `standard_usage.txt`)

1. Run `DiscordChatExporter.Cli.exe export` to pull a date-range slice of one
   channel to HTML + a `_Files` media folder.
2. Run this script with `--input_file_path`, `--input_image_folder`, `--output`.

```
py -3.13 discord_tesseract_transcription_v3.py
    --input_file_path "<exported>.html"
    --input_image_folder "<exported>.html_Files"
    --output "<running transcript>.txt"
```

The two steps are chained with `;` in one PowerShell line, but they are
independent programs — the GUI presumably needs to either also drive the
exporter CLI, or just consume HTML+media the user has already exported.

## Step-by-step behavior

1. **State file (`trans_v3_run_dates.txt`)** — keeps an append-only log of end
   dates from previous runs. On startup, the script reads the *last* line as
   the proposed start date and lets the user confirm/override it via prompt.
   This is how the tool knows where the *last* transcription left off, so
   re-running with a wider HTML export doesn't duplicate work.

2. **Cache prompt** — asks y/n whether to reuse a pickled cache
   (`unedited_trans_file.txt`) of raw OCR output instead of re-running
   Tesseract on every image. Useful when re-running just to redo the manual
   correction pass without waiting on OCR again.

3. **OCR pass** (skipped if using cache):
   - Lists every file in the image folder.
   - Skips anything without a `.` in the name, and skips `.svg/.woff2/.js/.css`
     (non-content files DiscordChatExporter also downloads).
   - Skips any image whose **file creation time** predates the start date
     (cheap pre-filter before even opening the HTML).
   - Runs Tesseract (`pytesseract.image_to_string`) on each remaining image.
   - Naive paragraph parsing: collapses `"\n\n"` into a sentinel, joins
     remaining single newlines into spaces, collapses double-spaces, then
     splits on the sentinel to get a list of "paragraphs" per image.
   - Prints progress every 10%.
   - Pickles the whole `{image_filename: [paragraphs]}` dict to
     `unedited_trans_file.txt` for later reuse.

4. **HTML parsing pass**:
   - Parses the exported HTML with BeautifulSoup (`lxml`).
   - Iterates `chatlog__message-group` blocks (Discord groups consecutive
     messages from the same author).
   - Filters to groups whose timestamp (`%d/%m/%Y %H:%M`) is after the start
     date, and whose author's `data-user-id` is in an **allow-list**
     (`'194265523339395072'` = wildbow, the GM; `'567170431413387265'` = a
     dice-roller bot). Everything else (other players chatting) is silently
     dropped.
   - For each `chatlog__message-primary` inside an approved group, calls
     `transcribe_message`.

5. **`transcribe_message`** — per message:
   - Grabs any plain text (`chatlog__markdown-preserve`) and splits to lines.
   - If the message also has an attached image
     (`chatlog__attachment-media`), looks up that image's OCR paragraphs by
     filename in `file_info`.
   - For each paragraph, runs an **interactive correction loop**:
     - Copies the OCR'd paragraph (+ `\n\n`) to the clipboard and prints it.
     - Prompts the user to press Enter to accept, or type a replacement.
     - Special suffix commands typed at the end of input change control flow:
       - `bbb` → go back one paragraph (pop last line, decrement index, retry)
       - `ccc` → re-prompt for the *same* paragraph (re-loop without advancing)
       - `ddd` → accept this and **all remaining** paragraphs in this image
         verbatim, no more prompts ("auto-pilot" mode)
       - `eee` → abandon all remaining paragraphs from this image
       - `fff` → skip just this one paragraph
       - `ggg` → accept this paragraph, then stop processing further
         paragraphs from this image
     - Plain Enter (empty input) accepts the OCR'd text as-is; otherwise the
       typed text replaces it verbatim.
   - Returns the combined list of text lines + (corrected) image paragraphs
     for that message.

6. **Writing output**: for each message with any lines, appends to the output
   `.txt`: blank-line padding, then each line (converting literal `\n`
   sequences in corrected text back to real newlines), then more padding.

7. **Cleanup regex pass** over the *entire* output file after the run:
   - `|` → `I` (common OCR misread).
   - Collapses excess blank lines after `%roll ...` and `%draw ...` lines.
   - Collapses 4+ blank lines anywhere down to exactly 3 blank lines (4
     newlines).
   - Strips any stray literal `\n` that wasn't converted.
   - Trims trailing `[BREAK]` markers (with surrounding whitespace) from the
     very end of the file.

8. **Bookkeeping**:
   - Computes `end_date` from the **HTML file's mtime** (not the last message
     timestamp!) and appends it to `trans_v3_run_dates.txt` for the next run.
   - Copies everything after the last `[BREAK]` marker to the clipboard (i.e.
     "this run's new content") for easy pasting elsewhere.
   - Appends a fresh `\n\n\n[BREAK]\n\n\n` marker to the output file as a
     bookmark for the *next* run.

## Dependencies / external state

- `pytesseract` + a hardcoded Tesseract install path
  (`C:\Program Files\Tesseract-OCR\tesseract.exe`).
- `pyperclip` for clipboard.
- `beautifulsoup4` + `lxml`.
- `jktracking.tracking` (`from ... import *`, uses a `note(...)` function) —
  a private/custom package not included in this folder, used only for
  `note(db3, "...")` debug-skip logging. **Confirmed legacy/irrelevant** —
  drop entirely in the rewrite (replace with a plain log line, or nothing).
- Two absolute external state files outside the project folder:
  `trans_v3_run_dates.txt` and `unedited_trans_file.txt`, both under
  `C:\Users\jktan\programming\python_projects_0\`.
- Hardcoded approved Discord user IDs (GM + dice bot) and the `[BREAK]` /
  `%roll` / `%draw` markers are specific to this one campaign/channel.

## Improvements worth making in the GUI version

- **Replace the CLI correction loop with real GUI widgets**: text box showing
  OCR text editable in place, image preview alongside it, buttons instead of
  memorizing `bbb/ccc/ddd/eee/fff/ggg` suffix codes. This is the main value
  proposition of the rewrite.
- **Make config explicit instead of hardcoded paths/IDs**: Tesseract path,
  state file locations, approved user IDs, skip-extensions, and the
  `[BREAK]`/`%roll`/`%draw` markers should be settings (a config file or GUI
  preferences), not baked into source — so the tool isn't tied to one
  Windows user/account and one Discord campaign.
- **Drop the `jktracking` dependency** — confirmed legacy/irrelevant, only
  used for a debug print.
- **Replace pickle cache with something safer/inspectable** (e.g. JSON keyed
  by filename), and key the cache by file hash or path rather than relying on
  a manual y/n prompt — auto-detect "have we already OCR'd this exact image"
  instead of an all-or-nothing cache toggle.
- **Don't rely on file creation time** to filter images by date — creation
  time can be unreliable (e.g. after a copy/restore) and doesn't necessarily
  match Discord's message timestamp. Could match images to messages via the
  filename/src attribute instead, and filter by the message's timestamp like
  the HTML pass already does.
- **Validate the start-date input** — currently `start_date.split("-")` will
  throw an unhandled exception on bad input instead of a friendly re-prompt.
- **Don't silently overwrite `end_date`** — the line
  `end_date = end_date = datetime.datetime.fromtimestamp(...)` reassigns the
  string read from disk to a different `datetime` value under the same name;
  harmless here but confusing, and a GUI rewrite should make this an
  unambiguous variable.
- **Surface skipped messages and skipped images** in the UI (currently
  `note()` calls are essentially silent/debug-only) so the user can audit
  what got excluded by the date filter or author allow-list, and adjust the
  allow-list from the GUI instead of editing source.
- **Allow undo/preview before committing to the output file** — right now
  text is appended directly to disk per message as you go; a GUI could batch
  the whole run as an in-memory/preview document the user approves before
  writing, with a real "undo last paragraph" instead of `bbb` popping list
  items.
- **Progress bar instead of console `%` prints**, plus a way to pause/resume
  a long correction session without losing place (state currently lives only
  in local variables for the run).
- **Security**: `standard_usage.txt` contains a live Discord bot token in
  plaintext. You said you'll remove/rotate it soon — no action taken on it
  here.
- **Keep the image-transcription step modular**: Tesseract is the only
  active OCR backend (EasyOCR is abandoned but not ruled out for a future
  revival per your note), so the GUI rewrite should put OCR behind a small
  swappable interface (e.g. a `transcribe_image(path) -> str` function/class
  the rest of the app calls) rather than calling `pytesseract` directly
  throughout. For now only the Tesseract implementation needs to exist and
  be wired up.
- **Approved-author allow-list**: leave hardcoded for now (not an immediate
  priority per your note), but keep it as a single named constant/config
  value rather than scattered literals, so it's a one-line change to make
  configurable later.

## Resolved questions (from your answers)

1. ~~`jktracking.note()` purpose~~ — confirmed legacy/irrelevant, dropped.
2. The second example file (`pq_tabbed_transcript_auto_part_2.txt`) is a
   periodic redownload of the **public Google Doc** that the CLI-transcribed
   text gets pasted into and then manually edited/polished in-browser; it's
   re-fetched occasionally to make full-text search easier. It is not a
   second output format this tool needs to produce — it's downstream of this
   tool's output, with the only catch being the `[BREAK]`-style breakpoints
   drift slightly from the GUI tool's own breakpoints once manual editing
   happens in the Doc.
3. `DiscordChatExporter.Cli.exe` export stays a separate, manual step — the
   GUI only needs to consume HTML+media the user has already exported.
4. Author allow-list: configurable eventually, but not an immediate
   priority — keeping it as a hardcoded-but-isolated constant for now.
5. OCR engine: Tesseract only for now; EasyOCR is abandoned but possibly
   revived later, so the OCR call should sit behind a swappable
   interface even though only one implementation exists today.

## Resolved questions (round 2)

1. Google Doc awareness: stays completely separate/out of scope for now.
2. Breakpoint drift: no issues noticed so far — not something the GUI needs
   to actively guard against at this time.
3. GUI framework: **Tkinter**, per past success with it on similar projects.
