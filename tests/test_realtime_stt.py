"""Streaming dictation session (services/openai_realtime_stt.py).

Contract: "any failure falls back to the batch upload". A session that lost
a turn, saw a server error, or died mid-way must raise from finish() rather
than return partial text as if it were the whole dictation. And the final
commit must be sent after every buffered audio chunk, never before.

The websocket is a fake; the sender thread is real.
"""
import json
import threading
from typing import List

import numpy as np
import pytest

from services.openai_realtime_stt import RealtimeDictationSession, StreamingSessionError


class FakeWs:
    def __init__(self) -> None:
        self.sent: List[dict] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def close(self) -> None:
        self.closed = True


def make_session(monkeypatch: pytest.MonkeyPatch) -> tuple[RealtimeDictationSession, FakeWs]:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = RealtimeDictationSession()
    ws = FakeWs()
    s._ws = ws
    s._session_ready.set()
    threading.Thread(target=s._send_loop, daemon=True).start()
    return s, ws


def one_good_turn(s: RealtimeDictationSession, text: str = "hello world") -> None:
    s._handle_event({"type": "input_audio_buffer.speech_started"})
    s._handle_event({"type": "input_audio_buffer.committed"})
    s._handle_event({"type": "conversation.item.added"})
    s._handle_event({"type": "conversation.item.input_audio_transcription.completed",
                     "transcript": text})


def test_commit_is_sent_after_every_buffered_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    s, ws = make_session(monkeypatch)
    for _ in range(30):
        s.feed(np.zeros(2400, dtype=np.float32))
    one_good_turn(s)
    assert s.finish() == "hello world"
    types = [m["type"] for m in ws.sent]
    assert types.count("input_audio_buffer.commit") == 1
    assert types.index("input_audio_buffer.commit") == len(types) - 1
    assert types.count("input_audio_buffer.append") >= 30
    assert ws.closed


def test_failed_turn_after_good_turns_forces_batch_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    s, _ = make_session(monkeypatch)
    one_good_turn(s, "first part")
    s._handle_event({"type": "input_audio_buffer.speech_started"})
    s._handle_event({"type": "input_audio_buffer.committed"})
    s._handle_event({"type": "conversation.item.added"})
    s._handle_event({"type": "conversation.item.input_audio_transcription.failed",
                     "error": {"message": "audio too noisy"}})
    with pytest.raises(StreamingSessionError, match="turn transcription failed"):
        s.finish()


def test_server_error_after_setup_forces_batch_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    s, _ = make_session(monkeypatch)
    one_good_turn(s)
    s._handle_event({"type": "error", "error": {"code": "rate_limit_exceeded", "message": "slow down"}})
    with pytest.raises(StreamingSessionError, match="rate_limit_exceeded"):
        s.finish()


def test_expected_empty_commit_error_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    s, _ = make_session(monkeypatch)
    one_good_turn(s)
    s._handle_event({"type": "error", "error": {"code": "input_audio_buffer_commit_empty"}})
    assert s.finish() == "hello world"


def test_session_that_died_mid_recording_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    s, _ = make_session(monkeypatch)
    one_good_turn(s)
    s._dead.set()
    with pytest.raises(StreamingSessionError):
        s.finish()
