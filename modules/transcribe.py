"""Multi-provider Speech-to-Text module with Strategy pattern"""
import os
import logging
from typing import Union, Optional
from pathlib import Path
from dotenv import load_dotenv

# Import all provider classes
from services.openai_stt import OpenAITranscriber
from services.google_stt import GoogleTranscriber
from services.custom_stt import CustomTranscriber
from modules.settings import Settings

# OpenAI Speech to text docs: https://platform.openai.com/docs/guides/speech-to-text
# ⚠️ IMPORTANT: OpenAI Audio API file uploads are currently limited to 25 MB

load_dotenv()

logger = logging.getLogger('voice_typing')

# Initialize settings
settings = Settings()


def _get_transcriber(provider_name: str):
    """
    Factory function to get a transcriber instance based on provider name

    Args:
        provider_name: Name of the provider ('openai', 'google', etc.)

    Returns:
        Transcriber instance for the specified provider

    Raises:
        ValueError: If provider is unknown
    """
    if provider_name == "openai":
        model = settings.get('openai_stt_model') or 'gpt-4o-mini-transcribe'
        language = settings.get('stt_language') or 'en'
        return OpenAITranscriber(model=model, language=language)
    elif provider_name == "google":
        language = settings.get('google_stt_language') or 'en-US'
        return GoogleTranscriber(language=language)
    elif provider_name == "custom":
        base_url = settings.get('custom_stt_base_url') or 'http://localhost:8000'
        model = settings.get('custom_stt_model') or 'parakeet-tdt-0.6b-v2'
        language = settings.get('stt_language') or 'en'
        return CustomTranscriber(base_url=base_url, model=model, language=language)
    # Add other providers here as needed
    else:
        raise ValueError(f"Unknown STT provider: {provider_name}")


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
    provider = settings.get('stt_provider') or 'openai'

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
        provider: Provider name ('openai', 'google', etc.)
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
    """Get the currently configured STT provider"""
    return settings.get('stt_provider') or 'openai'


def get_available_providers() -> list:
    """Get list of available STT providers"""
    providers = []

    # Check OpenAI
    if os.environ.get("OPENAI_API_KEY"):
        providers.append({
            'name': 'openai',
            'display_name': 'OpenAI',
            'models': ['whisper-1', 'gpt-4o-transcribe', 'gpt-4o-mini-transcribe']
        })

    # Check Google
    if os.environ.get("GOOGLE_CLOUD_API_KEY"):
        providers.append({
            'name': 'google',
            'display_name': 'Google Cloud',
            'models': []  # Google doesn't have selectable models in same way
        })
    
    # Custom STT provider is always available (for local or remote models)
    providers.append({
        'name': 'custom',
        'display_name': 'Custom STT',
        'models': ['parakeet-tdt-0.6b-v2', 'whisper', 'faster-whisper'],  # Common models
        'configurable': True  # Indicates URL and model can be configured
    })

    return providers


# Maintain backward compatibility with old function signature
def transcribe_audio_legacy(filename: str, language: str = "en") -> str:
    """Legacy function for backward compatibility"""
    return transcribe_audio(filename, language)