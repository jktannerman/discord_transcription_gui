"""Row/text-box construction for the review screen.

Mixed into ReviewFrame rather than taken as a standalone object, since
every method here reaches into ReviewFrame's bookkeeping dicts
(self._row_frames, self._text_widgets, self._text_containers,
self._saved_texts, self._images, self._canvas) and keyboard_nav.py's
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
from tkinter import ttk
from typing import Optional, Tuple

from .. import logging_config
from ..review_item import ReviewItem
from . import theme
from .image_loading import THUMBNAIL_SIZE, fitted_image_size
from .layout_constants import (
    GAP_BETWEEN_STACKED_PX,
    ROW_FRAME_BORDERWIDTH_PX,
    ROW_FRAME_PADDING_PX,
    ROW_PACK_PADY_PX,
    SPACER_BOX_HEIGHT_PX,
    TEXT_BOX_MARGIN_PX,
)
from .text_undo import UndoLog, attach_undo_recording, replay_onto

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
        pack_kwargs = {"fill": "x", "pady": ROW_PACK_PADY_PX, "padx": 4}
        if before is not None:
            pack_kwargs["before"] = before

        row = ttk.Frame(
            self._scroll_frame, relief="groove",
            borderwidth=ROW_FRAME_BORDERWIDTH_PX, padding=ROW_FRAME_PADDING_PX,
        )
        row.pack(**pack_kwargs)
        self._row_frames[index] = row

        left = ttk.Frame(row)
        left.pack(side="left", padx=6, fill="y")
        right = ttk.Frame(row)
        right.pack(side="left", fill="x", expand=True, padx=6)

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
                self._build_editable_text_box(
                    right, index, "message", item.initial_message_text, message_h, pady_bottom=gap,
                )
            elif role.startswith("ocr"):
                image_index = int(role[len("ocr"):])
                image_h = self._build_image_placeholder(
                    left, item.image_paths[image_index], index, image_index, pady_bottom=gap,
                )
                self._build_editable_text_box(
                    right, index, role, item.initial_ocr_texts[image_index], image_h, pady_bottom=gap,
                )
            else:
                self._build_spacer_text_box(
                    right, index, role, item.initial_spacer_texts[role], pady_bottom=gap,
                )

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
        measured with pack_propagate left on (sizing naturally around the
        label, per its wraplength) before being pinned to a fixed
        width/height - same end state as the image case
        (_build_image_placeholder), just measured rather than computed
        upfront from a cheap header read. This measure-once-at-build-time
        step is unrelated to (and much cheaper than) the per-keystroke
        remeasuring removed from its paired editable box (see
        _fixed_text_box_height): the label is never edited, so there's
        nothing to remeasure here after the initial build."""
        preview = "\n".join(item.entry.text_lines).strip() or "(no text)"
        container = ttk.Frame(parent)
        container.pack(pady=(0, pady_bottom))
        ttk.Label(
            container, text=preview, wraplength=THUMBNAIL_SIZE[0], justify="left",
            font=(theme.TEXT_FONT_FAMILY, theme.TEXT_FONT_SIZE),
        ).pack(anchor="w", fill="x")
        container.update_idletasks()
        floor_px = max(container.winfo_reqheight(), 1)
        container.configure(width=THUMBNAIL_SIZE[0], height=floor_px)
        container.pack_propagate(False)
        return floor_px

    def _build_image_placeholder(
        self, parent: tk.Widget, image_path, index: int, image_index: int, pady_bottom: int = 0,
    ) -> int:
        """Build the fixed-size image placeholder (actual pixels loaded
        lazily on scroll - see image_loading.py) for one of this row's
        images and register it with self._images, keyed by (index,
        image_index) since a row can now have more than one. Returns the
        image's on-screen height in px, used as its paired editable OCR
        box's height floor.

        Width is the global THUMBNAIL_SIZE[0] constant, same for every row,
        so images/text boxes still line up into two neat columns - only
        height is sized per image (to its actual aspect-preserving fit
        height, not the full bounding box) since most images here are
        landscape, and a box-shaped placeholder would letterbox them with
        large empty bands above/below the real photo. Fixed size (rather
        than left to the real loaded photo's size) so loading/unloading the
        image on scroll doesn't change the row's layout (which would jump
        the scroll position)."""
        _, image_h = fitted_image_size(image_path)
        container = ttk.Frame(parent, width=THUMBNAIL_SIZE[0], height=image_h)
        container.pack_propagate(False)
        container.pack(pady=(0, pady_bottom))
        image_label = ttk.Label(container, text="(scroll to load image)", anchor="center")
        image_label.pack(fill="both", expand=True)
        self._images.register(index, image_index, image_path, image_label)
        return image_h

    def _build_editable_text_box(
        self,
        parent: tk.Widget,
        index: int,
        role: str,
        initial_text: str,
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
        if key in self._text_widgets:
            # Should be impossible - _sync_materialized_rows only builds an
            # index that isn't already in self._row_frames - but if it ever
            # happens, the old widget's content (anything typed into it
            # since its last teardown) is about to be silently orphaned:
            # _destroy_row never runs for it, so it's never captured into
            # self._saved_texts. Logged loudly rather than just overwriting
            # self._text_widgets[key] without a trace.
            logger.warning(
                "building a box for a key that already has a live widget - "
                "the old widget's content is about to be orphaned",
                extra=logging_config.extra(key=key, old_widget=str(self._text_widgets[key])),
            )
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
            text_container, wrap="word", relief="flat", undo=True,
            font=(theme.TEXT_FONT_FAMILY, theme.TEXT_FONT_SIZE),
            bg=theme.DARK_TEXT_BG, fg=theme.DARK_FG, insertbackground=theme.DARK_INSERT,
            selectbackground=theme.DARK_ACCENT, selectforeground="white",
            highlightthickness=1, highlightbackground=theme.DARK_BG_ALT,
            highlightcolor=theme.DARK_FOCUS_HIGHLIGHT,
            padx=TEXT_BOX_INNER_PADX, pady=4,
        )
        # Set after construction (rather than passed as a kwarg) since the
        # callback needs to close over text_widget itself.
        text_widget.configure(
            yscrollcommand=lambda first, last, sb=scrollbar, t=text_widget: self._set_text_scrollbar(
                sb, t, first, last
            )
        )
        scrollbar.configure(command=text_widget.yview)
        # Scrollbar itself is packed/unpacked on demand by _set_text_scrollbar,
        # only while content actually overflows the box.
        text_widget.pack(side="left", fill="both", expand=True)

        self._populate_text_box(key, text_widget, initial_text)
        self._text_widgets[key] = text_widget
        self._text_containers[key] = text_container

    def _build_spacer_text_box(
        self,
        parent: tk.Widget,
        index: int,
        role: str,
        initial_text: str,
        pady_bottom: int = 0,
    ) -> None:
        """Build one spacer slot's text box - role is "spacer_msg_img"
        (between a message's text and its first image), "spacer_img{N}"
        (between the Nth and (N+1)th image on the same message), or
        "spacer_end" (the gap before the next message) - see
        review_item.ReviewItem.slot_roles. Unlike _build_editable_text_box,
        there is no left-column counterpart to pair against: the box is fixed at
        exactly one Tk text line tall (SPACER_BOX_HEIGHT_PX, via `height=1`
        on the real Text widget) regardless of content, with no internal
        scrollbar - it's meant to hold only a handful of literal "\\n"
        tokens, not wrapped prose. Registered in the same (index, role)-keyed
        bookkeeping dicts as a content box, so it's just as reachable by
        Tab/Shift-Tab and just as covered by row-teardown/resume edit
        persistence."""
        key = (index, role)
        if key in self._text_widgets:
            logger.warning(
                "building a box for a key that already has a live widget - "
                "the old widget's content is about to be orphaned",
                extra=logging_config.extra(key=key, old_widget=str(self._text_widgets[key])),
            )
        text_container = ttk.Frame(parent, height=SPACER_BOX_HEIGHT_PX)
        text_container.pack(side="top", fill="x", pady=(0, pady_bottom))
        text_container.pack_propagate(False)

        text_widget = tk.Text(
            text_container, height=1, wrap="none", relief="flat", undo=True,
            font=(theme.TEXT_FONT_FAMILY, theme.TEXT_FONT_SIZE),
            bg=theme.DARK_TEXT_BG, fg=theme.DARK_FG, insertbackground=theme.DARK_INSERT,
            selectbackground=theme.DARK_ACCENT, selectforeground="white",
            highlightthickness=1, highlightbackground=theme.DARK_BG_ALT,
            highlightcolor=theme.DARK_FOCUS_HIGHLIGHT,
            padx=TEXT_BOX_INNER_PADX, pady=4,
        )
        text_widget.pack(side="left", fill="both", expand=True)

        self._populate_text_box(key, text_widget, initial_text)
        self._text_widgets[key] = text_widget
        self._text_containers[key] = text_container

    def _populate_text_box(self, key: Tuple[int, str], text_widget: tk.Text, initial_text: str) -> None:
        """Insert a box's starting text and undo/redo history, and wire up
        the keyboard/undo/modified bindings shared by every editable box,
        content or spacer alike.

        self._undo_logs being empty for `key` means this box has never
        been built before this session - the common case, and also what a
        resumed session looks like, since undo history isn't persisted to
        disk (see text_undo.py) - so it's seeded directly from
        self._saved_texts (a saved edit, including one resumed from disk)
        or `initial_text`, with no undo history of its own yet; whichever
        one was used is recorded as log.baseline. Otherwise this box's row
        was torn down and is being rebuilt after being paged back in: the
        widget starts from log.baseline - NOT initial_text directly, see
        UndoLog's docstring for why that distinction is exactly what a
        real data-loss bug turned on - and replays every op recorded
        against it so far, which both reproduces the edited text and
        rebuilds an equivalent native undo/redo stack - see
        text_undo.replay_onto."""
        log = self._undo_logs.get(key)
        if log is None:
            log = UndoLog()
            self._undo_logs[key] = log
            saved = self._saved_texts.get(key)
            source = "saved_texts" if saved is not None else "initial_text"
            text_to_insert = saved if saved is not None else initial_text
            log.baseline = text_to_insert
            text_widget.insert("1.0", text_to_insert)
            text_widget.edit_reset()  # don't let the initial insert be undoable
            self._log_event(
                "box_build_fresh",
                key=key,
                widget=str(text_widget),
                source=source,
                **logging_config.text_fingerprint(text_to_insert),
            )
        else:
            # Replay onto log.baseline (what this box actually started from
            # the first time it was built this session - a resumed edit, or
            # initial_text if there was none) rather than onto initial_text
            # directly - log.ops are deltas relative to whichever baseline
            # was actually used, and re-basing onto initial_text instead
            # would silently discard a resumed edit on this box's very next
            # rebuild whenever there were zero further ops to replay on top
            # of it (see UndoLog's docstring).
            if log.baseline is None:
                # Should be impossible - log.baseline is always set in the
                # branch above, the only place a log is ever created - but
                # inserting "None" itself (str(None)) into the box would be
                # a worse failure than falling back to initial_text, so
                # this degrades instead of corrupting the box's content.
                logger.error(
                    "existing UndoLog has no recorded baseline - falling "
                    "back to initial_text, which may discard a resumed edit",
                    extra=logging_config.extra(key=key),
                )
            baseline = log.baseline if log.baseline is not None else initial_text
            text_widget.insert("1.0", baseline)
            text_widget.edit_reset()  # don't let this insert be undoable either
            self._log_event(
                "box_build_replay_start",
                key=key,
                widget=str(text_widget),
                op_count=len(log.ops),
                **logging_config.text_fingerprint(baseline),
            )
            replay_onto(
                text_widget,
                log,
                on_op=lambda name, args, k=key: self._log_event(
                    "box_replay_op", key=k, op=name, args=repr(args)[:200]
                ),
            )
            result_text = text_widget.get("1.0", "end-1c")
            self._log_event(
                "box_build_replay_done",
                key=key,
                **logging_config.text_fingerprint(result_text),
            )
            # Regression alarm, not a test: if this rebuild landed back on
            # the item's bare default while self._saved_texts disagrees -
            # the box had a different edit recorded as recently as its
            # last teardown - something upstream has silently discarded
            # that edit, the exact failure this method's baseline-tracking
            # exists to prevent. Heuristic (a coincidental match is
            # possible in principle) but cheap and loud, so a future
            # regression of this shape surfaces in app.log immediately
            # instead of requiring the kind of multi-hour forensic
            # reconstruction this bug originally took to diagnose.
            saved = self._saved_texts.get(key)
            if result_text == initial_text and saved is not None and saved != initial_text:
                logger.error(
                    "box rebuilt back to its bare default despite a different "
                    "saved edit on record - possible silent data loss",
                    extra=logging_config.extra(
                        key=key,
                        result=logging_config.text_fingerprint(result_text),
                        saved_texts_on_record=logging_config.text_fingerprint(saved),
                        log_baseline=logging_config.text_fingerprint(log.baseline),
                        op_count=len(log.ops),
                    ),
                )

        # The "insert" mark has right gravity, so inserting at "1.0" (where
        # it already sits on a fresh widget) leaves it at the *end* of the
        # new text rather than the start - then Tab-focusing this box later
        # would put the cursor (and the box's own auto-scroll-to-cursor) at
        # the bottom, with the start of the text scrolled out of view. Reset
        # to wherever the cursor was when this row was last torn down
        # (self._saved_cursor, see _destroy_row), falling back to "1.0" for
        # a box that's never been visited (or whose row was never destroyed
        # while focused) - Tk clamps an index past the end of shorter text
        # rather than raising, so a stale saved index from longer text is
        # harmless.
        text_widget.mark_set("insert", self._saved_cursor.get(key, "1.0"))
        text_widget.see("insert")
        text_widget.edit_modified(False)  # don't count any of the above as a user edit
        self._undo_detach[key] = attach_undo_recording(
            text_widget,
            log,
            on_op=lambda name, args, k=key: self._log_event(
                "box_op_recorded", key=k, op=name, args=repr(args)[:200], total_ops=len(log.ops)
            ),
        )

        text_widget.bind("<Control-BackSpace>", self._delete_word_backward)
        text_widget.bind("<Tab>", self._on_tab)
        text_widget.bind("<Shift-Tab>", self._on_shift_tab)
        text_widget.bind("<Prior>", self._on_page_up)
        text_widget.bind("<Next>", self._on_page_down)
        text_widget.bind("<Control-z>", self._undo_text)
        text_widget.bind("<Control-Z>", self._redo_text)
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
        ARCHITECTURE.md). Fixing height to something knowable upfront - the
        same way an image's height already was, via fitted_image_size's
        cheap header read - removes that correction's reason to exist
        instead of just estimating it more carefully."""
        max_px = self._max_text_box_height_px()
        target_px = paired_height + TEXT_BOX_MARGIN_PX
        return max(1, min(target_px, max_px))

    def _set_text_scrollbar(
        self, scrollbar: ttk.Scrollbar, text_widget: tk.Text, first: str, last: str
    ) -> None:
        """yscrollcommand for an editable text box: show its scrollbar only
        while content actually overflows the box. Most boxes' fixed height
        (see _fixed_text_box_height) comfortably fits their typical content,
        so a permanently-visible empty scrollbar would be pure visual noise
        - this reveals one only once a message is long enough (or the box's
        fixed height short enough) that the content actually overflows."""
        if float(first) <= 0.0 and float(last) >= 1.0:
            scrollbar.pack_forget()
        else:
            # before=text_widget: Tk allocates cavity space to packed slaves
            # in pack-call order, and the text box (packed first, with
            # fill="both"/expand=True) already claims the full cavity by the
            # time this fires - packing the scrollbar in afterwards with no
            # `before` would shrink it to a width-0 sliver, hidden but
            # "mapped", since it'd be last in that order with nothing left
            # to claim.
            scrollbar.pack(side="right", fill="y", before=text_widget)
        scrollbar.set(first, last)

    def _on_text_modified(self, key: Tuple[int, str], text_widget: tk.Text) -> None:
        """Bound to a text box's <<Modified>> event. The box's own height is
        now fixed at build time (see _fixed_text_box_height) and never
        changes as the user types - overflow is handled entirely by the
        box's internal scrollbar (_set_text_scrollbar) - so there's nothing
        left to resize or remeasure here, just the modified-flag reset and
        keeping the edited row on screen.

        Scrolling the row back into view matters because focus alone
        doesn't keep a box on screen: the mouse wheel/scrollbar can move the
        viewport without touching focus at all, and Tk happily keeps
        delivering keystrokes to a focused-but-off-screen widget - typing is
        the easiest visible signal that the user is "at" this box and would
        want to see it, without needing a separate scroll-position watcher
        for an otherwise-rare case. Gated on the widget actually having
        focus, since <<Modified>> also fires for a freshly-built row's own
        initial text insert (see _build_editable_text_box) - that insert's
        own edit_modified(False) reset doesn't suppress it, because Tk
        queues <<Modified>> for the next idle tick rather than firing it
        synchronously, by which point this binding already exists. Without
        this guard, a row built only because it entered the virtualization
        buffer (not because the user scrolled it into view) would yank the
        canvas to reveal it anyway."""
        had_focus = text_widget is self.focus_get()
        self._log_event(
            "box_modified",
            key=key,
            had_focus=had_focus,
            **logging_config.text_fingerprint(text_widget.get("1.0", "end-1c")),
        )
        text_widget.edit_modified(False)
        if had_focus:
            self._scroll_box_into_view(key)
