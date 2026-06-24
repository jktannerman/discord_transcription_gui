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

# Gap (px) left below a stacked caption (label/box) when a row also has an
# image (and its paired editable box) beneath it - see
# review_view.ReviewFrame._build_row.
GAP_BELOW_MESSAGE_PX = 6

# A row's own ttk.Frame chrome (relief="groove", borderwidth=..., padding=...)
# - see review_view.ReviewFrame._build_row.
ROW_FRAME_PADDING_PX = 6
ROW_FRAME_BORDERWIDTH_PX = 1
# Padding top+bottom plus a couple px for the groove border.
ROW_FRAME_OVERHEAD_PX = ROW_FRAME_PADDING_PX * 2 + 2
