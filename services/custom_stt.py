"""Custom / self-hosted speech-to-text over the OpenAI-compatible API.

Every local STT server people actually run (speaches / faster-whisper-server,
whisper.cpp server, the parakeet FastAPI images, LocalAI, vLLM audio) speaks
``POST {base_url}/v1/audio/transcriptions`` with a multipart ``file`` field
and answers ``{"text": ...}``. So the custom provider is the OpenAI client
pointed at a different base URL, uploading WAV (which every server decodes)
instead of FLAC. Optional ``CUSTOM_STT_API_KEY`` in .env is sent as the
bearer token.

Before 1.0 this module probed three endpoint shapes and guessed among seven
response fields; that compatibility layer was removed as part of the
OpenAI-compatible-only contract documented in README.md.
"""
import os

from services.openai_stt import OpenAITranscriber


def api_root(base_url: str) -> str:
    """Normalize a user-entered server URL to the API root the client expects.

    ``http://localhost:8000`` and ``http://localhost:8000/`` become
    ``http://localhost:8000/v1``; a URL already ending in ``/v1`` is kept.
    """
    url = base_url.strip().rstrip('/')
    return url if url.endswith('/v1') else url + '/v1'


class CustomTranscriber(OpenAITranscriber):
    """OpenAI-compatible transcription against a user-configured server."""

    def __init__(self, base_url: str, model: str = "parakeet-tdt-0.6b-v2",
                 language: str = "en") -> None:
        super().__init__(
            model=model,
            language=language,
            # The OpenAI client insists on a key; local servers ignore it
            api_key=os.environ.get("CUSTOM_STT_API_KEY") or "not-needed",
            base_url=api_root(base_url),
            upload_format='WAV',
        )
        self.base_url = base_url
