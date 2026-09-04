"""Caps Lock hotkey interpretation as a pure state machine.

The Windows low-level keyboard hook (pynput's ``win32_event_filter``) delivers
raw WM_KEYDOWN / WM_KEYUP messages: OS auto-repeat while a key is held, the
Ctrl+Caps chord users press to *really* toggle Caps Lock, and the corrective
keystroke the app injects to undo an accidental Caps Lock. This module decides
what each message means. The hook callback in ``voice_typing.pyw`` is a thin
adapter that performs the side effects (spawn the toggle, suppress the event).

Keeping the decision pure and clock-injectable lets ``tests/test_hotkey.py``
replay every sequence that has bitten real users — mashing, holding, chords,
lost key-ups — without a keyboard. If you change the rules here, add the
sequence that motivated the change to the tests.
"""
import time
from enum import Enum, auto
from typing import Callable

VK_CONTROL = 0x11    # Generic Ctrl (rare from a low-level hook, but possible)
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_CAPITAL = 0x14
CTRL_KEYS = frozenset((VK_CONTROL, VK_LCONTROL, VK_RCONTROL))

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104  # Key down while Alt is held
WM_SYSKEYUP = 0x0105

LLKHF_INJECTED = 0x10   # KBDLLHOOKSTRUCT.flags: event came from SendInput/keybd_event

# Windows auto-repeat fires at least every ~500 ms (the slowest repeat rate),
# and the first repeat arrives within 1 s. A "held" Caps Lock that goes quiet
# longer than this means the key-up was never delivered (hook stalled, RDP or
# VM quirk, event lost). Without recovery every later press would be treated
# as a repeat and swallowed, and the hotkey would look dead until restart.
STUCK_DOWN_RESET_S = 1.5


class HotkeyAction(Enum):
    IGNORE = auto()        # Not a key we handle; nothing to do
    PASS_THROUGH = auto()  # Caps Lock event the OS should receive (Ctrl+Caps, injected)
    SUPPRESS = auto()      # Swallow silently (auto-repeat, key-up of a handled press)
    TOGGLE = auto()        # Fresh physical press: toggle recording and swallow the event


class CapsLockHotkey:
    """Maps raw hook messages to one TOGGLE per physical Caps Lock press.

    Rules:
    - Plain Caps Lock press: TOGGLE on the first key-down; every auto-repeat
      key-down and the key-up are SUPPRESSed so the OS never toggles caps.
    - Ctrl (either side) + Caps Lock: the whole press, repeats and release
      included, PASS_THROUGH to the OS so the user can toggle real Caps Lock.
      This holds even if Ctrl is released before Caps Lock is.
    - Injected Caps Lock events (our own corrective keystroke) PASS_THROUGH
      without touching state.
    - A key-down arriving while we still think the key is held, but after a
      gap longer than any auto-repeat interval, is treated as a fresh press.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._ctrl_down: set[int] = set()
        self._caps_down = False
        self._caps_passthrough = False
        self._last_down_at = 0.0

    def handle(self, msg: int, vk: int, flags: int = 0) -> HotkeyAction:
        if vk in CTRL_KEYS:
            if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                self._ctrl_down.add(vk)
            elif msg in (WM_KEYUP, WM_SYSKEYUP):
                if vk == VK_CONTROL:
                    self._ctrl_down.clear()
                else:
                    self._ctrl_down.discard(vk)
            return HotkeyAction.IGNORE

        if vk != VK_CAPITAL:
            return HotkeyAction.IGNORE

        if flags & LLKHF_INJECTED:
            return HotkeyAction.PASS_THROUGH

        if msg == WM_KEYDOWN:
            return self._key_down()
        if msg == WM_KEYUP:
            return self._key_up()
        # Alt+Caps (WM_SYSKEY*) is left to the OS, as it always has been
        return HotkeyAction.IGNORE

    def _key_down(self) -> HotkeyAction:
        now = self._clock()
        stuck = self._caps_down and (now - self._last_down_at) >= STUCK_DOWN_RESET_S
        if self._caps_down and not stuck:
            # OS auto-repeat of a press we already handled
            self._last_down_at = now
            return (HotkeyAction.PASS_THROUGH if self._caps_passthrough
                    else HotkeyAction.SUPPRESS)

        self._caps_down = True
        self._last_down_at = now
        if self._ctrl_down:
            self._caps_passthrough = True
            return HotkeyAction.PASS_THROUGH
        self._caps_passthrough = False
        return HotkeyAction.TOGGLE

    def _key_up(self) -> HotkeyAction:
        self._caps_down = False
        if self._caps_passthrough:
            self._caps_passthrough = False
            return HotkeyAction.PASS_THROUGH
        # Also covers a stray key-up we never saw the key-down for (app started
        # with the key held): Caps Lock toggles on key-down, so swallowing the
        # release is harmless.
        return HotkeyAction.SUPPRESS
