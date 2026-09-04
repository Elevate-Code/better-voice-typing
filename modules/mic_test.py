"""Live microphone level monitor for the setup window's mic test.

Opens its own short input stream on the chosen device (independent of the
recorder) and keeps the latest RMS level plus the peak over a rolling window
so the UI can say "hearing you" / "very quiet". Thread-safe; the UI polls.
"""
import logging
import threading
import time
from collections import deque
from typing import Deque, Optional, Tuple

logger = logging.getLogger('voice_typing')

# Speech through a working mic lands well above this RMS; the recorder's
# silence threshold is 0.01
SPEECH_RMS = 0.02
QUIET_RMS = 0.006
WINDOW_S = 3.0


class MicMonitor:
    def __init__(self, device_id: Optional[int]) -> None:
        self.device_id = device_id
        self._stream = None
        self._lock = threading.Lock()
        self._level = 0.0
        self._history: Deque[Tuple[float, float]] = deque()
        self.error: Optional[str] = None
        self.heard_speech = False

    def start(self) -> None:
        import numpy as np
        import sounddevice as sd

        def callback(indata, frames, time_info, status) -> None:
            rms = float(np.sqrt(np.mean(np.square(indata), dtype=np.float64)))
            now = time.monotonic()
            with self._lock:
                self._level = rms
                self._history.append((now, rms))
                while self._history and now - self._history[0][0] > WINDOW_S:
                    self._history.popleft()
                if rms >= SPEECH_RMS:
                    self.heard_speech = True

        try:
            self._stream = sd.InputStream(device=self.device_id, channels=1,
                                          blocksize=2048, callback=callback)
            self._stream.start()
        except Exception as e:
            logger.warning(f"Mic test could not open device {self.device_id}: {e}")
            self.error = str(e)
            self._stream = None

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    @property
    def running(self) -> bool:
        return self._stream is not None

    def level(self) -> float:
        """Current RMS mapped to 0..1 for a meter (speech fills most of it)."""
        with self._lock:
            return min(1.0, self._level / 0.12)

    def peak(self) -> float:
        with self._lock:
            return max((r for _, r in self._history), default=0.0)

    def verdict(self) -> str:
        """'silent', 'quiet', 'ok' or 'error' for the rolling window."""
        if self.error:
            return 'error'
        peak = self.peak()
        if peak >= SPEECH_RMS:
            return 'ok'
        if peak >= QUIET_RMS:
            return 'quiet'
        return 'silent'
