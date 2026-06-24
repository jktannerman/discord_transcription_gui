from pathlib import Path

from gui_transcription.app.chatlog import MessageEntry
from gui_transcription.app.gui.keyboard_nav import KeyboardNavMixin
from gui_transcription.app.pipeline import ReviewItem


class _FakeTextWidget:
    """Stands in for a real tk.Text box in _NavStub below - only
    focus_set/see are ever called on one by the navigation code under
    test, and focus_set just needs to update the owning stub's tracked
    focus so self.focus_get() (also read by the navigation code) reflects
    it, the same relationship a real Tk focus model has."""

    def __init__(self, owner: "_NavStub", key):
        self.owner = owner
        self.key = key

    def focus_set(self) -> None:
        self.owner._focused = self

    def see(self, *_args) -> None:
        pass


class _NavStub(KeyboardNavMixin):
    """Exercises _move_focus/_focused_slot without building any real Tk
    widgets or a display - everything _move_focus touches (self._slots,
    self._text_widgets, self._finalize_button, self.focus_get(),
    self._ensure_materialized) is faked out below; _ensure_materialized and
    _log_event are no-ops since this stub treats every row as already
    materialized. self._text_containers/_row_frames are left empty
    deliberately - the real _scroll_box_into_view (unstubbed) bails out
    early on its container/row None-checks before touching anything else,
    so nothing here needs to fake up real Tk geometry."""

    def __init__(self, items):
        self._items = items
        self._slots = [(idx, role) for idx, item in enumerate(items) for role in item.slot_roles]
        self._slot_positions = {slot: pos for pos, slot in enumerate(self._slots)}
        self._text_widgets = {slot: _FakeTextWidget(self, slot) for slot in self._slots}
        self._text_containers = {}
        self._row_frames = {}
        self._finalize_button = _FakeTextWidget(self, "finalize_button")
        self._focused = None

    def focus_get(self):
        return self._focused

    def _ensure_materialized(self, index):
        pass

    def _log_event(self, *args, **kwargs):
        pass


def _text_item():
    return ReviewItem(
        entry=MessageEntry(message_id="msg-text", text_lines=["hi"], image_names=[]),
        image_paths=[], initial_message_text="hi", initial_ocr_texts=[],
    )


def _image_item():
    return ReviewItem(
        entry=MessageEntry(message_id="msg-image", text_lines=[], image_names=["card.png"]),
        image_paths=[Path("card.png")], initial_message_text=None, initial_ocr_texts=[""],
    )


def _image_with_caption_item():
    return ReviewItem(
        entry=MessageEntry(message_id="msg-image-caption", text_lines=["caption"], image_names=["card.png"]),
        image_paths=[Path("card.png")], initial_message_text="caption", initial_ocr_texts=[""],
    )


def _two_images_item():
    return ReviewItem(
        entry=MessageEntry(message_id="msg-two-images", text_lines=[], image_names=["a.png", "b.png"]),
        image_paths=[Path("a.png"), Path("b.png")], initial_message_text=None,
        initial_ocr_texts=["", ""],
    )


def test_slots_built_message_before_ocr_for_an_item_with_both():
    nav = _NavStub([_image_with_caption_item()])
    assert nav._slots == [(0, "message"), (0, "spacer_msg_img"), (0, "ocr0"), (0, "spacer_end")]


def test_slots_skip_roles_an_item_does_not_have():
    nav = _NavStub([_text_item(), _image_item()])
    assert nav._slots == [
        (0, "message"), (0, "spacer_end"), (1, "ocr0"), (1, "spacer_end"),
    ]


def test_slots_built_one_per_image_in_attachment_order():
    nav = _NavStub([_two_images_item()])
    assert nav._slots == [
        (0, "ocr0"), (0, "spacer_img0"), (0, "ocr1"), (0, "spacer_end"),
    ]


def test_move_focus_forward_from_nothing_focused_lands_on_first_slot():
    nav = _NavStub([_text_item(), _image_item()])
    nav._move_focus(1)
    assert nav.focus_get().key == (0, "message")


def test_move_focus_backward_from_nothing_focused_lands_on_last_slot():
    nav = _NavStub([_text_item(), _image_item()])
    nav._move_focus(-1)
    assert nav.focus_get().key == (1, "spacer_end")


def test_move_focus_forward_advances_through_slots_in_order():
    # Includes the spacer slots: Tab visits them too, per the project
    # owner's decision.
    nav = _NavStub([_image_with_caption_item(), _text_item()])
    nav._move_focus(1)
    assert nav.focus_get().key == (0, "message")
    nav._move_focus(1)
    assert nav.focus_get().key == (0, "spacer_msg_img")
    nav._move_focus(1)
    assert nav.focus_get().key == (0, "ocr0")
    nav._move_focus(1)
    assert nav.focus_get().key == (0, "spacer_end")
    nav._move_focus(1)
    assert nav.focus_get().key == (1, "message")


def test_move_focus_advances_through_every_image_slot_of_a_multi_image_message():
    nav = _NavStub([_two_images_item()])
    nav._move_focus(1)
    assert nav.focus_get().key == (0, "ocr0")
    nav._move_focus(1)
    assert nav.focus_get().key == (0, "spacer_img0")
    nav._move_focus(1)
    assert nav.focus_get().key == (0, "ocr1")


def test_move_focus_forward_off_the_end_focuses_finalize_button():
    nav = _NavStub([_text_item()])
    nav._move_focus(1)
    assert nav.focus_get().key == (0, "message")
    nav._move_focus(1)
    assert nav.focus_get().key == (0, "spacer_end")
    nav._move_focus(1)
    assert nav.focus_get() is nav._finalize_button


def test_move_focus_backward_from_finalize_button_focuses_last_slot():
    nav = _NavStub([_text_item(), _image_item()])
    nav._finalize_button.focus_set()
    nav._move_focus(-1)
    assert nav.focus_get().key == (1, "spacer_end")


def test_move_focus_backward_off_the_start_stays_put():
    nav = _NavStub([_text_item(), _image_item()])
    nav._move_focus(1)  # focus the first (only reachable) slot
    first_widget = nav.focus_get()
    nav._move_focus(-1)
    assert nav.focus_get() is first_widget


def test_move_focus_with_no_slots_at_all_does_not_raise():
    nav = _NavStub([])
    nav._move_focus(1)
    assert nav.focus_get() is None
