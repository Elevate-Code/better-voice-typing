"""Keep the tray icon always visible instead of hidden in the overflow menu.

Windows 11 hides every new notification-area icon behind the "^" overflow
until the user promotes it. This app's tray icon is its only UI, and its
identity is the executable path, so a fresh install (or moving from the
source checkout to the installed exe) starts hidden again.

Per-icon visibility lives at
``HKCU\\Control Panel\\NotifyIconSettings\\<id>`` with ``ExecutablePath``
and ``IsPromoted`` (DWORD 1 = always visible). The entry is created by
Explorer when the icon first appears, so callers poll shortly after startup.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger('voice_typing')

_SETTINGS_KEY = r"Control Panel\NotifyIconSettings"


def promote_tray_icon(executable: str) -> Optional[bool]:
    """Make the tray icon for ``executable`` always visible.

    Returns True if a matching entry exists and is promoted (already, or
    just now), False if no entry exists yet (icon not registered by
    Explorer), None if the registry could not be read (non-Windows, or
    a Windows version without this key)."""
    try:
        import winreg
    except ImportError:
        return None
    wanted = os.path.normcase(os.path.abspath(executable))
    found = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SETTINGS_KEY) as root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name, 0,
                                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as entry:
                        try:
                            exe, _ = winreg.QueryValueEx(entry, "ExecutablePath")
                        except FileNotFoundError:
                            continue
                        if os.path.normcase(os.path.abspath(str(exe))) != wanted:
                            continue
                        found = True
                        try:
                            promoted, _ = winreg.QueryValueEx(entry, "IsPromoted")
                        except FileNotFoundError:
                            promoted = 0
                        if promoted != 1:
                            winreg.SetValueEx(entry, "IsPromoted", 0, winreg.REG_DWORD, 1)
                            logger.info("Pinned the tray icon as always visible")
                except OSError:
                    continue
    except OSError:
        return None
    return found
