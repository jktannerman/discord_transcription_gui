"""Per-box undo/redo history for the review screen's text boxes.

The history is a stack of full-text snapshots kept outside the Tk widget, so
a row can be torn down and rebuilt without losing it: rebuilding a box just
inserts its current text. The widgets themselves are created with Tk's own
undo turned off (undo=False). History is kept in memory only; it is never
saved with the session.

Consecutive edits merge into one undo step until one of these starts a new
step:

- a pause longer than PAUSE_SECONDS since the previous edit;
- switching between inserting and deleting;
- an edit that isn't next to the previous one (the cursor moved);
- typing a word character right after a non-word character, so each step
  is roughly one word plus the space or punctuation after it;
- an edit that inserts or removes more than one character at once, or
  replaces text (paste, cut, Ctrl+Backspace, typing over a selection) -
  always a step of its own;
- a standalone edit recorded by the caller (a checkbox toggle), or an
  undo/redo.

No Tk here, so this is unit-testable without a display.
"""

from collections import deque
from typing import Deque, List, Optional, Tuple

# A gap between two edits longer than this starts a new undo step.
PAUSE_SECONDS = 1.0

# Oldest steps are dropped beyond this many per box.
MAX_UNDO_STEPS = 200

_INSERT = "insert"
_DELETE = "delete"


def diff_span(old: str, new: str) -> Tuple[int, str, str]:
    """Find the single contiguous region where two texts differ.

    Args:
        old: The text before the change.
        new: The text after the change.

    Returns:
        (start, removed, inserted): the offset where the texts start to
        differ, the part of `old` that was replaced, and what replaced it.
        Both are "" for identical texts.
    """
    limit = min(len(old), len(new))
    start = 0
    while start < limit and old[start] == new[start]:
        start += 1
    suffix = 0
    while suffix < limit - start and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    return start, old[start:len(old) - suffix], new[start:len(new) - suffix]


def cursor_after_change(old: str, new: str) -> int:
    """Character offset in `new` just past the region that changed from `old`.

    Used to place the cursor after an undo/redo, where an editor normally
    puts it: at the change, not wherever it happened to be before.

    Args:
        old: The text before the undo/redo.
        new: The text after it.

    Returns:
        A character offset into `new`.
    """
    start, _, inserted = diff_span(old, new)
    return start + len(inserted)


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


class EditHistory:
    """Undo/redo stacks for one text box, plus the state of the open step.

    Each undo-stack entry is the box's full text as it was before one undo
    step began; the current text itself lives with the caller (see
    SlotState), which passes it in to undo()/redo().
    """

    def __init__(self) -> None:
        self._undo: Deque[str] = deque(maxlen=MAX_UNDO_STEPS)
        self._redo: List[str] = []
        # Kind of the step still open for merging (_INSERT/_DELETE), or
        # None when the next edit must start a new step.
        self._open_kind: Optional[str] = None
        # Offset where the next edit must happen to continue the open step:
        # the end of the last insert, or the start of the last delete.
        self._last_pos = 0
        self._last_char = ""
        self._last_time = 0.0

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_depth(self) -> int:
        return len(self._undo)

    @property
    def redo_depth(self) -> int:
        return len(self._redo)

    def record(self, old: str, new: str, now: float, standalone: bool = False) -> None:
        """Record one change to the box's text.

        Args:
            old: The box's text before the change.
            new: The box's text after it.
            now: The current time in seconds (time.monotonic()), for the
                pause rule.
            standalone: True to make this change its own undo step,
                closed to further merging (e.g. a checkbox toggle).
        """
        if old == new:
            return
        start, removed, inserted = diff_span(old, new)
        if standalone or (removed and inserted) or len(removed) > 1 or len(inserted) > 1:
            kind = None
        elif inserted:
            kind = _INSERT
        else:
            kind = _DELETE

        if not self._continues_open_step(kind, start, inserted, now):
            self._undo.append(old)
        self._redo.clear()

        self._open_kind = kind
        if kind == _INSERT:
            self._last_pos = start + 1
            self._last_char = inserted
        elif kind == _DELETE:
            self._last_pos = start
        self._last_time = now

    def _continues_open_step(self, kind: Optional[str], start: int, inserted: str, now: float) -> bool:
        if kind is None or kind != self._open_kind:
            return False
        if now - self._last_time > PAUSE_SECONDS:
            return False
        if kind == _INSERT:
            if start != self._last_pos:
                return False
            return not (_is_word_char(inserted) and not _is_word_char(self._last_char))
        # Backspace deletes just before the last deletion; Delete deletes
        # at the same offset.
        return start in (self._last_pos - 1, self._last_pos)

    def break_step(self) -> None:
        """Close the open step, so the next edit starts a new one."""
        self._open_kind = None

    def undo(self, current: str) -> Optional[str]:
        """Step back one undo step.

        Args:
            current: The box's text right now.

        Returns:
            The text to show instead, or None if there is nothing to undo.
        """
        if not self._undo:
            return None
        self._redo.append(current)
        self._open_kind = None
        return self._undo.pop()

    def redo(self, current: str) -> Optional[str]:
        """Re-apply the most recently undone step.

        Args:
            current: The box's text right now.

        Returns:
            The text to show instead, or None if there is nothing to redo.
        """
        if not self._redo:
            return None
        self._undo.append(current)
        self._open_kind = None
        return self._redo.pop()
