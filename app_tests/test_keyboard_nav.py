from pathlib import Path
from types import SimpleNamespace

from discord_transcription.chatlog import MessageEntry
from discord_transcription.gui.keyboard_nav import FocusNavigator
from discord_transcription.gui.slot_boxes import SlotBoxes
from discord_transcription.review_item import ReviewItem


class _FakeFocusOwner:
    """Stands in for Tk's focus model: what focus_get() reports."""

    def __init__(self):
        self.focused = None

    def focus_get(self):
        return self.focused


class _FakeTextWidget:
    """Stands in for a real tk.Text box - only focus_set/see are ever
    called on one by the navigation code under test, and focus_set updates
    the fake focus model the navigator reads."""

    def __init__(self, focus_owner: _FakeFocusOwner, key):
        self.focus_owner = focus_owner
        self.key = key

    def focus_set(self) -> None:
        self.focus_owner.focused = self

    def see(self, *_args) -> None:
        pass


class _FakeBoxes:
    """The part of SlotBoxes the navigator uses: built boxes' views."""

    def __init__(self, views):
        self.views = views

    key_for_widget = SlotBoxes.key_for_widget


class _NavStub(FocusNavigator):
    """A FocusNavigator over fake boxes and rows - no real Tk widgets or
    display. Every row counts as already built (ensure_materialized is a
    no-op); each view's container and the rows' row_frames are left empty
    deliberately, so the real scroll_box_into_view bails out on its
    None-checks before touching any geometry."""

    def __init__(self, items):
        focus_owner = _FakeFocusOwner()
        slots = [(idx, role) for idx, item in enumerate(items) for role in item.slot_roles]
        views = {
            slot: SimpleNamespace(text_widget=_FakeTextWidget(focus_owner, slot), container=None)
            for slot in slots
        }
        rows = SimpleNamespace(
            row_frames={},
            ensure_materialized=lambda index: None,
            log_event=lambda *args, **kwargs: None,
        )
        super().__init__(
            focus_owner, slots, _FakeBoxes(views), rows,
            _FakeTextWidget(focus_owner, "finalize_button"), on_view_moved=lambda: None,
        )

    def focus_get(self):
        return self._focus_owner.focus_get()


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
    assert nav.slots == [(0, "message"), (0, "spacer_msg_img"), (0, "ocr0"), (0, "spacer_end")]


def test_slots_skip_roles_an_item_does_not_have():
    nav = _NavStub([_text_item(), _image_item()])
    assert nav.slots == [
        (0, "message"), (0, "spacer_end"), (1, "ocr0"), (1, "spacer_end"),
    ]


def test_slots_built_one_per_image_in_attachment_order():
    nav = _NavStub([_two_images_item()])
    assert nav.slots == [
        (0, "ocr0"), (0, "spacer_img0"), (0, "ocr1"), (0, "spacer_end"),
    ]


def test_move_focus_forward_from_nothing_focused_lands_on_first_slot():
    nav = _NavStub([_text_item(), _image_item()])
    nav.move_focus(1)
    assert nav.focus_get().key == (0, "message")


def test_move_focus_backward_from_nothing_focused_lands_on_last_slot():
    nav = _NavStub([_text_item(), _image_item()])
    nav.move_focus(-1)
    assert nav.focus_get().key == (1, "spacer_end")


def test_move_focus_forward_advances_through_slots_in_order():
    # Includes the spacer slots: Tab visits them too, per the project
    # owner's decision.
    nav = _NavStub([_image_with_caption_item(), _text_item()])
    nav.move_focus(1)
    assert nav.focus_get().key == (0, "message")
    nav.move_focus(1)
    assert nav.focus_get().key == (0, "spacer_msg_img")
    nav.move_focus(1)
    assert nav.focus_get().key == (0, "ocr0")
    nav.move_focus(1)
    assert nav.focus_get().key == (0, "spacer_end")
    nav.move_focus(1)
    assert nav.focus_get().key == (1, "message")


def test_move_focus_advances_through_every_image_slot_of_a_multi_image_message():
    nav = _NavStub([_two_images_item()])
    nav.move_focus(1)
    assert nav.focus_get().key == (0, "ocr0")
    nav.move_focus(1)
    assert nav.focus_get().key == (0, "spacer_img0")
    nav.move_focus(1)
    assert nav.focus_get().key == (0, "ocr1")


def test_move_focus_forward_off_the_end_focuses_finalize_button():
    nav = _NavStub([_text_item()])
    nav.move_focus(1)
    assert nav.focus_get().key == (0, "message")
    nav.move_focus(1)
    assert nav.focus_get().key == (0, "spacer_end")
    nav.move_focus(1)
    assert nav.focus_get() is nav._finalize_button


def test_move_focus_backward_from_finalize_button_focuses_last_slot():
    nav = _NavStub([_text_item(), _image_item()])
    nav._finalize_button.focus_set()
    nav.move_focus(-1)
    assert nav.focus_get().key == (1, "spacer_end")


def test_move_focus_backward_off_the_start_stays_put():
    nav = _NavStub([_text_item(), _image_item()])
    nav.move_focus(1)  # focus the first (only reachable) slot
    first_widget = nav.focus_get()
    nav.move_focus(-1)
    assert nav.focus_get() is first_widget


def test_move_focus_with_no_slots_at_all_does_not_raise():
    nav = _NavStub([])
    nav.move_focus(1)
    assert nav.focus_get() is None


def test_focus_text_box_on_a_slot_with_no_live_widget_does_not_raise():
    """A slot listed in the navigator's slots (built once, up front) but missing from
    the built boxes' views - normally impossible, since goto_slot always calls
    ensure_materialized first, but exactly the gap a failed row build
    leaves (see virtual_rows.py's _try_build_row and
    INVESTIGATION_shift_tab_reconcile_lockup.md). Before this guard, every
    Tab/Shift-Tab press aimed at that slot raised KeyError - repeating on
    every keypress for as long as the user kept trying to navigate there."""
    nav = _NavStub([_text_item(), _image_item()])
    missing_slot = (1, "ocr0")
    del nav._boxes.views[missing_slot]

    nav.focus_text_box(*missing_slot)  # must not raise

    assert nav.focus_get() is None


def test_move_focus_skips_over_nothing_specially_but_does_not_crash_on_a_missing_widget():
    """move_focus itself doesn't need to know about a missing widget - it
    just calls goto_slot, which calls the now-guarded focus_text_box -
    confirms the guard holds through the real Tab/Shift-Tab entry point,
    not just a direct call."""
    nav = _NavStub([_text_item()])
    only_slot = (0, "message")
    del nav._boxes.views[only_slot]

    nav.move_focus(1)  # must not raise KeyError

    assert nav.focus_get() is None


class _UndoKeyStub(SlotBoxes):
    """Records which of undo/redo on_undo_key dispatches to."""

    def __init__(self):
        self.calls = []

    def undo_text(self, event):
        self.calls.append("undo")
        return "break"

    def redo_text(self, event):
        self.calls.append("redo")
        return "break"


def _key_event(state):
    return SimpleNamespace(state=state, widget=None)


SHIFT, CAPS_LOCK, CONTROL = 0x1, 0x2, 0x4


def test_undo_key_dispatch_depends_on_shift_not_caps_lock():
    stub = _UndoKeyStub()
    stub.on_undo_key(_key_event(CONTROL))
    stub.on_undo_key(_key_event(CONTROL | CAPS_LOCK))
    stub.on_undo_key(_key_event(CONTROL | SHIFT))
    stub.on_undo_key(_key_event(CONTROL | SHIFT | CAPS_LOCK))

    assert stub.calls == ["undo", "undo", "redo", "redo"]
