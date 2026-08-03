"""Right-click image context menu (Open Image in Browser / Open Image
Location / Copy Image) - image_context_menu.py.

Split into two groups the same way test_image_loading.py is:

- Action logic (_run_image_menu_action's success/failure logging,
  _open_image_in_browser/_open_image_location/_copy_image_to_clipboard's
  actual subprocess/webbrowser/clipboard calls) needs no real Tk widget at
  all - tested via a bare mixin instance the same way test_row_building.py
  stubs RowBuildingMixin.
- The menu itself (posting, and - the actual scope of the "clicks do
  nothing" bug this feature is deliberately built to avoid, see the
  module's docstring - scroll freeze/unfreeze around it) needs a real
  ReviewFrame and a real Tk root, so those are marked "gui" and excluded by
  default (see pyproject.toml's addopts).
"""
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import tkinter as tk
from PIL import Image

from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.gui.image_context_menu import ImageContextMenuMixin
from gui_transcription.app.gui.review_view import ReviewFrame
from gui_transcription.app.review_item import build_review_items


# -- _run_image_menu_action's success/failure logging ------------------------


class _MenuActionStub(ImageContextMenuMixin):
    pass


def test_run_image_menu_action_logs_success_when_func_does_not_raise():
    stub = _MenuActionStub()
    calls = []
    with patch("gui_transcription.app.gui.image_context_menu.logger") as mock_logger:
        stub._run_image_menu_action("open_in_browser", "fake.png", calls.append)
    assert calls == ["fake.png"]
    logged_events = [c.args[0] for c in mock_logger.info.call_args_list]
    assert "image context menu action clicked" in logged_events
    assert "image context menu action succeeded" in logged_events
    mock_logger.exception.assert_not_called()


def test_run_image_menu_action_logs_failure_when_func_raises():
    stub = _MenuActionStub()

    def _boom(path):
        raise OSError("no")

    with patch("gui_transcription.app.gui.image_context_menu.logger") as mock_logger:
        stub._run_image_menu_action("copy_image", "fake.png", _boom)
    logged_events = [c.args[0] for c in mock_logger.info.call_args_list]
    assert "image context menu action clicked" in logged_events
    assert "image context menu action succeeded" not in logged_events
    mock_logger.exception.assert_called_once()


# -- the three actions themselves --------------------------------------------


def test_open_image_in_browser_opens_a_file_uri(tmp_path):
    stub = _MenuActionStub()
    image_path = tmp_path / "shot.png"
    Image.new("RGB", (10, 10), color="blue").save(image_path)
    with patch("gui_transcription.app.gui.image_context_menu.webbrowser") as mock_webbrowser:
        stub._open_image_in_browser(image_path)
    (uri,), _ = mock_webbrowser.open.call_args
    assert uri.startswith("file:")
    assert image_path.name in uri


def test_open_image_location_selects_the_file_in_explorer(tmp_path):
    stub = _MenuActionStub()
    image_path = tmp_path / "shot.png"
    Image.new("RGB", (10, 10), color="blue").save(image_path)
    with patch("gui_transcription.app.gui.image_context_menu.subprocess") as mock_subprocess:
        stub._open_image_location(image_path)
    args, _ = mock_subprocess.run.call_args
    command = args[0]
    assert command[0] == "explorer"
    assert command[1] == "/select,"
    assert str(image_path.resolve()) in command[2]


def test_copy_image_to_clipboard_writes_cf_dib_via_win32clipboard(tmp_path):
    stub = _MenuActionStub()
    image_path = tmp_path / "shot.png"
    Image.new("RGB", (10, 10), color="blue").save(image_path)

    mock_win32clipboard = MagicMock()
    mock_win32clipboard.CF_DIB = "CF_DIB"
    with patch.dict(sys.modules, {"win32clipboard": mock_win32clipboard}):
        stub._copy_image_to_clipboard(image_path)

    mock_win32clipboard.OpenClipboard.assert_called_once()
    mock_win32clipboard.EmptyClipboard.assert_called_once()
    (fmt, data), _ = mock_win32clipboard.SetClipboardData.call_args
    assert fmt == "CF_DIB"
    assert isinstance(data, bytes)
    assert len(data) > 0
    mock_win32clipboard.CloseClipboard.assert_called_once()


def test_copy_image_to_clipboard_closes_clipboard_even_if_set_data_fails(tmp_path):
    stub = _MenuActionStub()
    image_path = tmp_path / "shot.png"
    Image.new("RGB", (10, 10), color="blue").save(image_path)

    mock_win32clipboard = MagicMock()
    mock_win32clipboard.SetClipboardData.side_effect = RuntimeError("clipboard busy")
    with patch.dict(sys.modules, {"win32clipboard": mock_win32clipboard}):
        with pytest.raises(RuntimeError):
            stub._copy_image_to_clipboard(image_path)

    mock_win32clipboard.CloseClipboard.assert_called_once()


# -- the real popup menu + scroll freeze (needs a real Tk root/ReviewFrame) --


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    r.geometry("900x700")
    yield r
    # A test that opens the real popup menu (tk_popup) and fails an assert
    # before reaching its own cleanup would otherwise leave that menu's
    # native grab active - destroying a window while something else still
    # holds the grab hangs on Windows rather than raising, which is exactly
    # the "window doesn't disappear until forcibly closed" failure mode this
    # guards against. Releasing any grab and unposting any leftover Menu
    # widget here is a backstop for that, on top of (not instead of) each
    # test's own try/finally cleanup below.
    try:
        r.grab_release()
    except tk.TclError:
        pass
    for widget in r.winfo_children():
        if isinstance(widget, tk.Menu):
            try:
                widget.unpost()
            except tk.TclError:
                pass
    r.update()
    r.destroy()


@pytest.fixture
def sample_image(tmp_path):
    path = tmp_path / "sample.png"
    Image.new("RGB", (800, 400), color="blue").save(path)
    return path


def _frame_with_one_image_row(root, sample_image):
    entries = [MessageEntry(message_id="1", text_lines=[], image_names=["sample.png"])]
    file_info = {"sample.png": ["ocr text"]}
    items = build_review_items(entries, file_info, image_folder=sample_image.parent)
    finalized = []
    frame = ReviewFrame(root, items, finalized.append)
    frame.pack(fill="both", expand=True)
    for _ in range(20):
        root.update()
        if frame._materialized_range is not None:
            break
    return frame


@pytest.mark.gui
def test_right_click_opens_menu_and_freezes_scrolling(root, sample_image):
    """tk.Menu.tk_popup() is mocked out here rather than actually called -
    on Windows it's backed by the native TrackPopupMenu API, which blocks
    the calling thread in its own message loop until a real person
    dismisses the menu (click, click-away, or Escape). Calling the real
    thing from an unattended test hangs indefinitely - confirmed by hand
    (the test window stayed open, with the real popup menu visible, until
    force-closed) rather than by inference - so this test asserts only
    what _show_image_context_menu itself controls: that the freeze is
    already in effect by the time it would hand off to that blocking call,
    without ever making the call for real."""
    frame = _frame_with_one_image_row(root, sample_image)
    fake_event = SimpleNamespace(x_root=root.winfo_rootx() + 50, y_root=root.winfo_rooty() + 50)

    frozen_when_popup_would_run = []

    def _fake_tk_popup(menu_self, x, y, entry=""):
        frozen_when_popup_would_run.append(frame._scroll_frozen)
        scrollbar_state = frame._scrollbar.state()
        frozen_when_popup_would_run.append("disabled" in scrollbar_state)

    assert frame._scroll_frozen is False
    with patch.object(tk.Menu, "tk_popup", _fake_tk_popup):
        frame._show_image_context_menu(fake_event, sample_image)

    assert frozen_when_popup_would_run == [True, True]


@pytest.mark.gui
def test_menu_closing_unfreezes_scrolling(root, sample_image):
    """Unfreezing happens in _show_image_context_menu's own `finally`, right
    after tk_popup returns - not via a menu.bind("<Unmap>", ...) callback,
    which an earlier version of this feature relied on and which turned out
    to never fire for a real popup menu on Windows (native TrackPopupMenu
    popups aren't Tk-managed windows, so Tk never sees them unmap) - a real,
    reported bug: right-clicking an image, then dismissing the menu with
    Escape or a click elsewhere (anything other than clicking one of its
    three commands), left scrolling frozen forever. tk_popup is mocked out
    here the same way the previous test does (see its docstring for why the
    real, blocking call can't be used in an unattended test), but the
    `finally` this test is actually checking runs for real, right after the
    mocked call returns - same as it would after a real one."""
    frame = _frame_with_one_image_row(root, sample_image)
    fake_event = SimpleNamespace(x_root=root.winfo_rootx() + 50, y_root=root.winfo_rooty() + 50)

    with patch.object(tk.Menu, "tk_popup", lambda menu_self, x, y, entry="": None):
        frame._show_image_context_menu(fake_event, sample_image)

    assert frame._scroll_frozen is False
    assert "disabled" not in frame._scrollbar.state()


@pytest.mark.gui
def test_frozen_mousewheel_and_page_keys_do_not_scroll(root, sample_image):
    frame = _frame_with_one_image_row(root, sample_image)
    frame._scroll_frozen = True
    top_before, _ = frame._canvas.yview()

    frame._on_page_down()
    root.update()
    top_after_page, _ = frame._canvas.yview()
    assert top_after_page == top_before

    frame._canvas.event_generate("<MouseWheel>", delta=-120, warp=False)
    root.update()
    top_after_wheel, _ = frame._canvas.yview()
    assert top_after_wheel == top_before
