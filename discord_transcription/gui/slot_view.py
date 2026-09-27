"""The live widgets for one editable box on the review screen.

A SlotView exists only while its box's row is materialized (see
review_view.py's virtualization); the box's lasting state is its SlotState
(slot_state.py). Keyed by (item_index, role) in ReviewFrame._slot_views.
"""

import tkinter as tk
from dataclasses import dataclass
from typing import Optional


@dataclass
class SlotView:
    """The widgets currently showing one box.

    Attributes:
        text_widget: The editable Text widget.
        container: The fixed-height frame holding the Text widget (and its
            scrollbar and checkbox column), used for on-screen geometry.
        checkbox_var: The variable behind an "ocr" box's checkbox; None
            for other roles.
        spellcheck_after_id: after() id of a pending debounced spellcheck
            pass, or None. Only ever set for "message"/"ocr" boxes.
    """

    text_widget: tk.Text
    container: tk.Widget
    checkbox_var: Optional[tk.BooleanVar] = None
    spellcheck_after_id: Optional[str] = None

    def cancel_spellcheck(self) -> None:
        """Cancel the pending spellcheck pass, if any, so it can't fire
        against a destroyed widget."""
        if self.spellcheck_after_id is None:
            return
        try:
            self.text_widget.after_cancel(self.spellcheck_after_id)
        except tk.TclError:
            pass
        self.spellcheck_after_id = None
