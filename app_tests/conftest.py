"""Shared pytest fixtures for the whole test suite."""

from pathlib import Path

import pytest

from gui_transcription.app import config


@pytest.fixture(autouse=True)
def isolated_app_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point APP_DATA_DIR and every state/log file path derived from it at a
    per-test temp directory.

    config.py computes those paths once at import time, so patching
    APP_DATA_DIR alone doesn't redirect them - any path a test didn't patch
    individually would otherwise be written into the real
    ~/.discord_transcription_gui. Tests that patch a path themselves still
    work: their monkeypatch runs after this one and wins.

    Returns:
        The temp directory standing in for APP_DATA_DIR.
    """
    real_dir = config.APP_DATA_DIR
    data_dir = tmp_path / "app_data"
    # Created up front: a test that re-patches APP_DATA_DIR to somewhere
    # else still writes any file it didn't re-patch into here, and
    # state._atomic_write_json only creates APP_DATA_DIR itself.
    data_dir.mkdir()
    monkeypatch.setattr(config, "APP_DATA_DIR", data_dir)
    for name in dir(config):
        value = getattr(config, name)
        if isinstance(value, Path) and value.parent == real_dir:
            monkeypatch.setattr(config, name, data_dir / value.name)
    return data_dir
