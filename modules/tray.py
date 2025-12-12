import os
import threading
import ctypes
import ctypes.wintypes
import time
import logging
from typing import Any, Dict, Optional, Callable

import pyperclip
import pystray
from PIL import Image, ImageDraw

from modules.audio_manager import get_input_devices, get_default_device_id, set_input_device, create_device_identifier
from modules import transcribe

# Windows constants for TaskbarCreated message
WM_USER = 0x0400
ICON_WATCHDOG_INTERVAL = 30  # Check icon health every 30 seconds
ICON_RESTART_DELAY = 2  # Wait 2 seconds before restarting icon after failure

logger = logging.getLogger(__name__)

def create_tray_icon(icon_path: str) -> Image.Image:
    """Create tray icon from file path"""
    current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icon_path = os.path.join(current_dir, icon_path)
    return Image.open(icon_path)

def create_copy_menu(app):
    """Creates dynamic menu of recent transcriptions"""
    def make_copy_handler(text):
        return lambda icon, item: pyperclip.copy(text)

    return [
        pystray.MenuItem(
            app.history.get_preview(text),
            make_copy_handler(text)
        )
        for text in app.history.get_recent()
    ]

def create_microphone_menu(app):
    """Creates dynamic menu of available microphones"""
    devices = sorted(get_input_devices(), key=lambda d: d['name'].lower())
    current_identifier = app.settings.get('selected_microphone')
    favorite_identifiers = app.settings.get('favorite_microphones')
    default_device_id = get_default_device_id()

    def make_mic_handler(device: Dict[str, any]):
        def handler(icon, item):
            identifier = create_device_identifier(device)._asdict()
            app.settings.set('selected_microphone', identifier)
            set_input_device(device['id'])
            # Log the device change
            app.logger.info(f"Microphone changed to: {device['name']} (ID: {device['id']}, Channels: {device['max_input_channels']}, Sample Rate: {device['default_samplerate']} Hz)")
        return handler

    def make_favorite_handler(device: Dict[str, any]):
        def handler(icon, item):
            identifier = create_device_identifier(device)._asdict()
            favorites = app.settings.get('favorite_microphones')

            if identifier in favorites:
                favorites.remove(identifier)
            else:
                favorites.append(identifier)

            app.settings.set('favorite_microphones', favorites)
            app.update_icon_menu()
        return handler

    # Create menu items
    select_items = []
    favorite_items = []

    for device in devices:
        identifier = create_device_identifier(device)._asdict()
        is_favorite = identifier in favorite_identifiers
        is_selected = identifier == current_identifier
        is_default = device['id'] == default_device_id

        star_prefix = "💫 " if is_favorite else "    "
        default_prefix = "🎙️ " if is_default else "    "
        combined_prefix = default_prefix if is_default else star_prefix

        select_items.append(
            pystray.MenuItem(
                f"{combined_prefix}{device['name']}",
                make_mic_handler(device),
                checked=lambda item, dev=device: create_device_identifier(dev)._asdict() == current_identifier
            )
        )

        favorite_items.append(
            pystray.MenuItem(
                f"{default_prefix}{device['name']}",
                make_favorite_handler(device),
                checked=lambda item, dev=device: create_device_identifier(dev)._asdict() in favorite_identifiers
            )
        )

    menu_items = [
        pystray.MenuItem(
            'Select Device',
            pystray.Menu(*select_items)
        ),
        pystray.MenuItem(
            'Manage Favorites',
            pystray.Menu(*favorite_items)
        ),
        pystray.MenuItem('Refresh Devices', lambda icon, item: app.refresh_microphones())
    ]

    return menu_items

def create_stt_provider_menu(app):
    """Creates menu for STT provider and model selection"""
    current_provider = transcribe.get_current_provider()
    available_providers = transcribe.get_available_providers()

    def make_provider_handler(provider_name: str):
        def handler(icon, item):
            try:
                transcribe.set_stt_provider(provider_name)
                app.update_icon_menu()
            except Exception as e:
                print(f"Error changing STT provider: {e}")
        return handler

    def make_model_handler(model: str):
        def handler(icon, item):
            app.settings.set('openai_stt_model', model)
            app.update_icon_menu()
        return handler

    # Create provider selection items
    provider_items = []
    for provider in available_providers:
        provider_items.append(
            pystray.MenuItem(
                provider['display_name'],
                make_provider_handler(provider['name']),
                checked=lambda item, p=provider: p['name'] == current_provider
            )
        )

    # Create model selection items (only for OpenAI currently)
    model_items = []
    if current_provider == 'openai':
        current_model = app.settings.get('openai_stt_model')
        openai_provider = next((p for p in available_providers if p['name'] == 'openai'), None)
        if openai_provider:
            for model in openai_provider['models']:
                display_name = {
                    'gpt-4o-transcribe': 'GPT-4o (Best)',
                    'gpt-4o-mini-transcribe': 'GPT-4o Mini',
                    'whisper-1': 'Whisper (Legacy)',
                }.get(model, model)

                model_items.append(
                    pystray.MenuItem(
                        display_name,
                        make_model_handler(model),
                        checked=lambda item, m=model: m == current_model
                    )
                )

    menu_items = []

    # Add provider selection
    menu_items.append(
        pystray.MenuItem(
            'Provider',
            pystray.Menu(*provider_items) if provider_items else pystray.Menu(
                pystray.MenuItem('No providers available', None, enabled=False)
            )
        )
    )

    # Add model selection (only shown for OpenAI)
    if model_items:
        menu_items.append(
            pystray.MenuItem(
                'OpenAI Model',
                pystray.Menu(*model_items)
            )
        )

    return menu_items


class TrayIconManager:
    """
    Manages the system tray icon with automatic recovery from failures.

    Handles:
    - Creating and running the tray icon
    - Automatic restart on thread crash
    - Periodic health checks (watchdog)
    - Recovery from Explorer.exe restart
    """

    def __init__(self, app):
        self.app = app
        self.icon: Optional[pystray.Icon] = None
        self.icon_thread: Optional[threading.Thread] = None
        self.watchdog_thread: Optional[threading.Thread] = None
        self.running = False
        self.icon_lock = threading.Lock()
        self.restart_count = 0
        self.last_restart_time = 0

        # Register for TaskbarCreated message (for Explorer restart detection)
        self._taskbar_created_msg = self._register_taskbar_created_message()

    def _register_taskbar_created_message(self) -> int:
        """Register the TaskbarCreated window message."""
        try:
            RegisterWindowMessage = ctypes.windll.user32.RegisterWindowMessageW
            RegisterWindowMessage.argtypes = [ctypes.wintypes.LPCWSTR]
            RegisterWindowMessage.restype = ctypes.wintypes.UINT
            msg_id = RegisterWindowMessage("TaskbarCreated")
            logger.debug(f"Registered TaskbarCreated message: {msg_id}")
            return msg_id
        except Exception as e:
            logger.warning(f"Failed to register TaskbarCreated message: {e}")
            return 0

    def _create_icon(self) -> pystray.Icon:
        """Create a new tray icon instance."""
        icon = pystray.Icon(
            'Voice Typing',
            icon=create_tray_icon('assets/microphone-blue.png')
        )
        icon.menu = self._get_menu()
        return icon

    def _get_menu(self):
        """Generate the context menu for the tray icon."""
        app = self.app
        copy_menu = create_copy_menu(app)
        microphone_menu = create_microphone_menu(app)
        stt_menu = create_stt_provider_menu(app)

        def copy_latest_transcription(icon, item) -> None:
            recent_texts = app.history.get_recent()
            if recent_texts:
                pyperclip.copy(recent_texts[0])

        def change_ui_position(new_pos: str):
            app.settings.set('ui_indicator_position', new_pos)
            app.ui_feedback.set_position(new_pos)
            self.update_menu()

        def change_ui_size(new_size: str):
            app.settings.set('ui_indicator_size', new_size)
            app.ui_feedback.set_size(new_size)
            self.update_menu()

        def on_exit(icon, item):
            app.logger.info("Application exiting.")
            self.stop()
            os._exit(0)

        return pystray.Menu(
            pystray.MenuItem(
                'Copy Last Transcription',
                copy_latest_transcription,
                default=True
            ),
            pystray.MenuItem(
                '🔄 Retry Last Transcription',
                lambda icon, item: app.retry_transcription(),
                enabled=lambda item: app.last_recording is not None
            ),
            pystray.MenuItem(
                'Recent Transcriptions',
                pystray.Menu(*copy_menu) if copy_menu else pystray.Menu(
                    pystray.MenuItem('No transcriptions yet', None, enabled=False)
                ),
                enabled=bool(copy_menu)
            ),
            pystray.MenuItem(
                'Microphone',
                pystray.Menu(*microphone_menu)
            ),
            pystray.MenuItem(
                'Settings',
                pystray.Menu(
                    pystray.MenuItem(
                        'Clean Transcription',
                        lambda icon, item: app.toggle_clean_transcription(),
                        checked=lambda item: app.settings.get('clean_transcription')
                    ),
                    pystray.MenuItem(
                        'Silent-Start Timeout',
                        lambda icon, item: app.toggle_silence_detection(),
                        checked=lambda item: app.settings.get('silent_start_timeout') is not None
                    ),
                    pystray.MenuItem(
                        'Smart Capture',
                        lambda icon, item: None,
                        enabled=False
                    ),
                    pystray.MenuItem(
                        'Recording Indicator',
                        pystray.Menu(
                            pystray.MenuItem(
                                'Normal Size',
                                lambda icon, item: change_ui_size('normal'),
                                checked=lambda item: app.settings.get('ui_indicator_size') == 'normal'
                            ),
                            pystray.MenuItem(
                                'Mini Size',
                                lambda icon, item: change_ui_size('mini'),
                                checked=lambda item: app.settings.get('ui_indicator_size') == 'mini'
                            ),
                            pystray.Menu.SEPARATOR,
                            pystray.MenuItem(
                                'Top Left',
                                lambda icon, item: change_ui_position('top-left'),
                                checked=lambda item: app.settings.get('ui_indicator_position') == 'top-left'
                            ),
                            pystray.MenuItem(
                                'Top Center',
                                lambda icon, item: change_ui_position('top-center'),
                                checked=lambda item: app.settings.get('ui_indicator_position') == 'top-center'
                            ),
                            pystray.MenuItem(
                                'Top Right',
                                lambda icon, item: change_ui_position('top-right'),
                                checked=lambda item: app.settings.get('ui_indicator_position') == 'top-right'
                            ),
                            pystray.MenuItem(
                                'Bottom Left',
                                lambda icon, item: change_ui_position('bottom-left'),
                                checked=lambda item: app.settings.get('ui_indicator_position') == 'bottom-left'
                            ),
                            pystray.MenuItem(
                                'Bottom Center',
                                lambda icon, item: change_ui_position('bottom-center'),
                                checked=lambda item: app.settings.get('ui_indicator_position') == 'bottom-center'
                            ),
                            pystray.MenuItem(
                                'Bottom Right',
                                lambda icon, item: change_ui_position('bottom-right'),
                                checked=lambda item: app.settings.get('ui_indicator_position') == 'bottom-right'
                            ),
                        )
                    ),
                    pystray.MenuItem(
                        'Speech-to-Text',
                        pystray.Menu(*stt_menu)
                    )
                )
            ),
            pystray.MenuItem('Restart', lambda icon, item: app.restart_app()),
            pystray.MenuItem('Exit', on_exit)
        )

    def update_menu(self) -> None:
        """Update the tray icon's menu."""
        with self.icon_lock:
            if self.icon:
                try:
                    self.icon.menu = self._get_menu()
                except Exception as e:
                    logger.warning(f"Failed to update menu: {e}")

    def update_icon(self, emoji_prefix: str, tooltip_text: str) -> None:
        """Update both the tray icon image and tooltip."""
        with self.icon_lock:
            if self.icon:
                try:
                    self.icon.icon = create_tray_icon(
                        self.app.status_manager.current_config.tray_icon_file
                    )
                    self.icon.title = f"{emoji_prefix} {tooltip_text}"
                except Exception as e:
                    logger.warning(f"Failed to update tray icon: {e}")

    def _run_icon(self) -> None:
        """Run the tray icon with exception handling."""
        while self.running:
            try:
                with self.icon_lock:
                    self.icon = self._create_icon()

                logger.info("Tray icon starting")
                self.icon.run()

                # If we get here, icon.run() returned normally (icon was stopped)
                logger.info("Tray icon stopped normally")
                break

            except Exception as e:
                logger.error(f"Tray icon crashed: {e}", exc_info=True)

                with self.icon_lock:
                    self.icon = None

                if not self.running:
                    break

                # Rate limit restarts
                current_time = time.time()
                if current_time - self.last_restart_time < 10:
                    self.restart_count += 1
                else:
                    self.restart_count = 1
                self.last_restart_time = current_time

                if self.restart_count > 5:
                    logger.error("Tray icon crashed too many times, giving up")
                    break

                logger.info(f"Restarting tray icon in {ICON_RESTART_DELAY}s (attempt {self.restart_count})")
                time.sleep(ICON_RESTART_DELAY)

    def _watchdog(self) -> None:
        """Periodically check if the tray icon is healthy and restart if needed."""
        logger.info("Tray icon watchdog started")

        while self.running:
            time.sleep(ICON_WATCHDOG_INTERVAL)

            if not self.running:
                break

            with self.icon_lock:
                icon_exists = self.icon is not None
                thread_alive = self.icon_thread and self.icon_thread.is_alive()

            if not icon_exists or not thread_alive:
                logger.warning("Watchdog detected tray icon is dead, restarting...")
                self._restart_icon()

    def _restart_icon(self) -> None:
        """Restart the tray icon."""
        with self.icon_lock:
            # Stop existing icon if any
            if self.icon:
                try:
                    self.icon.stop()
                except Exception:
                    pass
                self.icon = None

        # Wait for old thread to finish
        if self.icon_thread and self.icon_thread.is_alive():
            self.icon_thread.join(timeout=2)

        # Start new icon thread
        if self.running:
            self.icon_thread = threading.Thread(target=self._run_icon, daemon=True)
            self.icon_thread.start()
            logger.info("Tray icon restarted by watchdog")

    def start(self) -> None:
        """Start the tray icon and watchdog."""
        self.running = True

        # Start the icon thread
        self.icon_thread = threading.Thread(target=self._run_icon, daemon=True)
        self.icon_thread.start()

        # Start the watchdog thread
        self.watchdog_thread = threading.Thread(target=self._watchdog, daemon=True)
        self.watchdog_thread.start()

        logger.info("Tray icon manager started")

    def stop(self) -> None:
        """Stop the tray icon and watchdog."""
        logger.info("Stopping tray icon manager")
        self.running = False

        with self.icon_lock:
            if self.icon:
                try:
                    self.icon.stop()
                except Exception:
                    pass
                self.icon = None

def setup_tray_icon(app):
    """Set up the system tray icon with automatic recovery."""
    manager = TrayIconManager(app)

    # Store references on app for external access
    app.tray_manager = manager
    app.update_tray_tooltip = manager.update_icon
    app.update_icon_menu = manager.update_menu

    # Start the tray icon
    manager.start()