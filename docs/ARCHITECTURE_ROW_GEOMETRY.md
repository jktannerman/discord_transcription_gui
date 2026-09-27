# Row geometry: the document-space spacing model

Part of [ARCHITECTURE.md](ARCHITECTURE.md) - see
[ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md) for the
review screen's other internals.

`VirtualRows.heights` (estimated pre-build, real post-build) is the single
source of truth `VirtualRows.offset_of`, the canvas `scrollregion`, and every
scroll-into-view calculation in `keyboard_nav.py` derive their numbers from.
All of that math implicitly assumes **`VirtualRows.heights[idx]` is the full
vertical screen space row `idx` consumes, including everything `VirtualRows.build_row`
puts around it that the row's own `winfo_height()` can't see** - not just
the row `ttk.Frame`'s own size. Any real on-screen spacing left out of that
number doesn't show up as a one-off glitch: it silently shifts every row
after it by the missed amount, compounding row over row, until a
Tab/Shift-Tab session far enough into a long transcript scrolls to entirely
the wrong place (or decides, wrongly, that no scroll is needed at all) -
exactly the bug this section exists to stop from recurring. The canonical
list of what has to be accounted for, and where:

| Constant (`layout_constants.py`) | Where it lives on screen | Inside the row's own `Frame` (`winfo_height()` sees it)? | Has to be added by hand in |
|---|---|---|---|
| `ROW_FRAME_PADDING_PX`/`ROW_FRAME_BORDERWIDTH_PX` (`ROW_FRAME_OVERHEAD_PX`) | The row `Frame`'s own `padding=`/`borderwidth=` chrome | Yes | `estimate_row_height` only |
| `GAP_BETWEEN_STACKED_PX` | Between stacked elements *within* a row's left/right column | Yes | `estimate_row_height` only |
| `TEXT_BOX_MARGIN_PX` | Extra headroom baked into a text box's own fixed height | Yes | `estimate_row_height` only |
| `ROW_PACK_PADY_PX` (counted ×2: above *and* below) | `VirtualRows.build_row`'s `row.pack(pady=ROW_PACK_PADY_PX)` - the gap *outside* the row's `Frame`, between it and its neighbors in `_scroll_frame` | **No** | `estimate_row_height`, `VirtualRows._remeasure_built_rows` (`real = row.winfo_height() + 2*ROW_PACK_PADY_PX`), *and* every place in `keyboard_nav.py` that turns `VirtualRows.offset_of(index)` into a real screen comparison (`VirtualRows.offset_of(index) + ROW_PACK_PADY_PX + ...`) |

`ROW_PACK_PADY_PX` is the one entry that lives *outside* the row's own
bounding box, which is what made it easy to miss: every other constant above
is chrome the row `Frame` itself contains, so once `_remeasure_built_rows`
captures `row.winfo_height()`, that chrome is automatically included - there
was nothing to add by hand. The pack gap is invisible to `winfo_height()` by
construction (it's the *parent* geometry manager's doing, not the row's own
size), so it had to be added back explicitly in three places:
`estimate_row_height` (the pre-build guess), `_remeasure_built_rows` (the
real height, captured once a row is actually built), and - easy to overlook
even after fixing the first two - everywhere `keyboard_nav.py` derives a
real screen y-coordinate from `VirtualRows.offset_of(index)`. That last one is
subtle for a second reason beyond just "remember to add it":
`VirtualRows.offset_of(index)` is defined as where row `index`'s full
pack-allocated *slot* starts (`sum(VirtualRows.heights[:index])`), not where
its `Frame`'s own visible top edge sits - the `Frame` starts
`ROW_PACK_PADY_PX` further down, past its own leading pady.
`FocusNavigator.scroll_box_into_view`/`FocusNavigator.keep_cursor_in_viewport` both anchor a box's
position off `row.winfo_rooty()` (the `Frame`'s real top edge), so they need
that `+ ROW_PACK_PADY_PX` correction explicitly, on top of `VirtualRows.heights`
already including it in the row's *total* height.

If a future change adds another constant here - more outer padding, a
border on `_scroll_frame` itself, anything `VirtualRows.build_row` packs around a row
rather than inside it - it needs the same three-way treatment, not just a
bump to `ROW_FRAME_OVERHEAD_PX` (which is for chrome *inside* the row only).
The fastest way to catch a future regression of this kind: focus a box
several hundred rows into a long transcript (jumping there, not scrolling
incrementally) and compare its container's real `winfo_rooty()` against the
canvas viewport's real screen bounds - any nonzero, *constant* (not growing)
offset points at a missing one-time correction like the `ROW_PACK_PADY_PX`
one in `keyboard_nav.py`; a *growing* offset (worse the further you've
scrolled) points at a missing per-row term in
`estimate_row_height`/`_remeasure_built_rows` instead.
