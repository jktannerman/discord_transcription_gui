from discord_transcription.ocr import split_into_paragraphs


def test_single_paragraph_no_blank_lines():
    raw = "line one\nline two"
    assert split_into_paragraphs(raw) == ["line one line two"]


def test_multiple_paragraphs_separated_by_blank_line():
    raw = "first paragraph\nstill first\n\nsecond paragraph"
    assert split_into_paragraphs(raw) == ["first paragraph still first", "second paragraph"]


def test_run_of_three_or_more_spaces_is_fully_collapsed():
    # A single non-overlapping-pass "  " -> " " replace (the old
    # implementation) leaves a run of 3 spaces as 2 instead of 1 - this
    # covers the regex-based replacement that collapses any whitespace run.
    raw = "first   second"
    assert split_into_paragraphs(raw) == ["first second"]


def test_blank_line_with_trailing_whitespace_still_separates_paragraphs():
    # "\n \n" (blank line with stray trailing whitespace) - not just an
    # exact "\n\n" - should still count as a paragraph break.
    raw = "first paragraph\n \nsecond paragraph"
    assert split_into_paragraphs(raw) == ["first paragraph", "second paragraph"]
