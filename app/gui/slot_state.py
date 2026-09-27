"""The review screen's per-box model, kept separate from its widgets.

One SlotState exists for every editable box in the transcript, keyed by
(item_index, role) like everything else on the review screen, from the
moment the screen is built - whether or not that box's row has ever been
materialized as widgets. It is the source of truth for the box's text:
widgets come and go as rows scroll in and out of range (see review_view.py),
and a rebuilt box is simply filled from here.

No Tk here, so this is unit-testable without a display.
"""

from dataclasses import dataclass, field
from typing import Optional

from .edit_history import EditHistory


@dataclass
class SlotState:
    """Everything the review screen knows about one editable box.

    Attributes:
        default: The box's default text (the message text, the corrected
            OCR text, or the default spacer tokens). An edit equal to this
            isn't reported as an edit.
        text: The box's current text - what it shows, or would show if its
            row were built. Kept in step with the live widget on every
            change (see ReviewFrame._sync_slot_from_widget).
        history: Undo/redo history, in memory only.
        cursor: Tk index of the cursor, restored when the box is rebuilt.
            Not saved with the session.
        checked: For an "ocr" box, whether its checkbox is ticked (the
            box shows the user's edit rather than the OCR default). Always
            False for other roles.
        user_edit: For an "ocr" box, the last text the user edited it to,
            kept while the box is unchecked so re-checking can bring it
            back. None if there never was one.
    """

    default: str
    text: str
    history: EditHistory = field(default_factory=EditHistory)
    cursor: str = "1.0"
    checked: bool = False
    user_edit: Optional[str] = None
