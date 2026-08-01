"""Multi-provider Speech-to-Text module with Strategy pattern"""
import os
import logging
from typing import Union, Optional
from pathlib import Path

# Provider modules are imported lazily inside _get_transcriber to keep app
# startup fast (the OpenAI SDK in particular is a heavy import).
# Note: importing Settings also loads .env, so os.environ checks below see
# the user's configured API keys.
from modules.settings import Settings, api_key_configured

# OpenAI Speech to text docs: https://platform.openai.com/docs/guides/speech-to-text
# ⚠️ IMPORTANT: OpenAI Audio API file uploads are currently limited to 25 MB

logger = logging.getLogger('voice_typing')

# Initialize settings
settings = Settings()

# Transcriber instances cached by their full configuration so repeat
# transcriptions reuse HTTP clients/connections. A settings change produces a
# different key, which transparently creates a fresh instance.
_transcriber_cache: dict = {}


def _get_transcriber(provider_name: str):
    """
    Factory function to get a transcriber instance based on provider name.
    Instances are cached per configuration.

    Args:
        provider_name: Name of the provider ('elevenlabs', 'openai', 'custom')

    Returns:
        Transcriber instance for the specified provider

    Raises:
        ValueError: If provider is unknown
    """
    if provider_name == "elevenlabs":
        language = settings.get('stt_language') or 'en'
        key = (provider_name, language)
        if key not in _transcriber_cache:
            from services.elevenlabs_stt import ElevenLabsDictationTranscriber
            _transcriber_cache[key] = ElevenLabsDictationTranscriber(language=language)
        return _transcriber_cache[key]
    elif provider_name == "openai":
        model = settings.get('openai_stt_model') or 'gpt-4o-mini-transcribe'
        language = settings.get('stt_language') or 'en'
        key = (provider_name, model, language)
        if key not in _transcriber_cache:
            from services.openai_stt import OpenAITranscriber
            _transcriber_cache[key] = OpenAITranscriber(model=model, language=language)
        return _transcriber_cache[key]
    elif provider_name == "custom":
        base_url = settings.get('custom_stt_base_url') or 'http://localhost:8000'
        model = settings.get('custom_stt_model') or 'parakeet-tdt-0.6b-v2'
        language = settings.get('stt_language') or 'en'
        key = (provider_name, base_url, model, language)
        if key not in _transcriber_cache:
            from services.custom_stt import CustomTranscriber
            _transcriber_cache[key] = CustomTranscriber(base_url=base_url, model=model, language=language)
        return _transcriber_cache[key]
    # Add other providers here as needed
    else:
        raise ValueError(f"Unknown STT provider: {provider_name}")


def _default_provider() -> str:
    """Resolve the automatic provider choice (stt_provider unset/null).

    ElevenLabs Scribe when its key is configured (benchmarked at lower WER
    on dictation-style speech than gpt-4o-transcribe), otherwise OpenAI —
    so a setup with only an OPENAI_API_KEY keeps working out of the box.
    """
    return 'elevenlabs' if api_key_configured('ELEVENLABS_API_KEY') else 'openai'


def is_multichannel_recording(filename: str) -> bool:
    """True if the file is a 2-channel meeting-mode recording.

    Normal dictation recordings are always mono, so channel count is a
    reliable marker that survives snapshots, retries, and app restarts.
    """
    try:
        import soundfile as sf
        return sf.info(filename).channels >= 2
    except Exception:
        return False


def is_phone_recording(filename: str) -> bool:
    """True if the file is a phone-mode recording (mono, multiple speakers).

    Phone recordings are plain mono mic audio, so the recorder tags them with
    a WAV comment — a marker that, like channel count, survives snapshots,
    retries, and app restarts.
    """
    try:
        import soundfile as sf
        from modules.recorder import PHONE_RECORDING_COMMENT
        with sf.SoundFile(filename) as f:
            return (f.comment or '').startswith(PHONE_RECORDING_COMMENT)
    except Exception:
        return False


def is_conversation_recording(filename: str) -> bool:
    """True for any multi-speaker recording (meeting or phone mode).

    These produce speaker-labeled transcripts, so callers use this to skip
    steps that would mangle the labels (e.g. LLM cleaning).
    """
    return is_multichannel_recording(filename) or is_phone_recording(filename)


def _get_meeting_transcriber():
    """Get the ElevenLabs multichannel transcriber for meeting recordings (cached)."""
    you_label = settings.get('meeting_speaker_you') or 'Me'
    them_label = settings.get('meeting_speaker_them') or 'Them'
    key = ('elevenlabs_meeting', you_label, them_label)
    if key not in _transcriber_cache:
        from services.elevenlabs_stt import ElevenLabsMeetingTranscriber
        _transcriber_cache[key] = ElevenLabsMeetingTranscriber(
            you_label=you_label, them_label=them_label)
    return _transcriber_cache[key]


def _get_phone_transcriber():
    """Get the ElevenLabs diarizing transcriber for phone recordings (cached)."""
    num_speakers = settings.get('phone_num_speakers')
    labeled = bool(settings.get('phone_speaker_labels'))
    my_speaker_id = settings.get('phone_my_speaker_id')
    you_label = settings.get('meeting_speaker_you') or 'Me'
    them_label = settings.get('meeting_speaker_them') or 'Them'
    use_library = bool(settings.get('use_speaker_library'))
    threshold = settings.get('phone_diarization_threshold')
    key = ('elevenlabs_phone', num_speakers, labeled, my_speaker_id,
           you_label, them_label, use_library, threshold)
    if key not in _transcriber_cache:
        from services.elevenlabs_stt import ElevenLabsDiarizedTranscriber
        _transcriber_cache[key] = ElevenLabsDiarizedTranscriber(
            num_speakers=num_speakers, labeled=labeled,
            my_speaker_id=my_speaker_id, you_label=you_label,
            them_label=them_label, use_speaker_library=use_library,
            diarization_threshold=threshold)
    return _transcriber_cache[key]


def transcribe_audio(filename: str, language: Optional[str] = None) -> str:
    """
    Transcribe audio using the configured provider

    This is the high-level function that the rest of the app calls.
    It routes to the appropriate provider based on settings.

    Args:
        filename: Path to the audio file to transcribe
        language: Optional language override (uses settings default if not provided)

    Returns:
        Transcribed text

    Raises:
        Exception: If transcription fails
    """
    # Meeting-mode recordings (2-channel: mic + system audio) always route to
    # ElevenLabs Scribe multichannel, which attributes speakers by channel.
    if is_multichannel_recording(filename):
        logger.info("Meeting recording detected; using ElevenLabs Scribe multichannel")
        return _get_meeting_transcriber().transcribe(filename)

    # Phone-mode recordings (mono, multiple speakers on one mic) route to
    # ElevenLabs Scribe with voice diarization for speaker attribution.
    if is_phone_recording(filename):
        logger.info("Phone recording detected; using ElevenLabs Scribe diarization")
        return _get_phone_transcriber().transcribe(filename)

    provider = settings.get('stt_provider') or _default_provider()

    # Get language from parameter or settings
    if language is None:
        language = settings.get('stt_language') or 'en'

    try:
        transcriber = _get_transcriber(provider)

        # Get model info if available
        model_info = ""
        if hasattr(transcriber, 'model'):
            model_info = f"/{transcriber.model}"

        logger.info(f"Using provider: {provider}{model_info}, language: {language}")

        # Update language if provided as parameter
        if language and hasattr(transcriber, 'update_language'):
            transcriber.update_language(language)

        # Transcribe the audio
        result = transcriber.transcribe(filename)
        return result

    except Exception as e:
        logger.error(f"Transcription failed with provider {provider}: {e}")
        raise


def set_stt_provider(provider: str) -> None:
    """
    Change the active STT provider

    Args:
        provider: Provider name ('elevenlabs', 'openai', 'custom')
    """
    # Validate provider
    try:
        _get_transcriber(provider)  # This will raise if provider is invalid
        settings.set('stt_provider', provider)
        logger.info(f"STT provider changed to: {provider}")
    except ValueError as e:
        logger.error(f"Failed to set STT provider: {e}")
        raise


def get_current_provider() -> str:
    """Get the currently configured STT provider (resolving the auto default)"""
    return settings.get('stt_provider') or _default_provider()


def get_available_providers() -> list:
    """Get list of available STT providers"""
    providers = []

    # Check ElevenLabs (preferred default when its key is set; also powers
    # meeting/phone modes)
    if api_key_configured("ELEVENLABS_API_KEY"):
        providers.append({
            'name': 'elevenlabs',
            'display_name': 'ElevenLabs Scribe',
            'models': ['scribe_v2']
        })

    # Check OpenAI
    if api_key_configured("OPENAI_API_KEY"):
        providers.append({
            'name': 'openai',
            'display_name': 'OpenAI',
            'models': ['whisper-1', 'gpt-4o-transcribe', 'gpt-4o-mini-transcribe']
        })

    # Custom STT provider is always available (for local or remote models)
    providers.append({
        'name': 'custom',
        'display_name': 'Custom STT',
        'models': ['parakeet-tdt-0.6b-v2', 'whisper', 'faster-whisper'],  # Common models
        'configurable': True  # Indicates URL and model can be configured
    })

    return providers