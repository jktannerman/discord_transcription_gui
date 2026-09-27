"""Building one review row's widgets, and estimating rows' heights before
they're built.

Every row has the same two-column shape, both columns stacked
text-above-images (mirroring Discord's own layout): an immutable left
column (the message's own text, its image(s), or both) and, on the right,
one editable box per slot (see review_item.ReviewItem.slot_roles) - a copy
of the message's text, one OCR box per image, and a spacer box
between/after them.

The estimate (virtualization.estimate_row_height) has to stay Tk-free to be
unit-testable, so it hand-mirrors fill_row's sizing rules rather than
calling into it; the constants both use live in layout_constants.py. If you
change how a role is sized here, change it there too, or the estimate
drifts from the real layout and shows up as a scroll jump when VirtualRows
remeasures the row.
"""

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk

from ..review_item import ReviewItem
from . import theme
from .image_context_menu import ImageContextMenu
from .image_loading import DEFAULT_IMAGE_COLUMN_WIDTH_PX, ImageLoader, fitted_image_size, image_bounding_box
from .layout_constants import COLUMN_PADX_PX, GAP_BETWEEN_STACKED_PX, TEXT_BOX_MARGIN_PX
from .slot_boxes import SlotBoxes, make_spacer_text_widget
from .virtualization import TextMetrics, estimate_row_height

# A content box is capped at this fraction of the viewport's height, not the
# whole of it, so a maxed-out box still fits on screen with room to spare.
TEXT_BOX_MAX_HEIGHT_FRACTION = 0.7


def _make_original_text_label(parent: tk.Widget, text: str, wraplength: int) -> ttk.Label:
    """Create (but don't pack) a row's immutable original-text label.

    Shared by the real row build and measure_text_metrics, so the label
    that's measured is exactly the one that's built.
    """
    return ttk.Label(
        parent, text=text, wraplength=wraplength, justify="left",
        font=(theme.TEXT_FONT_FAMILY, theme.TEXT_FONT_SIZE),
    )


def measure_text_metrics(parent: tk.Widget) -> TextMetrics:
    """Measure the review screen's text sizes on the running display.

    Builds a throwaway original-text label and spacer box (never shown) and
    reads their requested heights, plus the text font's own metrics. These
    depend on which font Tk actually resolves (Consolas is substituted on
    most Linux systems) and on the display's scaling.

    Args:
        parent: Any widget on the review screen's display.

    Returns:
        The measured sizes.
    """
    # Built from family/size rather than tkfont.Font(font=(family, size)):
    # the latter double-applies the display's scaling on some systems.
    font = tkfont.Font(root=parent, family=theme.TEXT_FONT_FAMILY, size=theme.TEXT_FONT_SIZE)
    line_height = font.metrics("linespace")

    label = _make_original_text_label(parent, "x", wraplength=100)
    label_height = label.winfo_reqheight()
    label.destroy()

    spacer = make_spacer_text_widget(parent)
    spacer_height = spacer.winfo_reqheight()
    spacer.destroy()

    return TextMetrics(
        char_width_px=max(1, font.measure("0")),
        line_height_px=max(1, line_height),
        label_padding_px=max(0, label_height - line_height),
        spacer_box_height_px=max(1, spacer_height),
    )


class RowBuilder:
    """Builds rows' widgets, and estimates their heights before that.

    Attributes:
        image_column_width_px: The left column's width - images are fitted
            to it and the original-text label wraps at it. Set by the
            column divider.
        text_metrics: Measured text sizes (see measure_text_metrics).
    """

    def __init__(
        self,
        items: list[ReviewItem],
        boxes: SlotBoxes,
        images: ImageLoader,
        context_menu: ImageContextMenu,
        viewport: tk.Misc,
        text_metrics: TextMetrics,
    ) -> None:
        """
        Args:
            items: The review items, in transcript order.
            boxes: Builds each row's editable boxes.
            images: Registers each image placeholder for lazy loading.
            context_menu: Bound to each image's right-click.
            viewport: The widget whose height caps a content box's height
                (the review canvas).
            text_metrics: Measured text sizes.
        """
        self._items = items
        self._boxes = boxes
        self._images = images
        self._context_menu = context_menu
        self._viewport = viewport
        self.text_metrics = text_metrics
        self.image_column_width_px = DEFAULT_IMAGE_COLUMN_WIDTH_PX

    # -- sizing ------------------------------------------------------------------

    def max_text_box_height_px(self) -> int:
        """The cap on a content box's height: TEXT_BOX_MAX_HEIGHT_FRACTION of
        the viewport, or of the screen if the viewport isn't laid out yet."""
        viewport = self._viewport.winfo_height()
        if viewport <= 1:
            viewport = self._viewport.winfo_screenheight()
        return int(viewport * TEXT_BOX_MAX_HEIGHT_FRACTION)

    def fixed_text_box_height(self, paired_height: int) -> int:
        """A content box's height, fixed at build time: its paired immutable
        element's height (the label's, for a "message" box; the image's,
        for an "ocr" box) plus TEXT_BOX_MARGIN_PX, capped at
        max_text_box_height_px. Overflow scrolls inside the box.

        Fixing the height up front (rather than growing the box with its
        content) is what makes a row's height knowable before it's built.
        """
        return max(1, min(paired_height + TEXT_BOX_MARGIN_PX, self.max_text_box_height_px()))

    def estimate_heights(self) -> list[int]:
        """Pre-build height estimates for every row at the current image
        column width - see virtualization.estimate_row_height."""
        max_box_px = self.max_text_box_height_px()
        return [
            estimate_row_height(
                item,
                max_text_box_height_px=max_box_px,
                metrics=self.text_metrics,
                image_column_width_px=self.image_column_width_px,
            )
            for item in self._items
        ]

    # -- building --------------------------------------------------------------

    def fill_row(self, index: int, row: tk.Widget) -> None:
        """Build items[index]'s widgets inside `row`.

        One stacked element per slot role; every element but the last gets
        GAP_BETWEEN_STACKED_PX below it. Spacer roles have no left-column
        counterpart.
        """
        item = self._items[index]
        left = ttk.Frame(row)
        left.pack(side="left", padx=COLUMN_PADX_PX, fill="y")
        right = ttk.Frame(row)
        right.pack(side="left", fill="x", expand=True, padx=COLUMN_PADX_PX)

        roles = item.slot_roles
        for position, role in enumerate(roles):
            gap = GAP_BETWEEN_STACKED_PX if position < len(roles) - 1 else 0
            key = (index, role)
            if role == "message":
                message_h = self._build_message_label(left, item, pady_bottom=gap)
                self._boxes.build_content_box(
                    right, key, self.fixed_text_box_height(message_h), pady_bottom=gap
                )
            elif role.startswith("ocr"):
                image_index = int(role[len("ocr"):])
                image_h = self._build_image_placeholder(
                    left, item.image_paths[image_index], index, image_index, pady_bottom=gap,
                )
                self._boxes.build_content_box(
                    right, key, self.fixed_text_box_height(image_h), pady_bottom=gap
                )
            else:
                self._boxes.build_spacer_box(
                    right, key, self.text_metrics.spacer_box_height_px, pady_bottom=gap
                )

    def _build_message_label(self, parent: tk.Widget, item: ReviewItem, pady_bottom: int) -> int:
        """Build the immutable label holding a message's own original text,
        in the same font as the editable boxes but the plain background, so
        it reads as distinct from its editable copy.

        Returns:
            The label's height (px), which its paired box is sized from.
        """
        preview = "\n".join(item.entry.text_lines).strip() or "(no text)"
        container = ttk.Frame(parent)
        container.pack(pady=(0, pady_bottom))
        label = _make_original_text_label(container, preview, self.image_column_width_px)
        label.pack(anchor="w", fill="x")
        # The label's requested height is known as soon as it's configured.
        # Don't flush the idle queue to measure the container instead: that
        # repaints the whole review screen mid-reconcile, half-rebuilt.
        floor_px = max(label.winfo_reqheight(), 1)
        container.configure(width=self.image_column_width_px, height=floor_px)
        container.pack_propagate(False)
        return floor_px

    def _build_image_placeholder(
        self, parent: tk.Widget, image_path: Path, index: int, image_index: int, pady_bottom: int = 0,
    ) -> int:
        """Build a fixed-size placeholder for one of the row's images (its
        pixels are loaded lazily - see image_loading.py) and register it.

        Every image is the column's width; its height is its own
        aspect-preserving fit, read cheaply from the file header. Fixed, so
        loading/unloading the image never changes the row's layout.

        Returns:
            The image's on-screen height (px), which its OCR box is sized
            from.
        """
        bounding_box = image_bounding_box(self.image_column_width_px)
        _, image_h = fitted_image_size(image_path, bounding_box)
        container = ttk.Frame(parent, width=self.image_column_width_px, height=image_h)
        container.pack_propagate(False)
        container.pack(pady=(0, pady_bottom))
        image_label = ttk.Label(container, text="(scroll to load image)", anchor="center")
        image_label.pack(fill="both", expand=True)
        self._images.register(index, image_index, image_path, image_label, bounding_box)
        self._context_menu.bind(image_label, image_path, self._items[index].message_id)
        return image_h
