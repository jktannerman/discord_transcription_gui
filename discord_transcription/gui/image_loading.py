"""Lazy load/unload of review-row image previews. Images within the
virtualized window's materialized rows are decoded only once their row is
near the visible viewport, and unloaded again once scrolled away - see
review_view.py's module docstring for why rows themselves are virtualized
the same way.
"""

import tkinter as tk
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple, Union

from PIL import Image, ImageOps, ImageTk

from .. import logging_config

logger = logging_config.get_logger(__name__)

# Bounding box for the image preview - roughly two-thirds of a 1200px-wide
# review window, per the project owner's request that images be large
# enough to actually read while transcribing.
THUMBNAIL_SIZE = (760, 950)

ImagePath = Union[str, Path]

# EXIF "Orientation" tag. Values 5-8 mean the stored pixels are a quarter
# turn away from how the photo should be shown (common for phone photos).
_EXIF_ORIENTATION_TAG = 0x0112
_QUARTER_TURN_ORIENTATIONS = frozenset({5, 6, 7, 8})


def _displayed_size(img: Image.Image) -> Tuple[int, int]:
    """An opened image's (width, height) as shown, after EXIF rotation.

    Args:
        img: An image opened with Image.open (only its header is read).

    Returns:
        The stored size, with width and height swapped if the EXIF
        orientation calls for a quarter turn.
    """
    width, height = img.size
    try:
        orientation = img.getexif().get(_EXIF_ORIENTATION_TAG)
    except Exception:
        orientation = None
    if orientation in _QUARTER_TURN_ORIENTATIONS:
        return height, width
    return width, height


def load_display_image(image_path: ImagePath) -> Image.Image:
    """Decode an image for the review screen: EXIF-rotated, fitted to THUMBNAIL_SIZE.

    Args:
        image_path: The image file.

    Returns:
        The decoded image, the size fitted_image_size predicts.
    """
    with Image.open(image_path) as opened:
        image = ImageOps.exif_transpose(opened)
        image.thumbnail(THUMBNAIL_SIZE)
    return image


def fitted_image_size(image_path: ImagePath, bounding_box: Tuple[int, int] = THUMBNAIL_SIZE) -> Tuple[int, int]:
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
            original_size = _displayed_size(img)
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
    """Tracks one image's load state. Row position/height for the
    visibility check comes from ReviewFrame's row-height table (keyed by
    item index), not from this slot, since widget geometry is relative to
    the repositioned scroll frame block rather than the canvas's coordinate
    space."""

    __slots__ = ("image_path", "label", "loaded", "photo")

    def __init__(self, image_path: ImagePath, label: tk.Widget) -> None:
        self.image_path = image_path
        self.label = label
        self.loaded = False
        self.photo: Optional[ImageTk.PhotoImage] = None


class ImageLoader:
    """Owns the set of materialized images and their load state, keyed by
    (item index, image index within that item) since a row can now have
    more than one image. ReviewFrame registers a slot per image when a row
    is built and unregisters every slot for a row (unregister_row) when
    it's torn down; update_visible() loads/unloads each slot based on its
    *row's* offset against the (buffered) viewport - the whole row is
    loaded/unloaded as a unit, not image-by-image, the same as before
    multiple images per row were possible."""

    def __init__(self) -> None:
        self._slots: Dict[Tuple[int, int], ImageSlot] = {}

    def register(self, index: int, image_index: int, image_path: ImagePath, label: tk.Widget) -> None:
        self._slots[(index, image_index)] = ImageSlot(image_path=image_path, label=label)

    def unregister_row(self, index: int) -> None:
        for key in [k for k in self._slots if k[0] == index]:
            del self._slots[key]

    def update_visible(
        self,
        offset_of: Callable[[int], float],
        row_heights: Sequence[float],
        visible_top: float,
        visible_bottom: float,
        log_event: Optional[Callable[..., None]] = None,
    ) -> None:
        """offset_of(index) and row_heights are the same authoritative
        layout source ReviewFrame uses for the canvas's scrollregion -
        absolute coordinates within the full virtual document, not widget-
        relative geometry.

        log_event, if given, is ReviewFrame._log_event - routing every
        load/unload through it (rather than logging directly here) stamps
        each one with the same seq/scroll-state context as every other
        scroll-trace event, so an image load can be correlated against the
        reconcile that triggered it without falling back to timestamps."""
        for (idx, image_idx), slot in self._slots.items():
            row_top = offset_of(idx)
            row_bottom = row_top + row_heights[idx]
            should_be_loaded = row_bottom >= visible_top and row_top <= visible_bottom

            if should_be_loaded and not slot.loaded:
                if log_event:
                    log_event(
                        "loading_image", index=idx, image_index=image_idx,
                        row_top=row_top, row_bottom=row_bottom,
                    )
                self._load_image(slot)
            elif not should_be_loaded and slot.loaded:
                if log_event:
                    log_event(
                        "unloading_image", index=idx, image_index=image_idx,
                        row_top=row_top, row_bottom=row_bottom,
                    )
                self._unload_image(slot)

    def _load_image(self, slot: ImageSlot) -> None:
        try:
            photo = ImageTk.PhotoImage(load_display_image(slot.image_path))
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
