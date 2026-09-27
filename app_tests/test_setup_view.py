"""SetupFrame._add_known_user - appending a known user must never join it
onto an existing last line that lacks a trailing newline."""
from types import SimpleNamespace

import pytest
import tkinter as tk

from discord_transcription.gui.setup_view import SetupFrame

# Builds a real (withdrawn) Tk root - see pyproject.toml's `gui` marker.
pytestmark = pytest.mark.gui


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available for Tk: {exc}")
    root.withdraw()
    yield root
    root.destroy()


def _add(root: tk.Tk, existing: str, pick: str) -> str:
    text = tk.Text(root)
    text.insert("1.0", existing)
    stub = SimpleNamespace(
        _known_user_pick=tk.StringVar(root, value=pick), _approved_users_text=text
    )
    SetupFrame._add_known_user(stub)
    return text.get("1.0", "end-1c")


@pytest.mark.parametrize(
    "existing, expected",
    [
        ("", "2 - Bob\n"),
        ("1 - Alice\n", "1 - Alice\n2 - Bob\n"),
        ("1 - Alice", "1 - Alice\n2 - Bob\n"),
    ],
)
def test_add_known_user_starts_on_its_own_line(root, existing, expected):
    assert _add(root, existing, "2 - Bob") == expected
