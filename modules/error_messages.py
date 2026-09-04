"""Turns transcription exceptions into short, actionable user-facing text.

The overlay and tray tooltip used to say "Transcription failed" for every
failure, which hid the one thing worth knowing (out of credits, bad key, no
network). `describe_transcription_error` classifies the exception into a
one-line reason plus a hint at the fix.

Classification is deliberately keyword-first and status-code-second: providers
overload status codes (ElevenLabs returns 401 for an exhausted quota, not just
for a bad key), so the message body is the more reliable signal.
"""

import re
from typing import NamedTuple, Optional


PROVIDER_NAMES = {
    'elevenlabs': 'ElevenLabs',
    'openai': 'OpenAI',
    'custom': 'Custom STT',
}

# Longest raw error text appended to the generic fallback hint
_MAX_DETAIL_CHARS = 90


class TranscriptionFailure(NamedTuple):
    """A classified failure ready for display.

    short: one line for the status bar / tray tooltip.
    hint: what the user can do about it; may be empty.
    silent: control-flow outcome (cancelled, nothing recorded) — show nothing.
    """
    short: str
    hint: str = ""
    silent: bool = False

    @property
    def overlay(self) -> str:
        """Text for the recording indicator, which renders multiple lines."""
        return f"{self.short}\n{self.hint}" if self.hint else self.short


CANCELLED = TranscriptionFailure("cancelled", silent=True)
NO_RECORDING = TranscriptionFailure("no recording", silent=True)


def provider_label(provider: Optional[str]) -> str:
    """Display name for a provider id, or '' when it isn't known."""
    if not provider:
        return ""
    return PROVIDER_NAMES.get(provider, provider.title())


def _status_code(exc: BaseException, text: str) -> Optional[int]:
    """HTTP status from the exception object, else parsed out of its message."""
    for attr in ('status_code', 'status', 'code'):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    # e.g. "ElevenLabs API error 401: ...", "HTTP 429: ..."
    match = re.search(r'\b(?:error|status|HTTP)\s+([1-5]\d{2})\b', text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def describe_transcription_error(exc: BaseException,
                                 provider: Optional[str] = None) -> TranscriptionFailure:
    """Classify a transcription exception into displayable text.

    `provider` falls back to the `provider` attribute that transcribe_audio
    stamps onto the exception before re-raising it.
    """
    text = str(exc)
    lowered = text.lower()
    name = provider_label(provider or getattr(exc, 'provider', None))
    who = f"{name} " if name else ""
    exc_type = type(exc).__name__

    # --- Keyword matches first: providers reuse status codes ambiguously ---

    if isinstance(exc, FileNotFoundError) or 'audio file not found' in lowered:
        return TranscriptionFailure(
            "📂 Recording file is missing",
            "It may have been cleaned up already",
        )

    if 'not found in environment' in lowered or 'environment variable not set' in lowered:
        return TranscriptionFailure(
            f"🔑 {who}API key not set".strip(),
            "Add it to .env, then restart the app",
        )

    if any(k in lowered for k in
           ('quota', 'credits remaining', 'insufficient_quota', 'billing',
            'payment required', 'exceeds your')):
        return TranscriptionFailure(
            f"💳 {who}quota exhausted".strip(),
            "Add credits, or switch provider in the tray menu",
        )

    if 'rate limit' in lowered or 'too many requests' in lowered or exc_type == 'RateLimitError':
        return TranscriptionFailure(
            f"🚦 {who}rate limit hit".strip(),
            "Wait a few seconds, then retry",
        )

    if 'timeout' in lowered or 'timed out' in lowered or exc_type in (
            'APITimeoutError', 'ReadTimeout', 'ConnectTimeout', 'Timeout'):
        return TranscriptionFailure(
            f"⏱️ {who}request timed out".strip(),
            "Retry — long recordings take longer",
        )

    if exc_type in ('APIConnectionError', 'ConnectionError', 'ConnectError') or any(
            k in lowered for k in ('getaddrinfo', 'name or service not known',
                                   'failed to establish', 'connection aborted',
                                   'connection refused', 'network is unreachable',
                                   'ssl', 'max retries exceeded')):
        return TranscriptionFailure(
            "🌐 Can't reach the transcription service",
            "Check your internet connection, then retry",
        )

    if any(k in lowered for k in ('too large', 'maximum size', 'file size',
                                  'request entity too large')):
        return TranscriptionFailure(
            f"📦 Recording too large for {name}".strip() if name
            else "📦 Recording too large",
            "Record in shorter takes",
        )

    if any(k in lowered for k in ('api key', 'api_key', 'unauthorized',
                                  'invalid authentication', 'incorrect api key')):
        return TranscriptionFailure(
            f"🔑 {who}rejected the API key".strip(),
            "Check the key in your .env file",
        )

    # --- Status codes: only reached when the message itself said nothing ---

    code = _status_code(exc, text)
    if code == 429:
        return TranscriptionFailure(
            f"🚦 {who}rate limit hit".strip(),
            "Wait a few seconds, then retry",
        )
    if code in (401, 403):
        return TranscriptionFailure(
            f"🔑 {who}refused the request ({code})".strip(),
            "Check the API key and account status",
        )
    if code == 413:
        return TranscriptionFailure(
            "📦 Recording too large",
            "Record in shorter takes",
        )
    if code and 500 <= code <= 599:
        return TranscriptionFailure(
            f"🛠️ {who}server error ({code})".strip(),
            "Usually temporary — retry in a moment",
        )
    if code and 400 <= code <= 499:
        return TranscriptionFailure(
            f"⚠️ {who}rejected the audio ({code})".strip(),
            "See the log for the full response",
        )

    # --- Unclassified: still show the real error rather than a shrug ---

    detail = " ".join(text.split())
    if len(detail) > _MAX_DETAIL_CHARS:
        detail = detail[:_MAX_DETAIL_CHARS - 1].rstrip() + "…"
    return TranscriptionFailure(
        f"⚠️ {name} transcription failed" if name else "⚠️ Transcription failed",
        detail or exc_type,
    )
