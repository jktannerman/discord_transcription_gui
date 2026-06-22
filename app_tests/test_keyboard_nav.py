from pathlib import Path

from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.gui.keyboard_nav import KeyboardNavMixin
from gui_transcription.app.pipeline import ReviewItem


class _NavStub(KeyboardNavMixin):
    """Exercises _find_text_index without building any Tk widgets - it only
    reads self._items, so a bare stand-in object is enough."""

    def __init__(self, items):
        self._items = items


def _text_item():
    return ReviewItem(entry=MessageEntry(text_lines=["hi"], image_name=None), image_path=None, initial_text=None)


def _image_item():
    return ReviewItem(
        entry=MessageEntry(text_lines=[], image_name="card.png"),
        image_path=Path("card.png"),
        initial_text="",
    )


def test_find_text_index_returns_start_if_it_has_an_image():
    nav = _NavStub([_text_item(), _image_item(), _text_item()])
    assert nav._find_text_index(1, step=1) == 1


def test_find_text_index_skips_text_only_items_searching_forward():
    nav = _NavStub([_text_item(), _text_item(), _image_item(), _text_item()])
    assert nav._find_text_index(0, step=1) == 2


def test_find_text_index_skips_text_only_items_searching_backward():
    nav = _NavStub([_text_item(), _image_item(), _text_item(), _text_item()])
    assert nav._find_text_index(3, step=-1) == 1


def test_find_text_index_returns_none_when_no_image_item_in_range():
    nav = _NavStub([_text_item(), _text_item(), _text_item()])
    assert nav._find_text_index(0, step=1) is None


def test_find_text_index_returns_none_past_either_end():
    nav = _NavStub([_image_item()])
    assert nav._find_text_index(1, step=1) is None
    assert nav._find_text_index(-1, step=-1) is None
