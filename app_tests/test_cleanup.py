from gui_transcription.app.cleanup import clean_transcript


def test_pipe_replaced_with_capital_i():
    assert clean_transcript("Health: ||||") == "Health: IIII"


def test_roll_line_excess_newlines_collapsed():
    text = "%roll 1d100\n\n\n\nresult\n"
    cleaned = clean_transcript(text)
    assert cleaned.startswith("%roll 1d100\nresult")


def test_draw_line_excess_newlines_collapsed():
    text = "%draw something\n\n\n\nresult\n"
    cleaned = clean_transcript(text)
    assert cleaned.startswith("%draw something\nresult")


def test_excess_blank_lines_collapsed_to_three():
    text = "a\n\n\n\n\n\nb"
    assert clean_transcript(text) == "a\n\n\n\nb"


def test_stray_literal_backslash_n_removed():
    text = "before\\nafter"
    assert clean_transcript(text) == "beforeafter"


def test_trailing_break_markers_trimmed():
    # Only the [BREAK] markers themselves (and whitespace between/after them)
    # are stripped; blank lines before the first marker are untouched -
    # matches the original script's regex, which anchors from [BREAK] to end.
    text = "content\n\n\n[BREAK]\n\n\n[BREAK]\n\n"
    assert clean_transcript(text) == "content\n\n\n"
