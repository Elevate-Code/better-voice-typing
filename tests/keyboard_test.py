import ctypes
import time
from pynput import keyboard

# Initialize state
recording = False
ctrl_pressed = False
caps_down = False
caps_passthrough = False

def win32_event_filter(msg, data):
    global ctrl_pressed, recording, caps_down, caps_passthrough

    VK_CONTROL = 0x11
    VK_LCONTROL = 0xA2
    VK_RCONTROL = 0xA3
    VK_CAPITAL = 0x14

    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101

    LLKHF_INJECTED = 0x10

    if data.vkCode in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL):
        if msg == WM_KEYDOWN:
            ctrl_pressed = True
        elif msg == WM_KEYUP:
            ctrl_pressed = False
        return True

    if data.vkCode == VK_CAPITAL:
        if data.flags & LLKHF_INJECTED:
            return True

        if msg == WM_KEYDOWN:
            if ctrl_pressed:
                caps_passthrough = True
                return True

            if caps_down:
                listener.suppress_event()

            caps_down = True
            caps_passthrough = False
            recording = not recording
            print(f"Recording {'started' if recording else 'stopped'}")
            correct_caps_lock_state()
            listener.suppress_event()

        elif msg == WM_KEYUP:
            caps_down = False
            if caps_passthrough:
                caps_passthrough = False
                return True
            listener.suppress_event()

    return True

def correct_caps_lock_state():
    VK_CAPITAL = 0x14
    if ctypes.windll.user32.GetKeyState(VK_CAPITAL) & 1:
        KEYEVENTF_KEYUP = 0x0002
        ctypes.windll.user32.keybd_event(VK_CAPITAL, 0x3A, 0, 0)
        ctypes.windll.user32.keybd_event(VK_CAPITAL, 0x3A, KEYEVENTF_KEYUP, 0)
        print("Corrected accidental Caps Lock activation")

listener = keyboard.Listener(
    win32_event_filter=win32_event_filter,
    suppress=False
)

if __name__ == "__main__":
    print("Starting keyboard shortcut test...")
    print("Press Caps Lock to toggle recording (Caps Lock state will not change)")
    print("Press Ctrl + Caps Lock to toggle Caps Lock state (Recording will not change)")
    print("Press Ctrl+C to exit")
    listener.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        listener.stop()
        if recording:
            print("Stopping recording...")
            recording = False
        print("\nTest ended")
