"""'Start when I sign in' for the installed app.

The installer offers the same thing as a task; both use a shortcut in the
user's Startup folder, so the setting reflects whichever created it. Only
meaningful for the frozen build (a source checkout is started by hand).
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

from modules.paths import APP_NAME, FROZEN

logger = logging.getLogger('voice_typing')


def _shortcut_path() -> Path:
    return Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / \
        'Programs' / 'Startup' / f'{APP_NAME}.lnk'


def available() -> bool:
    return FROZEN and bool(os.environ.get('APPDATA'))


def is_enabled() -> bool:
    return _shortcut_path().exists()


def set_enabled(enabled: bool) -> bool:
    """Create or remove the Startup shortcut. Returns True on success."""
    link = _shortcut_path()
    try:
        if not enabled:
            if link.exists():
                link.unlink()
            return True
        link.parent.mkdir(parents=True, exist_ok=True)
        exe = sys.executable
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}'); "
            "$s.TargetPath = '{exe}'; $s.WorkingDirectory = '{cwd}'; $s.Save()"
        ).format(link=str(link).replace("'", "''"), exe=exe.replace("'", "''"),
                 cwd=str(Path(exe).parent).replace("'", "''"))
        subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                       check=True, capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return link.exists()
    except Exception as e:
        logger.error(f"Could not update the startup shortcut: {e}")
        return False
