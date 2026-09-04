"""Deliver transcribed text to the focused application.

One strategy, the one that works everywhere: put the text on the clipboard,
send Ctrl+V, then restore whatever the clipboard held before. Before 1.0 this
was a pluggable "output provider" system that loaded arbitrary Python from
Documents\\VoiceTyping\\plugins; it was removed with the 1.0 breaking release.
"""
import logging
import threading
from typing import Callable

import pyperclip
from pynput.keyboard import Controller, Key

logger = logging.getLogger('voice_typing')

# Serializes clipboard + keystroke sequences so two deliveries (a session
# chunk landing while a dictation pastes) can't interleave
_paste_lock = threading.Lock()
_keyboard = Controller()


def paste_text(text: str, restore_delay_ms: int,
               schedule: Callable[[int, Callable[[], None]], object]) -> None:
    """Paste ``text`` at the cursor via the clipboard and Ctrl+V.

    ``schedule(delay_ms, fn)`` (Tk's ``root.after``) runs the clipboard
    restore later: slow paste targets read the clipboard late, so restoring
    immediately would paste the user's old clipboard instead of the text.
    """
    with _paste_lock:
        original_clipboard = pyperclip.paste()
        pyperclip.copy(text)
        with _keyboard.pressed(Key.ctrl):
            _keyboard.press('v')
            _keyboard.release('v')
        # pyperclip only round-trips text: an empty paste() means the
        # clipboard held non-text (image/files) or nothing — restoring would
        # clobber it with emptiness, so leave the transcript there instead
        if original_clipboard:
            schedule(restore_delay_ms, lambda: pyperclip.copy(original_clipboard))
