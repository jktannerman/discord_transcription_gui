from gui_transcription.app.cleanup import clean_transcript

# The "|" -> "I" OCR-misread fix that used to be covered here moved to
# ocr_corrections.py (see app/ocr_corrections.txt and
# test_ocr_corrections.py) - it now runs per-image, before the user ever
# sees the text, rather than over the whole output file at Finalize time.


def test_deliberately_large_gap_is_not_collapsed():
    # Blank-line spacing is now entirely owned by the review screen's
    # spacer slots (see review_item.ReviewItem.slot_roles), so a gap larger
    # than the old 3-blank-line cap is left exactly as written rather than
    # being clobbered back down.
    text = "a\n\n\n\n\n\nb"
    assert clean_transcript(text) == text


def test_stray_literal_backslash_n_removed():
    text = "before\\nafter"
    assert clean_transcript(text) == "beforeafter"


def test_trailing_break_markers_trimmed():
    # Only the [BREAK] markers themselves (and whitespace between/after them)
    # are stripped; blank lines before the first marker are untouched -
    # matches the original script's regex, which anchors from [BREAK] to end.
    text = "content\n\n\n[BREAK]\n\n\n[BREAK]\n\n"
    assert clean_transcript(text) == "content\n\n\n"
