"""Recording label composition (modules/ui.py, pure helpers only).

Contract: the elapsed-time label is what the user watches during every
recording; its shape ("🎤 Recording  1:05") must survive UI refactors, and
the session note must sit between the text and the clock so it reads as a
status, not a suffix. No Qt widgets are created here.
"""
from modules.ui import darken, format_recording_label


def test_label_shape_with_and_without_note() -> None:
    assert format_recording_label("🎤 Recording", "", 65) == "🎤 Recording  1:05"
    assert format_recording_label("🎧 Meeting", "⏳ 2 queued", 7) == "🎧 Meeting  ⏳ 2 queued  0:07"
    assert format_recording_label("🎤 Recording", "", -3) == "🎤 Recording  0:00"


def test_darken_keeps_hex_shape_and_rejects_garbage() -> None:
    assert darken('#ff0000', 0.5) == '#7f0000'
    assert darken('not a color') == '#000000'
