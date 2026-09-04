"""System-audio (WASAPI loopback) recorder for meeting mode.

Captures whatever is playing on the default output device (meeting audio,
etc.) via the `soundcard` library. Runs alongside the normal microphone
recorder; the two streams are composed into a 2-channel WAV after recording.
"""
import logging
import threading
import time
from typing import Optional

import numpy as np

logger = logging.getLogger('voice_typing')


def loopback_available() -> tuple[bool, str]:
    """Check whether system-audio capture is possible right now."""
    try:
        import soundcard as sc
        speaker = sc.default_speaker()
        sc.get_microphone(speaker.id, include_loopback=True)
        return True, speaker.name
    except Exception as e:
        return False, str(e)


class LoopbackRecorder(threading.Thread):
    """Records the default output device's loopback until stop() is called.

    Mono float32 blocks accumulate in memory; a 15-minute recording at
    22.05 kHz is ~75 MB, well within reason for meeting clips.
    """

    def __init__(self, samplerate: int = 22050) -> None:
        super().__init__(daemon=True)
        self.samplerate = samplerate
        self.blocksize = samplerate // 10  # 100ms blocks
        self._stop_event = threading.Event()
        # Guards _blocks and _sealed: a capture thread stuck in the driver
        # can outlive stop(), and must then neither grow the buffer forever
        # nor append while audio() is concatenating it
        self._lock = threading.Lock()
        self._blocks: list[np.ndarray] = []
        self._sealed = False
        self.first_block_time: Optional[float] = None
        self.error: Optional[Exception] = None

    def run(self) -> None:
        try:
            import soundcard as sc
            speaker = sc.default_speaker()
            loopback = sc.get_microphone(speaker.id, include_loopback=True)
            logger.info(f"Loopback capture started on: {speaker.name}")
            with loopback.recorder(samplerate=self.samplerate,
                                   blocksize=self.blocksize) as rec:
                while not self._stop_event.is_set():
                    data = rec.record(numframes=self.blocksize)
                    if self.first_block_time is None:
                        self.first_block_time = time.time()
                    block = data.mean(axis=1).astype(np.float32)
                    with self._lock:
                        if self._sealed:
                            break
                        self._blocks.append(block)
        except Exception as e:
            self.error = e
            logger.error(f"Loopback capture failed: {e}")

    def stop(self) -> None:
        """Stop capture and seal the buffer. If the thread is stuck in the
        audio driver it may keep running, but it can no longer add audio."""
        self._stop_event.set()
        self.join(timeout=3.0)
        with self._lock:
            self._sealed = True
        if self.is_alive():
            logger.warning("Loopback recorder thread did not stop cleanly; buffer sealed")

    def audio(self) -> np.ndarray:
        """Captured mono audio as float32; empty array if capture failed."""
        with self._lock:
            blocks = list(self._blocks)
        if not blocks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(blocks)
