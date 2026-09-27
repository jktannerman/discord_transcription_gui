"""Row/text-box construction for the review screen.

Mixed into ReviewFrame rather than taken as a standalone object, since
every method here reaches into ReviewFrame's bookkeeping
(self._row_frames, self._slot_views, self._slot_states, self._images,
self._canvas) and keyboard_nav.py's
mixin methods (self._scroll_box_into_view, self._delete_word_backward, etc.)
- threading all of that through as constructor args would just relocate
the coupling, not remove it. This module owns *building* a row's widgets;
review_view.py's ReviewFrame owns deciding *which* rows should exist and
tearing them back down (_reconcile/_sync_materialized_rows/_destroy_row) -
that virtualization core is deliberately kept together in review_view.py
itself (see its module docstring) since it's the part that previously
disagreed with itself across files. Building a single row's widgets is
self-contained enough, by contrast, to live in its own module the same way
image_loading.py and keyboard_nav.py already do.
"""

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk
from typing import Optional, Tuple

from .. import logging_config, spellcheck
from ..review_item import ReviewItem
from . import theme
from .image_loading import fitted_image_size, image_bounding_box
from .layout_constants import (
    COLUMN_PADX_PX,
    GAP_BETWEEN_STACKED_PX,
    ROW_FRAME_BORDERWIDTH_PX,
    ROW_FRAME_PADDING_PX,
    ROW_PACK_PADX_PX,
    ROW_PACK_PADY_PX,
    TEXT_BOX_MARGIN_PX,
)
from .slot_view import SlotView
from .virtualization import TextMetrics
from .wheel import WHEEL_EVENT_SEQUENCES

logger = logging_config.get_logger(__name__)

# Inner horizontal padding for an editable text box's own content (applied
# symmetrically by Tk's Text.padx), so wrapped/long lines don't run right up
# against the box's edge - it previously had none, which read as cramped
# against the right edge in particular since text there is ragged (wrapped
# at arbitrary word boundaries) rather than flush like the left edge.
TEXT_BOX_INNER_PADX = 6

# A maxed-out text box is capped at this fraction of the canvas viewport
# (see RowBuildingMixin._max_text_box_height_px), not the full viewport - a
# box that exactly fills the viewport leaves no margin, so tabbing to it
# rarely lands with it comfortably fully on-screen (Tk's "scroll just enough
# to reveal the target" positioning doesn't line it up pixel-perfectly with
# the viewport edge). Capping below 1.0 leaves room to spare instead.
TEXT_BOX_MAX_HEIGHT_FRACTION = 0.7

# How long to wait, after the most recent keystroke, before actually running
# a spellcheck pass on a box - keeps typing from re-scanning the whole box's
# text on every single character.
SPELLCHECK_DEBOUNCE_MS = 300

# Tag name used to mark a misspelled word's range in a text widget (see
# RowBuildingMixin._run_spellcheck) - never applied to a spacer box, only to
# "message"/"ocr{N}" content boxes.
SPELLCHECK_TAG = "misspelled"


def _make_original_text_label(parent: tk.Widget, text: str, wraplength: int) -> ttk.Label:
    """Create (but don't pack) a row's immutable original-text label.

    Shared by the real row build and measure_text_metrics, so the label
    that's measured is exactly the one that's built.

    Args:
        parent: The widget to create it in.
        text: The label's text.
        wraplength: Width (px) to wrap at - the image column's width.
    """
    return ttk.Label(
        parent, text=text, wraplength=wraplength, justify="left",
        font=(theme.TEXT_FONT_FAMILY, theme.TEXT_FONT_SIZE),
    )


def _make_spacer_text_widget(parent: tk.Widget) -> tk.Text:
    """Create (but don't pack) a spacer slot's one-line text box.

    Shared by the real row build and measure_text_metrics, so the box
    that's measured is exactly the one that's built.
    """
    return tk.Text(
        parent, height=1, wrap="none", relief="flat", undo=False,
        **theme.dark_text_kwargs(),
        padx=TEXT_BOX_INNER_PADX, pady=4,
    )


def measure_text_metrics(parent: tk.Widget) -> TextMetrics:
    """Measure the review screen's text sizes on the running display.

    Builds a throwaway original-text label and spacer box (never packed or
    shown) and reads their requested heights, plus the text font's own
    metrics. These depend on which font Tk actually resolves (Consolas
    isn't installed on most Linux systems, so a substitute is used) and on
    the display's DPI scaling, so hardcoded pixel values can't match them.

    Args:
        parent: Any widget on the review screen's display.

    Returns:
        The measured sizes, for virtualization.estimate_row_height and the
        spacer box's fixed height.
    """
    # Built from family/size rather than tkfont.Font(font=(family, size)):
    # the latter double-applies the display's scaling on some systems and
    # reports a font larger than the one the widgets actually draw with.
    font = tkfont.Font(
        root=parent, family=theme.TEXT_FONT_FAMILY, size=theme.TEXT_FONT_SIZE,
    )
    line_height = font.metrics("linespace")

    label = _make_original_text_label(parent, "x", wraplength=100)
    label_height = label.winfo_reqheight()
    label.destroy()

    spacer = _make_spacer_text_widget(parent)
    spacer_height = spacer.winfo_reqheight()
    spacer.destroy()

    return TextMetrics(
        char_width_px=max(1, font.measure("0")),
        line_height_px=max(1, line_height),
        label_padding_px=max(0, label_height - line_height),
        spacer_box_height_px=max(1, spacer_height),
    )


class RowBuildingMixin:
    def _build_row(self, index: int, before: Optional[tk.Widget] = None) -> tk.Widget:
        """Materialize the row widget(s) for items[index] and register it in
        the window's bookkeeping dicts. If `before` is given, the row is
        inserted immediately above that widget instead of appended at the
        bottom (used when paging in rows above the current window).

        Every row has the same two-column shape, both columns stacked
        text-above-images (mirroring Discord's own layout): an immutable
        left column (the message's own original text, its image(s), or
        both) paired with the matching editable box(es) on the right - a
        copy of the message's own text whenever it has any, and one OCR
        text box per attached image, in attachment order.

        The per-role sizing below (gap placement, each box's height) is
        hand-mirrored by virtualization.estimate_row_height, which can't
        call into this method directly since it has to stay Tk-free to be
        unit-testable - if you change a role's sizing/gap rule here, change
        it there too, or the pre-build estimate drifts from the real
        layout (see that function's docstring)."""
        item = self._items[index]
        pack_kwargs = {"fill": "x", "pady": ROW_PACK_PADY_PX, "padx": ROW_PACK_PADX_PX}
        if before is not None:
            pack_kwargs["before"] = before

        row = ttk.Frame(
            self._scroll_frame, relief="groove",
            borderwidth=ROW_FRAME_BORDERWIDTH_PX, padding=ROW_FRAME_PADDING_PX,
        )
        row.pack(**pack_kwargs)
        self._row_frames[index] = row

        left = ttk.Frame(row)
        left.pack(side="left", padx=COLUMN_PADX_PX, fill="y")
        right = ttk.Frame(row)
        right.pack(side="left", fill="x", expand=True, padx=COLUMN_PADX_PX)

        # One stacked sub-element per slot role (message/ocrN paired with
        # their left-column counterpart, plus a left-column-less spacer
        # role between/after them) - every element but the last gets a gap
        # below it so stacked boxes (and their immutable counterparts)
        # don't touch. See review_item.ReviewItem.slot_roles for the ordering.
        roles = item.slot_roles
        for position, role in enumerate(roles):
            gap = GAP_BETWEEN_STACKED_PX if position < len(roles) - 1 else 0

            if role == "message":
                message_h = self._build_immutable_message_label(left, item, pady_bottom=gap)
                self._build_editable_text_box(right, index, "message", message_h, pady_bottom=gap)
            elif role.startswith("ocr"):
                image_index = int(role[len("ocr"):])
                image_h = self._build_image_placeholder(
                    left, item.image_paths[image_index], index, image_index, pady_bottom=gap,
                )
                self._build_editable_text_box(right, index, role, image_h, pady_bottom=gap)
            else:
                self._build_spacer_text_box(right, index, role, pady_bottom=gap)

        if self._refocus_slot is not None and self._refocus_slot[0] == index:
            slot = self._refocus_slot
            self._refocus_slot = None
            # Deferred to the next idle tick rather than called right here:
            # this can run mid-_reconcile (inside _sync_materialized_rows),
            # and _focus_text_box's scroll-into-view would otherwise get
            # clobbered by _reconcile's own scroll-position correction
            # (_remeasure_built_rows) that still runs after this returns.
            # Guarded on nothing else having taken focus in the meantime
            # (e.g. the user tabbed to a different box, or to the Finalize
            # button, before this row was rebuilt) - NOT on focus_get() being
            # None, since destroying a focused widget makes Tk hand focus to
            # an ancestor frame rather than clearing it outright, so it's
            # never actually None by the time this runs.
            self.after_idle(
                lambda s=slot: self._focus_text_box(*s)
                if self._focused_slot() is None and self.focus_get() is not self._finalize_button
                else None
            )

        return row

    def _build_immutable_message_label(
        self, parent: tk.Widget, item: ReviewItem, pady_bottom: int
    ) -> int:
        """Build the immutable, plain-styled label holding a message's own
        original text (entry.text_lines) - used for a text-only row's
        "original" column, and (stacked above the image) for an image
        row's caption too, now that both are edited the same way. Returns
        the label's measured height in px, used to size its paired editable
        box (see _fixed_text_box_height).

        Font matches the editable text boxes (theme.TEXT_FONT_FAMILY/SIZE)
        rather than the ttk default Label font, for visual consistency
        with the editable copy beside it; everything else about it (e.g.
        background) is left at the ttk default, matching the image
        column's own background, to stay visually distinct from that
        editable copy and from an editable box.

        The container's height isn't known until the label exists, so it's
        taken from the label's own requested height (per its wraplength)
        before being pinned to a fixed width/height - same end state as the image case
        (_build_image_placeholder), just measured rather than computed
        upfront from a cheap header read. This measure-once-at-build-time
        step is unrelated to (and much cheaper than) the per-keystroke
        remeasuring removed from its paired editable box (see
        _fixed_text_box_height): the label is never edited, so there's
        nothing to remeasure here after the initial build."""
        preview = "\n".join(item.entry.text_lines).strip() or "(no text)"
        container = ttk.Frame(parent)
        container.pack(pady=(0, pady_bottom))
        label = _make_original_text_label(container, preview, self._image_column_width_px)
        label.pack(anchor="w", fill="x")
        # The label's requested height is known as soon as it's configured,
        # and the unpadded container sizes to exactly that. Don't flush the
        # idle queue to measure the container instead: that repaints the
        # whole review screen mid-reconcile, showing it half-rebuilt.
        floor_px = max(label.winfo_reqheight(), 1)
        container.configure(width=self._image_column_width_px, height=floor_px)
        container.pack_propagate(False)
        return floor_px

    def _build_image_placeholder(
        self, parent: tk.Widget, image_path: Path, index: int, image_index: int, pady_bottom: int = 0,
    ) -> int:
        """Build the fixed-size image placeholder (actual pixels loaded
        lazily on scroll - see image_loading.py) for one of this row's
        images and register it with self._images, keyed by (index,
        image_index) since a row can now have more than one. Returns the
        image's on-screen height in px, used as its paired editable OCR
        box's height floor.

        Width is the image column's current width
        (self._image_column_width_px), the same for every row,
        so images/text boxes still line up into two neat columns - only
        height is sized per image (to its actual aspect-preserving fit
        height, not the full bounding box) since most images here are
        landscape, and a box-shaped placeholder would letterbox them with
        large empty bands above/below the real photo. Fixed size (rather
        than left to the real loaded photo's size) so loading/unloading the
        image on scroll doesn't change the row's layout (which would jump
        the scroll position)."""
        bounding_box = image_bounding_box(self._image_column_width_px)
        _, image_h = fitted_image_size(image_path, bounding_box)
        container = ttk.Frame(parent, width=self._image_column_width_px, height=image_h)
        container.pack_propagate(False)
        container.pack(pady=(0, pady_bottom))
        image_label = ttk.Label(container, text="(scroll to load image)", anchor="center")
        image_label.pack(fill="both", expand=True)
        self._images.register(index, image_index, image_path, image_label, bounding_box)
        self._bind_image_context_menu(image_label, image_path, self._items[index].message_id)
        return image_h

    def _reclaim_widget_if_present(self, key: Tuple[int, str]) -> None:
        """Tear down a live widget already registered for `key`, if any.

        Should be impossible - _sync_materialized_rows only builds an index
        that isn't already in self._row_frames - but a bug in the
        virtualization core's own bookkeeping could get here anyway (see
        INVESTIGATION_shift_tab_reconcile_lockup.md). Brings the SlotState
        up to date with the old widget first, then destroys its container,
        so nothing typed into it is lost and no widget is leaked.

        Args:
            key: The (item_index, role) about to get a new widget.
        """
        if key not in self._slot_views:
            return
        logger.warning(
            "building a box for a key that already has a live widget - "
            "reclaiming its content before replacing it, rather than "
            "silently orphaning it",
            extra=logging_config.extra(key=key, old_widget=str(self._slot_views[key].text_widget)),
        )
        self._release_slot_view(key).container.destroy()

    def _release_slot_view(self, key: Tuple[int, str]) -> SlotView:
        """Unregister a box's widgets, saving what the SlotState needs first.

        Brings the SlotState up to date with the widget's text and cursor
        and cancels any pending spellcheck. Doesn't destroy anything: the
        caller destroys the row (or container).

        Args:
            key: The box's (item_index, role); must have a SlotView.

        Returns:
            The SlotView that was removed.
        """
        view = self._slot_views.pop(key)
        view.cancel_spellcheck()
        try:
            self._sync_slot_from_widget(key, view.text_widget)
            self._slot_states[key].cursor = view.text_widget.index("insert")
        except tk.TclError:
            logger.error(
                "could not read back this box's widget - whatever it held "
                "since the last sync is lost",
                exc_info=True,
                extra=logging_config.extra(key=key),
            )
        return view

    def _build_editable_text_box(
        self,
        parent: tk.Widget,
        index: int,
        role: str,
        paired_height: int,
        pady_bottom: int = 0,
    ) -> None:
        """Build one editable text box - role is "message" (a copy of the
        message's own text) or "ocr{N}" (the Nth attached image's OCR
        text) - and register it in the window's bookkeeping dicts, keyed
        by (index, role) since a row can now have a message box, any
        number of OCR boxes, or both, stacked text-above-images to match
        the left column (_build_row). paired_height is the on-screen
        height of this box's immutable counterpart in the left column -
        the label's, for a "message" box, or that image's, for an "ocrN"
        box - see _fixed_text_box_height."""
        key = (index, role)
        self._reclaim_widget_if_present(key)
        # Fixed-height container (same pack_propagate(False) trick as the
        # left column's placeholders) so the text box's height is exactly
        # _fixed_text_box_height's verdict, computed once up front, rather
        # than stretching to fill whatever vertical space is left in
        # `parent` or being re-measured from content as the user types (see
        # _fixed_text_box_height's docstring for why the latter was removed).
        text_container = ttk.Frame(parent, height=self._fixed_text_box_height(paired_height))
        text_container.pack(side="top", fill="x", pady=(0, pady_bottom))
        text_container.pack_propagate(False)

        scrollbar = ttk.Scrollbar(text_container, orient="vertical")
        text_widget = tk.Text(
            text_container, wrap="word", relief="flat", undo=False,
            **theme.dark_text_kwargs(),
            padx=TEXT_BOX_INNER_PADX, pady=4,
        )
        self._configure_spellcheck_tag(text_widget)

        # An "ocr" box (one per attached image) gets a checkbox in an
        # otherwise-invisible column at its top-right, tracking "edited vs.
        # not" (see _on_ocr_checkbox_toggle/_on_text_modified) - a "message"
        # box has no such column, since it was never OCR'd in the first
        # place and so has no "original" to revert to. Built (and packed)
        # before text_widget so it's earlier in the container's pack order,
        # claiming a slice off the right edge before text_widget's
        # expand=True claims everything still left - see
        # _set_text_scrollbar for how the scrollbar then has to be inserted
        # even earlier than this column, not just before text_widget, to
        # land at the true right edge outside it.
        checkbox_column: Optional[tk.Widget] = None
        checked_var: Optional[tk.BooleanVar] = None
        if role.startswith("ocr"):
            # ttk.Checkbutton (styled via "OcrCheckbox.TCheckbutton" -
            # theme.py), not a raw tk.Checkbutton - this app's clam ttk
            # theme draws a checked indicator as a cross, matching the
            # setup screen's checkboxes, where a raw tk widget would fall
            # back to Tk's native tick-mark rendering instead. The column
            # frame uses its own matching style ("OcrCheckboxColumn.TFrame",
            # DARK_TEXT_BG) rather than plain "TFrame" (DARK_BG_ALT), so
            # there's no visible seam around the checkbox.
            checkbox_column = ttk.Frame(text_container, style="OcrCheckboxColumn.TFrame")
            checkbox_column.pack(side="right", fill="y")
            checked_var = tk.BooleanVar(value=self._slot_states[key].checked)
            checkbox = ttk.Checkbutton(
                checkbox_column, variable=checked_var, takefocus=0,
                style="OcrCheckbox.TCheckbutton",
                command=lambda k=key: self._on_ocr_checkbox_toggle(k),
            )
            checkbox.pack(side="top")

        # Set after construction (rather than passed as a kwarg) since the
        # callback needs to close over text_widget itself.
        before_widget = checkbox_column if checkbox_column is not None else text_widget
        text_widget.configure(
            yscrollcommand=lambda first, last, sb=scrollbar, t=text_widget, b=before_widget: (
                self._set_text_scrollbar(sb, t, b, first, last)
            )
        )
        scrollbar.configure(command=text_widget.yview)
        # Scrollbar itself is packed/unpacked on demand by _set_text_scrollbar,
        # only while content actually overflows the box.
        text_widget.pack(side="left", fill="both", expand=True)

        self._populate_text_box(key, text_widget)
        self._slot_views[key] = SlotView(text_widget, text_container, checkbox_var=checked_var)
        self._schedule_spellcheck(key)

    def _build_spacer_text_box(
        self,
        parent: tk.Widget,
        index: int,
        role: str,
        pady_bottom: int = 0,
    ) -> None:
        """Build one spacer slot's text box - role is "spacer_msg_img"
        (between a message's text and its first image), "spacer_img{N}"
        (between the Nth and (N+1)th image on the same message), or
        "spacer_end" (the gap before the next message) - see
        review_item.ReviewItem.slot_roles. Unlike _build_editable_text_box,
        there is no left-column counterpart to pair against: the box is fixed at
        exactly one Tk text line tall (the measured
        TextMetrics.spacer_box_height_px, what a `height=1` Text widget
        requests on this display) regardless of content, with no internal
        scrollbar - it's meant to hold only a handful of literal "\\n"
        tokens, not wrapped prose. Registered in the same (index, role)-keyed
        bookkeeping dicts as a content box, so it's just as reachable by
        Tab/Shift-Tab and just as covered by row-teardown/resume edit
        persistence."""
        key = (index, role)
        self._reclaim_widget_if_present(key)
        text_container = ttk.Frame(parent, height=self._text_metrics.spacer_box_height_px)
        text_container.pack(side="top", fill="x", pady=(0, pady_bottom))
        text_container.pack_propagate(False)

        text_widget = _make_spacer_text_widget(text_container)
        text_widget.pack(side="left", fill="both", expand=True)

        self._populate_text_box(key, text_widget)
        self._slot_views[key] = SlotView(text_widget, text_container)

    def _populate_text_box(self, key: Tuple[int, str], text_widget: tk.Text) -> None:
        """Fill a freshly-built box from its SlotState and wire up the
        keyboard/modified bindings shared by every editable box, content or
        spacer alike.

        The SlotState already holds everything that must survive the row
        being torn down (text, cursor, undo history), so a rebuild is the
        same as a first build.

        Args:
            key: The box's (item_index, role).
            text_widget: The new, empty Text widget for it.
        """
        state = self._slot_states[key]
        text_widget.insert("1.0", state.text)
        # The "insert" mark has right gravity, so the insert above left it at
        # the end of the text; put it back where it was when the row was
        # torn down ("1.0" for a box never visited). Tk clamps an index past
        # the end rather than raising.
        text_widget.mark_set("insert", state.cursor)
        text_widget.see("insert")
        text_widget.edit_modified(False)
        self._log_event(
            "box_build",
            key=key,
            widget=str(text_widget),
            **logging_config.text_fingerprint(state.text),
        )

        text_widget.bind("<Control-BackSpace>", self._delete_word_backward)
        text_widget.bind("<FocusIn>", lambda e, k=key: self._note_focused_slot(k), add="+")
        text_widget.bind("<Tab>", self._on_tab)
        # <<PrevWindow>> rather than <Shift-Tab> - see the same binding on
        # the Finalize button in review_view.py for why.
        text_widget.bind("<<PrevWindow>>", self._on_shift_tab)
        # Widget-level, so it runs before - and via "break" replaces - Tk's
        # own Text class wheel binding, which would otherwise scroll the box
        # a second time for the same event.
        for sequence in WHEEL_EVENT_SEQUENCES:
            text_widget.bind(sequence, self._on_mousewheel)
        text_widget.bind("<Prior>", self._on_page_up)
        text_widget.bind("<Next>", self._on_page_down)
        # Both cases go to one handler that checks Shift itself: the
        # keysym's case alone would make Caps Lock swap undo and redo.
        text_widget.bind("<Control-z>", self._on_undo_key)
        text_widget.bind("<Control-Z>", self._on_undo_key)
        text_widget.bind("<Up>", lambda e, k=key: self._on_vertical_arrow(e, k))
        text_widget.bind("<Down>", lambda e, k=key: self._on_vertical_arrow(e, k))
        text_widget.bind(
            "<<Modified>>",
            lambda e, k=key, t=text_widget: self._on_text_modified(k, t),
        )

    def _max_text_box_height_px(self) -> int:
        """Cap an editable text box's height at TEXT_BOX_MAX_HEIGHT_FRACTION
        of the canvas viewport - falling back to the full screen if it
        hasn't been laid out yet - so a very long message scrolls inside
        its box (see _set_text_scrollbar) instead of growing taller than
        what comfortably fits on screen with some margin to spare."""
        viewport = self._canvas.winfo_height()
        if viewport <= 1:
            viewport = self.winfo_screenheight()
        return int(viewport * TEXT_BOX_MAX_HEIGHT_FRACTION)

    def _fixed_text_box_height(self, paired_height: int) -> int:
        """A box's height (px), decided once at build time from a fixed
        rule rather than measured from its actual content: its paired
        immutable element's own on-screen height (the label's, for a
        "message" box; the image's, for an "ocr" box) plus
        TEXT_BOX_MARGIN_PX, capped at _max_text_box_height_px - any overflow
        is handled by the box's own internal scrollbar (_set_text_scrollbar)
        rather than the box growing. Both roles use the same rule (an
        earlier version gave "message" boxes a flat minimum instead, on the
        assumption that most messages here are short - in practice many are
        not, so that assumption is gone and a "message" box now tracks its
        label's real height the same way an "ocr" box already tracked its
        image's).

        This replaces an earlier design (_size_text_container, removed) that
        measured the text's actual wrapped line count and resized the box to
        fit it, re-running on every keystroke (_on_text_modified). That made
        a row's true height unknowable until it was built and typed in -
        which is exactly the gap _remeasure_built_rows existed to correct,
        and the repeated source of this screen's scroll-position bugs (see
        docs/ARCHITECTURE_REVIEW_SCREEN.md). Fixing height to something
        knowable upfront - the same way an image's height already was, via
        fitted_image_size's cheap header read - removes that correction's
        reason to exist instead of just estimating it more carefully."""
        max_px = self._max_text_box_height_px()
        target_px = paired_height + TEXT_BOX_MARGIN_PX
        return max(1, min(target_px, max_px))

    def _set_text_scrollbar(
        self, scrollbar: ttk.Scrollbar, text_widget: tk.Text, before_widget: tk.Widget,
        first: str, last: str,
    ) -> None:
        """yscrollcommand for an editable text box: show its scrollbar only
        while content actually overflows the box. Most boxes' fixed height
        (see _fixed_text_box_height) comfortably fits their typical content,
        so a permanently-visible empty scrollbar would be pure visual noise
        - this reveals one only once a message is long enough (or the box's
        fixed height short enough) that the content actually overflows.

        before_widget is text_widget itself for a box with no checkbox
        column (a "message"/spacer box), or that column's frame for an
        "ocr" box - either way, it's whatever currently sits immediately to
        the scrollbar's left, so inserting the scrollbar immediately before
        it in pack order puts the scrollbar at the true right edge with
        nothing claiming space outside it."""
        if float(first) <= 0.0 and float(last) >= 1.0:
            scrollbar.pack_forget()
        else:
            # before=before_widget: Tk allocates cavity space to packed
            # slaves in pack-call order, and text_widget/the checkbox
            # column (packed first) already claim the full remaining cavity
            # by the time this fires - packing the scrollbar in afterwards
            # with no `before` would shrink it to a width-0 sliver, hidden
            # but "mapped", since it'd be last in that order with nothing
            # left to claim.
            scrollbar.pack(side="right", fill="y", before=before_widget)
        scrollbar.set(first, last)

    def _configure_spellcheck_tag(self, text_widget: tk.Text) -> None:
        """Configure this box's "misspelled" tag - a straight red underline
        (Tk doesn't support wavy/squiggly underlines) via the tag-level
        "underlinefg" option, so the underline is red without recoloring the
        word's own text. That option was only added in Tk 8.6.6 - guarded
        with a fallback to a plain (uncolored) underline for the rare case
        of an older Tk, rather than raising and losing the text box
        entirely over a cosmetic feature."""
        try:
            text_widget.tag_configure(
                SPELLCHECK_TAG, underline=True, underlinefg=theme.SPELLCHECK_UNDERLINE
            )
        except tk.TclError:
            logger.warning(
                "this Tk version doesn't support underlinefg - falling back "
                "to a plain (uncolored) underline for misspelled words",
            )
            text_widget.tag_configure(SPELLCHECK_TAG, underline=True)

    def _schedule_spellcheck(self, key: Tuple[int, str]) -> None:
        """Debounce a spellcheck pass on this box: cancel whatever pass was
        already pending for it and schedule a fresh one SPELLCHECK_DEBOUNCE_MS
        from now. Called on every keystroke (_on_text_modified) as well as
        right after a box is (re)built (_build_editable_text_box) - the
        latter so a rebuilt row's tags (which don't survive the old widget
        being destroyed) are always recomputed rather than left
        blank until the user's next keystroke in that specific box.

        Args:
            key: The box's (item_index, role). Ignored if its row isn't built.
        """
        view = self._slot_views.get(key)
        if view is None:
            return
        view.cancel_spellcheck()
        view.spellcheck_after_id = view.text_widget.after(
            SPELLCHECK_DEBOUNCE_MS,
            lambda k=key, t=view.text_widget: self._run_spellcheck(k, t),
        )

    def _run_spellcheck(self, key: Tuple[int, str], text_widget: tk.Text) -> None:
        """Actually run the spellcheck pass and (re)tag this box's misspelled
        words. Guarded against the widget having been destroyed (its row
        paged out) between this being scheduled and it firing - _destroy_row/
        _reclaim_widget_if_present already cancel any pending timer up front,
        but this is a cheap last-resort backstop rather than relying on that
        alone, the same defensive posture the rest of this module takes
        toward a torn-down widget."""
        view = self._slot_views.get(key)
        if view is not None and view.text_widget is text_widget:
            view.spellcheck_after_id = None
        try:
            content = text_widget.get("1.0", "end-1c")
        except tk.TclError:
            return
        try:
            spans = spellcheck.find_misspelled_spans(content)
            text_widget.tag_remove(SPELLCHECK_TAG, "1.0", "end")
            for start, end in spans:
                text_widget.tag_add(SPELLCHECK_TAG, f"1.0+{start}c", f"1.0+{end}c")
        except tk.TclError:
            logger.warning(
                "spellcheck tagging failed on a box that was torn down mid-pass",
                extra=logging_config.extra(key=key),
            )

    def _sync_slot_from_widget(self, key: Tuple[int, str], text_widget: tk.Text) -> bool:
        """Bring a box's SlotState up to date with its live widget.

        Called on every <<Modified>> event, and before anything reads or
        replaces the box's text, since <<Modified>> arrives on a later idle
        tick than the edit itself. Any difference found here is an edit
        made in the widget (typing, paste, Ctrl+Backspace, a middle-click
        paste, or a test's direct insert): the app's own writes (building
        the box, undo/redo, the OCR checkbox) set SlotState.text first, so
        they never show up as a difference.

        A difference is recorded in the box's undo history, marks the slot
        as touched, and ticks an "ocr" box's checkbox.

        Args:
            key: The box's (item_index, role).
            text_widget: The live widget currently backing that box.

        Returns:
            True if the widget held an edit the SlotState didn't have yet.
        """
        state = self._slot_states[key]
        text = text_widget.get("1.0", "end-1c")
        if text == state.text:
            return False
        state.history.record(state.text, text, self._clock())
        state.text = text
        state.touched = True
        if key[1].startswith("ocr"):
            self._on_ocr_box_user_edit(key, text)
        return True

    def _set_box_text(self, key: Tuple[int, str], text: str, cursor: str = "1.0") -> None:
        """Replace a box's text as the app, not as a user edit.

        Updates the SlotState first, so the <<Modified>> event this write
        causes isn't taken for a user edit, then the widget if the box's
        row is built. Callers record the change in the undo history
        themselves, if it belongs there.

        Args:
            key: The box's (item_index, role).
            text: The new text.
            cursor: Tk index to put the cursor at afterwards.
        """
        state = self._slot_states[key]
        state.text = text
        state.cursor = cursor
        view = self._slot_views.get(key)
        if view is None:
            return
        text_widget = view.text_widget
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", text)
        text_widget.mark_set("insert", cursor)
        text_widget.see("insert")

    def _on_text_modified(self, key: Tuple[int, str], text_widget: tk.Text) -> None:
        """Bound to a text box's <<Modified>> event.

        Records any user edit (see _sync_slot_from_widget), re-runs the
        spellcheck, and - if the box has focus - scrolls it back into view.
        That matters because focus alone doesn't keep a box on screen: the
        mouse wheel/scrollbar can move the viewport without touching focus,
        and Tk keeps delivering keystrokes to a focused-but-off-screen box.
        The focus check also keeps a freshly-built box from yanking the
        canvas to itself: its build-time insert fires this event too, on a
        later idle tick, while nothing has focused it yet.

        Args:
            key: The box's (item_index, role).
            text_widget: The widget the event came from.
        """
        had_focus = text_widget is self.focus_get()
        changed = self._sync_slot_from_widget(key, text_widget)
        self._log_event(
            "box_modified",
            key=key,
            had_focus=had_focus,
            changed=changed,
            **logging_config.text_fingerprint(self._slot_states[key].text),
        )
        text_widget.edit_modified(False)
        # A spacer box never gets the "misspelled" tag configured (see
        # _build_spacer_text_box), and holds nothing but "\n" tokens anyway.
        if not key[1].startswith("spacer"):
            self._schedule_spellcheck(key)
        if had_focus:
            self._scroll_box_into_view(key)

    def _on_ocr_box_user_edit(self, key: Tuple[int, str], text: str) -> None:
        """An "ocr" box was edited by the user, so its checkbox ticks itself.

        This is the "any change checks the box" rule: unlike undo/redo
        (which compare the result to the OCR default), it doesn't untick
        even if the new text happens to match the default again.

        Args:
            key: The box's (item_index, role).
            text: The box's new text.
        """
        state = self._slot_states[key]
        state.checked = True
        state.user_edit = text
        view = self._slot_views.get(key)
        var = view.checkbox_var if view is not None else None
        if var is not None and not var.get():
            var.set(True)

    def _on_ocr_checkbox_toggle(self, key: Tuple[int, str]) -> None:
        """Command callback for an "ocr" box's checkbox.

        By the time this runs, Tk has already flipped the checkbox's
        variable. Unchecking shows the OCR default without discarding the
        edit (kept in SlotState.user_edit); checking shows that edit again,
        or leaves the default in place if there never was one. The swap is
        one undo step of its own.

        Args:
            key: The box's (item_index, role).
        """
        view = self._slot_views[key]
        var = view.checkbox_var
        checked = var.get()
        # Read before syncing: an edit not yet synced would tick the box
        # again via _on_ocr_box_user_edit, undoing the click.
        self._sync_slot_from_widget(key, view.text_widget)
        var.set(checked)
        state = self._slot_states[key]
        state.checked = checked
        state.touched = True
        text_to_show = state.user_edit if checked and state.user_edit is not None else state.default

        state.history.record(state.text, text_to_show, self._clock(), standalone=True)
        self._set_box_text(key, text_to_show)

        self._log_event(
            "ocr_checkbox_toggled",
            key=key,
            checked=checked,
            **logging_config.text_fingerprint(text_to_show),
        )
