"""OpenAI Realtime API streaming transcription for dictation (beta).

Streams microphone audio to a Realtime transcription session while the user
is still recording, so the transcript is essentially ready the moment they
stop — instead of uploading the whole file afterward and waiting.

Used only for normal dictation mode. The WAV file is still written to disk in
parallel; if the stream fails at any point the caller falls back to the batch
upload path, so streaming can only ever make things faster, not less reliable.
"""
import base64
import json
import logging
import os
import queue
import threading
import time
from typing import Optional

import numpy as np

logger = logging.getLogger('voice_typing')

REALTIME_URL = "wss://api.openai.com/v1/realtime"

# Realtime API pcm16 input is 24 kHz mono; the recorder switches to this rate
# for streamed recordings so chunks can be forwarded without resampling.
REALTIME_SAMPLE_RATE = 24000

# How long finish() waits for the tail of the audio to come back transcribed.
FINISH_TIMEOUT_S = 5.0


class StreamingSessionError(Exception):
    """The streaming session failed; caller should fall back to batch."""


class RealtimeDictationSession:
    """One websocket transcription session, spanning one recording.

    Lifecycle: start() -> feed(chunk) from the audio callback -> finish() or
    abort(). All websocket I/O happens on background threads; feed() only
    enqueues, so it is safe to call from the PortAudio callback thread.
    """

    # Defaults chosen empirically (st-vtt-bench, 2026-07-21): energy-based
    # server_vad missed 15-17% of words on quiet casual dictation regardless
    # of threshold, and near_field noise reduction made it far worse (86% loss
    # on phone-band audio). semantic_vad (model-based turn detection) cut
    # deletions to ~4% and beat even batch gpt-4o on casual speech. Pass an
    # explicit turn_detection dict (e.g. {"type": "server_vad", ...}) to
    # override; noise_reduction stays off because gpt-4o-transcribe is
    # noise-robust on its own.
    DEFAULT_TURN_DETECTION = {"type": "semantic_vad", "eagerness": "medium"}

    def __init__(self, model: str = "gpt-4o-transcribe", language: str = "en",
                 noise_reduction: Optional[str] = None,
                 turn_detection: Optional[dict] = None):
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise StreamingSessionError("OPENAI_API_KEY not set")
        self.api_key = api_key
        self.model = model
        self.language = language
        self.noise_reduction = noise_reduction
        self.turn_detection = turn_detection or self.DEFAULT_TURN_DETECTION

        self._ws = None
        self._send_queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=600)
        self._segments: list[str] = []
        self._segments_lock = threading.Lock()
        # Every committed speech turn produces a conversation item, and every
        # item eventually gets a transcription.completed (or .failed) event;
        # finish() waits for the counts to balance so no tail text is lost.
        self._items_added = 0
        self._items_finished = 0
        # VAD turns that have started but not yet been committed; finish()
        # must not conclude while one is open or its text would be lost
        self._turns_open = 0
        self._last_event_time = time.time()
        self._dead = threading.Event()
        self._session_ready = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._sender_thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self, connect_timeout: float = 3.0) -> None:
        """Connect and configure the session. Raises StreamingSessionError."""
        import websocket  # websocket-client; imported lazily (startup cost)

        try:
            self._ws = websocket.create_connection(
                REALTIME_URL + "?intent=transcription",
                header=[f"Authorization: Bearer {self.api_key}"],
                timeout=connect_timeout,
            )
            # Generous read timeout once connected; reader thread blocks on recv
            self._ws.settimeout(10.0)
        except Exception as e:
            raise StreamingSessionError(f"Realtime connect failed: {e}") from e

        input_config = {
            "format": {"type": "audio/pcm", "rate": REALTIME_SAMPLE_RATE},
            "transcription": {
                "model": self.model,
                "language": self.language,
            },
            "turn_detection": self.turn_detection,
        }
        if self.noise_reduction:
            input_config["noise_reduction"] = {"type": self.noise_reduction}
        self._send_json({
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {"input": input_config},
            },
        })

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        self._sender_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._sender_thread.start()

        if not self._session_ready.wait(timeout=connect_timeout):
            self.abort()
            raise StreamingSessionError(
                f"Session setup not acknowledged ({self.error or 'timeout'})")

    def feed(self, indata: np.ndarray) -> None:
        """Enqueue an audio chunk (float32, mono or (n,1)). Never blocks/raises."""
        if self._dead.is_set():
            return
        try:
            samples = indata[:, 0] if indata.ndim > 1 else indata
            pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype('<i2').tobytes()
            self._send_queue.put_nowait(pcm16)
        except queue.Full:
            # Drop rather than stall the audio callback; batch fallback covers us
            logger.warning("Streaming send queue full; marking session dead")
            self._dead.set()
        except Exception:
            self._dead.set()

    def finish(self) -> str:
        """Flush the tail, wait for final transcripts, return the full text.

        Raises StreamingSessionError if the session died or produced nothing.
        """
        if self._dead.is_set():
            raise StreamingSessionError(self.error or "session died mid-recording")

        # Flush the final turn: the user usually stops recording right after
        # the last word, so the VAD never sees the trailing silence it needs
        # to close the turn. Feed it synthetic silence, then commit as a
        # belt-and-braces (an "empty buffer" error on the commit is normal).
        silence = np.zeros(REALTIME_SAMPLE_RATE // 10, dtype=np.float32)
        for _ in range(8):  # 800ms > silence_duration_ms (400ms)
            self.feed(silence)

        # Wait for the sender to drain what the audio callback enqueued
        deadline = time.time() + 2.0
        while not self._send_queue.empty() and time.time() < deadline:
            time.sleep(0.02)

        self._send_json({"type": "input_audio_buffer.commit"})

        # Wait until every committed speech turn has its final transcription.
        # The tail commit needs a moment to register as a new item, so also
        # require a short quiet period before trusting the balanced counts.
        deadline = time.time() + FINISH_TIMEOUT_S
        while time.time() < deadline:
            if self._dead.is_set():
                break
            no_open_turn = self._turns_open <= 0
            counts_balanced = self._items_finished >= self._items_added
            gone_quiet = time.time() - self._last_event_time > 0.5
            if no_open_turn and counts_balanced and gone_quiet and self._segments:
                break
            time.sleep(0.05)

        self.abort()

        with self._segments_lock:
            text = " ".join(s.strip() for s in self._segments if s.strip()).strip()
        if not text:
            raise StreamingSessionError(self.error or "no transcript received")
        return text

    def abort(self) -> None:
        """Close the websocket and stop threads. Safe to call repeatedly."""
        self._dead.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # -- internals ---------------------------------------------------------

    def _send_json(self, payload: dict) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            ws.send(json.dumps(payload))
        except Exception as e:
            self.error = f"send failed: {e}"
            self._dead.set()

    def _send_loop(self) -> None:
        while not self._dead.is_set():
            try:
                chunk = self._send_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is None:
                break
            self._send_json({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode("ascii"),
            })

    def _read_loop(self) -> None:
        import socket
        import websocket
        while not self._dead.is_set():
            ws = self._ws
            if ws is None:
                break
            try:
                raw = ws.recv()
            except (websocket.WebSocketTimeoutException, socket.timeout):
                # No event within the read timeout — normal during a long
                # uninterrupted speech turn (server only emits events on VAD
                # boundaries). Keep listening.
                continue
            except Exception as e:
                if not self._dead.is_set():
                    self.error = f"recv failed: {e}"
                    self._dead.set()
                break
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            self._handle_event(event)

    def _handle_event(self, event: dict) -> None:
        etype = event.get("type", "")
        self._last_event_time = time.time()

        if etype in ("session.created", "session.updated",
                     "transcription_session.created", "transcription_session.updated"):
            self._session_ready.set()
        elif etype == "input_audio_buffer.speech_started":
            self._turns_open += 1
        elif etype == "input_audio_buffer.committed":
            self._turns_open = max(0, self._turns_open - 1)
        elif etype == "conversation.item.added":
            self._items_added += 1
        elif etype == "conversation.item.input_audio_transcription.completed":
            self._items_finished += 1
            transcript = event.get("transcript", "")
            if transcript:
                with self._segments_lock:
                    self._segments.append(transcript)
        elif etype == "conversation.item.input_audio_transcription.failed":
            # Count it so finish() doesn't wait forever on a failed turn
            self._items_finished += 1
            logger.warning(f"Realtime transcription failed for one turn: "
                           f"{event.get('error', {}).get('message', '')}")
        elif etype == "error":
            err = event.get("error", {}) or {}
            code = err.get("code", "")
            # Committing an already-empty buffer at finish() is expected
            if code == "input_audio_buffer_commit_empty":
                return
            self.error = f"{code or 'error'}: {err.get('message', '')}"
            logger.warning(f"Realtime session error event: {self.error}")
            # Setup errors are fatal; transcription errors for one turn are not
            if not self._session_ready.is_set():
                self._dead.set()
