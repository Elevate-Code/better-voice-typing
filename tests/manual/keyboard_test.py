"""Interactive Caps Lock hotkey check on a real keyboard.

Drives the PRODUCTION state machine (modules/hotkey.py) through a real pynput
hook so what you see here is what the app does — the logic is not duplicated.
Use it to confirm suppression, Ctrl+Caps pass-through, and LED correction on
a specific machine or keyboard; the message-level rules are covered by
tests/test_hotkey.py.

    .\\.venv\\Scripts\\python.exe tests\\manual\\keyboard_test.py
"""
import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pynput import keyboard  # noqa: E402

from modules.hotkey import VK_CAPITAL, CapsLockHotkey, HotkeyAction  # noqa: E402

hotkey = CapsLockHotkey()
recording = False


def correct_caps_lock_state() -> None:
    if ctypes.windll.user32.GetKeyState(VK_CAPITAL) & 1:
        KEYEVENTF_KEYUP = 0x0002
        ctypes.windll.user32.keybd_event(VK_CAPITAL, 0x3A, 0, 0)
        ctypes.windll.user32.keybd_event(VK_CAPITAL, 0x3A, KEYEVENTF_KEYUP, 0)
        print("  corrected accidental Caps Lock activation")


def win32_event_filter(msg: int, data) -> bool:
    global recording
    action = hotkey.handle(msg, data.vkCode, data.flags)
    if action is HotkeyAction.TOGGLE:
        recording = not recording
        print(f"TOGGLE -> recording {'started' if recording else 'stopped'}")
        correct_caps_lock_state()
    elif action is HotkeyAction.PASS_THROUGH:
        print("PASS_THROUGH (OS receives this Caps Lock event)")
    if action in (HotkeyAction.TOGGLE, HotkeyAction.SUPPRESS):
        listener.suppress_event()
    return True


listener = keyboard.Listener(win32_event_filter=win32_event_filter, suppress=False)

if __name__ == "__main__":
    print("Caps Lock hotkey check (production state machine)")
    print("  Caps Lock         -> one TOGGLE per press, LED must not change")
    print("  hold Caps Lock    -> exactly one TOGGLE")
    print("  Ctrl + Caps Lock  -> PASS_THROUGH, LED toggles, no TOGGLE")
    print("  Ctrl+C to exit")
    listener.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        listener.stop()
        print("\nDone")
