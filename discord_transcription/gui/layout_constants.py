"""Shared row-layout constants for the review screen.

Kept in one Tk-free module so both review_view.py (the real layout) and
virtualization.py (the pre-build height estimate) read the same values
instead of each hardcoding its own copy that has to be kept in sync by
hand - a stale copy in one of them is exactly what previously made
estimate_row_height diverge from the real built layout, surfacing as a
scroll jump once VirtualRows._remeasure_built_rows corrected it away.
"""

# Extra headroom (px) added to an editable text box's height on top of its
# paired immutable element's own on-screen height (the label's, for a
# "message" box; the image's, for an "ocr" box) - see
# RowBuilder.fixed_text_box_height.
TEXT_BOX_MARGIN_PX = 12

# Gap (px) left below one stacked element (the caption label/box, or an
# image/OCR-box pair) when another follows it in the same column - a
# message with a caption and N images stacks caption, image_0, ..., image_(N-1)
# in the left column (and their editable counterparts in the right column),
# each but the last followed by this gap - see RowBuilder.fill_row.
GAP_BETWEEN_STACKED_PX = 6

# A row's own ttk.Frame chrome (relief="groove", borderwidth=..., padding=...)
# - see VirtualRows.build_row.
ROW_FRAME_PADDING_PX = 6
ROW_FRAME_BORDERWIDTH_PX = 1
# Padding top+bottom plus a couple px for the groove border.
ROW_FRAME_OVERHEAD_PX = ROW_FRAME_PADDING_PX * 2 + 2

# Vertical gap (px) pack() leaves *outside* a row's own Frame, above and
# below it (VirtualRows.build_row's pack(pady=...)) -
# distinct from ROW_FRAME_OVERHEAD_PX, which is chrome *inside* the row's own
# bounding box and so already included in its winfo_height(). This gap is
# real on-screen vertical space a row consumes that winfo_height() can't see
# at all, on either side - omitting it from a row's recorded height (in both
# estimate_row_height and VirtualRows._remeasure_built_rows) was what let the
# document-space model (VirtualRows.heights, VirtualRows.offset_of) drift away from
# the real screen position by 2*ROW_PACK_PADY_PX for every row scrolled past,
# silently breaking keyboard_nav.py's scroll-into-view math the more boxes a
# Tab/Shift-Tab session crossed.
ROW_PACK_PADY_PX = 4

# Horizontal gap (px) pack() leaves outside a row's own Frame, on its left
# and right - VirtualRows.build_row's pack(padx=...).
ROW_PACK_PADX_PX = 4

# Horizontal padding (px) pack() leaves on each side of a row's image column
# and text column (RowBuilder.fill_row's left/right frames), so the two columns are
# 2 * COLUMN_PADX_PX apart.
COLUMN_PADX_PX = 6

# x position (px, from the canvas's left edge) where every row's image column
# starts: the row's outer pack gap, its Frame's border and padding, then the
# image column's own left padding.
IMAGE_COLUMN_LEFT_PX = (
    ROW_PACK_PADX_PX + ROW_FRAME_BORDERWIDTH_PX + ROW_FRAME_PADDING_PX + COLUMN_PADX_PX
)

# Horizontal space (px) a row uses besides its two columns' contents: the
# chrome on both sides of the row plus both columns' padding on both sides.
ROW_HORIZONTAL_OVERHEAD_PX = (
    2 * (ROW_PACK_PADX_PX + ROW_FRAME_BORDERWIDTH_PX + ROW_FRAME_PADDING_PX) + 4 * COLUMN_PADX_PX
)

# Narrowest the image column and the text column can be dragged to (see
# column_divider.py). If the window is too narrow for both, the image
# column's minimum wins.
MIN_IMAGE_COLUMN_WIDTH_PX = 200
MIN_TEXT_COLUMN_WIDTH_PX = 250

# Width (px) of the draggable divider between the two columns - it sits in
# the 2 * COLUMN_PADX_PX gap between them.
COLUMN_DIVIDER_WIDTH_PX = 6
