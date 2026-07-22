import logging
import threading
from typing import Optional, Callable, Tuple, Any
import time

import numpy as np
import sounddevice as sd
import soundfile as sf

from modules.settings import Settings

logger = logging.getLogger('voice_typing')

# NOTE: Optimized settings for speech recording
# - 16kHz sample rate is optimal for STT, using 22.05kHz for safety margin
# - 16-bit depth is standard for speech
# - Mono channel as stereo provides no benefit
# - WAV format ensures compatibility and quality
# NOTE: Ends up being ~2.6 megabytes for every 60 seconds with these settings.

settings = Settings()

# Minimum duration in seconds for valid recordings
MIN_DURATION = 1.0

# WAV comment written into phone-mode recordings so the transcription pipeline
# can recognize them later (survives snapshots, retries, and app restarts —
# the mono audio itself is indistinguishable from normal dictation).
PHONE_RECORDING_COMMENT = 'voice_typing:phone'
# Time of continuous silence (in seconds) before auto-stopping
DEFAULT_SILENT_START_TIMEOUT = 4.0


def _silence_threshold() -> float:
    """RMS threshold below which audio is considered silence.
    (-30 dB = 0.0316, -40 dB = 0.01, -50 dB = 0.003) Configurable via settings.json."""
    return settings.get('silence_threshold')

class AudioRecorder:
    # Controls how smooth/reactive the audio level indicator bar appears in the UI
    # (0.0 to 1.0) Higher = more responsive but jerky, Lower = more smooth, but slower
    # 0.2 provides a good balance between smoothness and responsiveness
    SMOOTHING_FACTOR = 0.2

    def __init__(self, filename: str = 'temp_audio.wav',
                 level_callback: Optional[Callable[[float], None]] = None,
                 silent_start_timeout: Optional[float] = None) -> None:
        self.filename = filename
        self.recording = False
        self.thread: Optional[threading.Thread] = None
        self.level_callback = level_callback
        self.smoothed_level: float = 0.0
        self.stream: Optional[sd.InputStream] = None
        self.file: Optional[sf.SoundFile] = None
        self._lock: threading.Lock = threading.Lock()
        self.audio_data: list[np.ndarray] = []  # Store audio chunks for analysis
        self.silence_start: Optional[float] = None
        self.silent_start_timeout = silent_start_timeout
        self.auto_stopped = False
        self.max_duration_reached = False
        self.max_duration: Optional[float] = None
        self.recording_start_time: Optional[float] = None
        self.initial_sound_detected = False  # Track if we've detected any sound

        # Recording sample rate. Normally 22050; the app sets 24000 for
        # streamed dictation so chunks match the Realtime API without
        # resampling. Set before start(); applies to the whole recording.
        self.samplerate = 22050
        # Optional per-recording sink for live audio chunks (streaming
        # transcription). Called from the audio callback thread with a copy
        # of each block; must be cheap and never raise.
        self.stream_callback: Optional[Callable[[np.ndarray], None]] = None

        # Meeting mode: capture system audio (loopback) alongside the mic and
        # compose a 2-channel file (ch0 = mic, ch1 = system) on stop.
        # Set by the app before start(); read once per recording.
        self.meeting_mode = False
        # Phone mode: mic-only recording of an in-room conversation (e.g. a
        # phone call on speaker); the WAV is tagged so transcription routes
        # to voice diarization. Set by the app before start().
        self.phone_mode = False
        # Continuous conversation sessions set this for every chunk after the
        # first: the user may listen silently for long stretches, so the
        # silent-start check must not cancel a continuation chunk.
        self.continuation_chunk = False
        self._loopback = None  # LoopbackRecorder instance while recording
        self._mic_first_block_time: Optional[float] = None

    def _calculate_level(self, indata: np.ndarray) -> float:
        """Calculate audio level from input data"""
        rms = np.sqrt(np.mean(np.square(indata)))

        # Convert to dB for level display
        db = 20 * np.log10(max(1e-10, rms))
        normalized = (db + 60) / 60
        current_level = max(0.0, min(1.0, normalized))

        # Only check for silence at the start of the recording, before any sound
        # is detected. Skipped in meeting mode: the far side may be talking while
        # the user's mic is silent, so a quiet mic must not cancel the recording.
        if (self.silent_start_timeout is not None and
            not self.meeting_mode and
            not self.continuation_chunk and
            self.recording_start_time is not None and
            not self.initial_sound_detected):

            if rms < _silence_threshold():
                if self.silence_start is None:
                    self.silence_start = time.time()
                elif time.time() - self.silence_start >= self.silent_start_timeout:
                    logger.info(f"Stopping due to {self.silent_start_timeout}s of initial silence")
                    self.auto_stopped = True
                    self.recording = False
                    return 0.0
            else:
                # We've detected sound, stop checking for silence
                self.initial_sound_detected = True
                self.silence_start = None

        # Apply smoothing for UI feedback
        self.smoothed_level = (self.SMOOTHING_FACTOR * current_level) + \
                              ((1 - self.SMOOTHING_FACTOR) * self.smoothed_level)

        return self.smoothed_level

    def analyze_recording(self, filepath: Optional[str] = None) -> Tuple[bool, str]:
        """Analyze the recorded audio file for silence and duration.

        Returns:
            Tuple[bool, str]: (is_valid, reason_if_invalid)
        """
        try:
            with sf.SoundFile(filepath or self.filename) as audio_file:
                # Check duration
                duration = len(audio_file) / audio_file.samplerate
                if duration < MIN_DURATION:
                    return False, f"Recording too short ({duration:.1f}s < {MIN_DURATION}s)"

                # Read the entire file
                audio_data = audio_file.read()

                # Calculate RMS value
                rms = np.sqrt(np.mean(np.square(audio_data)))

                # Check if mostly silence
                threshold = _silence_threshold()
                if rms < threshold:
                    db_value = 20 * np.log10(max(1e-10, rms))
                    return False, f"Recording contains mostly silence (RMS: {rms:.4f} / {db_value:.1f}dB < threshold: {threshold:.4f})"

                return True, ""

        except Exception as e:
            return False, f"Error analyzing audio: {str(e)}"

    def _record(self) -> None:
        """Record audio in a separate thread"""
        def audio_callback(indata: np.ndarray,
                         frames: int,
                         time_info: Any,
                         status: int) -> None:
            if status:
                logger.warning(f'Audio callback status: {status}')

            if self._mic_first_block_time is None:
                self._mic_first_block_time = time.time()

            with self._lock:
                if not self.recording or self.file is None:
                    return

                if self.level_callback:
                    level = self._calculate_level(indata)
                    self.level_callback(level)

                    # If auto-stopped, stop the stream
                    if self.auto_stopped:
                        self.recording = False
                        raise sd.CallbackStop()

                # Stop at max duration but keep the audio for transcription
                if (self.max_duration is not None and
                        self.recording_start_time is not None and
                        time.time() - self.recording_start_time >= self.max_duration):
                    logger.warning(f"Max recording duration ({self.max_duration}s) reached, auto-stopping")
                    self.max_duration_reached = True
                    self.recording = False
                    raise sd.CallbackStop()

                # Only write audio data if not auto-stopped
                if not self.auto_stopped and self.file is not None:
                    try:
                        self.file.write(indata.copy())
                    except Exception as e:
                        logger.error(f"Audio callback error: {e}")
                        self.recording = False
                        raise sd.CallbackStop()

                    if self.stream_callback is not None:
                        try:
                            self.stream_callback(indata.copy())
                        except Exception:
                            pass  # streaming is best-effort; file is the source of truth

        try:
            with sf.SoundFile(self.filename, mode='w',
                            samplerate=self.samplerate,
                            channels=1,
                            subtype='PCM_16',
                            format='WAV') as self.file:
                if self.phone_mode:
                    self.file.comment = PHONE_RECORDING_COMMENT
                with sd.InputStream(samplerate=self.samplerate,
                                  channels=1,
                                  callback=audio_callback) as self.stream:
                    while self.recording:
                        sd.sleep(100)
        except Exception as e:
            logger.error(f"Recording error: {e}", exc_info=True)
            self.auto_stopped = True
        finally:
            with self._lock:
                if self.stream is not None:
                    try:
                        self.stream.close()
                    except:
                        pass
                    self.stream = None
                if self.file is not None:
                    try:
                        self.file.close()
                    except:
                        pass
                    self.file = None

    def start(self) -> None:
        """Start recording and reset silence detection"""
        self.auto_stopped = False
        self.max_duration_reached = False
        self.max_duration = settings.get('max_recording_duration')
        self.silence_start = None
        self.initial_sound_detected = False
        self._mic_first_block_time = None
        self._loopback = None
        if self.meeting_mode:
            try:
                from modules.loopback_recorder import LoopbackRecorder
                self._loopback = LoopbackRecorder(samplerate=self.samplerate)
                self._loopback.start()
            except Exception as e:
                logger.error(f"Could not start loopback capture, falling back to mic-only: {e}")
                self._loopback = None
        self.recording_start_time = time.time()
        self.recording = True
        self.thread = threading.Thread(target=self._record)
        self.thread.start()

    def stop(self) -> None:
        """Stop recording with timeout to prevent hanging"""
        with self._lock:
            self.recording = False

        if self.thread:
            # Add timeout to thread.join() to prevent hanging
            self.thread.join(timeout=2.0)
            if self.thread.is_alive():
                logger.warning("Recording thread did not stop cleanly")
                # Force cleanup
                with self._lock:
                    if self.stream is not None:
                        try:
                            self.stream.close()
                        except:
                            pass
                        self.stream = None
                    if self.file is not None:
                        try:
                            self.file.close()
                        except:
                            pass
                        self.file = None

        if self._loopback is not None:
            loopback = self._loopback
            self._loopback = None
            try:
                loopback.stop()
                self._compose_meeting_file(loopback)
            except Exception:
                logger.error("Failed to compose meeting recording; keeping mic-only audio",
                             exc_info=True)

    def _compose_meeting_file(self, loopback) -> None:
        """Overwrite the mic recording with a 2-channel file (ch0=mic, ch1=system).

        The two streams start at slightly different wall-clock times; the later
        starter is padded at its head so the timelines line up. If loopback
        capture produced nothing, the mono mic file is left untouched (the
        normal transcription path then applies).
        """
        import os
        loop_audio = loopback.audio()
        if loop_audio.size == 0 or not os.path.exists(self.filename):
            if loopback.error is not None:
                logger.warning(f"No system audio captured ({loopback.error}); mic-only recording kept")
            return

        mic_audio, samplerate = sf.read(self.filename, dtype='float32')
        if mic_audio.ndim > 1:
            mic_audio = mic_audio.mean(axis=1)

        if self._mic_first_block_time and loopback.first_block_time:
            offset = loopback.first_block_time - self._mic_first_block_time
            pad = int(abs(offset) * samplerate)
            if pad:
                zeros = np.zeros(pad, dtype=np.float32)
                if offset > 0:
                    loop_audio = np.concatenate([zeros, loop_audio])
                else:
                    mic_audio = np.concatenate([zeros, mic_audio])

        length = max(len(mic_audio), len(loop_audio))
        mic_audio = np.pad(mic_audio, (0, length - len(mic_audio)))
        loop_audio = np.pad(loop_audio, (0, length - len(loop_audio)))

        sf.write(self.filename, np.stack([mic_audio, loop_audio], axis=1),
                 samplerate, subtype='PCM_16', format='WAV')
        logger.info(f"Composed 2-channel meeting recording ({length / samplerate:.1f}s)")

    def was_auto_stopped(self) -> bool:
        """Check if recording was automatically stopped due to silence"""
        return self.auto_stopped