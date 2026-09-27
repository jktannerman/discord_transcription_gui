# Spacer slots

Part of [ARCHITECTURE.md](ARCHITECTURE.md).

Blank-line spacing in the output is set entirely by "spacer" text boxes on
the review screen: one-line-tall, right-column-only editable boxes between
every adjacent pair of transcript elements, holding literal `\n` *tokens*
(the two characters `\` and `n`, not real newlines) that the user can
freely edit. Nothing else in a spacer box has any effect - see
"Finalize-time parsing" below. Nothing after the review screen changes the
spacing, so a deliberately large gap is written as is.

## Slot ordering (`review_item.ReviewItem.slot_roles`)

`ReviewItem.slot_roles` is the one place the order of an item's boxes is
defined. Row building (`RowBuilder.fill_row`), height estimation
(`virtualization.estimate_row_height`), keyboard navigation
(`FocusNavigator.slots`, built in `ReviewFrame.__init__`) and output
writing (`review_item.lines_for_item`) all walk it, so they can't disagree.

For an item with a message and N images, `slot_roles` is: `["message",
"spacer_msg_img", "ocr0", "spacer_img0", "ocr1", ..., "ocr{N-1}",
"spacer_end"]` - `"spacer_msg_img"` only appears when the item has both a
message and at least one image; `"spacer_img{i}"` appears between every pair
of images (not after the last one); `"spacer_end"` (the gap before the next
message) always appears, even for an item with no images at all. A spacer
role has no left-column counterpart, and it's exactly one Tk text line tall
(the height a `height=1` Text widget requests on this display, measured
once as `TextMetrics.spacer_box_height_px` - see
`row_building.measure_text_metrics` and `SlotBoxes.build_spacer_box`)
rather than following `RowBuilder.fixed_text_box_height`'s paired-height
rule.

Tab/Shift-Tab visit spacer slots the same as any content slot.

## Default newline counts

Computed once in `build_review_items`, from each message's *original*
text - not whatever the user later edits it to, so editing a message's
transcribed text never changes its own default spacing:

| Gap | Default |
|---|---|
| Message text → its first image (`spacer_msg_img`) | 1 empty line (2 tokens) |
| Between two images on the same message (`spacer_img{i}`) | 2 empty lines (3 tokens) |
| End of a normal message (text-only or image-ending) → next message | 3 empty lines (4 tokens) |
| Die-roll command (`config.DICE_COMMAND_RE`: `%roll \d*(d\|l\|h)\d+` or `%draw \d+ \d+`) → its result | 0 empty lines (1 token) |
| Die-roll result → next message that is itself a die-roll command | 1 empty line (2 tokens) |
| Die-roll result → next message that is *not* a die-roll command | 3 empty lines (4 tokens), same as normal |

A die-roll command's result is simply "the next approved message" - there's
no separate regex identifying a result; `build_review_items` classifies
every entry once via `_is_dice_command` and looks at each item's immediate
predecessor/successor in the (already author/date-filtered) entries list.
Die-roll messages are assumed to never have images, so they only ever get a
`"message"` slot plus a single trailing `"spacer_end"` slot. The token count
written into a spacer box's default content (`review_item._spacer_default`)
is always the empty-line count plus one, since the gap also includes the
newline that ends the line right before it - see "Finalize-time parsing"
below for why that one extra newline isn't *also* added by the preceding
content box.

## Finalize-time parsing (`review_item.lines_for_item`)

- **Content roles** (`"message"`/`"ocr{i}"`): only *trailing* real
  newline/carriage-return characters are stripped from the box's text; the
  rest (including any internal newlines from a multi-line message) is
  written verbatim, with no newline forced onto the end. The spacer role
  that always immediately follows a content role in `slot_roles` supplies
  that terminator, plus however many blank lines the box was left with -
  this is why a spacer's default token count is the empty-line count plus
  one rather than just the empty-line count on its own.
- **Spacer roles**: every real newline/carriage-return character anywhere in
  the box - leading, trailing, or mixed through the middle - is discarded
  first, then the remaining literal `"\n"` tokens are counted
  (`review_item._count_spacer_tokens`) and that many real newline characters
  are written. Any other stray character typed into a spacer box is
  ignored, never written - nothing but backslash/`n` characters has any
  effect there.
- `pipeline.render_items` adds no padding around an item's chunks: each
  item's own `"spacer_end"` chunk supplies the entire gap before the next
  item. The gap before the very first item of a run comes from the
  previous run's trailing `\n\n\n{BREAK_MARKER}\n\n\n` (written by
  `finalize_run`), so no leading padding is needed there either.

## Session format

`ReviewFrame.collect_edited_texts` and the autosaved session's
`edited_texts` field hold one `{role: text}` dict per item, covering every
role in that item's `slot_roles`, content and spacer alike (see
`session.SavedSession`).

## Finalized edit persistence

When the user clicks Finalize and the output file has been written,
`_on_finalize_clicked` saves the changes worked out by
`session.build_finalized_updates` to `finalized_edits.json` (via
`state.save_finalized_edits`), keyed by the HTML path and each message's
Discord `message_id`. On a later fresh run of the same chatlog,
`_show_review` loads these via `state.load_finalized_edits` and passes them
to `ReviewFrame` as `initial_finalized_texts`.

**Priority order** in `slot_boxes.initial_slot_states`: each box's
starting text is the in-progress session's edit (`initial_saved_texts`) if
there is one, else the finalized edit, else the default. An "ocr" box
starts checked exactly when that text differs from its OCR default, so a
finalized edit that differs from the OCR default starts its checkbox checked
automatically with no special-case code.

**What counts as an edit**: `SlotBoxes.reported_text` reports `None`
for any box whose text equals its default (and for any unchecked OCR box),
so a box whose row was merely built - scrolled past - is never an edit.

**Update semantics** (`session.build_finalized_updates` + `state.save_finalized_edits`):
a non-`None` value stores that text. A `None` value removes the stored edit
for that slot, but only if the slot is in `ReviewFrame.get_touched_slots()`
- the user deliberately acted on it this session (typed/pasted/undid in it
while focused, or clicked its OCR checkbox). Every other slot is left out
of the update entirely, keeping whatever is stored for it: requiring
positive evidence of intent means a logic bug that unticks a box or resets
its text can't remove a finalized edit. Touched slots are saved with the
session (`touched_slots`, as `[message_id, role]` pairs) so a revert made
before closing the app still counts after resuming. Before any stored edit
is replaced by different text or removed, the old version is appended to
`finalized_edits_history.json` (`state.load_finalized_edits_history`),
which is written first and never trimmed.

**Matching** (`session.match_finalized_edits`): stored `{message_id: {role: text}}`
data is aligned to the current item list by Discord `message_id` (not by
position) using the same approach as `session.match_saved_edits` for session
resume - message IDs no longer in the transcript are dropped silently, and
so are roles an item no longer has (e.g. because the chatlog was
re-exported with fewer images). All `slot_roles`, `spacer_*` included, can
be stored and pre-populated.
