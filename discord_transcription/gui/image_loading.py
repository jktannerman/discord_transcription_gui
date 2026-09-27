"""Image previews on the review screen: sizing, and lazy loading.

A built row's images are decoded only while the row is near the viewport,
and unloaded again once it's scrolled away. Their on-screen size is known
before that from the file header alone (fitted_image_size).
"""

import functools
import tkinter as tk
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple, Union

from PIL import Image, ImageOps, ImageTk

from .. import logging_config

logger = logging_config.get_logger(__name__)

# Default width (px) of the review screen's image column - large enough to
# read screenshots while transcribing. The column divider changes it (see
# column_divider.py).
DEFAULT_IMAGE_COLUMN_WIDTH_PX = 760
# Tallest an image preview is ever shown, whatever the column width.
MAX_IMAGE_HEIGHT_PX = 950
# Most an image preview is ever enlarged beyond its own size, so a tiny image
# (an emoji, a small icon) doesn't blow up to fill the whole column.
MAX_IMAGE_ENLARGEMENT = 4.0
# Bounding box for an image preview at the default column width.
THUMBNAIL_SIZE = (DEFAULT_IMAGE_COLUMN_WIDTH_PX, MAX_IMAGE_HEIGHT_PX)


def image_bounding_box(column_width_px: int) -> Tuple[int, int]:
    """The box an image preview is fitted into for a given column width.

    Args:
        column_width_px: The image column's width.

    Returns:
        (width, height) of the bounding box.
    """
    return (column_width_px, MAX_IMAGE_HEIGHT_PX)

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


def fit_to_box(size: Tuple[int, int], bounding_box: Tuple[int, int]) -> Tuple[int, int]:
    """Scale `size` to fill `bounding_box` as far as it can, keeping its
    aspect ratio - shrinking a large image and enlarging a small one alike,
    so small images are as easy to read as the column allows, but never
    enlarging past MAX_IMAGE_ENLARGEMENT times the original size.

    Args:
        size: The image's (width, height).
        bounding_box: The (width, height) to fit within.

    Returns:
        The fitted (width, height); `bounding_box` itself for an empty size.
    """
    ow, oh = size
    bw, bh = bounding_box
    if ow <= 0 or oh <= 0:
        return bounding_box
    ratio = min(bw / ow, bh / oh, MAX_IMAGE_ENLARGEMENT)
    return max(1, round(ow * ratio)), max(1, round(oh * ratio))


def load_display_image(
    image_path: ImagePath, bounding_box: Tuple[int, int] = THUMBNAIL_SIZE
) -> Image.Image:
    """Decode an image for the review screen: EXIF-rotated, fitted to bounding_box.

    Args:
        image_path: The image file.
        bounding_box: The (width, height) to fit within, enlarging the
            image if it's smaller.

    Returns:
        The decoded image, the size fitted_image_size predicts.
    """
    with Image.open(image_path) as opened:
        image = ImageOps.exif_transpose(opened)
        target = fit_to_box(image.size, bounding_box)
        if target != image.size:
            # reducing_gap speeds up large reductions; it has no effect when enlarging.
            image = image.resize(target, Image.Resampling.LANCZOS, reducing_gap=3.0)
        else:
            image.load()
    return image


@functools.lru_cache(maxsize=None)
def _natural_size(image_path: str) -> Optional[Tuple[int, int]]:
    """An image's displayed (EXIF-rotated) size, read from its header once.

    Cached per path, so recomputing every row's height for a new column
    width (see ColumnDivider.set_width) is plain arithmetic
    rather than a file read per image.

    Args:
        image_path: The image file, as a string (the cache key).

    Returns:
        (width, height), or None if the file can't be read.
    """
    try:
        with Image.open(image_path) as img:
            return _displayed_size(img)
    except Exception:
        logger.warning("could not read image size", extra=logging_config.extra(image_path=image_path))
        return None


def fitted_image_size(image_path: ImagePath, bounding_box: Tuple[int, int] = THUMBNAIL_SIZE) -> Tuple[int, int]:
    """The size an image is shown at on the review screen.

    Uses the same fit_to_box rule as load_display_image, but reads only the
    file header, so it's cheap enough to size every row's placeholder (and
    height estimate) before any image is decoded.

    Args:
        image_path: The image file.
        bounding_box: The (width, height) to fit within.

    Returns:
        The fitted (width, height), or `bounding_box` itself if the file
        can't be read.
    """
    original_size = _natural_size(str(image_path))
    if original_size is None:
        return bounding_box

    return fit_to_box(original_size, bounding_box)


class ImageSlot:
    """One built image placeholder and its load state.

    Its position comes from its row's document offset (see
    ImageLoader.update_visible), not from widget geometry.
    """

    __slots__ = ("image_path", "label", "bounding_box", "loaded", "photo")

    def __init__(
        self, image_path: ImagePath, label: tk.Widget, bounding_box: Tuple[int, int] = THUMBNAIL_SIZE
    ) -> None:
        self.image_path = image_path
        self.label = label
        self.bounding_box = bounding_box
        self.loaded = False
        self.photo: Optional[ImageTk.PhotoImage] = None


class ImageLoader:
    """The built rows' images, keyed by (item index, image index).

    RowBuilder registers each image as its row is built, and the row's
    images are unregistered when it's torn down. update_visible() loads or
    unloads a row's images together, by the row's position.
    """

    def __init__(self) -> None:
        self._slots: Dict[Tuple[int, int], ImageSlot] = {}

    def register(
        self,
        index: int,
        image_index: int,
        image_path: ImagePath,
        label: tk.Widget,
        bounding_box: Tuple[int, int] = THUMBNAIL_SIZE,
    ) -> None:
        self._slots[(index, image_index)] = ImageSlot(
            image_path=image_path, label=label, bounding_box=bounding_box
        )

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
        """Load the images of rows overlapping [visible_top, visible_bottom]
        and unload the rest.

        Args:
            offset_of: A row's document offset (VirtualRows.offset_of).
            row_heights: Every row's height (VirtualRows.heights).
            visible_top: Top of the range to load, in document coordinates.
            visible_bottom: Bottom of that range.
            log_event: VirtualRows.log_event, so loads and unloads appear in
                the scroll trace alongside the reconcile that caused them.
        """
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
            photo = ImageTk.PhotoImage(load_display_image(slot.image_path, slot.bounding_box))
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
