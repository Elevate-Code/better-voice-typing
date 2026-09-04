"""Win32 console helper (the recording overlay and DPI handling live in Qt now)."""
import ctypes
import sys


def hide_console_window() -> bool:
    """Hide the console window on Windows. Returns True if hidden."""
    if sys.platform != 'win32':
        return False
    try:
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
        return True
    except Exception:
        return False
