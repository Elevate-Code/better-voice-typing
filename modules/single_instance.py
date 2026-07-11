"""Single-instance enforcement via a named Windows mutex.

Two running copies of the app mean two low-level keyboard hooks fighting over
Caps Lock and two writers to the same temp audio file, so a second launch must
exit early. Restart flows release the mutex before spawning the replacement;
the acquire retry below covers any remaining hand-off gap.
"""
import ctypes
import logging
import sys
import time
from typing import Optional

logger = logging.getLogger('voice_typing')

_MUTEX_NAME = "Local\\BetterVoiceTyping_SingleInstance"
_ERROR_ALREADY_EXISTS = 183
# How long to keep retrying when the previous instance is still shutting down
# (e.g. during a restart hand-off)
_ACQUIRE_RETRY_SECONDS = 3.0
_ACQUIRE_RETRY_INTERVAL = 0.3


def acquire_single_instance_lock() -> Optional[int]:
    """
    Try to become the single running instance.

    Returns a mutex handle to keep for the process lifetime (the OS releases it
    automatically on exit), or None if another instance is already running.
    Fails open (returns a sentinel handle of -1) if the Win32 call itself errors,
    so a mutex problem can never block the app from starting.
    """
    if sys.platform != 'win32':
        return -1

    try:
        kernel32 = ctypes.windll.kernel32
        deadline = time.monotonic() + _ACQUIRE_RETRY_SECONDS
        while True:
            handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
            if not handle:
                logger.warning("CreateMutexW failed; skipping single-instance check")
                return -1
            if kernel32.GetLastError() != _ERROR_ALREADY_EXISTS:
                return handle
            kernel32.CloseHandle(handle)
            if time.monotonic() >= deadline:
                return None
            time.sleep(_ACQUIRE_RETRY_INTERVAL)
    except Exception as e:
        logger.warning(f"Single-instance check failed ({e}); continuing anyway")
        return -1


def release_single_instance_lock(handle: Optional[int]) -> None:
    """Release the mutex so a replacement instance can start (used before restart)."""
    if not handle or handle == -1 or sys.platform != 'win32':
        return
    try:
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass
