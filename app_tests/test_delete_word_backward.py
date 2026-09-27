"""Ctrl+Backspace in a review-screen text box (slot_boxes.delete_word_backward)."""
from types import SimpleNamespace

import pytest
import tkinter as tk

from discord_transcription.gui.slot_boxes import delete_word_backward

# Builds a real (withdrawn) Tk root - see pyproject.toml's `gui` marker.
pytestmark = pytest.mark.gui


@pytest.fixture
def text():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    root.withdraw()
    yield tk.Text(root)
    root.destroy()


def _press(text: tk.Text) -> str:
    return delete_word_backward(SimpleNamespace(widget=text))


def test_deletes_the_word_before_the_cursor(text):
    text.insert("1.0", "hello there world")
    text.mark_set("insert", "1.11")

    assert _press(text) == "break"
    assert text.get("1.0", "end-1c") == "hello  world"


def test_at_line_start_merges_with_the_previous_line(text):
    text.insert("1.0", "one\ntwo")
    text.mark_set("insert", "2.0")

    _press(text)

    assert text.get("1.0", "end-1c") == "onetwo"


def test_with_a_selection_deletes_just_the_selection(text):
    text.insert("1.0", "hello there world")
    text.tag_add("sel", "1.6", "1.11")
    text.mark_set("insert", "1.17")

    assert _press(text) == "break"
    assert text.get("1.0", "end-1c") == "hello  world"
