"""EditHistory's undo-step grouping and undo/redo stacks (edit_history.py).

Pure Python, no Tk, so these run in the default suite. `type_chars` and
`backspace` feed the history one keystroke at a time, the same way the
review screen records each <<Modified>> event.
"""
import pytest

from discord_transcription.gui.edit_history import (
    MAX_UNDO_STEPS,
    PAUSE_SECONDS,
    EditHistory,
    cursor_after_change,
    diff_span,
)


def type_chars(history, text, chars, now=0.0, step=0.01):
    """Append `chars` to `text` one at a time. Returns (text, now)."""
    for char in chars:
        history.record(text, text + char, now)
        text += char
        now += step
    return text, now


def backspace(history, text, count, now=0.0, step=0.01):
    """Delete `count` characters from the end of `text`, one at a time."""
    for _ in range(count):
        history.record(text, text[:-1], now)
        text = text[:-1]
        now += step
    return text, now


def undo_all(history, text):
    """Every text undo passes through, most recent first."""
    seen = []
    while (previous := history.undo(text)) is not None:
        seen.append(previous)
        text = previous
    return seen


@pytest.mark.parametrize("old, new, expected", [
    ("abc", "abc", (3, "", "")),
    ("abc", "abXc", (2, "", "X")),
    ("abc", "ac", (1, "b", "")),
    ("hello world", "hello there", (6, "world", "there")),
    ("", "new", (0, "", "new")),
])
def test_diff_span(old, new, expected):
    assert diff_span(old, new) == expected


def test_cursor_after_change_lands_just_past_the_change():
    assert cursor_after_change("hello world", "hello") == 5
    assert cursor_after_change("hello", "hello world") == 11
    assert cursor_after_change("abc", "aXYc") == 3


def test_nothing_to_undo_or_redo_on_a_fresh_history():
    history = EditHistory()
    assert history.undo("text") is None
    assert history.redo("text") is None


def test_identical_text_records_nothing():
    history = EditHistory()
    history.record("same", "same", 0.0)
    assert not history.can_undo


def test_typing_groups_by_word_including_the_trailing_space():
    history = EditHistory()
    text, _ = type_chars(history, "", "hello world")
    assert text == "hello world"
    assert undo_all(history, text) == ["hello ", ""]


def test_punctuation_ends_a_word_the_same_way_a_space_does():
    history = EditHistory()
    text, _ = type_chars(history, "", "hi, you")
    assert undo_all(history, text) == ["hi, ", ""]


def test_a_pause_starts_a_new_step_mid_word():
    history = EditHistory()
    text, now = type_chars(history, "", "hel")
    text, _ = type_chars(history, text, "lo", now=now + PAUSE_SECONDS + 0.1)
    assert undo_all(history, text) == ["hel", ""]


def test_a_short_gap_does_not_start_a_new_step():
    history = EditHistory()
    text, now = type_chars(history, "", "hel")
    text, _ = type_chars(history, text, "lo", now=now + PAUSE_SECONDS - 0.1)
    assert undo_all(history, text) == [""]


def test_switching_from_typing_to_deleting_starts_a_new_step():
    history = EditHistory()
    text, now = type_chars(history, "", "abc")
    text, _ = backspace(history, text, 2, now=now)
    assert text == "a"
    assert undo_all(history, text) == ["abc", ""]


def test_consecutive_backspaces_are_one_step():
    history = EditHistory()
    history.record("", "hello world", 0.0)
    text, _ = backspace(history, "hello world", 5, now=10.0)
    assert history.undo(text) == "hello world"


def test_forward_deletes_at_the_same_spot_are_one_step():
    history = EditHistory()
    text = "abcdef"
    for now in (0.0, 0.01, 0.02):
        history.record(text, text[:2] + text[3:], now)
        text = text[:2] + text[3:]
    assert text == "abf"
    assert undo_all(history, text) == ["abcdef"]


def test_typing_somewhere_else_starts_a_new_step():
    history = EditHistory()
    history.record("abc", "abcd", 0.0)
    history.record("abcd", "Xabcd", 0.01)
    assert undo_all(history, "Xabcd") == ["abcd", "abc"]


@pytest.mark.parametrize("old, new", [
    ("start", "start pasted text"),   # paste: several characters at once
    ("select me", "me"),              # cut, or Ctrl+Backspace
    ("hello world", "hello there"),   # typing over a selection
])
def test_multi_character_or_replacing_edits_are_their_own_step(old, new):
    history = EditHistory()
    text, now = type_chars(history, "", old)
    history.record(text, new, now)
    # The following keystroke doesn't merge into it either.
    history.record(new, new + "x", now + 0.01)
    assert undo_all(history, new + "x")[:2] == [new, text]


def test_standalone_edit_is_never_merged_with_its_neighbours():
    history = EditHistory()
    history.record("a", "ab", 0.0)
    history.record("ab", "abc", 0.01, standalone=True)
    history.record("abc", "abcd", 0.02)
    assert undo_all(history, "abcd") == ["abc", "ab", "a"]


def test_redo_reapplies_what_undo_removed():
    history = EditHistory()
    text, _ = type_chars(history, "", "one two")
    first = history.undo(text)
    second = history.undo(first)
    assert (first, second) == ("one ", "")
    assert history.redo(second) == "one "
    assert history.redo("one ") == "one two"
    assert history.redo("one two") is None


def test_a_new_edit_clears_the_redo_stack():
    history = EditHistory()
    text, _ = type_chars(history, "", "abc")
    text = history.undo(text)
    assert history.can_redo
    history.record(text, text + "z", 5.0)
    assert not history.can_redo


def test_typing_after_an_undo_starts_a_new_step():
    history = EditHistory()
    text, now = type_chars(history, "", "one two")
    text = history.undo(text)  # back to "one "
    text, _ = type_chars(history, text, "x", now=now)
    assert history.undo(text) == "one "


def test_history_is_capped_and_drops_the_oldest_steps():
    history = EditHistory()
    text = ""
    for n in range(MAX_UNDO_STEPS + 50):
        history.record(text, text + "ab", float(n))
        text += "ab"
    assert history.undo_depth == MAX_UNDO_STEPS
    oldest = undo_all(history, text)[-1]
    assert oldest == "ab" * 50
