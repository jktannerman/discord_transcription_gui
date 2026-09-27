# Review screen internals

Part of [ARCHITECTURE.md](ARCHITECTURE.md) - see there for the general
testing heuristic this codebase follows, and for links to the other topic
docs (row geometry, spacer slots, testing, logging).

The review screen is the most architecturally involved part of this app.
`ReviewFrame` (`discord_transcription/gui/review_view.py`) doesn't do the
work itself: it creates one object per concern and wires them together,
each getting what it needs through its constructor. Each module's
docstring explains that part in detail; this doc covers how they fit
together and the non-obvious rules they depend on.

| Object | Module | Owns |
|---|---|---|
| `VirtualRows` | `virtual_rows.py` | The canvas and scrollbar, every row's height (`heights`), which rows are built (`reconcile`/`_sync_materialized_rows`/`_remeasure_built_rows`/`offset_of`/`ensure_materialized`/`destroy_row`), debouncing, the scroll freeze, and the scroll trace (`log_event`). Knows nothing about what a row contains. |
| `RowBuilder` | `row_building.py` | Building one row's widgets (`fill_row`), each content box's fixed height, the image column width, and the pre-build height estimates (`estimate_heights`). |
| `SlotBoxes` | `slot_boxes.py` | Every editable box's `SlotState` (`states`) and, while its row is built, its `SlotView` (`views`): building boxes, syncing edits, the OCR checkbox, spellcheck, undo/redo, and reporting edits for autosave/Finalize. |
| `FocusNavigator` | `keyboard_nav.py` | The slot order (`slots`), Tab/Shift-Tab, keeping the focused box on screen, and restoring focus across a row rebuild. |
| `ColumnDivider` | `column_divider.py` | The image/text divider, its drag, and re-laying out the rows at a new width. |
| `ImageContextMenu` | `image_context_menu.py` | The right-click image menu and its actions. |
| `ImageLoader` | `image_loading.py` | Lazily loading/unloading the built rows' images. |

`ReviewFrame` itself keeps the scroll input handlers (wheel, Page Up/Down),
the floating Finalize button, the first layout (`_apply_initial_position`),
and the methods `App` calls (`collect_edited_texts`, `get_focused_slot`,
...). Callbacks connect the pieces: `VirtualRows` asks `ReviewFrame` to fill
or release a row (`_fill_row`/`_on_row_destroying`), and `SlotBoxes` asks
it to bind navigation keys on a new box and to keep a focused box in view.

The windowing state stays together in `VirtualRows`: splitting it further
would spread the invariants below across objects without removing any.

## Row virtualization

- **The built window is recomputed from scratch.** Only rows near the
  viewport (`SCROLL_BUFFER_VIEWPORTS` either side) are built as Tk
  widgets. `VirtualRows.reconcile` works out that range as a pure function
  of the scroll position and `VirtualRows.heights`
  (`virtualization.compute_visible_range`), then builds and destroys rows
  to match. It keeps no window state of its own, so calling it twice with
  no scroll movement is a no-op, and it's safe to re-enter. Scroll input
  schedules it through a debounce (`DEBOUNCE_MS`). The layout math lives in
  the Tk-free `virtualization.py` so it's testable without a display.
- **Nothing may repaint while the built block is out of place.** All built
  rows are packed into one frame, positioned on the canvas at its first
  row's offset. Destroying rows above shifts everything left in the frame up
  by their height, and building rows above shifts it down, so
  `canvas.coords` has to move the frame to match *before* anything flushes
  Tk's idle queue, since `update_idletasks()` repaints the screen. It's
  moved right after the teardown in `_sync_materialized_rows` and again
  right after the builds in `reconcile`. Row building itself must not flush
  the idle queue either: that repaints the half-rebuilt screen and shows as
  flicker. `RowBuilder._build_message_label` therefore measures its label
  with `winfo_reqheight()` rather than laying it out.
- **Heights are estimated, then measured.** Until a row is built, its
  height is `virtualization.estimate_row_height`'s estimate; once built,
  `_remeasure_built_rows` records the real height and shifts the scroll
  offset by the difference for rows above the viewport, so the content on
  screen doesn't move. Any estimation error still shows as a scroll jump at
  that point, and a row that's never built (skipped by a far jump or a
  scrollbar drag) keeps its error in every later row's offset. So the
  estimate mirrors `RowBuilder.fill_row`'s layout exactly: the same slot
  order (`ReviewItem.slot_roles`), the same constants
  (`layout_constants.py`), each column summed separately, images sized with
  the same header-only `fitted_image_size` read, and content boxes capped at
  the same `max_text_box_height_px`. That cap depends on the canvas height,
  which is why `ReviewFrame` estimates the heights only after the canvas
  exists. [ARCHITECTURE_ROW_GEOMETRY.md](ARCHITECTURE_ROW_GEOMETRY.md) lists
  every term a row's height has to include.
  `test_jumping_focus_past_a_capped_long_message_row_lands_target_fully_in_view`
  covers the cap.
- **Text sizes are measured, not hardcoded.** How big the text font really
  draws depends on which font Tk resolves (Consolas is substituted on most
  Linux systems) and on the display's DPI scaling. `ReviewFrame.__init__`
  calls `row_building.measure_text_metrics` once, which reads the font's
  character width and line height and the requested heights of a throwaway
  original-text label and spacer box, built by the same helpers
  (`_make_original_text_label`, `slot_boxes.make_spacer_text_widget`) the
  real rows use. The resulting `TextMetrics` feeds `estimate_row_height` and
  sets every spacer box's height. Measure with `tkfont.Font(family=...,
  size=...)`, not `tkfont.Font(font=(family, size))`: the latter over-scales
  on some displays.
- **A newly built row can report `winfo_height() == 1`.** A widget has no
  real geometry until the window system maps it. `update_idletasks()` only
  runs idle callbacks (pack's layout), not the `Map` event, and for a large
  tree of never-mapped widgets - a resume that jumps deep into the
  transcript builds a whole window of rows in the very first reconcile - one
  pass isn't always enough. Taking that `1` at face value would record
  bogus heights that throw off every later offset for the rest of the
  session. So `reconcile` calls `_settle_pending_geometry` before
  measuring: a bounded number of `update_idletasks()` retries, then of full
  `update()` calls, which process all pending events and do resolve it.
  `update()` can run a pending debounced `reconcile` from inside the outer
  one; that's deliberately allowed, since the nested call builds nothing and
  its `scrollregion`/`coords` calls help finish the mapping. If the bound is
  hit anyway, `_remeasure_built_rows` keeps the estimate for any row still
  at `winfo_height() <= 1`. Regression test:
  `test_resuming_deep_in_a_long_transcript_remeasures_rows_correctly_on_first_build`.
- **One row's build failure doesn't stop the rest.** `_try_build_row`
  logs the exception, tears down whatever was built and skips the row (a
  later reconcile retries it), so `materialized_range` never goes out of
  step with `row_frames`. `SlotBoxes._reclaim_if_present` is the matching
  backstop for a box built while an old widget for the same slot is still
  registered: it syncs the old one into its `SlotState` and destroys it. And
  `main.py`'s `root.report_callback_exception` routes any uncaught Tk
  callback exception into `app.log`.

## Boxes and their state

- **Slot-addressed boxes.** A row can have a "message" box (a copy of the
  message's own text), one `"ocr{N}"` box per attached image, and spacer
  boxes between and after them (see
  [ARCHITECTURE_SPACER_SLOTS.md](ARCHITECTURE_SPACER_SLOTS.md)), so every
  box is addressed by its slot, `(item_index, role)`. The role order comes
  from `ReviewItem.slot_roles`, and `FocusNavigator.slots` is the flat,
  transcript-ordered list of every slot: what Tab/Shift-Tab step through and
  what a resumed session's focus position names. `ImageLoader` keys images
  the same way, by `(item_index, image_index)`.
- **The per-box model lives outside the widgets.** (`slot_state.py`,
  `slot_view.py`, `edit_history.py`.) Each box has two halves, both keyed
  by slot:
  - `SlotBoxes.states` holds one `SlotState` per box, built up front (in
    `SlotBoxes.__init__`) for every slot: its default text, current text,
    cursor, undo/redo history, whether the user touched it this session,
    and (for an "ocr" box) its checkbox state and hidden user edit.
  - `SlotBoxes.views` holds one `SlotView` per *currently built* box: its
    Text widget, container, checkbox variable and pending spellcheck timer.
    `SlotBoxes._release_view` is the one place a view is unregistered (row
    teardown and `_reclaim_if_present` both use it): it syncs the SlotState
    from the widget, records the cursor and cancels the timer.

  The widgets are just a view of the SlotState. Three rules keep the two in
  step:
  - **A (re)build is always the same:** `SlotBoxes._populate` inserts
    `SlotState.text` and restores the cursor. There is no separate "first
    build" path, and nothing is replayed.
  - **Widget to model:** `SlotBoxes.sync_from_widget` compares the widget's
    text with `SlotState.text`. It runs on every `<<Modified>>` event, and
    also before anything reads or replaces a box's text (teardown,
    `reported_text`, undo/redo, the checkbox), because `<<Modified>>`
    arrives on a later idle tick than the edit itself. A difference is a
    user edit: it's recorded in the history, marks the slot touched, and
    ticks an "ocr" box's checkbox.
  - **Model to widget:** the app's own writes (undo/redo, the checkbox
    swap) go through `SlotBoxes.set_text`, which updates `SlotState.text`
    *before* touching the widget. The `<<Modified>>` event that write
    causes then finds no difference, so it is never mistaken for a user
    edit - no suppression flags needed.

  Undo/redo uses the box's own `EditHistory`, a stack of full-text
  snapshots capped at `MAX_UNDO_STEPS`, not Tk's (the widgets are created
  with `undo=False`). Because it never lives on a widget, a teardown and
  rebuild can't change it: the property tests in `test_review_view.py`
  check that repeated Ctrl+Z after a rebuild walks through exactly the
  texts it would have without one. `EditHistory` decides where undo steps
  start and end (by word, pause, insert/delete switch, cursor jump;
  paste/cut/checkbox toggles are always their own step - see its module
  docstring), and those rules are unit-tested without a display in
  `test_edit_history.py`. After an undo/redo the cursor goes to the change
  (`cursor_after_change`). ARCHITECTURE.md's general heuristic explains why
  snapshots rather than replayed edits.
- **`<<Modified>>` also fires for a box's initial text.** Tk queues the
  event for the next idle tick, so `edit_modified(False)` right after the
  initial insert doesn't stop it, and every newly built row - including
  ones built only because they entered the buffer - sends one.
  `SlotBoxes._on_text_modified` therefore scrolls a box back into view only
  if it has focus, which a build-time event never does. (Scrolling a
  focused box back into view is for typing into one that the wheel or
  scrollbar moved off screen.)
- **Focus and cursor survive a row being torn down.** A fast Page Up/Down
  burst can move several viewports between (debounced) reconciles and tear
  down the row whose box has focus. `ReviewFrame._on_row_destroying`
  records that box's slot (`FocusNavigator.refocus_slot`), and every box's
  cursor goes into `SlotState.cursor`. `FocusNavigator.restore_focus_after_build`
  refocuses it once the row is rebuilt, via `after_idle` so that the
  still-running reconcile's scroll correction doesn't undo its
  scroll-into-view. It's skipped if another box or the Finalize button has
  taken focus since - not keyed on `focus_get()` being `None`, because
  destroying a focused widget hands focus to an ancestor frame instead of
  clearing it. The cursor isn't saved with the session, so a resumed box's
  cursor starts at `"1.0"`.
- **Autosave can't ask Tk what's focused.** `focus_get()` returns `None`
  whenever the app isn't the active window, which on some desktops
  includes the moment the window-close handler runs its final save.
  `get_focused_slot` therefore falls back to
  `FocusNavigator.last_focused_slot`, which every box's `<FocusIn>` (and
  `FocusNavigator.focus_text_box`, since FocusIn only arrives while the app
  is active) records, and the Finalize button's `<FocusIn>` clears. When
  there's no box to refocus, a resume restores the saved scroll fraction
  instead, after setting the scrollregion (`yview_moveto` is silently
  ignored without one). Regression tests:
  `test_focus_survives_app_losing_focus_save_and_resume` and
  `test_saved_scroll_fraction_is_restored_when_no_box_was_focused`.
- **Per-OCR-box checkbox.** Every "ocr" box (never a "message" box, which
  has no OCR original to go back to) has a checkbox for switching between
  the OCR text (after `ocr_corrections.txt`) and the user's own edit
  without losing either. The state is in the box's SlotState: `checked`,
  and `user_edit` (the last edited version, kept while the box shows the
  OCR default). It survives a rebuild like the rest of the SlotState.

  `SlotBoxes.reported_text` (what autosave and Finalize read) reports
  `None` for an unchecked box, so an edit hidden behind an unchecked box is
  kept only while the app stays open, and a saved non-default value always
  means a real edit. `slot_boxes.initial_slot_states` seeds each box checked
  exactly when its starting text (a resumed or finalized edit) differs from
  the OCR default.

  Three rules decide the checked state:
  - **Any user edit ticks it** (`_on_ocr_box_user_edit`, called from
    `sync_from_widget`), even if the new text happens to match the default.
  - **Clicking it swaps the text** (`on_ocr_checkbox_toggle`): unchecking
    shows the OCR default, checking shows `user_edit` again. The swap is one
    undo step of its own. The new checked state is read before syncing any
    pending edit, since that sync would otherwise tick the box straight
    back.
  - **Undo/redo re-derives it** (`_resync_ocr_checkbox_after_undo`):
    checked exactly when the resulting text differs from the OCR default.
    So undoing an untick brings back both the edit and the tick, and
    undoing a box's only edit unticks it.

  The checkboxes are `takefocus=0`, so Tab/Shift-Tab skip them. The
  checkbox sits in a column packed `side="right"` into the box's container
  *before* the text widget, so it claims its slice of the right edge before
  the text widget's `expand=True` takes the rest. For the same reason, the
  box's scrollbar is packed `before=` the checkbox column (or the text
  widget, for a box with no checkbox), putting it at the true right edge.
- **Spellcheck tagging.** (`discord_transcription/spellcheck.py`, wired in
  via `SlotBoxes.schedule_spellcheck`/`run_spellcheck`.) A misspelled word
  gets the `misspelled` tag, a red straight underline (Tk has no wavy one).
  Tags don't change a box's text, so they never count as an edit or reach
  the undo history. They live on the Text widget, not the SlotState, so
  every (re)build schedules a fresh pass. Spacer boxes never get the tag.

  A word is flagged if the dictionary doesn't know it or it's in
  `spellcheck_blacklist.txt`, unless it's in `spellcheck_whitelist.txt`:
  the whitelist is subtracted from the candidates before the blacklist is
  added. Both files are re-read when their modification time changes.

  Each pass is debounced per box (`SPELLCHECK_DEBOUNCE_MS`). A box's pending
  timer is cancelled when its view is released (row teardown), and
  `ReviewFrame`'s `<Destroy>` handler cancels every still-built box's timer
  too, since rows still built when the whole screen goes away never go
  through `destroy_row`. Tcl's `after` queue is shared by every Tk
  interpreter in a process, so leaked timers from earlier test cases slow
  later ones' `update()` calls, enough to exhaust
  `_settle_pending_geometry`'s bound.

## Layout

- **Fixed-height boxes.** Each editable box lives in its own fixed-height
  container (`pack_propagate(False)`), so it doesn't stretch to fill the
  row. `RowBuilder.fixed_text_box_height` sets that height once, at build
  time: its paired immutable element's on-screen height (the label's, for a
  "message" box; the image's, for an "ocr" box) plus `TEXT_BOX_MARGIN_PX`,
  capped at `TEXT_BOX_MAX_HEIGHT_FRACTION` of the viewport. Longer content
  scrolls inside the box, with a scrollbar shown only while it overflows
  (`_set_text_scrollbar`, driven by the box's `yscrollcommand`); the box
  never grows. That's what makes a row's height knowable before it's built,
  the same way an image's height is known from its header.
- **Left column sizing.** (`RowBuilder`.) Every row's left column is the
  same width (`RowBuilder.image_column_width_px`, set by the column
  divider), so the column pairs line up across the whole transcript. An
  image's height is its own aspect-preserving fit within
  `image_bounding_box(width)` (`fitted_image_size`, header-only), not the
  full bounding box, so a landscape image isn't letterboxed. The pixels are
  decoded later, by `ImageLoader`, once the row is near the viewport - on
  worker threads, polled for from the Tk thread, which only turns the
  finished image into a `PhotoImage`. A decode that finishes after its
  image was unloaded or its row torn down is dropped. An unreadable image
  gets a short fixed-height strip (`UNREADABLE_IMAGE_HEIGHT_PX`). The
  original-text label's height is read from its own `winfo_reqheight()`,
  valid as soon as it's configured.
- **The column divider re-lays out through the rebuild path.**
  (`ColumnDivider`.) The image column's width sets every row's height
  (images are fitted to it, and the original-text label wraps at it), so a
  new width can't be patched onto the built rows alone: rows that aren't
  built have estimated heights that depend on it too.
  `ColumnDivider.set_width` treats it like a far scroll jump: it records an
  anchor (the focused box's top edge if it's in view, otherwise the top
  row's), tears down every built row, re-estimates every row's height at
  the new width, scrolls so the anchor's *estimated* position is back at
  the same screen offset, reconciles, and then repeats that scroll against
  the rebuilt rows' *real* geometry and reconciles again. It adds no second
  sizing path: `RowBuilder.fill_row` and `estimate_row_height` both just
  take the width, and the per-box model carries edits, cursor, undo history
  and focus across the rebuild the same way it does for scrolling.
  Re-estimating stays cheap because `image_loading._natural_size` caches
  each image's header size.

  The divider is a `tk.Frame` `place()`d over the canvas (`in_=canvas`, so
  its x is in the rows' own coordinates) at `divider_x_for_width`, centered
  in the `2 * COLUMN_PADX_PX` gap between the columns. That position comes
  from the same layout constants `RowBuilder.fill_row` packs with
  (`IMAGE_COLUMN_LEFT_PX`), and
  `test_column_divider_sits_in_the_gap_between_the_columns` checks it
  against the real widgets. Dragging only moves the divider; the relayout
  runs on release. The width is stored as a fraction of the canvas width,
  so a window resize (debounced `<Configure>`, `RESIZE_DEBOUNCE_MS`) keeps
  the proportion, and `ReviewFrame`'s caller saves it per chatlog
  (`state.save_image_column_fraction`). Clamping
  (`virtualization.clamp_image_column_width`) keeps both columns above
  their minimums, the image column's winning if the window is too narrow
  for both. A relayout costs about as much as a far scroll jump (roughly
  half a second at 1900x1000, nearly all of it Tk laying out and painting
  the new rows), which is why the rows aren't re-laid out live while
  dragging.
- **Floating Finalize button.** The Finalize button's container is
  `place()`d relative to `ReviewFrame` itself, pinned to the bottom of the
  viewport, rather than packed, so it never takes vertical space from the
  canvas. `_update_finalize_button_visibility` shows it only once
  `canvas.yview()`'s bottom fraction reaches the end (or the transcript
  fits on screen). It runs after every reconcile and whenever
  `FocusNavigator` moves the view, so Tab from the last box shows it
  immediately.

## Scrolling and focus

- **Horizontal wheel events must not scroll vertically.** Tk 8.6 on X11
  delivers horizontal scrolling (buttons 6/7, e.g. a touchpad's sideways
  drift during a two-finger scroll) as Shift+Button-4/5, and a plain
  `<Button-4>`/`<Button-5>` binding matches those too; treated as vertical,
  that drift cancels out downward scrolling. `wheel.wheel_delta` returns 0
  for any Shift-modified wheel event, and the `input_mousewheel` trace
  event records each event's raw `num`/`state`.
- **Wheel over a box scrolls the box first.** The wheel is bound app-wide
  (`canvas.bind_all` for every `wheel.WHEEL_EVENT_SEQUENCES` entry) and on
  every box (replacing Tk's own Text scrolling), and the handler gets the
  widget under the pointer as `event.widget`. Hovering a box that can
  scroll scrolls it (`_scroll_text_widget`) until it reaches its limit in
  that direction, then the whole review window scrolls.
- **Scroll-into-view works per box, not per row.** A row can hold several
  boxes and be taller than the viewport, so checking the row's bounds could
  find it "visible" while the focused box itself is mostly off screen.
  `FocusNavigator.scroll_box_into_view` and `keep_cursor_in_viewport`
  compute the box's own document position: `VirtualRows.offset_of(index)`
  plus `ROW_PACK_PADY_PX` plus the box's offset within its row from
  `winfo_rooty()` (see `box_document_top` and
  [ARCHITECTURE_ROW_GEOMETRY.md](ARCHITECTURE_ROW_GEOMETRY.md)). With
  `align_top` (Tab/Shift-Tab), the box's top is always moved to the top of
  the viewport.

## Image context menu

- **A popup `tk.Menu`'s close can't be detected with `<Unmap>` on Windows.**
  (`ImageContextMenu.show`.) Scrolling is frozen while the menu is open
  (`VirtualRows.frozen`, checked by the scrollbar handler and by
  `ReviewFrame`'s wheel and Page Up/Down handlers), so the menu's target
  image can't move out from under it. On Windows the popup is the native
  `TrackPopupMenu`, which Tk never sees unmap, so an `<Unmap>` binding
  never fires when it closes by Escape or an outside click. But
  `TrackPopupMenu` also blocks: `menu.tk_popup(...)` doesn't return until
  the menu has been dismissed, however that happened. So `show` unfreezes
  in a `finally` right after `tk_popup`. The tests mock `tk_popup` for the
  same reason - the real one would block until a person closed it.
  `_on_image_context_menu_closed` is a method rather than a closure so
  tests can call it directly.
- **"Open in browser" needs the default browser, not the image's file
  association.** (`_launch_url_in_default_browser`.) Both
  `webbrowser.open()` and `os.startfile()` on a local path or `file://` URI
  resolve it through the file's extension association (`.png` -> Photos),
  never the default browser. So on Windows `_default_browser_command` reads
  the http protocol's association directly, the way Explorer does -
  `HKEY_CURRENT_USER\...\UrlAssociations\http\UserChoice`'s `ProgId`, then
  that `ProgId`'s `shell\open\command` - and the URI is substituted for its
  `%1` (after `shlex.split`, so a quoted path with spaces stays one
  argument). The command is launched with `subprocess.Popen`, not `run`,
  which would block the UI until the browser exits. If either lookup step
  fails (no `UserChoice`, or a `ProgId` left by an uninstalled browser), it
  raises `RuntimeError`, which `_run_image_menu_action` logs as a failed
  action. Open Chatlog at Message uses the same lookup, with the message's
  `#chatlog__message-container-<id>` anchor appended to the export's URI.

  Linux has the same file-vs-browser split (`xdg-open` on a `file://` URI
  follows the file's MIME association), so `desktop_linux.py` does the
  freedesktop equivalent: `xdg-settings get default-web-browser` names the
  browser's `.desktop` file, found in XDG precedence order
  (`$XDG_DATA_HOME` first, so a user override wins), and its `Exec` line is
  expanded with the URL in place of `%u`/`%U`/`%f`/`%F`. Open Image
  Location uses the `org.freedesktop.FileManager1.ShowItems` D-Bus call
  (Nemo, Nautilus, Dolphin, ... all implement it) as the equivalent of
  Explorer's `/select`. Copy Image pipes PNG bytes to xclip/wl-copy with
  stdout/stderr *not* captured: both fork a background process to keep
  serving the clipboard, which would inherit a captured pipe and make
  `subprocess.run` wait until the clipboard changed hands.
