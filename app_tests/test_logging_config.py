import json
import logging

from gui_transcription.app import logging_config


def _make_record(**extra_fields) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.pipeline", level=logging.INFO, pathname=__file__, lineno=1,
        msg="something happened", args=(), exc_info=None,
    )
    if extra_fields:
        record.extra_fields = extra_fields
    return record


def test_json_formatter_includes_core_fields():
    payload = json.loads(logging_config.JsonFormatter().format(_make_record()))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.pipeline"
    assert payload["message"] == "something happened"
    assert "timestamp" in payload


def test_json_formatter_includes_extra_fields():
    payload = json.loads(
        logging_config.JsonFormatter().format(_make_record(image="card.png", count=3))
    )

    assert payload["image"] == "card.png"
    assert payload["count"] == 3


def test_json_formatter_omits_extra_fields_key_when_none_given():
    payload = json.loads(logging_config.JsonFormatter().format(_make_record()))

    assert "extra_fields" not in payload


def test_json_formatter_includes_exc_info_when_present():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="app.pipeline", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )

    payload = json.loads(logging_config.JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exc_info"]


def test_get_logger_main_falls_back_to_app_logger():
    logger = logging_config.get_logger("__main__")
    assert logger.name == logging_config.LOGGER_NAME


def test_get_logger_returns_logger_named_after_module():
    logger = logging_config.get_logger("app.pipeline")
    assert logger.name == "app.pipeline"


def test_extra_wraps_fields_under_extra_fields_key():
    assert logging_config.extra(image="card.png", count=3) == {
        "extra_fields": {"image": "card.png", "count": 3}
    }
