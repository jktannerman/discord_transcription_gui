"""Pure-ish sizing/visibility-dispatch logic for review-row image previews.

fitted_image_size's aspect-fit math and ImageLoader.update_visible's
load/unload boundary decision are exactly the kind of off-by-one sizing
logic that previously caused the review screen's scroll-jump bugs (see
ARCHITECTURE.md) - this had no test coverage at all before."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import tkinter as tk
from PIL import Image

from discord_transcription.gui.image_loading import (
    MAX_IMAGE_ENLARGEMENT,
    MAX_IMAGE_HEIGHT_PX,
    THUMBNAIL_SIZE,
    ImageLoader,
    fitted_image_size,
    image_bounding_box,
    load_display_image,
)


@pytest.fixture
def landscape_image(tmp_path):
    path = tmp_path / "landscape.png"
    Image.new("RGB", (3000, 1500), color="blue").save(path)
    return path


@pytest.fixture
def portrait_image(tmp_path):
    path = tmp_path / "portrait.png"
    Image.new("RGB", (1000, 4000), color="red").save(path)
    return path


@pytest.fixture
def small_image(tmp_path):
    path = tmp_path / "small.png"
    Image.new("RGB", (50, 30), color="green").save(path)
    return path


@pytest.fixture
def rotated_phone_photo(tmp_path):
    """Stored 2000x1000 (landscape) with EXIF orientation 6: shown portrait."""
    path = tmp_path / "phone.jpg"
    image = Image.new("RGB", (2000, 1000), color="blue")
    exif = image.getexif()
    exif[0x0112] = 6
    image.save(path, exif=exif)
    return path


# -- fitted_image_size ------------------------------------------------------


def test_fitted_image_size_enlarges_an_image_smaller_than_the_box(tmp_path):
    path = tmp_path / "medium.png"
    Image.new("RGB", (400, 200), color="green").save(path)
    # 400x200 fills the 760px width: 1.9x.
    assert fitted_image_size(path, bounding_box=THUMBNAIL_SIZE) == (760, 380)


def test_fitted_image_size_enlargement_is_capped_at_max_enlargement(small_image):
    # 50x30 would need 15.2x to fill 760px; it stops at 4x.
    assert MAX_IMAGE_ENLARGEMENT == 4.0
    assert fitted_image_size(small_image, bounding_box=THUMBNAIL_SIZE) == (200, 120)


def test_fitted_image_size_enlargement_is_capped_by_box_height(tmp_path):
    path = tmp_path / "tall.png"
    Image.new("RGB", (300, 400), color="green").save(path)
    # The width allows 6.7x and the cap 4x, but the 950px height allows only 2.375x.
    assert fitted_image_size(path, bounding_box=(2000, 950)) == (712, 950)


def test_load_display_image_enlarges_to_the_fitted_size(small_image):
    image = load_display_image(small_image, bounding_box=THUMBNAIL_SIZE)
    assert image.size == fitted_image_size(small_image, bounding_box=THUMBNAIL_SIZE) == (200, 120)


def test_fitted_image_size_landscape_is_width_constrained(landscape_image):
    w, h = fitted_image_size(landscape_image, bounding_box=(760, 950))
    assert w == 760
    assert h == round(1500 * (760 / 3000))
    assert h < 950  # landscape: shorter than the full bounding box, not letterboxed


def test_fitted_image_size_portrait_is_height_constrained(portrait_image):
    w, h = fitted_image_size(portrait_image, bounding_box=(760, 950))
    assert h == 950
    assert w == round(1000 * (950 / 4000))
    assert w < 760


def test_fitted_image_size_preserves_aspect_ratio(landscape_image):
    w, h = fitted_image_size(landscape_image, bounding_box=(760, 950))
    original_ratio = 3000 / 1500
    assert abs((w / h) - original_ratio) < 0.02


def test_fitted_image_size_unreadable_path_falls_back_to_bounding_box(tmp_path):
    bogus = tmp_path / "does_not_exist.png"
    assert fitted_image_size(bogus, bounding_box=(760, 950)) == (760, 950)


# -- ImageLoader.update_visible's load/unload dispatch -----------------------


def _loader_with_dispatch_spies():
    loader = ImageLoader()
    loader.register(0, 0, "fake_path_0", SimpleNamespace())
    loader.register(1, 0, "fake_path_1", SimpleNamespace())
    loader.register(2, 0, "fake_path_2", SimpleNamespace())
    loader.register(3, 0, "fake_path_3", SimpleNamespace())
    return loader


def _update(loader, visible_top, visible_bottom):
    row_heights = [100, 100, 100, 100]

    def offset_of(idx):
        return sum(row_heights[:idx])

    with patch.object(loader, "_load_image") as load, patch.object(loader, "_unload_image") as unload:
        loader.update_visible(offset_of, row_heights, visible_top, visible_bottom)
    return load, unload


def test_update_visible_loads_only_rows_overlapping_the_viewport():
    loader = _loader_with_dispatch_spies()
    load, unload = _update(loader, visible_top=150, visible_bottom=250)
    loaded_indices = {call.args[0].image_path for call in load.call_args_list}
    # rows: 0 -> [0,100), 1 -> [100,200), 2 -> [200,300), 3 -> [300,400)
    # overlapping [150, 250]: rows 1 and 2 only.
    assert loaded_indices == {"fake_path_1", "fake_path_2"}
    unload.assert_not_called()


def test_update_visible_does_not_reload_an_already_loaded_row():
    loader = _loader_with_dispatch_spies()
    loader._slots[(1, 0)].loaded = True
    load, unload = _update(loader, visible_top=150, visible_bottom=250)
    loaded_indices = {call.args[0].image_path for call in load.call_args_list}
    assert loaded_indices == {"fake_path_2"}  # row 1 already loaded, skipped


def test_update_visible_unloads_rows_that_scrolled_away():
    loader = _loader_with_dispatch_spies()
    for slot in loader._slots.values():
        slot.loaded = True
    load, unload = _update(loader, visible_top=150, visible_bottom=250)
    load.assert_not_called()
    unloaded_indices = {call.args[0].image_path for call in unload.call_args_list}
    assert unloaded_indices == {"fake_path_0", "fake_path_3"}


def test_update_visible_does_not_unload_a_row_still_in_view():
    loader = _loader_with_dispatch_spies()
    for slot in loader._slots.values():
        slot.loaded = True
    _, unload = _update(loader, visible_top=150, visible_bottom=250)
    unloaded_indices = {call.args[0].image_path for call in unload.call_args_list}
    assert "fake_path_1" not in unloaded_indices
    assert "fake_path_2" not in unloaded_indices


def test_update_visible_calls_log_event_for_each_load_and_unload():
    loader = _loader_with_dispatch_spies()
    loader._slots[(3, 0)].loaded = True  # will be unloaded
    logged = []
    row_heights = [100, 100, 100, 100]
    with patch.object(loader, "_load_image"), patch.object(loader, "_unload_image"):
        loader.update_visible(
            lambda idx: sum(row_heights[:idx]), row_heights, 150, 250,
            log_event=lambda event, **fields: logged.append((event, fields)),
        )
    events = {event for event, _ in logged}
    assert "loading_image" in events
    assert "unloading_image" in events


# -- ImageLoader._load_image / _unload_image (real Tk widgets) --------------
#
# Only these three tests build a real Tk root - the brief window it creates
# can flash on screen (most visibly the first Tk() call in a process, which
# also runs Tcl/Tk's one-time subsystem init), so they're marked "gui" and
# excluded from the default run (see pyproject.toml's addopts) rather than
# the whole module, since everything above this point needs no real Tk at
# all. Run with `-m gui` to include them.


@pytest.fixture
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.mark.gui
def test_load_image_success_sets_photo_on_label(tk_root, small_image):
    loader = ImageLoader()
    label = tk.Label(tk_root)
    loader.register(0, 0, small_image, label)
    loader.update_visible(lambda idx: 0, [100], visible_top=0, visible_bottom=100)
    assert loader._slots[(0, 0)].loaded is True
    assert loader._slots[(0, 0)].photo is not None
    assert label.cget("image") != ""


@pytest.mark.gui
def test_load_image_failure_shows_fallback_text_and_marks_loaded_to_avoid_retry_storm(tk_root, tmp_path):
    loader = ImageLoader()
    label = tk.Label(tk_root)
    bogus = tmp_path / "does_not_exist.png"
    loader.register(0, 0, bogus, label)
    loader.update_visible(lambda idx: 0, [100], visible_top=0, visible_bottom=100)
    assert loader._slots[(0, 0)].loaded is True  # marked loaded even on failure
    assert "could not preview" in label.cget("text")


@pytest.mark.gui
def test_unload_image_clears_photo_and_restores_placeholder_text(tk_root, small_image):
    loader = ImageLoader()
    label = tk.Label(tk_root)
    loader.register(0, 0, small_image, label)
    loader.update_visible(lambda idx: 0, [100], visible_top=0, visible_bottom=100)
    assert loader._slots[(0, 0)].loaded is True

    loader.update_visible(lambda idx: 0, [100], visible_top=1000, visible_bottom=2000)
    assert loader._slots[(0, 0)].loaded is False
    assert loader._slots[(0, 0)].photo is None
    assert label.cget("text") == "(scroll to load image)"


# -- EXIF orientation -------------------------------------------------------


def test_fitted_image_size_accounts_for_exif_rotation(rotated_phone_photo):
    # Shown as 1000x2000, so it's height-constrained like any portrait image.
    assert fitted_image_size(rotated_phone_photo, bounding_box=(760, 950)) == (475, 950)


def test_load_display_image_applies_exif_rotation_and_matches_fitted_size(rotated_phone_photo):
    image = load_display_image(rotated_phone_photo)
    assert image.size == fitted_image_size(rotated_phone_photo)


def test_load_display_image_leaves_unrotated_images_alone(landscape_image):
    image = load_display_image(landscape_image)
    assert image.size == fitted_image_size(landscape_image)


def test_image_bounding_box_uses_column_width_and_fixed_max_height():
    assert image_bounding_box(1200) == (1200, MAX_IMAGE_HEIGHT_PX)


def test_fitted_image_size_follows_bounding_box_width(landscape_image):
    assert fitted_image_size(landscape_image, image_bounding_box(1500)) == (1500, 750)
    assert fitted_image_size(landscape_image, image_bounding_box(600)) == (600, 300)


def test_load_display_image_fits_to_given_bounding_box(landscape_image):
    image = load_display_image(landscape_image, image_bounding_box(1500))
    assert image.size == fitted_image_size(landscape_image, image_bounding_box(1500))
