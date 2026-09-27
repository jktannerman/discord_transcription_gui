"""Linux desktop integration for the image context menu (image_context_menu.py).

Each helper uses a freedesktop.org standard rather than a specific desktop
environment's tools, so it works across file managers and browsers:

- the default browser comes from ``xdg-settings`` and is launched from its
  .desktop entry's Exec line, so a local file:// URI opens in the browser
  rather than in whatever handles that file type (the same problem the
  Windows code works around via the registry);
- "show in folder" uses the org.freedesktop.FileManager1 D-Bus interface,
  which Nemo, Nautilus, Dolphin and others implement;
- images go on the clipboard via xclip (X11) or wl-copy (Wayland).
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .. import logging_config

logger = logging_config.get_logger(__name__)

# Bound on how long a helper process may block the UI.
_SUBPROCESS_TIMEOUT_S = 5

# Desktop Entry Exec field codes that take the file/URL argument.
_URL_FIELD_CODES = frozenset({"%u", "%U", "%f", "%F"})


def _application_dirs() -> list[Path]:
    """XDG application directories, highest precedence first."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(d) / "applications" for d in [data_home, *data_dirs.split(":")] if d]


def find_desktop_file(desktop_id: str) -> Optional[Path]:
    """Locate a .desktop file by its ID (e.g. "firefox.desktop").

    Args:
        desktop_id: The desktop file ID.

    Returns:
        The first match in XDG precedence order, or None.
    """
    for directory in _application_dirs():
        candidate = directory / desktop_id
        if candidate.is_file():
            return candidate
    return None


def read_exec_line(desktop_file: Path) -> Optional[str]:
    """Return the Exec value of a .desktop file's main [Desktop Entry] group.

    Args:
        desktop_file: The .desktop file.

    Returns:
        The Exec value, or None if the group has none.
    """
    in_main_group = False
    for line in desktop_file.read_text(encoding="utf8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("["):
            in_main_group = line == "[Desktop Entry]"
        elif in_main_group and line.startswith("Exec="):
            return line[len("Exec="):]
    return None


def build_exec_command(exec_line: str, url: str) -> list[str]:
    """Turn a Desktop Entry Exec value into argv for opening one URL.

    %u/%U/%f/%F become the URL, %% becomes a literal %, and the other field
    codes (%i, %c, %k, deprecated ones) are dropped. If the Exec line takes
    no URL argument at all, the URL is appended.

    Args:
        exec_line: The Exec value, e.g. "firefox %u".
        url: The URL to open.

    Returns:
        The command to run.
    """
    command: list[str] = []
    placed = False
    for part in shlex.split(exec_line):
        if part in _URL_FIELD_CODES:
            if not placed:
                command.append(url)
                placed = True
        elif len(part) == 2 and part.startswith("%") and part != "%%":
            continue
        else:
            command.append(part.replace("%%", "%"))
    if not placed:
        command.append(url)
    return command


def default_browser_exec_line() -> Optional[str]:
    """The default browser's Exec line, per ``xdg-settings``.

    Returns:
        The Exec value, or None if the default browser can't be determined.
    """
    try:
        desktop_id = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True, text=True, check=True, timeout=_SUBPROCESS_TIMEOUT_S,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        logger.warning("could not query the default browser", exc_info=True)
        return None
    desktop_file = find_desktop_file(desktop_id) if desktop_id else None
    if desktop_file is None:
        logger.warning(
            "default browser's desktop file not found",
            extra=logging_config.extra(desktop_id=desktop_id),
        )
        return None
    return read_exec_line(desktop_file)


def show_in_file_manager(path: Path) -> None:
    """Open the folder containing `path` with `path` selected.

    Uses the FileManager1 D-Bus interface; if no file manager provides it,
    falls back to opening the folder without a selection.

    Args:
        path: The file to show.

    Raises:
        OSError: If neither approach could be started.
    """
    uri = path.resolve().as_uri()
    try:
        subprocess.run(
            [
                "dbus-send", "--session", "--print-reply",
                "--dest=org.freedesktop.FileManager1", "--type=method_call",
                "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                f"array:string:{uri}", "string:",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True, timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("FileManager1.ShowItems failed, opening the folder instead", exc_info=True)
        subprocess.Popen(["xdg-open", str(path.resolve().parent)])


def copy_png_to_clipboard(png_bytes: bytes) -> None:
    """Put PNG image data on the clipboard.

    Args:
        png_bytes: The encoded PNG.

    Raises:
        RuntimeError: If no supported clipboard tool is installed.
        subprocess.SubprocessError: If the tool fails.
    """
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        command = ["wl-copy", "--type", "image/png"]
    elif shutil.which("xclip"):
        command = ["xclip", "-selection", "clipboard", "-t", "image/png", "-i"]
    else:
        raise RuntimeError("Copy Image needs xclip (X11) or wl-copy (Wayland) installed")
    # Both tools fork a background process to keep serving the clipboard.
    # That process inherits any pipes given here, so stdout/stderr must not
    # be captured, or run() would wait for it to exit.
    subprocess.run(
        command, input=png_bytes,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=True, timeout=_SUBPROCESS_TIMEOUT_S,
    )
