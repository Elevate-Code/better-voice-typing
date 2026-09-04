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
]

# Qt modules the app never imports (their DLLs would otherwise ride along)
qt_excludes = [
    'PySide6.QtNetwork', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets', 'PySide6.QtQml',
    'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtSvg', 'PySide6.QtSvgWidgets',
    'PySide6.QtPdf', 'PySide6.QtPrintSupport', 'PySide6.QtSql', 'PySide6.QtTest',
    'PySide6.QtXml', 'PySide6.QtConcurrent', 'PySide6.QtDBus', 'PySide6.QtDesigner',
    'PySide6.QtHelp', 'PySide6.QtUiTools',
]

a = Analysis(
    ['voice_typing.pyw'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['unittest', 'pydoc', 'doctest', 'pytest', 'tkinter'] + qt_excludes,
    noarchive=False,
)

# Trim Qt payload we never use: the software OpenGL fallback (20 MB; widgets
# render through the raster engine), Qt's translations, and DLLs of the
# excluded modules that the PySide6 hook still collects.
_qt_dll_drop = ('opengl32sw.dll', 'qt6network', 'qt6opengl', 'qt6qml', 'qt6quick', 'qt6svg',
                'qt6pdf', 'qt6printsupport', 'qt6sql', 'qt6test', 'qt6xml', 'qt6concurrent',
                'qt6designer', 'qt6help', 'qt6uitools', 'qt6virtualkeyboard', 'qt6labs',
                'qt6websockets', 'qt6shadertools', 'qt6multimedia', 'qt6charts', 'qt6datavis',
                'qt6positioning', 'qt6sensors', 'qt6serialport', 'qt6remoteobjects',
                'qt6statemachine', 'qt6scxml', 'qt6nfc', 'qt6bluetooth', 'qt6texttospeech',
                'qt6webchannel', 'qt6httpserver', 'qt6graphs', 'qt6spatialaudio', 'qt63d',
                'qt6location', 'qt6webview', 'qt6examplesassetdownloader')


def _keep(entry):
    name = entry[0].replace(chr(92), '/').lower()
    base = name.rsplit('/', 1)[-1]
    if 'pyside6/translations/' in name:
        return False
    if base == 'opengl32sw.dll' or any(base.startswith(p) for p in _qt_dll_drop):
        return False
    return True


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]
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
