"""System tray icon and its context menu (Qt).

The menu is described as a plain ``MenuItem`` tree (testable without Qt) and
rendered into a ``QMenu`` every time it is about to be shown, so checkmarks
and device lists are always current. The icon is a microphone glyph painted
at the current status color, so the tray always matches the overlay.
Explorer restarts are handled by Qt (it re-adds the icon on TaskbarCreated).
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import pyperclip
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from modules import transcribe
from modules.audio_manager import (create_device_identifier, get_default_device_id,
                                   get_input_devices, names_match)
from modules.logger import get_log_dir
from modules.paths import APP_NAME, app_version
from modules.settings import ENV_FILE

logger = logging.getLogger('voice_typing')

UI_POSITIONS = [
    ('Top Left', 'top-left'), ('Top Center', 'top-center'), ('Top Right', 'top-right'),
    ('Bottom Left', 'bottom-left'), ('Bottom Center', 'bottom-center'),
    ('Bottom Right', 'bottom-right'),
]


@dataclass
class MenuItem:
    """One entry of the tray menu. ``checked`` None = no checkmark slot;
    ``children`` makes a submenu; ``separator`` ignores everything else."""
    label: str = ''
    action: Optional[Callable[[], None]] = None
    checked: Optional[bool] = None
    enabled: bool = True
    children: Optional[List['MenuItem']] = None
    default: bool = False
    separator: bool = False


SEPARATOR = MenuItem(separator=True)


def make_position_items(get_position: Callable[[], Optional[str]],
                        set_position: Callable[[str], None]) -> List[MenuItem]:
    """Radio-style items for the indicator corner."""
    current = get_position()

    def item(label: str, pos: str) -> MenuItem:
        return MenuItem(label, lambda: set_position(pos), checked=(current == pos))
    return [item(label, pos) for label, pos in UI_POSITIONS]


def populate_menu(menu: QMenu, items: List[MenuItem]) -> None:
    """Render a MenuItem tree into a QMenu (replacing its contents)."""
    menu.clear()
    for item in items:
        if item.separator:
            menu.addSeparator()
            continue
        if item.children is not None:
            sub = menu.addMenu(item.label)
            sub.setEnabled(item.enabled)
            populate_menu(sub, item.children)
            continue
        action = QAction(item.label, menu)
        action.setEnabled(item.enabled)
        if item.checked is not None:
            action.setCheckable(True)
            action.setChecked(item.checked)
        if item.action is not None:
            action.triggered.connect(lambda _checked=False, fn=item.action: fn())
        if item.default:
            font = action.font()
            font.setBold(True)
            action.setFont(font)
            menu.setDefaultAction(action)
        menu.addAction(action)


# ---- icon ----------------------------------------------------------------

def dark_taskbar() -> bool:
    """Windows: is the taskbar/tray dark (so a light glyph is needed)?"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            value, _ = winreg.QueryValueEx(k, "SystemUsesLightTheme")
            return int(value) == 0
    except Exception:
        return True


def tray_color(ui_color: str, idle: bool) -> str:
    """Glyph color for a status: idle follows the taskbar theme; active
    statuses use the overlay color, lightened when it would vanish on a
    dark taskbar."""
    dark = dark_taskbar()
    if idle:
        return '#F2F2F2' if dark else '#1F1F1F'
    c = QColor(ui_color)
    if dark and c.lightnessF() < 0.3:
        c = c.lighter(180)
    return c.name()


def _paint_mic(pixmap: QPixmap, color: str) -> None:
    s = pixmap.width()
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    col = QColor(color)
    # Capsule
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(col)
    cap = QRectF(s * 0.35, s * 0.06, s * 0.30, s * 0.52)
    p.drawRoundedRect(cap, s * 0.15, s * 0.15)
    # Cradle arc, stem and base
    pen = QPen(col, max(1.5, s * 0.09), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    arc = QRectF(s * 0.20, s * 0.22, s * 0.60, s * 0.52)
    p.drawArc(arc, 180 * 16, 180 * 16)
    p.drawLine(QPointF(s * 0.5, s * 0.74), QPointF(s * 0.5, s * 0.88))
    p.drawLine(QPointF(s * 0.34, s * 0.90), QPointF(s * 0.66, s * 0.90))
    p.end()


_icon_cache: Dict[str, QIcon] = {}


def make_tray_icon(color: str) -> QIcon:
    """Microphone glyph in ``color`` at the sizes Windows asks for."""
    icon = _icon_cache.get(color)
    if icon is None:
        icon = QIcon()
        for size in (16, 20, 24, 32, 48, 64):
            pm = QPixmap(size, size)
            pm.fill(Qt.GlobalColor.transparent)
            _paint_mic(pm, color)
            icon.addPixmap(pm)
        _icon_cache[color] = icon
    return icon


# ---- menu model ----------------------------------------------------------

def create_copy_menu(app) -> List[MenuItem]:
    """Recent transcriptions; clicking one copies it."""
    def copier(text: str) -> Callable[[], None]:
        return lambda: pyperclip.copy(text)
    return [MenuItem(app.history.get_preview(text), copier(text))
            for text in app.history.get_recent()]


def create_microphone_menu(app) -> List[MenuItem]:
    """Device selection and favorites.

    Devices are matched to saved settings by name rather than full identifier
    (name, channels, samplerate): the same physical device can report
    different specs via different host APIs, and exact-tuple equality made
    selection checkmarks and favorites silently stop matching."""
    devices = sorted(get_input_devices(), key=lambda d: d['name'].lower())
    try:
        default_device_id = get_default_device_id()
    except Exception:
        logger.warning("Could not determine default input device", exc_info=True)
        default_device_id = None

    def is_favorite(device: Dict[str, any]) -> bool:
        return any(isinstance(f, dict) and names_match(f.get('name') or '', device['name'])
                   for f in app.settings.get('favorite_microphones'))

    def is_selected(device: Dict[str, any]) -> bool:
        selected = app.settings.get('selected_microphone')
        return isinstance(selected, dict) and names_match(selected.get('name') or '', device['name'])

    def select(device: Dict[str, any]) -> Callable[[], None]:
        return lambda: app.set_microphone(device['id'])

    def toggle_favorite(device: Dict[str, any]) -> Callable[[], None]:
        def handler() -> None:
            favorites = [f for f in app.settings.get('favorite_microphones') if isinstance(f, dict)]
            if is_favorite(device):
                favorites = [f for f in favorites
                             if not names_match(f.get('name') or '', device['name'])]
            else:
                favorites.append(create_device_identifier(device)._asdict())
            app.settings.set('favorite_microphones', favorites)
        return handler

    def select_item(device: Dict[str, any]) -> MenuItem:
        prefix = ("💫 " if is_favorite(device) else "") + \
                 ("🎙️ " if device['id'] == default_device_id else "")
        return MenuItem(f"{prefix}{device['name']}", select(device), checked=is_selected(device))

    favorite_devices = [d for d in devices if is_favorite(d)]
    other_devices = [d for d in devices if not is_favorite(d)]
    select_items = [select_item(d) for d in favorite_devices]
    if favorite_devices and other_devices:
        select_items.append(SEPARATOR)
    select_items.extend(select_item(d) for d in other_devices)
    favorite_items = [MenuItem(d['name'], toggle_favorite(d), checked=is_favorite(d)) for d in devices]

    return [
        MenuItem('Select Device', children=select_items or [MenuItem('No input devices', enabled=False)]),
        MenuItem('Manage Favorites', children=favorite_items or [MenuItem('No input devices', enabled=False)]),
        MenuItem('Refresh Devices', app.refresh_microphones),
    ]


def create_stt_provider_menu(app) -> List[MenuItem]:
    """Provider (stored None = automatic) and OpenAI model selection."""
    stored_provider = app.settings.get('stt_provider')
    current_provider = transcribe.get_current_provider()
    available = transcribe.get_available_providers()

    def choose_provider(name: str) -> Callable[[], None]:
        def handler() -> None:
            try:
                transcribe.set_stt_provider(name)
            except Exception as e:
                logger.error(f"Error changing STT provider: {e}")
        return handler

    def choose_model(model: str) -> Callable[[], None]:
        return lambda: app.settings.set('openai_stt_model', model)

    resolved = next((p['display_name'] for p in available if p['name'] == current_provider),
                    current_provider)
    provider_items = [MenuItem(f'Automatic ({resolved})',
                               lambda: app.settings.set('stt_provider', None),
                               checked=stored_provider is None)]
    provider_items += [MenuItem(p['display_name'], choose_provider(p['name']),
                                checked=p['name'] == stored_provider) for p in available]

    items = [MenuItem('Provider', children=provider_items)]
    if current_provider == 'openai':
        current_model = app.settings.get('openai_stt_model')
        openai_provider = next((p for p in available if p['name'] == 'openai'), None)
        if openai_provider:
            names = {'gpt-4o-transcribe': 'GPT-4o (Best)', 'gpt-4o-mini-transcribe': 'GPT-4o Mini',
                     'whisper-1': 'Whisper (Legacy)'}
            items.append(MenuItem('OpenAI Model', children=[
                MenuItem(names.get(m, m), choose_model(m), checked=(m == current_model))
                for m in openai_provider['models']]))
    return items


def build_tray_menu(app, manager: 'TrayIconManager') -> List[MenuItem]:
    """The whole tray menu for the app's current state."""
    settings = app.settings
    recent = create_copy_menu(app)

    def copy_latest() -> None:
        texts = app.history.get_recent()
        if texts:
            pyperclip.copy(texts[0])

    def set_position(pos: str) -> None:
        settings.set('ui_indicator_position', pos)
        app.ui_feedback.set_position(pos)

    def set_size(size: str) -> None:
        settings.set('ui_indicator_size', size)
        app.ui_feedback.set_size(size)

    def toggle_all_displays() -> None:
        value = not settings.get('ui_indicator_all_displays')
        settings.set('ui_indicator_all_displays', value)
        app.ui_feedback.set_all_displays(value)

    indicator_items = [
        MenuItem('Normal Size', lambda: set_size('normal'), checked=settings.get('ui_indicator_size') == 'normal'),
        MenuItem('Mini Size', lambda: set_size('mini'), checked=settings.get('ui_indicator_size') == 'mini'),
        SEPARATOR,
        MenuItem('Show on All Displays', toggle_all_displays,
                 checked=bool(settings.get('ui_indicator_all_displays'))),
        SEPARATOR,
        *make_position_items(lambda: settings.get('ui_indicator_position'), set_position),
    ]

    return [
        MenuItem(f'Open {APP_NAME}', app.show_main_window, default=True),
        SEPARATOR,
        MenuItem('Copy Last Transcription', copy_latest, enabled=bool(recent)),
        MenuItem('🔄 Retry Last Transcription', app.retry_transcription,
                 enabled=app.last_recording is not None),
        MenuItem('Recent Transcriptions', children=recent or [MenuItem('No transcriptions yet', enabled=False)],
                 enabled=bool(recent)),
        MenuItem('Microphone', children=create_microphone_menu(app)),
        MenuItem('🎧 Meeting Mode', app.toggle_meeting_mode, checked=bool(settings.get('meeting_mode'))),
        MenuItem('📞 Phone Mode', app.toggle_phone_mode, checked=bool(settings.get('phone_mode'))),
        MenuItem('Quick Settings', children=[
            MenuItem('Clean Transcription', app.toggle_clean_transcription,
                     checked=bool(settings.get('clean_transcription'))),
            MenuItem('Streaming Dictation (Beta)', app.toggle_streaming_dictation,
                     checked=bool(settings.get('streaming_dictation'))),
            MenuItem('Silent-Start Timeout', app.toggle_silence_detection,
                     checked=settings.get('silent_start_timeout') is not None),
            MenuItem('Recording Indicator', children=indicator_items),
            MenuItem('Speech-to-Text', children=create_stt_provider_menu(app)),
            SEPARATOR,
            MenuItem('Open Settings File', lambda: os.startfile(settings.settings_file)),
            MenuItem('Open API Keys (.env)', lambda: os.startfile(str(ENV_FILE))),
            MenuItem('Open Logs Folder', lambda: os.startfile(str(get_log_dir()))),
        ]),
        SEPARATOR,
        MenuItem(f'Check for Updates (v{app_version()})', app.check_for_updates),
        MenuItem('Restart', app.restart_app),
        MenuItem('Exit', manager.exit_app),
    ]


# ---- manager ---------------------------------------------------------------

class TrayIconManager:
    """Owns the QSystemTrayIcon. Must be created on the main thread after
    the QApplication exists (UIFeedback creates it)."""

    def __init__(self, app) -> None:
        self.app = app
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._rebuild_menu)
        self.icon = QSystemTrayIcon(make_tray_icon(tray_color('#333333', idle=True)))
        self.icon.setToolTip(APP_NAME)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.icon.show()
        logger.info("Tray icon started")

    def _rebuild_menu(self) -> None:
        try:
            populate_menu(self.menu, build_tray_menu(self.app, self))
        except Exception:
            logger.exception("Failed to build tray menu")
            populate_menu(self.menu, [MenuItem('Menu unavailable (see log)', enabled=False),
                                      MenuItem('Exit', self.exit_app)])

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.app.show_main_window()

    def update_icon(self, emoji_prefix: str, tooltip_text: str) -> None:
        """Recolor the icon for the current status and set the tooltip. Thread-safe."""
        def impl() -> None:
            config = self.app.status_manager.current_config
            idle = config.ui_text == "Ready"
            self.icon.setIcon(make_tray_icon(tray_color(config.ui_color, idle)))
            self.icon.setToolTip(f"{emoji_prefix} {tooltip_text}")
        self.app.ui_feedback.call_on_main(impl)

    def update_menu(self) -> None:
        """The menu is rebuilt on every open, so nothing to do eagerly."""

    def show_message(self, title: str, body: str, ms: int = 5000) -> None:
        self.app.ui_feedback.call_on_main(
            lambda: self.icon.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, ms))

    def exit_app(self) -> None:
        self.app.logger.info("Application exiting.")
        self.stop()
        os._exit(0)

    def stop(self) -> None:
        self.icon.hide()


def setup_tray_icon(app) -> TrayIconManager:
    """Create the tray icon and expose its hooks on the app."""
    manager = TrayIconManager(app)
    app.tray_manager = manager
    app.update_tray_tooltip = manager.update_icon
    app.update_icon_menu = manager.update_menu
    return manager
