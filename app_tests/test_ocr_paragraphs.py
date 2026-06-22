from gui_transcription.app.ocr import split_into_paragraphs


def test_single_paragraph_no_blank_lines():
    raw = "line one\nline two"
    assert split_into_paragraphs(raw) == ["line one line two"]


def test_multiple_paragraphs_separated_by_blank_line():
    # Matches the original script's naive splitting: a trailing space is left
    # on the paragraph before the blank-line break (artifact of the
    # "\n\n" -> " %10 %10" substitution), which this rewrite intentionally
    # preserves for behavior parity.
    raw = "first paragraph\nstill first\n\nsecond paragraph"
    assert split_into_paragraphs(raw) == ["first paragraph still first ", "second paragraph"]
