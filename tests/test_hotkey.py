"""Caps Lock hotkey state machine (modules/hotkey.py).

Contract: one physical Caps Lock press is exactly one TOGGLE, and the OS
never sees a Caps Lock event we handled. Each test replays a raw hook-message
sequence that has bitten real users: holding the key (auto-repeat), mashing
it, Ctrl chords (the escape hatch to really toggle Caps Lock), our own
injected corrective keystroke, and a lost key-up.
"""
from typing import List

from modules.hotkey import (
    LLKHF_INJECTED, STUCK_DOWN_RESET_S, VK_CAPITAL, VK_CONTROL, VK_LCONTROL,
    VK_RCONTROL, WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP,
    CapsLockHotkey, HotkeyAction as A,
)

VK_A = 0x41
VK_SHIFT = 0x10


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make() -> tuple[CapsLockHotkey, FakeClock]:
    clock = FakeClock()
    return CapsLockHotkey(clock=clock), clock


def down(hk: CapsLockHotkey, vk: int = VK_CAPITAL, flags: int = 0) -> A:
    return hk.handle(WM_KEYDOWN, vk, flags)


def up(hk: CapsLockHotkey, vk: int = VK_CAPITAL, flags: int = 0) -> A:
    return hk.handle(WM_KEYUP, vk, flags)


def tap(hk: CapsLockHotkey, vk: int = VK_CAPITAL) -> List[A]:
    return [down(hk, vk), up(hk, vk)]


# --- Plain presses -----------------------------------------------------------

def test_single_tap_toggles_once_and_swallows_both_events() -> None:
    hk, _ = make()
    assert tap(hk) == [A.TOGGLE, A.SUPPRESS]


def test_holding_the_key_never_retoggles() -> None:
    """Hold Caps Lock: Windows repeats WM_KEYDOWN every 30-500 ms. Only the
    first one may toggle; the repeats and the release are swallowed."""
    hk, clock = make()
    assert down(hk) is A.TOGGLE
    for _ in range(40):
        clock.advance(0.03)
        assert down(hk) is A.SUPPRESS
    assert up(hk) is A.SUPPRESS


def test_mashing_yields_exactly_one_toggle_per_physical_press() -> None:
    hk, clock = make()
    actions: List[A] = []
    for _ in range(50):
        actions += tap(hk)
        clock.advance(0.02)
    assert actions.count(A.TOGGLE) == 50
    assert actions.count(A.SUPPRESS) == 50
    assert A.PASS_THROUGH not in actions


def test_mashing_with_repeats_interleaved_counts_physical_presses() -> None:
    """Fast presses that each linger long enough to auto-repeat."""
    hk, clock = make()
    toggles = 0
    for _ in range(10):
        toggles += down(hk) is A.TOGGLE
        for _ in range(3):
            clock.advance(0.05)
            toggles += down(hk) is A.TOGGLE
        assert up(hk) is A.SUPPRESS
    assert toggles == 10


# --- Ctrl chord: the user wants real Caps Lock --------------------------------

def test_ctrl_caps_passes_through_to_the_os_without_toggling() -> None:
    hk, clock = make()
    assert down(hk, VK_LCONTROL) is A.IGNORE
    assert down(hk) is A.PASS_THROUGH
    for _ in range(5):
        clock.advance(0.05)
        assert down(hk) is A.PASS_THROUGH
    assert up(hk) is A.PASS_THROUGH
    assert up(hk, VK_LCONTROL) is A.IGNORE


def test_releasing_ctrl_while_caps_is_still_held_does_not_start_toggling() -> None:
    """Ctrl+Caps, then let go of Ctrl first while Caps keeps repeating. The
    old implementation started toggling recording on those repeats."""
    hk, clock = make()
    down(hk, VK_LCONTROL)
    assert down(hk) is A.PASS_THROUGH
    up(hk, VK_LCONTROL)
    for _ in range(5):
        clock.advance(0.05)
        assert down(hk) is A.PASS_THROUGH
    assert up(hk) is A.PASS_THROUGH
    # And the next plain press is a normal toggle again
    assert tap(hk) == [A.TOGGLE, A.SUPPRESS]


def test_left_and_right_ctrl_are_tracked_independently() -> None:
    """Hold both Ctrl keys, release one: the chord is still active."""
    hk, _ = make()
    down(hk, VK_LCONTROL)
    down(hk, VK_RCONTROL)
    up(hk, VK_LCONTROL)
    assert down(hk) is A.PASS_THROUGH
    assert up(hk) is A.PASS_THROUGH
    up(hk, VK_RCONTROL)
    assert tap(hk) == [A.TOGGLE, A.SUPPRESS]


def test_generic_ctrl_release_clears_every_ctrl_key() -> None:
    hk, _ = make()
    down(hk, VK_LCONTROL)
    down(hk, VK_RCONTROL)
    up(hk, VK_CONTROL)
    assert tap(hk) == [A.TOGGLE, A.SUPPRESS]


def test_ctrl_pressed_via_alt_chord_messages_is_still_tracked() -> None:
    """With Alt held, Ctrl arrives as WM_SYSKEYDOWN/UP. Missing the SYS
    key-up would leave Ctrl stuck and turn every later press into a pass-through."""
    hk, _ = make()
    assert hk.handle(WM_SYSKEYDOWN, VK_LCONTROL) is A.IGNORE
    assert hk.handle(WM_SYSKEYUP, VK_LCONTROL) is A.IGNORE
    assert tap(hk) == [A.TOGGLE, A.SUPPRESS]


# --- Injected events: our own Caps Lock correction ----------------------------

def test_injected_caps_events_pass_through_without_touching_state() -> None:
    """After a toggle the app injects a Caps Lock down/up to undo the LED.
    That must reach the OS and must not be mistaken for a release."""
    hk, clock = make()
    assert down(hk) is A.TOGGLE
    assert down(hk, flags=LLKHF_INJECTED) is A.PASS_THROUGH
    assert up(hk, flags=LLKHF_INJECTED) is A.PASS_THROUGH
    clock.advance(0.05)
    assert down(hk) is A.SUPPRESS  # still the same physical press
    assert up(hk) is A.SUPPRESS


# --- Lost key-up recovery -----------------------------------------------------

def test_lost_key_up_recovers_after_a_quiet_period() -> None:
    """The hook never saw the release (stalled hook, RDP). Without recovery
    every later press would be swallowed as a repeat and the hotkey would
    look dead until restart."""
    hk, clock = make()
    assert down(hk) is A.TOGGLE
    clock.advance(STUCK_DOWN_RESET_S + 0.1)
    assert down(hk) is A.TOGGLE
    clock.advance(0.05)
    assert down(hk) is A.SUPPRESS
    assert up(hk) is A.SUPPRESS


def test_a_long_continuous_hold_does_not_trigger_recovery() -> None:
    """Repeats at the slowest Windows repeat rate keep the hold alive."""
    hk, clock = make()
    assert down(hk) is A.TOGGLE
    for _ in range(40):  # 20 s at 500 ms
        clock.advance(0.5)
        assert down(hk) is A.SUPPRESS
    assert up(hk) is A.SUPPRESS


# --- Everything else ----------------------------------------------------------

def test_unrelated_keys_are_ignored_and_do_not_disturb_caps_state() -> None:
    hk, _ = make()
    assert down(hk, VK_A) is A.IGNORE
    assert down(hk, VK_SHIFT) is A.IGNORE
    assert down(hk) is A.TOGGLE
    assert up(hk, VK_A) is A.IGNORE
    assert up(hk) is A.SUPPRESS


def test_stray_key_up_at_startup_is_swallowed() -> None:
    """App started while Caps Lock was already held: the orphan release must
    not toggle anything."""
    hk, _ = make()
    assert up(hk) is A.SUPPRESS
    assert tap(hk) == [A.TOGGLE, A.SUPPRESS]


def test_alt_caps_is_left_to_the_os() -> None:
    hk, _ = make()
    assert hk.handle(WM_SYSKEYDOWN, VK_CAPITAL) is A.IGNORE
    assert hk.handle(WM_SYSKEYUP, VK_CAPITAL) is A.IGNORE
