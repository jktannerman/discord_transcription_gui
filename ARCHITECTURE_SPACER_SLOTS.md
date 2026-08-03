# Spacer slots

Part of [ARCHITECTURE.md](ARCHITECTURE.md).

Blank-line spacing between/within review items used to be produced by a
post-run regex pass (`cleanup.py`) that collapsed excess newlines to a fixed
cap and special-cased die-roll commands - it couldn't express anything finer
than its hardcoded rules, and silently clobbered a deliberately larger gap
back down to its cap. Spacing is now fully owned by dedicated "spacer" text
boxes on the review screen: one-line-tall, right-column-only editable boxes
between every adjacent pair of transcription elements, holding literal `\n`
*tokens* (the two characters `\` and `n`, not real newlines) that the user
can freely edit. Nothing else in a spacer box has any effect - see
"Finalize-time parsing" below.

## Slot ordering (`review_item.ReviewItem.slot_roles`)

Four different places used to each independently re-derive "message, then
ocr0, ocr1, ..." from `item.initial_message_text`/`item.image_paths` - row
building, height estimation, the keyboard-navigable slot list, and output
writing. Adding spacer slots meant inserting new roles into that sequence,
so `ReviewItem.slot_roles` now computes the full ordered list once and every
one of those four places (`row_building.RowBuildingMixin._build_row`,
`virtualization.estimate_row_height`, `review_view.ReviewFrame.__init__`'s
`self._slots`, `review_item.lines_for_item`) just walks it, rather than each
re-deriving its own copy that could drift out of sync with the others.

For an item with a message and N images, `slot_roles` is: `["message",
"spacer_msg_img", "ocr0", "spacer_img0", "ocr1", ..., "ocr{N-1}",
"spacer_end"]` - `"spacer_msg_img"` only appears when the item has both a
message and at least one image; `"spacer_img{i}"` appears between every pair
of images (omitted after the last one); `"spacer_end"` (the gap before the
next message) always appears, even for an item with no images at all. A
spacer role has no left-column counterpart - row building puts nothing in
the left column for it, and it's sized to exactly one Tk text line
(`SPACER_BOX_HEIGHT_PX`/`height=1`, see
`row_building.RowBuildingMixin._build_spacer_text_box`) rather than via
`_fixed_text_box_height`'s paired-height rule.

Tab/Shift-Tab visit spacer slots the same as any content slot (per the
project owner's decision) - `self._slots` already generalizes to any role
string, so `keyboard_nav.py`'s `_move_focus` needed no changes at all.

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
newline that terminates the line right before it - see "Finalize-time
parsing" below for why that one extra newline isn't *also* added by the
preceding content box.

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
- `write_message_lines`/`write_all_items` no longer add any fixed padding
  around an item's chunks (the old unconditional `"\n\n\n\n"` prefix/`"\n\n"`
  suffix was exactly the behavior spacer slots replace) - each item's own
  `"spacer_end"` chunk now supplies the entire gap before the next item. The
  gap before the very first item of a run is already provided by the
  previous run's trailing `\n\n\n{BREAK_MARKER}\n\n\n` (written by
  `finalize_run`), so no leading padding is needed there either.

## Session format

`ReviewFrame.collect_edited_texts`/the autosaved `edited_texts` session
field changed shape from a fixed per-item `(message_text, ocr_texts)` tuple
to a per-item `{role: text}` dict (covering every role in that item's
`slot_roles`, content and spacer alike) - the old shape had no way to
address a spacer slot at all. `_show_review` discarded a saved session in
the old shape (detected structurally, via a saved per-message edit dict
containing the literal key `"ocr"`) for a transition period after this
change shipped; that detection has since been removed now that no
pre-spacer-slot session is expected to still be on disk.

## Finalized edit persistence

When the user clicks Finalize and the pipeline succeeds,
`_on_finalize_clicked` saves every non-`None` slot value to
`finalized_edits.json` (via `state.save_finalized_edits`), keyed by the HTML
path and each message's Discord `message_id`. On a subsequent fresh run of
the same chatlog, `_show_review` loads these via `state.load_finalized_edits`
and passes them to `ReviewFrame` as `initial_finalized_texts`.

**Priority order** inside `ReviewFrame.__init__`: `_saved_texts` is seeded
first from the in-progress session (`initial_saved_texts`), then from
finalized edits for any slot not already covered. The existing
checkbox-seeding loop (`checked = saved is not None and saved != default`)
runs last, so a finalized edit that differs from the OCR default starts its
checkbox checked automatically with no special-case code.

**Merge semantics** (`state.save_finalized_edits`): new non-`None` values
are merged into whatever was already stored for this chatlog; `None` values
(unchecked OCR boxes) are skipped, leaving the prior finalized text for that
slot intact. A finalized edit is never deleted - once stored it persists
until overwritten by a subsequent finalize that supplies a non-`None` value
for that slot.

**Matching** (`_match_finalized_edits`): stored `{message_id: {role: text}}`
data is aligned to the current item list by Discord `message_id` (not by
position) using the same approach as `_match_saved_edits` for session
resume - orphaned message IDs are dropped silently, and roles that no
longer appear in an item's `slot_roles` (e.g. because the chatlog was
re-exported with fewer images) are also dropped. All `slot_roles` including
`spacer_*` are eligible for storage and pre-population.
