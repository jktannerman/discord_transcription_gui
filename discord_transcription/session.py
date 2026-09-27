"""Review sessions: what's saved while reviewing, so a closed review can be
resumed, and mapping saved or finalized edits back onto a transcript.

A saved session (SavedSession) records the run's inputs plus the review's
state: every box's edit, which boxes the user deliberately acted on, the
focused box, and the scroll position. Boxes are identified by Discord
message ID and role rather than by position, because resuming re-parses
the chatlog, and a re-export may have added or removed messages anywhere.
The match_* functions translate those back onto the freshly parsed items'
current positions, dropping whatever no longer exists - an expected result
of a chatlog growing, so it's logged, not reported as an error.

No Tk here: App (gui/main_window.py) decides when to save and resume.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import logging_config
from .pipeline import RunContext
from .review_item import ReviewItem

logger = logging_config.get_logger(__name__)

# A box on the current screen: (item index, role).
Slot = tuple[int, str]
# A box as saved: (Discord message ID, role).
MessageSlot = tuple[str, str]
# {message_id: {role: text}}; None means "the default text".
EditsByMessage = dict[str, dict[str, Optional[str]]]
# One {role: text} dict per item, in transcript order.
EditsByItem = list[dict[str, Optional[str]]]


class MalformedSessionError(ValueError):
    """A saved session is missing, or has unusable, run inputs."""


def _as_message_slot(value: Any) -> Optional[MessageSlot]:
    """A saved [message_id, role] pair as a tuple, or None if it isn't one."""
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, str) for v in value):
        return value[0], value[1]
    return None


@dataclass
class RestoredReview:
    """A saved session mapped onto a freshly parsed transcript.

    Attributes:
        saved_texts: One role->text dict per item (empty where nothing was
            saved).
        touched_slots: The touched slots that still exist.
        focus_slot: The focused box's current slot, if it still exists.
        scroll_fraction: The saved scroll position, used when there's no
            focus slot.
    """

    saved_texts: EditsByItem
    touched_slots: set[Slot]
    focus_slot: Optional[Slot]
    scroll_fraction: Optional[float]


@dataclass
class SavedSession:
    """An in-progress review, as saved to disk (see state.save_session).

    Attributes:
        run: The run's inputs, which resuming re-runs.
        edited_texts: Edits by message ID; only messages with at least one
            edit are present.
        touched_slots: Boxes the user deliberately acted on (see
            SlotState.touched).
        focus_slot: The box that had focus, if any.
        scroll_fraction: The scroll position (0-1), if known.
    """

    run: RunContext
    edited_texts: EditsByMessage = field(default_factory=dict)
    touched_slots: list[MessageSlot] = field(default_factory=list)
    focus_slot: Optional[MessageSlot] = None
    scroll_fraction: Optional[float] = None

    @classmethod
    def capture(
        cls,
        run: RunContext,
        items: list[ReviewItem],
        edited_texts: EditsByItem,
        touched_slots: Iterable[Slot],
        focus_slot: Optional[Slot],
        scroll_fraction: Optional[float],
    ) -> "SavedSession":
        """Build a session from the review screen's current state.

        Args:
            run: The run's inputs.
            items: The review items, in transcript order.
            edited_texts: One role->text dict per item (see
                ReviewFrame.collect_edited_texts).
            touched_slots: The slots the user deliberately acted on.
            focus_slot: The focused slot, if any.
            scroll_fraction: The scroll position.
        """
        by_message = {
            items[idx].message_id: edited
            for idx, edited in enumerate(edited_texts)
            if any(text is not None for text in edited.values())
        }
        return cls(
            run=run,
            edited_texts=by_message,
            touched_slots=sorted((items[idx].message_id, role) for idx, role in touched_slots),
            focus_slot=(items[focus_slot[0]].message_id, focus_slot[1]) if focus_slot is not None else None,
            scroll_fraction=scroll_fraction,
        )

    def to_json(self) -> dict[str, Any]:
        """The session as the JSON-serialisable dict state.save_session stores."""
        run = self.run
        return {
            "html_path": str(run.html_path),
            "image_folder": str(run.image_folder),
            "output_path": str(run.output_path),
            "start_time": run.start_time,
            "approved_author_ids": (
                sorted(run.approved_author_ids) if run.approved_author_ids is not None else None
            ),
            "use_cache": run.use_cache,
            "edited_texts": self.edited_texts,
            "touched_slots": [list(slot) for slot in self.touched_slots],
            "focus_slot": list(self.focus_slot) if self.focus_slot is not None else None,
            "scroll_fraction": self.scroll_fraction,
        }

    @classmethod
    def from_json(cls, data: Any) -> "SavedSession":
        """Read a session saved by to_json (or by an older version of it).

        The run inputs must be present and usable, since resuming re-runs
        them. Anything else that's missing or malformed is dropped (and
        logged) rather than failing the whole resume.

        Args:
            data: What state.load_session returned.

        Raises:
            MalformedSessionError: If a run input is missing or unusable.
        """
        if not isinstance(data, dict):
            raise MalformedSessionError(f"saved session is a {type(data).__name__}, not a dict")
        for key in ("html_path", "image_folder", "output_path", "start_time", "approved_author_ids"):
            if key not in data:
                raise MalformedSessionError(f"saved session has no {key!r}")
        paths = [data["html_path"], data["image_folder"], data["output_path"]]
        if not all(isinstance(path, str) and path for path in paths):
            raise MalformedSessionError("saved session has an unusable path")
        start_time = data["start_time"]
        if isinstance(start_time, bool) or not isinstance(start_time, (int, float)):
            raise MalformedSessionError(f"saved session has an unusable start_time {start_time!r}")
        raw_ids = data["approved_author_ids"]
        if raw_ids is not None and not (
            isinstance(raw_ids, list) and all(isinstance(i, str) for i in raw_ids)
        ):
            raise MalformedSessionError("saved session has unusable approved_author_ids")
        run = RunContext(
            html_path=Path(paths[0]),
            image_folder=Path(paths[1]),
            output_path=Path(paths[2]),
            start_time=int(start_time),
            approved_author_ids=set(raw_ids) if raw_ids is not None else None,
            use_cache=data.get("use_cache") is not False,
        )

        raw_edits = data.get("edited_texts") or {}
        edited_texts: EditsByMessage = {}
        if isinstance(raw_edits, dict):
            for message_id, roles in raw_edits.items():
                if isinstance(roles, dict):
                    edited_texts[message_id] = {
                        role: text for role, text in roles.items()
                        if text is None or isinstance(text, str)
                    }
        else:
            logger.warning(
                "saved session edited_texts has unexpected shape, discarding",
                extra=logging_config.extra(saved_texts_type=type(raw_edits).__name__),
            )

        raw_touched = data.get("touched_slots")
        touched = [
            slot for slot in map(_as_message_slot, raw_touched if isinstance(raw_touched, list) else [])
            if slot is not None
        ]
        scroll_fraction = data.get("scroll_fraction")
        if isinstance(scroll_fraction, bool) or not isinstance(scroll_fraction, (int, float)):
            scroll_fraction = None
        return cls(
            run=run,
            edited_texts=edited_texts,
            touched_slots=touched,
            focus_slot=_as_message_slot(data.get("focus_slot")),
            scroll_fraction=float(scroll_fraction) if scroll_fraction is not None else None,
        )

    def restore_onto(self, items: list[ReviewItem]) -> RestoredReview:
        """Map this session onto a freshly parsed transcript.

        Args:
            items: The resumed run's review items.
        """
        return RestoredReview(
            saved_texts=match_saved_edits(items, self.edited_texts),
            touched_slots=match_touched_slots(items, self.touched_slots),
            focus_slot=match_focus_slot(items, self.focus_slot),
            scroll_fraction=self.scroll_fraction,
        )


def _match_edits_by_message_id(
    items: list[ReviewItem],
    edits: EditsByMessage,
    *,
    summary_event: str,
    summary_level: str,
    log_per_box_detail: bool,
) -> EditsByItem:
    """Translate {message_id: {role: text}} edits onto the items' current
    positions, one role->text dict per item.

    Edits whose message no longer appears are dropped. Each matched entry's
    roles are filtered to the item's *current* slot_roles, in case a
    re-export changed how many images (and so "ocrN"/"spacer_imgN" slots)
    the message has.

    `summary_event`/`summary_level` name the matched/dropped-count log line.
    `log_per_box_detail` (resumes only) also logs each matched box at DEBUG,
    including whether its saved text equals the current default - which
    counts alone can't show.
    """
    by_id = {item.message_id: idx for idx, item in enumerate(items)}
    built: EditsByItem = [{} for _ in items]
    matched = dropped = 0
    for message_id, edit in edits.items():
        idx = by_id.get(message_id)
        if idx is None:
            dropped += 1
            continue
        matched += 1
        item = items[idx]
        valid_roles = set(item.slot_roles)
        built[idx] = {role: text for role, text in edit.items() if role in valid_roles}
        if log_per_box_detail:
            for role, text in built[idx].items():
                if text is None or role.startswith("spacer"):
                    continue
                default_text = item.initial_text_for_role(role)
                logger.debug(
                    "resumed box matched",
                    extra=logging_config.extra(
                        message_id=message_id,
                        role=role,
                        saved=logging_config.text_fingerprint(text),
                        current_default=logging_config.text_fingerprint(default_text),
                        equals_current_default=(text == default_text),
                    ),
                )
    getattr(logger, summary_level)(
        summary_event,
        extra=logging_config.extra(
            matched_count=matched, dropped_count=dropped, current_item_count=len(items)
        ),
    )
    return built


def match_saved_edits(items: list[ReviewItem], saved_texts: EditsByMessage) -> EditsByItem:
    """Map a resumed session's edits onto the items' current positions."""
    return _match_edits_by_message_id(
        items, saved_texts,
        summary_event="resumed session edits matched by message_id",
        summary_level="info",
        log_per_box_detail=True,
    )


def match_finalized_edits(items: list[ReviewItem], finalized: EditsByMessage) -> EditsByItem:
    """Map previously finalized edits onto the items' current positions."""
    return _match_edits_by_message_id(
        items, finalized,
        summary_event="finalized edits matched by message_id",
        summary_level="debug",
        log_per_box_detail=False,
    )


def match_focus_slot(
    items: list[ReviewItem], focus_slot: Optional[Iterable[str]]
) -> Optional[Slot]:
    """Translate a saved (message_id, role) focus slot onto its current
    slot, or None if that message no longer appears (the saved scroll
    position is used instead)."""
    if focus_slot is None:
        return None
    message_id, role = focus_slot
    by_id = {item.message_id: idx for idx, item in enumerate(items)}
    idx = by_id.get(message_id)
    return (idx, role) if idx is not None else None


def match_touched_slots(items: list[ReviewItem], raw_slots: Any) -> set[Slot]:
    """Translate saved (message_id, role) touched slots onto current slots,
    dropping any whose message or role no longer exists, and any entry that
    isn't a pair."""
    if not isinstance(raw_slots, (list, tuple)):
        return set()
    by_id = {item.message_id: idx for idx, item in enumerate(items)}
    matched = set()
    for entry in raw_slots:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        message_id, role = entry
        idx = by_id.get(message_id)
        if idx is not None and role in items[idx].slot_roles:
            matched.add((idx, role))
    return matched


def build_finalized_updates(
    items: list[ReviewItem], edited_texts: EditsByItem, touched_slots: set[Slot]
) -> EditsByMessage:
    """Work out what Finalize should change in the stored finalized edits.

    Args:
        items: This run's items, in transcript order.
        edited_texts: One role->text dict per item (None = default/unchecked).
        touched_slots: Slots the user deliberately acted on.

    Returns:
        {message_id: {role: text_or_None}} for state.save_finalized_edits:
        an edited box stores its text; a box at its default (or unchecked)
        maps to None - remove the stored edit - *only* if the user touched
        it this session. Any other box is left out, keeping whatever is
        stored for it.
    """
    updates: EditsByMessage = {}
    for idx, (item, edited) in enumerate(zip(items, edited_texts)):
        per_msg: dict[str, Optional[str]] = {}
        for role, text in edited.items():
            if text is not None:
                per_msg[role] = text
            elif (idx, role) in touched_slots:
                per_msg[role] = None
        if per_msg:
            updates[item.message_id] = per_msg
    return updates


def log_edit_changes(previous: EditsByMessage, current: EditsByMessage, tag: str) -> None:
    """Log exactly which (message_id, role) edits appeared, disappeared, or
    changed between two saves - diagnostic only.

    Args:
        previous: The edits as of the previous save.
        current: The edits being saved now.
        tag: What triggered this save (e.g. "autosave_tick", "window_close").
    """
    for message_id in current.keys() - previous.keys():
        for role, text in current[message_id].items():
            logger.info(
                "autosave diff: new edit",
                extra=logging_config.extra(
                    tag=tag, message_id=message_id, role=role,
                    **logging_config.text_fingerprint(text),
                ),
            )
    for message_id in previous.keys() - current.keys():
        logger.info(
            "autosave diff: edit entry removed entirely",
            extra=logging_config.extra(
                tag=tag, message_id=message_id, roles=sorted(previous[message_id].keys()),
            ),
        )
    for message_id in current.keys() & previous.keys():
        old_edit = previous[message_id]
        new_edit = current[message_id]
        for role in old_edit.keys() | new_edit.keys():
            old_text = old_edit.get(role)
            new_text = new_edit.get(role)
            if old_text != new_text:
                logger.info(
                    "autosave diff: edit changed",
                    extra=logging_config.extra(
                        tag=tag, message_id=message_id, role=role,
                        old=logging_config.text_fingerprint(old_text),
                        new=logging_config.text_fingerprint(new_text),
                    ),
                )
