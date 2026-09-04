"""OpenAI Speech-to-Text Service Implementation"""
import os
import logging
from typing import Optional, Union
from pathlib import Path
import io
import soundfile as sf
import numpy as np
from openai import OpenAI
import httpx

logger = logging.getLogger('voice_typing')

# NOTE: Temp workaround for OpenAI bug where transcription cuts off.
# See: https://community.openai.com/t/gpt-4o-transcribe-truncates-the-transcript/1148347
PADDING_DURATION_S = 1.5
NOISE_AMPLITUDE = 0.08


def _make_brown_noise(samples: int, amplitude: float) -> np.ndarray:
    """Quiet brown noise (integrated white noise) — sounds more organic than white noise."""
    white_noise = np.random.randn(samples).astype('float32')
    brown_noise_unscaled = np.cumsum(white_noise)
    max_abs_val = np.max(np.abs(brown_noise_unscaled))
    if max_abs_val > 0:
        return (brown_noise_unscaled / max_abs_val) * amplitude
    return np.zeros_like(brown_noise_unscaled)


def _prepare_upload(
    audio_data: Union[bytes, str, Path],
    pad_duration_s: float = 0.0,
    noise_amplitude: float = NOISE_AMPLITUDE,
    upload_format: str = 'FLAC',
) -> io.BytesIO:
    """
    Loads audio (bytes or file path), optionally pads the end with quiet brown
    noise, and encodes it for upload.

    FLAC is lossless and roughly halves the upload size versus WAV, which cuts
    request latency and doubles the recording length that fits under OpenAI's
    25 MB upload cap. Self-hosted servers get WAV, which every server decodes.
    """
    input_stream = io.BytesIO(audio_data) if isinstance(audio_data, bytes) else audio_data
    data, samplerate = sf.read(input_stream, dtype='float32')

    if pad_duration_s > 0:
        padding_samples = int(pad_duration_s * samplerate)
        data = np.concatenate([data, _make_brown_noise(padding_samples, noise_amplitude)])

    buffer = io.BytesIO()
    sf.write(buffer, data, samplerate, format=upload_format, subtype='PCM_16')
    buffer.seek(0)
    buffer.name = f"audio.{upload_format.lower()}"
    return buffer


class OpenAITranscriber:
    """Speech-to-text over the OpenAI audio transcription API.

    Also the client for any OpenAI-compatible server (see services/custom_stt.py):
    pass base_url and api_key explicitly."""

    def __init__(self, model: str = "gpt-4o-mini-transcribe", language: str = "en",
                 api_key: Optional[str] = None, base_url: Optional[str] = None,
                 upload_format: str = 'FLAC'):
        """
        Args:
            model: Model to use ('whisper-1', 'gpt-4o-transcribe', 'gpt-4o-mini-transcribe')
            language: Language code for transcription (e.g., 'en', 'es', 'fr')
            api_key: Defaults to OPENAI_API_KEY from the environment
            base_url: OpenAI-compatible endpoint root (…/v1); None = api.openai.com
            upload_format: soundfile format name for the upload ('FLAC' or 'WAV')
        """
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set")

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            # Configure timeout: 60s total timeout, 10s connect timeout
            timeout=httpx.Timeout(60.0, connect=10.0)
        )
        self.model = model
        self.language = language
        self.upload_format = upload_format

    def transcribe(self, audio_data: Union[bytes, str, Path]) -> str:
        """
        Transcribe audio using OpenAI's API

        Args:
            audio_data: Either raw audio bytes, file path as string, or Path object

        Returns:
            Transcribed text

        Raises:
            Exception: If transcription fails
        """
        try:
            if isinstance(audio_data, (str, Path)) and not Path(audio_data).exists():
                raise FileNotFoundError(f"Audio file not found: {audio_data}")

            # Pad gpt-4o models as a truncation workaround; whisper needs no padding
            pad_duration = PADDING_DURATION_S if "gpt-4o" in self.model else 0.0
            if pad_duration:
                logger.debug(f"Padding audio with {pad_duration}s of quiet noise for {self.model}")

            file_to_send = _prepare_upload(audio_data, pad_duration,
                                           upload_format=self.upload_format)

            response = self.client.audio.transcriptions.create(
                model=self.model,
                file=file_to_send,
                language=self.language
            )
            return response.text

        except Exception as e:
            logger.error(f"OpenAI transcription failed: {e}", exc_info=True)
            raise
