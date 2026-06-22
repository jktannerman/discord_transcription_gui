import pytest

from gui_transcription.app.pipeline import ParagraphCorrectionController, parse_start_date


def test_parse_start_date_valid():
    ts = parse_start_date("2024-01-01")
    assert ts > 0


def test_parse_start_date_with_time():
    ts = parse_start_date("2024-01-01-12-30-00")
    assert ts > 0


def test_parse_start_date_invalid_raises():
    with pytest.raises(ValueError):
        parse_start_date("not-a-date")


def test_parse_start_date_wrong_field_count_raises():
    with pytest.raises(ValueError):
        parse_start_date("2024")


def test_accept_default_uses_paragraph_text():
    controller = ParagraphCorrectionController(["para one", "para two"])
    controller.accept()
    assert controller.lines == ["para one\\n\\n"]
    assert controller.current_paragraph == "para two"


def test_accept_with_edited_text():
    controller = ParagraphCorrectionController(["para one"])
    controller.accept("edited text")
    assert controller.lines == ["edited text"]
    assert controller.done


def test_go_back_undoes_last_accept():
    controller = ParagraphCorrectionController(["para one", "para two"])
    controller.accept()
    controller.accept()
    controller.go_back()
    assert controller.lines == ["para one\\n\\n"]
    assert controller.current_paragraph == "para two"


def test_accept_all_remaining_fills_rest_verbatim():
    controller = ParagraphCorrectionController(["a", "b", "c"])
    controller.accept_all_remaining()
    assert controller.done
    assert controller.lines == ["a\\n\\n", "b\\n\\n", "c\\n\\n"]


def test_skip_rest_drops_current_and_following():
    controller = ParagraphCorrectionController(["a", "b", "c"])
    controller.skip_rest()
    assert controller.done
    assert controller.lines == []


def test_skip_this_only_drops_current_paragraph():
    controller = ParagraphCorrectionController(["a", "b"])
    controller.skip_this_only()
    assert controller.current_paragraph == "b"
    assert controller.lines == []


def test_accept_and_stop_accepts_current_then_stops():
    controller = ParagraphCorrectionController(["a", "b"])
    controller.accept_and_stop()
    assert controller.done
    assert controller.lines == ["a\\n\\n"]
