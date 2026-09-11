"""Live microphone level monitor for the setup window's mic test.

Opens its own short input stream on the chosen device (independent of the
recorder) and keeps a rolling speaking-level estimate so the UI can tell the
user whether this microphone is actually usable. Thread-safe; the UI polls.

IMPORTANT: the verdict here comes from modules/audio_level.py, the same code
the recorder uses to judge a finished recording. They used to disagree — this
monitor scored the *peak* over a 3-second window while the recorder scored the
*mean* of a whole file, so the panel could say "hearing you loud and clear"
about a microphone whose recordings were then rejected as silence. Any new
level judgement belongs in audio_level.py, not here.
"""
import logging
from typing import Optional

from modules.audio_level import (
    GOOD_DB,
    LiveLevelTracker,
    VERDICT_OK,
    to_db,
)

logger = logging.getLogger('voice_typing')

WINDOW_S = 4.0

# Meter scale. Speech sits between roughly -45 dBFS (barely usable) and -10
# dBFS (hot), so a dB scale over this range keeps a working microphone in the
# middle of the meter; the old linear mapping left normal speech in the first
# two bars, which read as "almost nothing" no matter what the text said.
METER_FLOOR_DB = -55.0
METER_CEILING_DB = -6.0


class MicMonitor:
    def __init__(self, device_id: Optional[int]) -> None:
        self.device_id = device_id
        self._stream = None
        self._tracker = LiveLevelTracker(window_s=WINDOW_S)
        self._instant_db = METER_FLOOR_DB
        self.error: Optional[str] = None
        self.heard_speech = False

    def start(self) -> None:
        import time

        import numpy as np
        import sounddevice as sd

        def callback(indata, frames, time_info, status) -> None:
            # Realtime callback: keep it to the RMS and an append. Judging the
            # verdict here meant a numpy allocate-and-sort on every block, and
            # the UI already polls verdict() 25 times a second anyway.
            rms = float(np.sqrt(np.mean(np.square(indata), dtype=np.float64)))
            self._tracker.add(rms, time.monotonic(), len(indata) / float(samplerate))
            self._instant_db = to_db(rms)

        stream = None
        try:
            samplerate = float(sd.query_devices(self.device_id, 'input')['default_samplerate'])
        except Exception:
            samplerate = 44100.0
        try:
            stream = sd.InputStream(device=self.device_id, channels=1,
                                    blocksize=2048, callback=callback)
            stream.start()
            self._stream = stream
        except Exception as e:
            logger.warning(f"Mic test could not open device {self.device_id}: {e}")
            self.error = str(e)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

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
        """Instantaneous level mapped to 0..1 for the meter, on a dB scale."""
        span = METER_CEILING_DB - METER_FLOOR_DB
        return max(0.0, min(1.0, (self._instant_db - METER_FLOOR_DB) / span))

    def target_level(self) -> float:
        """Where on the meter a healthy speaking level starts."""
        span = METER_CEILING_DB - METER_FLOOR_DB
        return max(0.0, min(1.0, (GOOD_DB - METER_FLOOR_DB) / span))

    def speech_db(self) -> float:
        """Estimated speaking level over the rolling window, in dBFS."""
        return self._tracker.speech_db()

    def verdict(self) -> str:
        """'error', or one of the audio_level VERDICT_* values for the window.

        Also latches ``heard_speech``, so the UI poll is the only thing that
        pays for the judgement (see the note in the audio callback).
        """
        if self.error:
            return 'error'
        verdict = self._tracker.verdict()
        if verdict == VERDICT_OK:
            self.heard_speech = True
        return verdict

    def reset(self) -> None:
        self._tracker.reset()
        self.heard_speech = False
        self._instant_db = METER_FLOOR_DB
