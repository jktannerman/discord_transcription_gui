import pytest

from discord_transcription import ocr


def test_transcribe_image_dispatches_to_named_backend(monkeypatch):
    monkeypatch.setitem(ocr._BACKENDS, "fake", lambda file_path: f"text for {file_path}")

    result = ocr.transcribe_image("card.png", backend="fake")

    assert result == "text for card.png"


def test_transcribe_image_unknown_backend_raises():
    with pytest.raises(KeyError):
        ocr.transcribe_image("card.png", backend="does-not-exist")


def test_transcribe_image_reraises_backend_exception(monkeypatch):
    def _boom(file_path):
        raise RuntimeError("tesseract exploded")

    monkeypatch.setitem(ocr._BACKENDS, "fake", _boom)

    with pytest.raises(RuntimeError, match="tesseract exploded"):
        ocr.transcribe_image("card.png", backend="fake")
