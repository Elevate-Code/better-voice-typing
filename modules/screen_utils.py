import sys
import ctypes
from typing import Optional, NamedTuple, List


class MonitorGeometry(NamedTuple):
    left: int
    top: int
    right: int
    bottom: int
    width: int
    height: int


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong),
                ("rcMonitor", _RECT),
                ("rcWork", _RECT),
                ("dwFlags", ctypes.c_ulong)]


def _geometry_of(hmonitor: int) -> Optional[MonitorGeometry]:
    """MonitorGeometry for a monitor handle, or None if Windows won't say."""
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if not ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi)):
        return None
    r = mi.rcMonitor
    return MonitorGeometry(left=r.left, top=r.top, right=r.right, bottom=r.bottom,
                           width=r.right - r.left, height=r.bottom - r.top)


def get_primary_monitor_geometry() -> Optional[MonitorGeometry]:
    """Geometry of the primary monitor, or None off-Windows / on any ctypes failure."""
    if sys.platform != 'win32':
        return None
    try:
        MONITOR_DEFAULTTOPRIMARY = 1
        handle = ctypes.windll.user32.MonitorFromPoint(0, 0, MONITOR_DEFAULTTOPRIMARY)
        return _geometry_of(handle)
    except Exception:
        return None


def get_all_monitor_geometries() -> List[MonitorGeometry]:
    """Geometry of every monitor, or an empty list off-Windows / on failure."""
    if sys.platform != 'win32':
        return []
    try:
        monitors: List[MonitorGeometry] = []

        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int,           # BOOL return
            ctypes.c_void_p,        # hMonitor
            ctypes.c_void_p,        # hdcMonitor
            ctypes.POINTER(_RECT),  # lprcMonitor
            ctypes.c_long,          # dwData
        )

        def on_monitor(hmonitor, hdc, rect_ptr, data) -> int:
            geometry = _geometry_of(hmonitor)
            if geometry:
                monitors.append(geometry)
            return 1  # continue enumeration

        ctypes.windll.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(on_monitor), 0)
        return monitors
    except Exception:
        return []


def set_process_dpi_awareness() -> bool:
    """Attempt to set the process DPI awareness on Windows. Returns True if successful."""
    if sys.platform != 'win32':
        return False
    try:
        # Per-monitor DPI awareness (Windows 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def hide_console_window() -> bool:
    """Hide the console window on Windows. Returns True if hidden."""
    if sys.platform != 'win32':
        return False
    try:
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
        return True
    except Exception:
        return False
