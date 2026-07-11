import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger('voice_typing')

# Settings live alongside logs/history so user data survives git operations on the repo
SETTINGS_DIR = Path.home() / "Documents" / "VoiceTyping"
_LEGACY_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.json')

class Settings:
    """Application settings. Singleton: every Settings() call returns the same
    instance so all modules share one in-memory state and never clobber each
    other's saves."""

    _instance: Optional["Settings"] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "Settings":
        with cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._initialized = False
                cls._instance = instance
            return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._save_lock = threading.Lock()
        self.settings_file: str = str(SETTINGS_DIR / 'settings.json')
        self._migrate_settings_location()
        self.default_settings: Dict[str, Any] = {
            'silent_start_timeout': 4.0,
            'silence_threshold': 0.01,  # RMS threshold for silence detection (0.01 = -40dB)
            'max_recording_duration': 900.0,  # Auto-stop (and still transcribe) after this many seconds; null to disable

            'stt_provider': 'openai',  # 'openai', 'custom'
            'stt_language': 'en',
            'openai_stt_model': 'gpt-4o-transcribe',  # 'whisper-1', 'gpt-4o-transcribe'
            'custom_stt_base_url': 'http://localhost:8000',
            'custom_stt_model': 'parakeet-tdt-0.6b-v2',

            'clean_transcription': False,
            'cleaning_timeout': 10.0,  # Timeout for LLM cleaning in seconds
            'llm_model': "openai/gpt-4o-mini",

            'selected_microphone': None,
            'favorite_microphones': [],

            # UI customization
            'ui_indicator_position': 'top-right',  # 'top-right', 'top-left', 'bottom-right', 'bottom-left', 'top-center', 'bottom-center'
            'ui_indicator_size': 'normal',  # 'normal', 'mini'
            'ui_indicator_all_displays': True,  # Show indicator on all monitors

            # Logging
            'log_retention_days': 60,
            'log_transcript_text': True,  # Include full transcript text in log files

            # Output
            'output_mode': 'standard',  # Output provider for text insertion
            'clipboard_restore_delay_ms': 300,  # Delay before restoring original clipboard after paste
        }
        self.current_settings: Dict[str, Any] = self.load_settings()
        self._run_migrations()

    def _migrate_settings_location(self) -> None:
        """One-time move of settings.json from modules/ into Documents\\VoiceTyping."""
        new_path = Path(self.settings_file)
        old_path = Path(_LEGACY_SETTINGS_FILE)
        if new_path.exists() or not old_path.exists():
            return
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_path), str(new_path))
            logger.info(f"Migrated settings file to {new_path}")
        except Exception as e:
            logger.error(f"Failed to migrate settings file, falling back to legacy location: {e}")
            self.settings_file = str(old_path)

    def _run_migrations(self) -> None:
        """Runs all necessary setting migrations and saves if changes were made."""
        migrations_run = [
            self._migrate_device_settings(),
            self._migrate_silence_timeout(),
            self._migrate_obsolete_settings()
        ]

        if any(migrations_run):
            self.save_settings()

    def _migrate_obsolete_settings(self) -> bool:
        """Removes obsolete settings keys. Returns True if changes were made."""
        obsolete_keys = [
            'continuous_capture',
            'smart_capture',        # never-implemented feature stub, removed 2026-07
            'google_stt_language',  # Google STT provider removed 2026-07
        ]
        changes_made = False
        
        for key in obsolete_keys:
            if key in self.current_settings:
                self.current_settings.pop(key)
                changes_made = True

        # Google STT provider was removed; fall back to OpenAI
        if self.current_settings.get('stt_provider') == 'google':
            self.current_settings['stt_provider'] = 'openai'
            changes_made = True

        return changes_made

    def _migrate_silence_timeout(self) -> bool:
        """Renames 'silence_timeout' to 'silent_start_timeout'. Returns True if changes were made."""
        if 'silence_timeout' in self.current_settings:
            # Copy value to new key, then remove old key to rename it
            self.current_settings['silent_start_timeout'] = self.current_settings.pop('silence_timeout')
            return True
        return False

    def _migrate_device_settings(self) -> bool:
        """
        Migrates old device ID settings to new identifier format.
        Returns True if any changes were made.
        """
        changes_made = False
        from modules.audio_manager import get_device_by_id, create_device_identifier

        # Migrate selected microphone
        if isinstance(self.current_settings.get('selected_microphone'), int):
            changes_made = True
            device = get_device_by_id(self.current_settings['selected_microphone'])
            if device:
                identifier = create_device_identifier(device)
                self.current_settings['selected_microphone'] = identifier._asdict()
            else:
                self.current_settings['selected_microphone'] = None

        # Migrate favorite microphones
        if self.current_settings.get('favorite_microphones'):
            new_favorites = []
            migrated_any_fav = False
            # We need to handle list of mixed types (already migrated dicts and old ints)
            for device_info in self.current_settings['favorite_microphones']:
                if isinstance(device_info, int):
                    migrated_any_fav = True
                    device = get_device_by_id(device_info)
                    if device:
                        identifier = create_device_identifier(device)
                        new_favorites.append(identifier._asdict())
                else:
                    new_favorites.append(device_info) # Keep as is

            if migrated_any_fav:
                self.current_settings['favorite_microphones'] = new_favorites
                changes_made = True

        return changes_made

    def load_settings(self) -> Dict[str, Any]:
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    return {**self.default_settings, **json.load(f)}
            else:
                # File doesn't exist, create it with default settings
                self.save_defaults()
                return self.default_settings.copy()
        except Exception as e:
            logger.error(f"Error loading settings: {e}")
            return self.default_settings.copy()

    def save_defaults(self) -> None:
        """Create settings file with default values if it doesn't exist"""
        try:
            os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)
            with open(self.settings_file, 'w') as f:
                json.dump(self.default_settings, f, indent=4)
        except Exception as e:
            logger.error(f"Error creating default settings file: {e}")

    def save_settings(self) -> None:
        try:
            with self._save_lock:
                os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)
                with open(self.settings_file, 'w') as f:
                    json.dump(self.current_settings, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving settings: {e}")

    def get(self, key: str) -> Any:
        return self.current_settings.get(key, self.default_settings.get(key))

    def set(self, key: str, value: Any) -> None:
        self.current_settings[key] = value
        self.save_settings()