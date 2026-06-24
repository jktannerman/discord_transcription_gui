"""Shared row-layout constants for the review screen.

Kept in one Tk-free module so both review_view.py (the real layout) and
virtualization.py (the pre-build height estimate) read the same values
instead of each hardcoding its own copy that has to be kept in sync by
hand - a stale copy in one of them is exactly what previously made
estimate_row_height diverge from the real built layout, surfacing as a
scroll jump once ReviewFrame._remeasure_built_rows corrected it away.
"""

# Extra headroom (px) added to an editable text box's height on top of its
# paired immutable element's own on-screen height (the label's, for a
# "message" box; the image's, for an "ocr" box) - see
# review_view.ReviewFrame._fixed_text_box_height.
TEXT_BOX_MARGIN_PX = 12

# Gap (px) left below one stacked element (the caption label/box, or an
# image/OCR-box pair) when another follows it in the same column - a
# message with a caption and N images stacks caption, image_0, ..., image_(N-1)
# in the left column (and their editable counterparts in the right column),
# each but the last followed by this gap - see review_view.ReviewFrame._build_row.
GAP_BETWEEN_STACKED_PX = 6

# A row's own ttk.Frame chrome (relief="groove", borderwidth=..., padding=...)
# - see review_view.ReviewFrame._build_row.
ROW_FRAME_PADDING_PX = 6
ROW_FRAME_BORDERWIDTH_PX = 1
# Padding top+bottom plus a couple px for the groove border.
ROW_FRAME_OVERHEAD_PX = ROW_FRAME_PADDING_PX * 2 + 2

# Vertical gap (px) pack() leaves *outside* a row's own Frame, above and
# below it (row_building.RowBuildingMixin._build_row's pack(pady=...)) -
# distinct from ROW_FRAME_OVERHEAD_PX, which is chrome *inside* the row's own
# bounding box and so already included in its winfo_height(). This gap is
# real on-screen vertical space a row consumes that winfo_height() can't see
# at all, on either side - omitting it from a row's recorded height (in both
# estimate_row_height and ReviewFrame._remeasure_built_rows) was what let the
# document-space model (self._row_heights, self._offset_of) drift away from
# the real screen position by 2*ROW_PACK_PADY_PX for every row scrolled past,
# silently breaking keyboard_nav.py's scroll-into-view math the more boxes a
# Tab/Shift-Tab session crossed.
ROW_PACK_PADY_PX = 4

# Height (px) of a spacer slot's text box - exactly one line of
# theme.TEXT_FONT_SIZE plus its own internal pady/border chrome, fixed via
# `height=1` on the real tk.Text widget rather than computed from a paired
# immutable element's height the way _fixed_text_box_height sizes a
# "message"/"ocr" box - a spacer box has no left-column counterpart to pair
# against. Approximate (the real height still comes from Tk once the row is
# built, same reconciliation every other row already gets) - see
# row_building.RowBuildingMixin._build_row and virtualization.estimate_row_height.
SPACER_BOX_HEIGHT_PX = 30
