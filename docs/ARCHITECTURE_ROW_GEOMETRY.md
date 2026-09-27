# Row geometry: the document-space spacing model

Part of [ARCHITECTURE.md](ARCHITECTURE.md) - see
[ARCHITECTURE_REVIEW_SCREEN.md](ARCHITECTURE_REVIEW_SCREEN.md) for the
review screen's other internals.

`VirtualRows.heights` (estimated before a row is built, measured after) is
the single source `VirtualRows.offset_of`, the canvas `scrollregion`, and
every scroll-into-view calculation in `keyboard_nav.py` derive their
numbers from. All of that math assumes **`VirtualRows.heights[idx]` is the
full vertical space row `idx` takes on screen, including everything
`VirtualRows.build_row` puts around it that the row's own `winfo_height()`
can't see**. Any spacing left out doesn't show as a one-off glitch: it
shifts every later row by the missed amount, compounding row after row, so
far enough into a long transcript Tab/Shift-Tab scrolls to the wrong place
(or wrongly decides no scroll is needed). Everything that has to be
accounted for, and where:

| Constant (`layout_constants.py`) | Where it is on screen | Inside the row's own `Frame` (`winfo_height()` sees it)? | Added by hand in |
|---|---|---|---|
| `ROW_FRAME_PADDING_PX`/`ROW_FRAME_BORDERWIDTH_PX` (`ROW_FRAME_OVERHEAD_PX`) | The row `Frame`'s own `padding=`/`borderwidth=` | Yes | `estimate_row_height` only |
| `GAP_BETWEEN_STACKED_PX` | Between stacked elements *within* a row's left/right column | Yes | `estimate_row_height` only |
| `TEXT_BOX_MARGIN_PX` | Extra height built into a text box's fixed height | Yes | `estimate_row_height` only |
| `ROW_PACK_PADY_PX` (counted twice: above *and* below) | `VirtualRows.build_row`'s `row.pack(pady=ROW_PACK_PADY_PX)` - the gap *outside* the row's `Frame`, between it and its neighbors | **No** | `estimate_row_height`, `VirtualRows._remeasure_built_rows` (`real = row.winfo_height() + 2*ROW_PACK_PADY_PX`), *and* every place in `keyboard_nav.py` that turns `VirtualRows.offset_of(index)` into a screen position |

`ROW_PACK_PADY_PX` is the one entry *outside* the row's own bounding box.
Everything else is inside the row `Frame`, so a measured
`row.winfo_height()` already includes it. The pack gap belongs to the
*parent's* geometry manager, so `winfo_height()` never includes it, and it
has to be added in three places: `estimate_row_height` (the estimate),
`_remeasure_built_rows` (the measured height), and everywhere
`keyboard_nav.py` derives a screen position from `VirtualRows.offset_of`.
The last one needs care: `offset_of(index)` is where row `index`'s
pack-allocated *slot* starts (`sum(VirtualRows.heights[:index])`), and its
`Frame`'s visible top edge is `ROW_PACK_PADY_PX` below that. Since
`FocusNavigator.box_document_top` measures a box's position from
`row.winfo_rooty()` (the `Frame`'s real top edge), it adds that
`ROW_PACK_PADY_PX` itself, on top of `heights` including it in the row's
total.

Anything new that `VirtualRows.build_row` packs *around* a row rather than
inside it (more outer padding, a border on `_scroll_frame`, ...) needs the
same three-way treatment, not just a bump to `ROW_FRAME_OVERHEAD_PX`, which
is for chrome inside the row only. To check for this kind of error: jump
focus (don't scroll) to a box several hundred rows into a long transcript,
and compare its container's real `winfo_rooty()` with the canvas
viewport's. `scroll_box_into_view` logs this comparison as
`model_real_discrepancy_px` in the scroll trace. A *constant* offset points
at a missing one-time correction like the `ROW_PACK_PADY_PX` one in
`keyboard_nav.py`; an offset that *grows* the further you are points at a
missing per-row term in `estimate_row_height`/`_remeasure_built_rows`.
