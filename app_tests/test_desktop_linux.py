"""desktop_linux.py's helpers, with every external command mocked."""
import subprocess
from unittest.mock import patch

import pytest

from discord_transcription.gui import desktop_linux

_MODULE = "discord_transcription.gui.desktop_linux"


@pytest.mark.parametrize(
    "exec_line, expected",
    [
        ("firefox %u", ["firefox", "URL"]),
        ("/opt/browser --new-tab %U", ["/opt/browser", "--new-tab", "URL"]),
        ("browser %i %c --flag %u", ["browser", "--flag", "URL"]),
        ('"/opt/my browser/run" %f', ["/opt/my browser/run", "URL"]),
        ("browser", ["browser", "URL"]),
        ("browser --pct=50%% %u", ["browser", "--pct=50%", "URL"]),
    ],
)
def test_build_exec_command(exec_line, expected):
    assert desktop_linux.build_exec_command(exec_line, "URL") == expected


def test_read_exec_line_uses_the_main_group_only(tmp_path):
    desktop_file = tmp_path / "b.desktop"
    desktop_file.write_text(
        "[Desktop Entry]\nName=B\nExec=b %u\n\n[Desktop Action new-window]\nExec=b --new\n",
        encoding="utf8",
    )
    assert desktop_linux.read_exec_line(desktop_file) == "b %u"


def test_find_desktop_file_prefers_the_user_data_dir(tmp_path, monkeypatch):
    user_dir = tmp_path / "home" / "applications"
    system_dir = tmp_path / "usr" / "applications"
    for directory in (user_dir, system_dir):
        directory.mkdir(parents=True)
        (directory / "b.desktop").write_text("[Desktop Entry]\n", encoding="utf8")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "usr"))

    assert desktop_linux.find_desktop_file("b.desktop") == user_dir / "b.desktop"
    assert desktop_linux.find_desktop_file("missing.desktop") is None


def test_default_browser_exec_line_reads_the_xdg_default(tmp_path, monkeypatch):
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "b.desktop").write_text("[Desktop Entry]\nExec=b %u\n", encoding="utf8")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "none"))
    result = subprocess.CompletedProcess([], 0, stdout="b.desktop\n")
    with patch(f"{_MODULE}.subprocess.run", return_value=result):
        assert desktop_linux.default_browser_exec_line() == "b %u"


def test_default_browser_exec_line_is_none_without_xdg_settings():
    with patch(f"{_MODULE}.subprocess.run", side_effect=FileNotFoundError()):
        assert desktop_linux.default_browser_exec_line() is None


def test_show_in_file_manager_calls_show_items(tmp_path):
    image = tmp_path / "shot.png"
    with patch(f"{_MODULE}.subprocess.run") as run:
        desktop_linux.show_in_file_manager(image)
    (command,), _ = run.call_args
    assert "org.freedesktop.FileManager1.ShowItems" in command
    assert f"array:string:{image.resolve().as_uri()}" in command


def test_show_in_file_manager_falls_back_to_opening_the_folder(tmp_path):
    image = tmp_path / "shot.png"
    with (
        patch(f"{_MODULE}.subprocess.run", side_effect=subprocess.CalledProcessError(1, "dbus-send")),
        patch(f"{_MODULE}.subprocess.Popen") as popen,
    ):
        desktop_linux.show_in_file_manager(image)
    (command,), _ = popen.call_args
    assert command == ["xdg-open", str(tmp_path.resolve())]


def test_copy_png_uses_xclip_on_x11(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with (
        patch(f"{_MODULE}.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
        patch(f"{_MODULE}.subprocess.run") as run,
    ):
        desktop_linux.copy_png_to_clipboard(b"png")
    (command,), kwargs = run.call_args
    assert command[0] == "xclip"
    assert kwargs["input"] == b"png"
    # Capturing output would make run() wait on xclip's background process.
    assert kwargs["stdout"] is subprocess.DEVNULL


def test_copy_png_uses_wl_copy_on_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    with (
        patch(f"{_MODULE}.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
        patch(f"{_MODULE}.subprocess.run") as run,
    ):
        desktop_linux.copy_png_to_clipboard(b"png")
    (command,), _ = run.call_args
    assert command[0] == "wl-copy"


def test_copy_png_raises_without_a_clipboard_tool(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with patch(f"{_MODULE}.shutil.which", return_value=None):
        with pytest.raises(RuntimeError):
            desktop_linux.copy_png_to_clipboard(b"png")
