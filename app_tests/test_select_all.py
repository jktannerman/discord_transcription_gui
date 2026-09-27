"""Ctrl+A selects all in every text field (keyboard_nav.bind_select_all).

Tk's X11 default for Ctrl+A is "move to line start", not select-all.
"""
from types import SimpleNamespace

import pytest
import tkinter as tk
from tkinter import ttk

from discord_transcription.gui import keyboard_nav

# Builds a real (withdrawn) Tk root - see pyproject.toml's `gui` marker.
pytestmark = pytest.mark.gui


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    root.withdraw()
    keyboard_nav.bind_select_all(root)
    yield root
    root.destroy()


@pytest.mark.parametrize("widget_class", ["Text", "Entry", "TEntry", "TCombobox"])
@pytest.mark.parametrize("sequence", ["<Control-a>", "<Control-A>", "<Control-Lock-A>"])
def test_ctrl_a_is_bound_for_every_text_field_class(root, widget_class, sequence):
    # A withdrawn window never gets keyboard focus, so a synthetic key press
    # can't be delivered; check the binding is in place instead.
    assert root.bind_class(widget_class, sequence)


def test_select_all_selects_whole_text_without_moving_cursor(root):
    text = tk.Text(root)
    text.insert("1.0", "line one\nline two")
    text.mark_set("insert", "2.3")

    result = keyboard_nav._select_all(SimpleNamespace(widget=text))

    assert result == "break"
    assert text.get("sel.first", "sel.last") == "line one\nline two\n"
    assert text.index("insert") == "2.3"


def test_select_all_selects_whole_entry(root):
    entry = ttk.Entry(root)
    entry.insert(0, "hello")

    keyboard_nav._select_all(SimpleNamespace(widget=entry))

    assert entry.selection_present()
    assert (entry.index("sel.first"), entry.index("sel.last")) == (0, 5)
