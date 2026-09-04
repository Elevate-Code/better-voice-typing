"""Custom STT = OpenAI client on a user-entered base URL (services/custom_stt.py).

Contract: settings written for pre-1.0 (`http://host:8000`, no /v1) keep
working, a URL that already names the API root is not doubled, and the
provider never needs OPENAI_API_KEY.
"""
import os

import pytest

from services.custom_stt import CustomTranscriber, api_root


@pytest.mark.parametrize("given, expected", [
    ("http://localhost:8000", "http://localhost:8000/v1"),
    ("http://localhost:8000/", "http://localhost:8000/v1"),
    ("http://192.168.1.100:5000/v1", "http://192.168.1.100:5000/v1"),
    ("https://stt.example.com/v1/", "https://stt.example.com/v1"),
    ("  http://localhost:8000  ", "http://localhost:8000/v1"),
])
def test_api_root_normalizes_user_urls(given: str, expected: str) -> None:
    assert api_root(given) == expected


def test_constructs_without_openai_key_and_uploads_wav(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_STT_API_KEY", raising=False)
    t = CustomTranscriber("http://localhost:8000", model="whisper")
    assert str(t.client.base_url).rstrip("/") == "http://localhost:8000/v1"
    assert t.model == "whisper"
    assert t.upload_format == "WAV"


def test_custom_api_key_is_used_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_STT_API_KEY", "secret-123")
    t = CustomTranscriber("http://localhost:8000")
    assert t.client.api_key == "secret-123"
    assert os.environ["CUSTOM_STT_API_KEY"] == "secret-123"
