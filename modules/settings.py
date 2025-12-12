import json
import os
from typing import Any, Dict

class Settings:
    def __init__(self) -> None:
        self.settings_file: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings.json')
        self.default_settings: Dict[str, Any] = {
            'smart_capture': False,
            'silent_start_timeout': 4.0,
            'silence_threshold': 0.01,  # RMS threshold for silence detection (0.01 = -40dB)

            'stt_provider': 'openai',  # 'openai', 'google', etc.
            'stt_language': 'en',
            'openai_stt_model': 'gpt-4o-transcribe',  # 'whisper-1', 'gpt-4o-transcribe'
            'google_stt_language': 'en-US',

            'clean_transcription': False,
            'cleaning_timeout': 10.0,  # Timeout for LLM cleaning in seconds
            'llm_model': "openai/gpt-4o-mini",

            'selected_microphone': None,
            'favorite_microphones': [],

            # UI customization
            'ui_indicator_position': 'top-right',  # 'top-right', 'top-left', 'bottom-right', 'bottom-left', 'top-center', 'bottom-center'
            'ui_indicator_size': 'normal',  # 'normal', 'mini'

            # Logging
            'log_retention_days': 60
        }
        self.current_settings: Dict[str, Any] = self.load_settings()
        self._run_migrations()

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
        obsolete_keys = ['continuous_capture']
        changes_made = False
        
        for key in obsolete_keys:
            if key in self.current_settings:
                self.current_settings.pop(key)
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
            print(f"Error loading settings: {str(e)}")
            return self.default_settings.copy()

    def save_defaults(self) -> None:
        """Create settings file with default values if it doesn't exist"""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.default_settings, f, indent=4)
        except Exception as e:
            print(f"Error creating default settings file: {str(e)}")

    def save_settings(self) -> None:
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.current_settings, f, indent=4)
        except Exception as e:
            print(f"Error saving settings: {str(e)}")

    def get(self, key: str) -> Any:
        return self.current_settings.get(key, self.default_settings.get(key))

    def set(self, key: str, value: Any) -> None:
        self.current_settings[key] = value
        self.save_settings()