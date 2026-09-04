import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from modules.fileutil import backup_path, read_json_with_backup, write_json_atomic
from modules.settings_migrations import SCHEMA_VERSION, migrate

logger = logging.getLogger('voice_typing')

# User data lives outside the app folder so it survives updates (which replace
# app files wholesale) and git operations on a source checkout.
SETTINGS_DIR = Path.home() / "Documents" / "VoiceTyping"
SETTINGS_FILE = SETTINGS_DIR / 'settings.json'
# API keys. Before 1.0 this was a .env in the app folder; see _load_env_files.
ENV_FILE = SETTINGS_DIR / '.env'

_APP_DIR = Path(__file__).resolve().parent.parent
_LEGACY_ENV_FILE = _APP_DIR / '.env'
_ENV_TEMPLATE = _APP_DIR / '.env.example'
_LEGACY_SETTINGS_FILE = Path(__file__).resolve().parent / 'settings.json'

# This module runs before logging is configured (the app sets up logging
# from Settings), so anything worth telling the user about startup is
# collected here and logged by the app once the logger exists.
startup_notes: List[str] = []


def _load_env_files() -> None:
    """Load API keys as early as possible: settings migrations and the
    transcriber factory both make decisions based on which keys are present,
    and this module is imported before any of them.

    Keys live in Documents\\VoiceTyping\\.env. On first run after 1.0 an
    app-folder .env from an earlier version is moved there; if there is no
    .env at all, the template is copied so "Open API Keys" in the tray has a
    file to open. The app-folder location is still read as a fallback (it
    never overrides a value already loaded)."""
    try:
        if not ENV_FILE.exists():
            SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
            if _LEGACY_ENV_FILE.exists():
                shutil.move(str(_LEGACY_ENV_FILE), str(ENV_FILE))
                startup_notes.append(f"Moved API keys file from the app folder to {ENV_FILE}")
            elif _ENV_TEMPLATE.exists():
                shutil.copyfile(_ENV_TEMPLATE, ENV_FILE)
                startup_notes.append(f"Created API keys file from template at {ENV_FILE}")
    except OSError as e:
        startup_notes.append(f"Could not set up the API keys file: {e}")
    load_dotenv(ENV_FILE)
    load_dotenv(_LEGACY_ENV_FILE)


_load_env_files()


def api_key_configured(name: str) -> bool:
    """True if the environment variable holds a real-looking API key.

    Unfilled .env template placeholders (e.g. 'sk_...') must not count as
    configured — provider auto-selection would otherwise route to a service
    that can only fail auth.
    """
    value = (os.environ.get(name) or '').strip().strip('"').strip("'")
    return bool(value) and not value.endswith('...')


class Settings:
    """Application settings. Singleton: every Settings() call returns the same
    instance so all modules share one in-memory state and never clobber each
    other's saves.

    Load order: raw JSON (with .bak recovery) → pure schema migrations
    (modules/settings_migrations.py) → merge defaults → save if anything
    changed → runtime device reconciliation against the hardware present."""

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
        self.settings_file: str = str(SETTINGS_FILE)
        self._migrate_settings_location()
        self.default_settings: Dict[str, Any] = {
            'schema_version': SCHEMA_VERSION,

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
            # Prepend a short note (for the LLM reading it) to the first chunk
            # delivered in a meeting/phone session: transcript limitations
            # plus silent-advisor collaboration guidance
            'session_preamble': True,

            'clean_transcription': False,
            'cleaning_timeout': 10.0,  # Timeout for LLM cleaning in seconds
            'llm_model': "gpt-4o-mini",  # OpenAI chat model used for transcript cleaning
            'llm_base_url': None,  # OpenAI-compatible server for cleaning (None = api.openai.com)

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
            'clipboard_restore_delay_ms': 300,  # Delay before restoring original clipboard after paste
        }
        self.current_settings, needs_save = self._load()
        if needs_save:
            self.save_settings()
        if self._reconcile_devices():
            self.save_settings()

    def _migrate_settings_location(self) -> None:
        """One-time move of settings.json from modules/ into Documents\\VoiceTyping."""
        new_path = Path(self.settings_file)
        if new_path.exists() or not _LEGACY_SETTINGS_FILE.exists():
            return
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(_LEGACY_SETTINGS_FILE), str(new_path))
            logger.info(f"Migrated settings file to {new_path}")
        except Exception as e:
            logger.error(f"Failed to migrate settings file, falling back to legacy location: {e}")
            self.settings_file = str(_LEGACY_SETTINGS_FILE)

    def _load(self) -> Tuple[Dict[str, Any], bool]:
        """(settings, needs_save). Raw JSON is migrated to the current schema
        before defaults are merged; unknown keys are preserved."""
        try:
            if not os.path.exists(self.settings_file) and not os.path.exists(
                    backup_path(self.settings_file)):
                return self.default_settings.copy(), True  # first run
            stored = read_json_with_backup(self.settings_file, default=None)
            if not isinstance(stored, dict):
                logger.error("Settings unreadable and no usable backup; using defaults")
                return self.default_settings.copy(), False
            migrated, notes = migrate(stored)
            startup_notes.extend(f"Settings migration: {note}" for note in notes)
            return {**self.default_settings, **migrated}, bool(notes)
        except Exception as e:
            logger.error(f"Error loading settings: {e}")
            return self.default_settings.copy(), False

    def _reconcile_devices(self) -> bool:
        """Runtime (not a migration): upgrade saved microphone identifiers whose
        names came from MME (which truncates names to 31 chars) to the full
        WASAPI name and specs, using the devices present right now.

        Only rewrites when exactly one current device matches the truncated
        name — ambiguous prefixes (e.g. two Bluetooth headsets whose long
        driver names share the first 31 chars) and unplugged devices are left
        alone; runtime matching stays truncation-tolerant for those, and this
        reruns every launch until they resolve."""
        from modules.audio_manager import (get_input_devices,
                                           create_device_identifier,
                                           MME_NAME_LIMIT)
        try:
            devices = get_input_devices()
        except Exception:
            return False
        changed = False

        def upgrade(entry: Any) -> Any:
            nonlocal changed
            name = entry.get('name') if isinstance(entry, dict) else None
            if not name or len(name) != MME_NAME_LIMIT:
                return entry
            # A device with this exact name exists: the saved name is a real
            # full name, not a truncation — leave it
            if any(d['name'] == name for d in devices):
                return entry
            matches = [d for d in devices if d['name'].startswith(name)]
            if len(matches) == 1:
                changed = True
                return create_device_identifier(matches[0])._asdict()
            return entry

        self.current_settings['selected_microphone'] = upgrade(
            self.current_settings.get('selected_microphone'))
        favorites = self.current_settings.get('favorite_microphones')
        if isinstance(favorites, list):
            self.current_settings['favorite_microphones'] = [upgrade(f) for f in favorites]
        return changed

    def save_settings(self) -> None:
        try:
            with self._save_lock:
                write_json_atomic(self.settings_file, self.current_settings)
        except Exception as e:
            logger.error(f"Error saving settings: {e}")

    def get(self, key: str) -> Any:
        return self.current_settings.get(key, self.default_settings.get(key))

    def set(self, key: str, value: Any) -> None:
        self.current_settings[key] = value
        self.save_settings()
