"""Right-click context menu on a review row's image: Open Image in Browser,
Open Image Location, and Copy Image (see the README's "Review screen"
section for the exact three actions and their expected behavior).

Built with a plain tk.Menu popped up via tk_popup(event.x_root, event.y_root)
- screen-absolute coordinates, not widget-relative event.x/event.y - rather
than a hand-rolled Toplevel/Canvas-drawn menu. tk_popup is what Tk's own
native menus use internally for their grab and hit-testing, so there's no
separate "where is the menu, really" bookkeeping this app has to keep in
sync with what's on screen: a sibling project's earlier attempt at this
exact feature shipped with a "clicks land on nothing" bug, traced to that
class of drift (the click handler's own idea of the menu's position/size
disagreeing with where Tk actually drew and hit-tested it - the classic
version of this mistake is popping up at event.x/event.y, which are
relative to the clicked widget, not the screen tk_popup expects). Native
tk_popup also means clicking elsewhere or pressing Escape already dismiss
the menu for free (Tk's own grab/keybindings) - unlike *noticing* that
dismissal to unfreeze scrolling again, which does need explicit handling;
see _show_image_context_menu's docstring comment for why that can't lean on
Tk's usual widget-unmap event the way it first tried to.
"""

import subprocess
import webbrowser
from io import BytesIO
from pathlib import Path
from typing import Callable

import tkinter as tk

from PIL import Image

from .. import logging_config
from . import theme

logger = logging_config.get_logger(__name__)


class ImageContextMenuMixin:
    """Mixed into ReviewFrame the same way row_building.py's
    RowBuildingMixin and keyboard_nav.py's KeyboardNavMixin already are -
    see review_view.py's module docstring for why this codebase splits
    self-contained per-feature behavior out of ReviewFrame this way rather
    than growing that class directly.

    Reaches into ReviewFrame's self._scroll_frozen/self._scrollbar (set in
    review_view.py's __init__, checked by its mousewheel/scrollbar handlers
    and keyboard_nav.py's Page Up/Down) so scrolling can't move rows - and
    therefore this menu's target image - out from under an open menu."""

    def _bind_image_context_menu(self, label: tk.Widget, image_path: Path) -> None:
        label.bind("<Button-3>", lambda event, p=image_path: self._show_image_context_menu(event, p))

    def _show_image_context_menu(self, event: tk.Event, image_path: Path) -> None:
        logger.info(
            "image context menu opened", extra=logging_config.extra(image_path=str(image_path))
        )
        self._scroll_frozen = True
        self._scrollbar.state(["disabled"])

        menu = tk.Menu(
            self,
            tearoff=0,
            bg=theme.DARK_BG_WIDGET,
            fg=theme.DARK_FG,
            activebackground=theme.DARK_ACCENT,
            activeforeground="white",
        )
        menu.add_command(
            label="Open Image in Browser",
            command=lambda: self._run_image_menu_action(
                "open_in_browser", image_path, self._open_image_in_browser
            ),
        )
        menu.add_command(
            label="Open Image Location",
            command=lambda: self._run_image_menu_action(
                "open_location", image_path, self._open_image_location
            ),
        )
        menu.add_command(
            label="Copy Image",
            command=lambda: self._run_image_menu_action(
                "copy_image", image_path, self._copy_image_to_clipboard
            ),
        )

        try:
            # On Windows, tk.Menu's popup is backed by the native
            # TrackPopupMenu API, which blocks this call - running its own
            # message loop - until a person actually dismisses the menu
            # (a command clicked, a click outside it, or Escape); confirmed
            # by hand, not just inferred, since it's also what made an
            # earlier, unattended version of this feature's own test suite
            # hang until force-closed (see test_image_context_menu.py).
            # That blocking is what makes unfreezing in `finally` below
            # deterministic: by the time tk_popup returns, the menu is
            # already gone, however it closed.
            #
            # This was originally done via a menu.bind("<Unmap>", ...)
            # instead, on the assumption that Tk would fire its usual
            # widget-unmap event when a popup closes the way it does for
            # an ordinary window - it doesn't, for this same native-menu
            # reason: TrackPopupMenu's popup isn't a regular Tk-managed
            # window, so Tk never sees (and can't report) it unmapping.
            # That silently left this app frozen after every real close
            # (Escape or an outside click, not just selecting a command),
            # since nothing else ever called _on_image_context_menu_closed.
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
            self._on_image_context_menu_closed(image_path)

    def _on_image_context_menu_closed(self, image_path: Path) -> None:
        self._scroll_frozen = False
        self._scrollbar.state(["!disabled"])
        logger.info(
            "image context menu closed", extra=logging_config.extra(image_path=str(image_path))
        )

    def _run_image_menu_action(
        self, action: str, image_path: Path, func: Callable[[Path], None]
    ) -> None:
        logger.info(
            "image context menu action clicked",
            extra=logging_config.extra(action=action, image_path=str(image_path)),
        )
        try:
            func(image_path)
        except Exception:
            logger.exception(
                "image context menu action failed",
                extra=logging_config.extra(action=action, image_path=str(image_path)),
            )
        else:
            logger.info(
                "image context menu action succeeded",
                extra=logging_config.extra(action=action, image_path=str(image_path)),
            )

    def _open_image_in_browser(self, image_path: Path) -> None:
        webbrowser.open(Path(image_path).resolve().as_uri())

    def _open_image_location(self, image_path: Path) -> None:
        # explorer.exe routinely exits non-zero even on a fully successful
        # /select - its return code isn't a reliable success signal, so this
        # doesn't check=True; a genuinely broken invocation (missing
        # explorer.exe, bad path) still raises OSError, which
        # _run_image_menu_action's try/except logs as a failure.
        subprocess.run(["explorer", "/select,", str(Path(image_path).resolve())])

    def _copy_image_to_clipboard(self, image_path: Path) -> None:
        # Local import: win32clipboard is Windows-only (see requirements.txt)
        # and this is the only place in the app that needs it.
        import win32clipboard

        with Image.open(image_path) as image:
            buffer = BytesIO()
            image.convert("RGB").save(buffer, "BMP")
            # CF_DIB is the BMP payload minus its 14-byte file header - the
            # clipboard format expects a bare DIB, not a complete .bmp file.
            dib = buffer.getvalue()[14:]

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
        finally:
            win32clipboard.CloseClipboard()
