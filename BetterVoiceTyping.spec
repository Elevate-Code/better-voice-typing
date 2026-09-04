# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build: one-folder, windowed.

    uv run pyinstaller BetterVoiceTyping.spec --noconfirm

Produces dist/BetterVoiceTyping/ (BetterVoiceTyping.exe + _internal/). The
Inno Setup script in installer/ packages that folder. One-folder rather than
one-file: no unpack-to-temp on every launch, faster start, and a real folder
to inspect when something goes wrong.
"""
from PyInstaller.utils.hooks import collect_data_files

# Read-only resources the app locates via modules/paths.py APP_DIR
datas = [
    ('assets', 'assets'),
    ('.env.example', '.'),
    ('version.txt', '.'),
    ('CHANGELOG.json', '.'),
]
# soundcard reads its cffi header (mediafoundation.py.h) from its package dir
datas += collect_data_files('soundcard')

hiddenimports = [
    'soundcard.mediafoundation',
    'pynput.keyboard._win32',
    'pynput.mouse._win32',
    'pystray._win32',
]

a = Analysis(
    ['voice_typing.pyw'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['unittest', 'pydoc', 'doctest', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BetterVoiceTyping',
    icon='assets/app.ico',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='BetterVoiceTyping',
)
