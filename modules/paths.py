"""Where the app lives and where its data lives.

Works the same for a source checkout (``python voice_typing.pyw``) and a
frozen PyInstaller one-folder build (``BetterVoiceTyping.exe`` with its
``_internal`` folder). Everything path-shaped should come from here.

- APP_DIR: bundled read-only resources (assets/, .env.example, version.txt).
  Frozen: ``<install>\\_internal``; source: the repo root.
- INSTALL_DIR: the folder holding the executable (frozen) or the repo root.
- USER_DATA_DIR: durable user data — settings.json, .env, history.json, logs.
  Lives in Documents so it survives updates, reinstalls and git operations.
- LOCAL_DATA_DIR: disposable, machine-local data — in-progress recordings and
  update downloads — under %LOCALAPPDATA%, never inside the program folder
  (which the installer replaces wholesale).
"""
import os
import sys
from pathlib import Path

FROZEN: bool = bool(getattr(sys, 'frozen', False))

APP_DIR: Path = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
INSTALL_DIR: Path = Path(sys.executable).resolve().parent if FROZEN else APP_DIR

USER_DATA_DIR: Path = Path.home() / "Documents" / "VoiceTyping"
LOCAL_DATA_DIR: Path = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local')) / 'BetterVoiceTyping'
RECORDINGS_DIR: Path = LOCAL_DATA_DIR / 'recordings'
UPDATE_CACHE_DIR: Path = LOCAL_DATA_DIR / 'updates'

APP_NAME = "Better Voice Typing"
EXE_NAME = "BetterVoiceTyping.exe"


def app_version() -> str:
    """Bare version string from the bundled version.txt ('1.0.0'), or '0.0.0'."""
    try:
        return (APP_DIR / 'version.txt').read_text(encoding='utf-8').strip() or '0.0.0'
    except OSError:
        return '0.0.0'
