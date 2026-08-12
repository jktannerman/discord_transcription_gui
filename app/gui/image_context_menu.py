"""Right-click context menu on a review row's image: Open Image, Open Image
in Browser, Open Image Location, Open Chatlog at Message, and Copy Image
(see the README's "Review screen" section for the exact five actions and
their expected behavior).

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

import os
import shlex
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional

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

    def _bind_image_context_menu(self, label: tk.Widget, image_path: Path, message_id: str) -> None:
        label.bind(
            "<Button-3>",
            lambda event, p=image_path, m=message_id: self._show_image_context_menu(event, p, m),
        )

    def _show_image_context_menu(self, event: tk.Event, image_path: Path, message_id: str) -> None:
        logger.info(
            "image context menu opened",
            extra=logging_config.extra(image_path=str(image_path), message_id=message_id),
        )
        self._scroll_frozen = True
        self._scrollbar.state(["disabled"])

        # Sized/hit-tested entirely by tk_popup itself (see the module
        # docstring) - a fifth entry here needs no manual layout/hitbox
        # bookkeeping the way it would have with a hand-rolled popup, which
        # is exactly the class of bug ("clicks land on nothing") that
        # docstring explains this design avoids.
        menu = tk.Menu(
            self,
            tearoff=0,
            bg=theme.DARK_BG_WIDGET,
            fg=theme.DARK_FG,
            activebackground=theme.DARK_ACCENT,
            activeforeground="white",
        )
        menu.add_command(
            label="Open Image",
            command=lambda: self._run_image_menu_action(
                "open_image", image_path, self._open_image
            ),
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
            label="Open Chatlog at Message",
            command=lambda: self._run_image_menu_action(
                "open_chatlog_at_message",
                image_path,
                lambda p: self._open_chatlog_at_message(message_id),
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

    def _open_image(self, image_path: Path) -> None:
        # The registered default *file* handler for this extension (e.g.
        # Photos), same as double-clicking the file in Explorer - unlike
        # _open_image_in_browser below, this is exactly what os.startfile
        # already does, no registry lookup needed.
        os.startfile(str(Path(image_path).resolve()))

    def _open_image_in_browser(self, image_path: Path) -> None:
        # Neither webbrowser.open() nor a plain os.startfile() on a
        # file:// URI actually opens the *browser* here - both resolve a
        # local file through its file-type association (Photos, same as
        # _open_image above), since that association - not "what's the
        # default browser" - is what Windows consults for a local path/
        # file:// URI regardless of which API asks. Confirmed by hand:
        # this action opened Photos, not Firefox/Chrome/Edge, until fixed
        # to look up and launch the default *browser* directly instead.
        #
        # The default browser is a separate piece of registry state
        # entirely (the "UserChoice" registered for the http protocol,
        # not for this file's extension) - _default_browser_command below
        # reads that and returns its command line, which this substitutes
        # the image's file:// URI into and launches directly, bypassing
        # file-type association altogether.
        _launch_url_in_default_browser(Path(image_path).resolve().as_uri())

    def _open_chatlog_at_message(self, message_id: str) -> None:
        # The export's own DiscordChatExporter markup gives every message's
        # chatlog__message-container div both a data-message-id attribute
        # (what chatlog.py already reads to key edits/sessions by) *and* an
        # id="chatlog__message-container-<that same id>" attribute on the
        # very same element - confirmed by hand against a real export (see
        # example_inputs/short_test_input.html). That id is exactly what an
        # HTML fragment (#...) anchor needs, so this needs no HTML parsing
        # of its own at open time - just string-formatting the id chatlog.py
        # already guarantees every kept message has.
        #
        # Routed through the same default-browser lookup as
        # _open_image_in_browser (not os.startfile/webbrowser.open) for
        # consistency - an .html file's own default-open association isn't
        # guaranteed to be a browser either, the same gap that action's
        # docstring explains for images.
        uri = self._html_path.resolve().as_uri()
        anchor = f"chatlog__message-container-{message_id}"
        _launch_url_in_default_browser(f"{uri}#{anchor}")

    def _open_image_location(self, image_path: Path) -> None:
        # Not yet implemented outside Windows: Explorer's /select flag (open
        # a folder with one file pre-selected) has no single equivalent
        # across Linux file managers (Nautilus/Dolphin/Thunar each need a
        # different flag, and there's no reliable way to detect which one is
        # in use) - deferred rather than guessed at. Raising here, instead
        # of just letting the explorer.exe call below fail with a raw
        # FileNotFoundError, gives _run_image_menu_action's failure log a
        # clear, intentional reason instead of a confusing one.
        if sys.platform != "win32":
            raise NotImplementedError(
                "Open Image Location is not yet implemented outside Windows"
            )
        # explorer.exe routinely exits non-zero even on a fully successful
        # /select - its return code isn't a reliable success signal, so this
        # doesn't check=True; a genuinely broken invocation (missing
        # explorer.exe, bad path) still raises OSError, which
        # _run_image_menu_action's try/except logs as a failure.
        subprocess.run(["explorer", "/select,", str(Path(image_path).resolve())])

    def _copy_image_to_clipboard(self, image_path: Path) -> None:
        # Not yet implemented outside Windows: Linux clipboard access is
        # split across X11 (xclip/xsel) and Wayland (wl-copy), neither
        # bundled with Python, with no single tool covering both - deferred
        # rather than guessed at. Raising here, instead of just letting the
        # win32clipboard import below fail with a raw ModuleNotFoundError,
        # gives _run_image_menu_action's failure log a clear, intentional
        # reason instead of a confusing one.
        if sys.platform != "win32":
            raise NotImplementedError("Copy Image is not yet implemented outside Windows")
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


def _launch_url_in_default_browser(url: str) -> None:
    """Launch `url` (a file:// URI, with or without a #fragment) in the
    user's actual default *browser*, looked up via _default_browser_command
    - shared by _open_image_in_browser and _open_chatlog_at_message, which
    otherwise differ only in what URI they build."""
    command_template = _default_browser_command()
    if command_template is None:
        raise RuntimeError("could not determine the default browser from the registry")
    command = [url if part == "%1" else part for part in shlex.split(command_template)]
    subprocess.run(command)


def _default_browser_command() -> Optional[str]:
    """The current user's default browser's raw command line, e.g.
    '"C:\\Program Files\\Mozilla Firefox\\firefox.exe" -osint -url "%1"' -
    read from the same two-step registry lookup Windows Explorer itself
    uses to resolve "open with default browser": the http protocol's
    UserChoice ProgId, then that ProgId's own shell\\open\\command. This is
    deliberately unrelated to a file's *extension* association (what
    os.startfile/webbrowser.open actually follow for a local path or
    file:// URI - see _open_image_in_browser above) - the two can name
    different programs entirely (e.g. Photos as the .png handler, Firefox
    as the http handler), and it's the second one this action needs.

    None if any step of the lookup fails (no UserChoice set, an installed-
    but-since-uninstalled browser's stale ProgId, ...) - the caller raises
    on that, which _run_image_menu_action logs as a failure rather than
    silently falling back to a file-association open that wouldn't
    actually satisfy "open in browser"."""
    # Local import: winreg is Windows-only, like win32clipboard below - a
    # top-level import here used to make this whole module (and therefore
    # every action in this menu, not just this lookup) fail to import on
    # any other platform.
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as key:
            command, _ = winreg.QueryValueEx(key, None)
    except OSError:
        return None
    return command
