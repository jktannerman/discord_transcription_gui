"""Right-click context menu on a review row's image: Open Image, Open Image
in Browser, Open Image Location, Open Chatlog at Message, and Copy Image.

A plain tk.Menu shown with tk_popup at the event's screen coordinates
(x_root/y_root, not the widget-relative x/y), so Tk does all the
positioning, hit-testing and dismissal (Escape, a click elsewhere) itself.
Scrolling is frozen while the menu is open; see show() for how its closing
is detected.

The Windows actions are here; the Linux ones are in desktop_linux.py.
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
from . import desktop_linux, theme

logger = logging_config.get_logger(__name__)


class ImageContextMenu:
    """The right-click menu on review rows' images."""

    def __init__(
        self, parent: Optional[tk.Widget], html_path: Path, set_scroll_frozen: Callable[[bool], None]
    ) -> None:
        """
        Args:
            parent: The widget menus are created in.
            html_path: The run's chatlog export, for "Open Chatlog at Message".
            set_scroll_frozen: Freezes (True) or unfreezes (False) the review
                screen's scrolling, so rows - and the menu's target image -
                can't move out from under an open menu.
        """
        self._parent = parent
        self._html_path = html_path
        self._set_scroll_frozen = set_scroll_frozen

    def bind(self, label: tk.Widget, image_path: Path, message_id: str) -> None:
        """Open the menu when `label` (an image placeholder) is right-clicked."""
        label.bind(
            "<Button-3>",
            lambda event, p=image_path, m=message_id: self.show(event, p, m),
        )

    def show(self, event: tk.Event, image_path: Path, message_id: str) -> None:
        """Pop up the menu for one image, freezing scrolling until it closes.

        Args:
            event: The right-click event.
            image_path: The clicked image.
            message_id: Its message's Discord ID.
        """
        logger.info(
            "image context menu opened",
            extra=logging_config.extra(image_path=str(image_path), message_id=message_id),
        )
        self._set_scroll_frozen(True)

        menu = tk.Menu(
            self._parent,
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
            # On Windows the popup is the native TrackPopupMenu, which
            # blocks here until the menu is dismissed, however that happens,
            # so the `finally` below runs exactly when it closes. An <Unmap>
            # binding can't be used instead: Tk never sees that native popup
            # unmap. (Tests mock tk_popup, since the real one would block
            # until a person closes it.)
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
            self._on_image_context_menu_closed(image_path)

    def _on_image_context_menu_closed(self, image_path: Path) -> None:
        self._set_scroll_frozen(False)
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
        # The default program for this file type, as a double-click in the
        # file manager would use.
        resolved = str(Path(image_path).resolve())
        if sys.platform == "win32":
            os.startfile(resolved)
        else:
            subprocess.Popen(["xdg-open", resolved])

    def _open_image_in_browser(self, image_path: Path) -> None:
        # webbrowser.open() and os.startfile() on a file:// URI both follow
        # the file type's association (e.g. Photos for .png), not the
        # default browser, so the browser is looked up and launched
        # directly.
        _launch_url_in_default_browser(Path(image_path).resolve().as_uri())

    def _open_chatlog_at_message(self, message_id: str) -> None:
        # DiscordChatExporter gives each message's container element
        # id="chatlog__message-container-<message id>", so a URL fragment
        # can jump straight to it. The default browser is looked up as for
        # _open_image_in_browser, since an .html file's association isn't
        # necessarily a browser either.
        uri = self._html_path.resolve().as_uri()
        anchor = f"chatlog__message-container-{message_id}"
        _launch_url_in_default_browser(f"{uri}#{anchor}")

    def _open_image_location(self, image_path: Path) -> None:
        if sys.platform != "win32":
            # Explorer's /select has no per-file-manager-agnostic flag on
            # Linux, but the FileManager1 D-Bus interface does the same job.
            desktop_linux.show_in_file_manager(Path(image_path))
            return
        # Popen, not run: waiting for explorer.exe to exit would freeze the
        # UI. Its exit code isn't a reliable success signal anyway (it
        # routinely exits non-zero on a successful /select); a genuinely
        # broken invocation (missing explorer.exe) still raises OSError,
        # which _run_image_menu_action's try/except logs as a failure.
        subprocess.Popen(["explorer", "/select,", str(Path(image_path).resolve())])

    def _copy_image_to_clipboard(self, image_path: Path) -> None:
        if sys.platform != "win32":
            with Image.open(image_path) as image:
                buffer = BytesIO()
                image.save(buffer, "PNG")
            desktop_linux.copy_png_to_clipboard(buffer.getvalue())
            return
        # Local import: win32clipboard is Windows-only (see pyproject.toml)
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
    """Launch `url` (a file:// URI, possibly with a #fragment) in the
    default browser: from the registry on Windows
    (_default_browser_command), from xdg-settings and its .desktop entry on
    Linux (desktop_linux.py).

    Raises:
        RuntimeError: If the default browser can't be determined.
    """
    if sys.platform == "win32":
        command_template = _default_browser_command()
        if command_template is None:
            raise RuntimeError("could not determine the default browser from the registry")
        command = [url if part == "%1" else part for part in shlex.split(command_template)]
    else:
        exec_line = desktop_linux.default_browser_exec_line()
        if exec_line is None:
            raise RuntimeError("could not determine the default browser via xdg-settings")
        command = desktop_linux.build_exec_command(exec_line, url)
    # Popen, not run: if the browser wasn't already running, the launched
    # process lives until the browser is closed, and waiting on it would
    # freeze the UI for that whole time.
    subprocess.Popen(command)


def _default_browser_command() -> Optional[str]:
    """The Windows default browser's command line, e.g.
    '"C:\\Program Files\\Mozilla Firefox\\firefox.exe" -osint -url "%1"'.

    Read the way Explorer resolves it: the http protocol's UserChoice
    ProgId, then that ProgId's shell\\open\\command.

    Returns:
        The command line, or None if either lookup step fails (no
        UserChoice set, or a ProgId left by an uninstalled browser).
    """
    # Local import: winreg exists only on Windows.
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
