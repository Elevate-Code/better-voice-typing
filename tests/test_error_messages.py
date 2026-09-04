"""Transcription error classification (modules/error_messages.py).

Contract: the overlay names the real cause (out of credits, bad key, offline,
too large) and what to do about it, and picks the provider by name. Keyword
matching beats HTTP status, because providers overload status codes
(ElevenLabs answers 401 for an exhausted quota). Unclassified errors still
show their text, but capped.
"""
import pytest

from modules.error_messages import (
    CANCELLED, NO_RECORDING, TranscriptionFailure, describe_transcription_error,
    provider_label,
)


class RateLimitError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class APITimeoutError(Exception):
    pass


def stamped(exc: BaseException, provider: str) -> BaseException:
    """Mirror what modules.transcribe does before re-raising."""
    exc.provider = provider  # type: ignore[attr-defined]
    return exc


@pytest.mark.parametrize("exc, provider, expect_short, expect_hint", [
    # ElevenLabs returns 401 for an exhausted quota: keyword must win
    (Exception("ElevenLabs API error 401: You have 0 credits remaining"),
     "elevenlabs", "💳 ElevenLabs quota exhausted", "Add credits"),
    (Exception("insufficient_quota: You exceeded your current quota"),
     "openai", "💳 OpenAI quota exhausted", "Add credits"),
    (RateLimitError("Rate limit reached for whisper-1"),
     "openai", "🚦 OpenAI rate limit hit", "Wait a few seconds"),
    (Exception("HTTP 429: slow down"), "custom", "🚦 Custom STT rate limit hit", "Wait"),
    (APITimeoutError("Request timed out."), "openai",
     "⏱️ OpenAI request timed out", "Retry"),
    (APIConnectionError("Connection error."), "openai",
     "🌐 Can't reach the transcription service", "internet"),
    (Exception("[Errno 11001] getaddrinfo failed"), "elevenlabs",
     "🌐 Can't reach the transcription service", "internet"),
    (Exception("OPENAI_API_KEY not found in environment"), "openai",
     "🔑 OpenAI API key not set", ".env"),
    (Exception("Incorrect API key provided: sk-abc"), "openai",
     "🔑 OpenAI rejected the API key", ".env"),
    (Exception("413 Request Entity Too Large"), "elevenlabs",
     "📦 Recording too large for ElevenLabs", "shorter"),
    (FileNotFoundError("temp_audio.wav.3.wav"), "openai",
     "📂 Recording file is missing", "cleaned up"),
])
def test_known_causes_get_a_named_reason_and_a_fix(
        exc: BaseException, provider: str, expect_short: str, expect_hint: str) -> None:
    failure = describe_transcription_error(stamped(exc, provider))
    assert failure.short == expect_short
    assert expect_hint in failure.hint
    assert not failure.silent


def test_status_code_attribute_is_used_when_the_message_says_nothing() -> None:
    exc = Exception("Service Unavailable")
    exc.status_code = 503  # type: ignore[attr-defined]
    failure = describe_transcription_error(stamped(exc, "elevenlabs"))
    assert failure.short == "🛠️ ElevenLabs server error (503)"


def test_generic_4xx_points_at_the_log() -> None:
    failure = describe_transcription_error(Exception("HTTP 422: bad audio"))
    assert failure.short == "⚠️ rejected the audio (422)"
    assert "log" in failure.hint


def test_explicit_provider_argument_beats_the_stamped_one() -> None:
    exc = stamped(Exception("quota"), "openai")
    assert "ElevenLabs" in describe_transcription_error(exc, provider="elevenlabs").short


def test_unknown_error_shows_its_text_capped() -> None:
    """The fallback must not become a wall of provider JSON on the overlay."""
    text = "x" * 300
    failure = describe_transcription_error(Exception(text))
    assert failure.short == "⚠️ Transcription failed"
    assert failure.hint.endswith("…")
    assert len(failure.hint) <= 90


def test_unknown_error_with_empty_message_falls_back_to_the_type_name() -> None:
    class WeirdError(Exception):
        pass
    failure = describe_transcription_error(WeirdError())
    assert failure.hint == "WeirdError"


def test_unknown_error_collapses_whitespace() -> None:
    failure = describe_transcription_error(Exception("line one\n\n   line   two"))
    assert failure.hint == "line one line two"


def test_silent_outcomes_are_control_flow_not_errors() -> None:
    assert CANCELLED.silent and NO_RECORDING.silent
    assert not describe_transcription_error(Exception("boom")).silent


def test_overlay_joins_short_and_hint_on_separate_lines() -> None:
    assert TranscriptionFailure("a", "b").overlay == "a\nb"
    assert TranscriptionFailure("a").overlay == "a"


def test_provider_label_handles_unknown_and_missing() -> None:
    assert provider_label("elevenlabs") == "ElevenLabs"
    assert provider_label("whisperx") == "Whisperx"
    assert provider_label(None) == ""
