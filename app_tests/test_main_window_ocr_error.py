"""Error and warning paths between starting a run and showing the review
screen.

_prepare_run runs on the worker thread and turns chatlog read/parse
failures into a _RunError carrying a user-facing message. _on_ocr_done runs
inside a root.after() callback, so anything it doesn't catch itself would
only reach Tk's report_callback_exception, leaving the user stuck on the
progress screen - these cover that it reports errors through
_on_run_error instead, and warns about missing images before continuing."""
import queue
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import tkinter as tk

from gui_transcription.app import pipeline
from gui_transcription.app.gui import main_window
from gui_transcription.app.gui.main_window import App, RunContext


def _run_context(tmp_path) -> RunContext:
    html_path = tmp_path / "chat.html"
    html_path.write_text("<html></html>", encoding="utf8")
    return RunContext(
        html_path=html_path,
        image_folder=tmp_path,
        output_path=tmp_path / "out.txt",
        start_time=0,
        approved_author_ids=None,
        use_cache=True,
    )


def _prepare(run: RunContext):
    return main_window._prepare_run(
        run, progress_callback=lambda frac: None, status_callback=lambda text: None
    )


def test_malformed_chatlog_raises_run_error_with_its_message(tmp_path):
    with patch.object(
        main_window.chatlog, "parse_message_groups",
        side_effect=ValueError("Could not parse export timezone from 'bogus'."),
    ):
        with pytest.raises(main_window._RunError) as exc_info:
            _prepare(_run_context(tmp_path))

    assert str(exc_info.value) == "Could not parse export timezone from 'bogus'."


def test_unreadable_html_file_raises_run_error(tmp_path):
    run = _run_context(tmp_path)
    run.html_path.unlink()

    with pytest.raises(main_window._RunError, match="Could not read HTML file"):
        _prepare(run)


def test_unexpected_parse_exception_raises_run_error(tmp_path):
    with patch.object(
        main_window.chatlog, "parse_message_groups", side_effect=KeyError("src"),
    ):
        with pytest.raises(main_window._RunError, match="KeyError"):
            _prepare(_run_context(tmp_path))


def test_prepare_run_ocrs_the_images_the_kept_messages_reference(tmp_path):
    entries = [
        main_window.chatlog.MessageEntry(message_id="1", text_lines=[], image_names=["a.png"]),
    ]
    calls = []

    def fake_batch(image_folder, image_names, use_cache, progress_callback=None):
        calls.append((image_folder, image_names, use_cache))
        return pipeline.OcrBatchResult(file_info={})

    with (
        patch.object(main_window.chatlog, "parse_message_groups", return_value=entries),
        patch.object(main_window.pipeline, "run_ocr_batch", side_effect=fake_batch),
    ):
        result_entries, _ = _prepare(_run_context(tmp_path))

    assert result_entries == entries
    assert calls == [(str(tmp_path), ["a.png"], True)]


def test_poll_worker_events_runs_queued_callbacks_until_the_final_one():
    events = queue.Queue()
    ran = []
    rescheduled = []
    stub = SimpleNamespace(
        root=SimpleNamespace(after=lambda ms, func, *args: rescheduled.append(args)),
        _poll_worker_events=None,  # only passed to after(), never called here
    )
    events.put((ran.append, ("progress",), False))

    App._poll_worker_events(stub, events)
    assert ran == ["progress"]
    assert len(rescheduled) == 1  # worker not finished yet: poll again

    events.put((ran.append, ("done",), True))
    events.put((ran.append, ("never",), False))
    App._poll_worker_events(stub, events)
    assert ran == ["progress", "done"]
    assert len(rescheduled) == 1  # final callback ran: polling stops

@pytest.mark.gui
class TestOnOcrDone:
    # Builds a real App (and so a real Tk root) - excluded from the default
    # run (see pyproject.toml's addopts) since the brief window it creates
    # can flash on screen; run with `-m gui` to include it.

    @pytest.fixture
    def app(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            pytest.skip(f"no display available for Tk: {exc}")
        root.withdraw()
        instance = App(root)
        yield instance
        root.destroy()

    def test_build_failure_reports_error_instead_of_raising(self, app, tmp_path):
        app._run = _run_context(tmp_path)
        reported_errors = []
        app._on_run_error = reported_errors.append

        with patch.object(
            main_window.review_item, "build_review_items", side_effect=KeyError("boom"),
        ):
            app._on_ocr_done([], pipeline.OcrBatchResult(file_info={}))  # must not raise

        assert len(reported_errors) == 1
        assert "KeyError" in reported_errors[0]

    def test_missing_images_warn_then_continue_to_review(self, app, tmp_path):
        app._run = _run_context(tmp_path)
        shown = []
        app._show_review = lambda: shown.append(True)
        result = pipeline.OcrBatchResult(
            file_info={}, missing_images=[f"{i}.png" for i in range(12)]
        )

        with patch.object(main_window.messagebox, "showwarning") as showwarning:
            app._on_ocr_done([], result)

        (title, message), _ = showwarning.call_args
        assert title == "Missing images"
        assert message.startswith("12 image(s)")
        assert "9.png" in message and "10.png" not in message
        assert "...and 2 more" in message
        assert shown == [True]
