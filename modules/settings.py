import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

logger = logging.getLogger('voice_typing')

# Load .env as early as possible: settings migrations and the transcriber
# factory both make decisions based on which API keys are present, and this
# module is imported before any of them. Explicit path so the .env is found
# regardless of the process working directory.
load_dotenv(Path(__file__).resolve().parent.parent / '.env')


def api_key_configured(name: str) -> bool:
    """True if the environment variable holds a real-looking API key.

    Unfilled .env template placeholders (e.g. 'sk_...') must not count as
    configured — provider auto-selection would otherwise route to a service
    that can only fail auth.
    """
    value = (os.environ.get(name) or '').strip().strip('"').strip("'")
    return bool(value) and not value.endswith('...')

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

            # 'elevenlabs', 'openai', 'custom', or null = auto (ElevenLabs
            # Scribe when ELEVENLABS_API_KEY is configured, else OpenAI)
            'stt_provider': None,
            'stt_language': 'en',
            'openai_stt_model': 'gpt-4o-transcribe',  # 'whisper-1', 'gpt-4o-transcribe'
            'custom_stt_base_url': 'http://localhost:8000',
            'custom_stt_model': 'parakeet-tdt-0.6b-v2',

            # Streaming dictation (beta): transcribe over an OpenAI Realtime
            # websocket while recording, so text is ready ~immediately on stop.
            # Normal dictation mode only; batch upload remains the fallback.
            'streaming_dictation': False,

            # Meeting mode: record mic + system audio, transcribe with speaker
            # labels via ElevenLabs Scribe (requires ELEVENLABS_API_KEY in .env)
            'meeting_mode': False,
            'meeting_speaker_you': 'Me',    # Label for your mic channel
            'meeting_speaker_them': 'Them',  # Label for the system-audio channel

            # Phone mode: mic-only recording of an in-room conversation (e.g. a
            # call on speakerphone), transcribed with voice diarization via
            # ElevenLabs Scribe (requires ELEVENLABS_API_KEY in .env)
            'phone_mode': False,
            'phone_num_speakers': 2,  # Max speakers hint for diarization; null to let Scribe decide
            # 'Speaker N' labels are assigned per chunk and can swap identities
            # between chunks of a session, so they're off by default: output is
            # one unattributed line per speaker turn
            'phone_speaker_labels': False,
            # Match diarized voices against the ElevenLabs workspace speaker
            # library (dashboard: Speech to Text -> Speakers). Harmless no-op
            # if the library is empty.
            'use_speaker_library': True,
            # Your registered Speaker ID in that library (e.g. 'jane-doe').
            # When set and voice-matched, your turns are labeled with
            # meeting_speaker_you and everyone else with meeting_speaker_them,
            # stable across session chunks.
            'phone_my_speaker_id': None,
            # Diarization threshold (0.1-0.4); when set it replaces the
            # phone_num_speakers hint (the API allows only one). Also gates
            # speaker-library match acceptance: a 2026-07-22 sweep showed the
            # enrolled speaker only matched at >=0.26 (~98% word accuracy,
            # stable through 0.4) while the default ~0.22 rejected the match.
            # Set null to use phone_num_speakers instead.
            'phone_diarization_threshold': 0.3,
            # Prepend a short transcript-limitations note (for the LLM reading
            # it) to the first chunk delivered in a meeting/phone session
            'session_preamble': True,

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
            self._migrate_obsolete_settings(),
            self._migrate_llm_model_prefix(),
            self._migrate_default_provider_elevenlabs()
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

    def _migrate_default_provider_elevenlabs(self) -> bool:
        """One-time move of settings still pinned to the old hardcoded
        'openai' default onto the new automatic default (null = ElevenLabs
        Scribe v2 when its key is configured, else OpenAI; Scribe benchmarked
        12.25% vs 17.07% WER on dictation-style speech, st-vtt-bench
        2026-07-31). Key-less setups resolve to OpenAI either way, so their
        behavior is unchanged. Runs once, marked by
        'migrated_default_elevenlabs', so an explicit provider choice made
        afterwards always sticks."""
        if self.current_settings.get('migrated_default_elevenlabs'):
            return False
        self.current_settings['migrated_default_elevenlabs'] = True
        if self.current_settings.get('stt_provider') == 'openai':
            self.current_settings['stt_provider'] = None
        return True

    def _migrate_llm_model_prefix(self) -> bool:
        """litellm >= 1.84 no longer infers the provider from bare model names
        (e.g. 'claude-3-5-haiku-latest'); prepend the provider prefix."""
        model = self.current_settings.get('llm_model')
        if isinstance(model, str) and model and '/' not in model:
            if model.startswith('claude'):
                self.current_settings['llm_model'] = f'anthropic/{model}'
                return True
            if model.startswith(('gpt', 'o1', 'o3', 'o4')):
                self.current_settings['llm_model'] = f'openai/{model}'
                return True
        return False

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