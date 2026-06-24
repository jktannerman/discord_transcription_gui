"""Lazy load/unload of review-row image previews. Images within the
virtualized window's materialized rows are decoded only once their row is
near the visible viewport, and unloaded again once scrolled away - see
review_view.py's module docstring for why rows themselves are virtualized
the same way.
"""

import tkinter as tk
from typing import Dict, Optional, Tuple

from PIL import Image, ImageTk

from .. import logging_config

logger = logging_config.get_logger(__name__)

# Bounding box for the image preview - roughly two-thirds of a 1200px-wide
# review window, per the project owner's request that images be large
# enough to actually read while transcribing.
THUMBNAIL_SIZE = (760, 950)


def fitted_image_size(image_path, bounding_box: Tuple[int, int] = THUMBNAIL_SIZE) -> Tuple[int, int]:
    """The on-screen size review rows actually display image_path at -
    same fit-within-bounding_box-preserving-aspect-ratio logic as
    Image.thumbnail() (used for the real photo in _load_image below), but
    reading only the file's header (Image.open() doesn't decode pixel
    data) so it's cheap enough to call for every row up front, not just
    once an image is scrolled near.

    Used to size a row's image placeholder/floor to the image's real
    displayed height instead of the full bounding box - most images here
    are landscape (wider than tall), so sizing the placeholder to the full
    box would letterbox them, leaving large empty bands above and below
    the actual photo."""
    try:
        with Image.open(image_path) as img:
            original_size = img.size
    except Exception:
        logger.warning(
            "could not read image size", extra=logging_config.extra(image_path=str(image_path))
        )
        return bounding_box

    ow, oh = original_size
    bw, bh = bounding_box
    if ow <= 0 or oh <= 0:
        return bounding_box
    if ow <= bw and oh <= bh:
        return ow, oh  # thumbnail() doesn't upscale past the original size
    ratio = min(bw / ow, bh / oh)
    return max(1, round(ow * ratio)), max(1, round(oh * ratio))


class ImageSlot:
    """Tracks one image row's load state. Row position/height for the
    visibility check comes from ReviewFrame's row-height table (keyed by
    item index), not from this slot, since widget geometry is relative to
    the repositioned scroll frame block rather than the canvas's coordinate
    space."""

    __slots__ = ("image_path", "label", "loaded", "photo")

    def __init__(self, image_path, label: tk.Widget):
        self.image_path = image_path
        self.label = label
        self.loaded = False
        self.photo: Optional[ImageTk.PhotoImage] = None


class ImageLoader:
    """Owns the set of materialized image rows and their load state.
    ReviewFrame registers a slot when a row is built and unregisters it
    when the row is torn down; update_visible() loads/unloads based on
    each slot's row offset against the (buffered) viewport."""

    def __init__(self):
        self._slots: Dict[int, ImageSlot] = {}

    def register(self, index: int, image_path, label: tk.Widget) -> None:
        self._slots[index] = ImageSlot(image_path=image_path, label=label)

    def unregister(self, index: int) -> None:
        self._slots.pop(index, None)

    def update_visible(
        self,
        offset_of,
        row_heights,
        visible_top: float,
        visible_bottom: float,
    ) -> None:
        """offset_of(index) and row_heights are the same authoritative
        layout source ReviewFrame uses for the canvas's scrollregion -
        absolute coordinates within the full virtual document, not widget-
        relative geometry."""
        for idx, slot in self._slots.items():
            row_top = offset_of(idx)
            row_bottom = row_top + row_heights[idx]
            should_be_loaded = row_bottom >= visible_top and row_top <= visible_bottom

            if should_be_loaded and not slot.loaded:
                logger.debug(
                    "loading image",
                    extra=logging_config.extra(index=idx, row_top=row_top, row_bottom=row_bottom),
                )
                self._load_image(slot)
            elif not should_be_loaded and slot.loaded:
                logger.debug(
                    "unloading image",
                    extra=logging_config.extra(index=idx, row_top=row_top, row_bottom=row_bottom),
                )
                self._unload_image(slot)

    def _load_image(self, slot: ImageSlot) -> None:
        try:
            image = Image.open(slot.image_path)
            image.thumbnail(THUMBNAIL_SIZE)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            logger.warning(
                "could not load image preview",
                extra=logging_config.extra(image_path=str(slot.image_path)),
            )
            slot.label.config(image="", text=f"(could not preview {slot.image_path.name})")
            slot.loaded = True  # don't keep retrying a permanently-broken image every scroll
            return

        slot.photo = photo
        slot.label.config(image=photo, text="")
        slot.loaded = True

    def _unload_image(self, slot: ImageSlot) -> None:
        slot.label.config(image="", text="(scroll to load image)")
        slot.photo = None  # drop the reference so Tk/PIL can free the memory
        slot.loaded = False
