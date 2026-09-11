"""Speech-presence and loudness analysis, shared by every place that judges
audio level: the recorder's post-recording check, the live "you are too quiet"
warning shown while recording, and the settings-window microphone test.

Why this module exists
----------------------
The original check compared the RMS of a *whole* recording against a fixed
threshold. That counts pauses as signal, so the score depends on how much of
the recording was speech rather than on how loudly the user spoke: a 45-second
dictation with normal thinking pauses scores several dB lower than a 5-second
one at the same speaking volume, and real speech was rejected as "mostly
silence". The mic test made it worse by judging the *peak* over a 3-second
window, so it could say "loud and clear" about audio the recorder then threw
away.

Everything here works on short frames and answers two separate questions:

* ``speech_db`` — how loud the user speaks, estimated from the loudest frames
  that carry signal, so silent pauses cannot move it.
* ``has_signal`` — is there anything worth sending to the transcription
  provider at all.

Those are deliberately different decisions. Rejecting a recording costs the
user everything they just said; sending a hopeless one costs a fraction of a
cent. So ``has_signal`` only rejects audio that is essentially a dead stream
(muted mic, wrong device, no signal), while ``speech_db`` drives advice.

IMPORTANT: this is an energy detector, not a voice-activity detector. It
cannot tell speech from hiss, hum or a DC offset, and it does not try to:
every judgement here is either "there is some signal" (deliberately
permissive) or advice about loudness. Two consequences worth knowing:

* ``speech_db`` is exactly independent of *silent* pauses, but a noise floor
  above SILENCE_FLOOR_DB counts as signal, so long noisy pauses do drag the
  estimate down.
* Accepting noise is by design. Anything downstream that acts on the result
  (pasting at the cursor, deleting the audio) must cope with an empty or
  nonsense transcript.

NOTE: all thresholds are dBFS of frame RMS, not peak.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Deque, Iterable, List, Optional, Sequence, Tuple
from collections import deque

import numpy as np

# Frame size for level analysis. 30 ms is the usual speech-analysis frame:
# long enough for a stable RMS, short enough that a pause is its own frame.
FRAME_MS = 30.0

# Below this, a frame carries no usable signal at any sane recording level.
# Observed in practice: a dead/muted stream sits at -70 to -96 dBFS, while even
# a badly set up mic captures speech well above -50 dBFS. Anything above this
# is treated as signal, because a false reject is far more expensive than a
# wasted API call.
SILENCE_FLOOR_DB = -52.0

# Amplitude form of the floor, for callers comparing a raw RMS (the recorder's
# live silent-start check) rather than a dB value.
SILENCE_FLOOR_RMS = 10.0 ** (SILENCE_FLOOR_DB / 20.0)

# How much audio above the floor must be present before a recording is worth
# sending. Low on purpose: a one-word answer ("yes", "no", a short command)
# can be under 200 ms of actual sound, and rejecting it loses the user's
# words. This only has to outlast a stray key tap or mouse click, which is one
# or two frames; MIN_DURATION in the recorder drops very short takes anyway.
MIN_SIGNAL_S = 0.15

# Speaking-level bands used for advice (never for rejection).
GOOD_DB = -29.0       # at or above: healthy level, say nothing
QUIET_DB = -40.0      # below GOOD_DB down to here: works, but warn
                      # below QUIET_DB: very quiet, likely to transcribe badly

# Fraction of the loudest frames used to estimate speaking level. Using the top
# slice (rather than the mean) is what makes the estimate independent of how
# much of the recording was pauses.
LOUD_FRACTION = 0.2

VERDICT_OK = 'ok'
VERDICT_QUIET = 'quiet'
VERDICT_VERY_QUIET = 'very_quiet'
VERDICT_SILENT = 'silent'


def to_db(amplitude: float) -> float:
    """Amplitude (0..1 RMS) to dBFS, with a floor instead of -inf."""
    return 20.0 * math.log10(max(1e-10, float(amplitude)))


def frame_rms(audio: np.ndarray, samplerate: int, frame_ms: float = FRAME_MS) -> np.ndarray:
    """Per-frame RMS of a mono signal. Returns an empty array for no audio.

    A trailing partial frame is dropped: it would be a shorter window and so
    not comparable with the rest.
    """
    # Not upcast to float64: a long recording is large, and every reduction
    # below accumulates in float64 regardless.
    audio = np.asarray(audio)
    if audio.size == 0 or samplerate <= 0:
        return np.empty(0, dtype=np.float64)
    if audio.ndim == 1:
        audio = audio[:, np.newaxis]
    frame_len = max(1, int(samplerate * frame_ms / 1000.0))
    usable = (audio.shape[0] // frame_len) * frame_len
    if usable == 0:
        # Shorter than one frame: treat the whole clip as a single frame.
        per_channel = np.sqrt(np.mean(np.square(audio, dtype=np.float64), axis=0))
        return np.array([float(per_channel.max())])
    frames = audio[:usable].reshape(-1, frame_len, audio.shape[1])
    # Meeting recordings are 2-channel (mic + system audio). Score each frame
    # on its loudest channel so a silent far side cannot make a perfectly good
    # recording look empty.
    return np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1)).max(axis=1)


def speech_level_db(levels: Sequence[float]) -> float:
    """Speaking level in dBFS, from the loudest LOUD_FRACTION of the frames
    that are above the floor.

    Exactly independent of *silent* pauses: padding a take with digital
    silence cannot change it, which is the failure this replaced. It is NOT
    independent of a noisy room — pause frames above SILENCE_FLOOR_DB count as
    signal and, in quantity, pull the estimate down. That only softens advice,
    never the accept/reject decision.

    Returns a floor value when there are no frames.
    """
    arr = np.asarray(list(levels), dtype=np.float64)
    if arr.size == 0:
        return to_db(0.0)
    above_floor = arr[arr > SILENCE_FLOOR_RMS]
    # Nothing above the floor: fall back to everything, so the caller still
    # gets a real (very low) number to report rather than a magic constant.
    candidates = above_floor if above_floor.size else arr
    keep = max(1, int(round(candidates.size * LOUD_FRACTION)))
    loudest = np.sort(candidates)[-keep:]
    return to_db(float(np.sqrt(np.mean(np.square(loudest)))))


def verdict_for_db(db: float) -> str:
    """Advice band for a speaking level."""
    if db < SILENCE_FLOOR_DB:
        return VERDICT_SILENT
    if db < QUIET_DB:
        return VERDICT_VERY_QUIET
    if db < GOOD_DB:
        return VERDICT_QUIET
    return VERDICT_OK


@dataclass(frozen=True)
class LevelReport:
    """What a recording (or a slice of one) looks like, level-wise."""
    speech_db: float         # estimated speaking level, dBFS
    peak_db: float           # loudest single frame, dBFS
    signal_seconds: float    # audio above SILENCE_FLOOR_DB
    total_seconds: float
    verdict: str             # VERDICT_* — advice, not a pass/fail

    @property
    def has_signal(self) -> bool:
        """Whether this is worth sending to a transcription provider.

        Deliberately permissive: only a dead stream fails. This is an energy
        test, so hiss and hum pass it too — see the module docstring for why
        that is the right trade and what callers must therefore tolerate.
        """
        return (self.speech_db >= SILENCE_FLOOR_DB and
                self.signal_seconds >= MIN_SIGNAL_S)

    def describe(self) -> str:
        """One-line summary for logs."""
        return (f"level {self.speech_db:.1f}dB, peak {self.peak_db:.1f}dB, "
                f"signal {self.signal_seconds:.2f}s of {self.total_seconds:.1f}s "
                f"({self.verdict})")


def _report_from_levels(levels: np.ndarray) -> LevelReport:
    frame_s = FRAME_MS / 1000.0
    if levels.size == 0:
        return LevelReport(to_db(0.0), to_db(0.0), 0.0, 0.0, VERDICT_SILENT)
    speech_db = speech_level_db(levels)
    return LevelReport(
        speech_db=speech_db,
        peak_db=to_db(float(levels.max())),
        signal_seconds=float(np.count_nonzero(levels > SILENCE_FLOOR_RMS)) * frame_s,
        total_seconds=float(levels.size) * frame_s,
        verdict=verdict_for_db(speech_db),
    )


def analyze(audio: np.ndarray, samplerate: int) -> LevelReport:
    """Level report for a decoded mono (or 2-channel) signal."""
    return _report_from_levels(frame_rms(audio, samplerate))


def frame_length(samplerate: int, frame_ms: float = FRAME_MS) -> int:
    """Samples per analysis frame. Callers reading a file in blocks should use
    a multiple of this as the block size so no frame straddles a block."""
    return max(1, int(samplerate * frame_ms / 1000.0))


def analyze_blocks(blocks: Iterable[np.ndarray], samplerate: int) -> LevelReport:
    """Level report built from successive blocks of a signal.

    Lets a caller analyze a long recording without decoding it whole: a
    15-minute stereo take is ~160 MB as float32, and squaring it for the RMS
    briefly doubles that. Only the per-frame levels are retained (~30k floats
    for 15 minutes).

    Gives the same answer as ``analyze`` for ANY block size, not just whole
    numbers of frames: the leftover samples at the end of a block are carried
    into the next one. Framing each block independently instead would discard
    up to a frame per block — a third of the audio at some block sizes — and
    the undercount lands on ``signal_seconds``, which is exactly the number
    that decides whether the user's recording is kept.
    """
    collected: List[np.ndarray] = []
    frame_len = frame_length(samplerate)
    carry: Optional[np.ndarray] = None
    for block in blocks:
        block = np.asarray(block)
        if block.size == 0:
            continue
        if carry is not None and carry.size:
            # Both must agree on channel count before they can be joined.
            if carry.ndim != block.ndim:
                carry = carry.reshape(carry.shape[0], -1)
                block = block.reshape(block.shape[0], -1)
            block = np.concatenate([carry, block])
        whole = (block.shape[0] // frame_len) * frame_len
        if whole:
            levels = frame_rms(block[:whole], samplerate)
            if levels.size:
                collected.append(levels)
        carry = block[whole:]
    if not collected:
        # Nothing reached a full frame. analyze() treats a clip shorter than
        # one frame as a single frame, so do the same rather than reporting
        # silence for audio that exists.
        if carry is not None and carry.size:
            return _report_from_levels(frame_rms(carry, samplerate))
        return _report_from_levels(np.empty(0, dtype=np.float64))
    # A trailing partial frame is dropped, exactly as ``analyze`` does.
    return _report_from_levels(np.concatenate(collected))


class LiveLevelTracker:
    """Rolling speaking-level estimate for an in-progress recording.

    Fed one RMS value per audio callback block from the recorder thread, read
    from the UI watchdog, so it keeps its own lock and stays cheap: a bounded
    deque of (timestamp, rms) pairs over the trailing window.

    The window matters — a user who started too far from the mic and then moved
    closer should stop being warned, and someone who drifts away should start.

    IMPORTANT: feed this a MONOTONIC clock. Timestamps only ever prune against
    the newest one, so wall-clock time going backwards (an NTP correction, a
    timezone change) parks stale samples in the window until real time catches
    up, skewing the verdict for as long as the jump was.
    """

    def __init__(self, window_s: float = 15.0) -> None:
        self.window_s = window_s
        self._lock = threading.Lock()
        # (timestamp, rms, block duration in seconds; 0 = unknown)
        self._samples: Deque[Tuple[float, float, float]] = deque()
        self._start: Optional[float] = None

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._start = None

    def add(self, rms: float, now: float, duration: float = 0.0) -> None:
        """Record one block's RMS. ``duration`` is how much audio that block
        actually covers, in seconds; pass it whenever the caller knows (it
        always does: block length over sample rate). Without it the duration
        is inferred from timestamps, and a stalled or jittery callback then
        makes a single loud block look like a second of speech."""
        with self._lock:
            if self._start is None:
                self._start = now
            self._samples.append((now, float(rms), float(duration)))
            cutoff = now - self.window_s
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def elapsed(self, now: float) -> float:
        """Seconds since the first sample (not just the window)."""
        with self._lock:
            start = self._start
        return 0.0 if start is None else max(0.0, now - start)

    def speech_db(self) -> float:
        with self._lock:
            values = [rms for _, rms, _ in self._samples]
        return speech_level_db(values)

    def signal_seconds(self) -> float:
        """Audio above the floor in the window.

        Exact when every block reported its duration (see ``add``). Otherwise
        estimated from the mean callback interval, which a stalled or jittery
        stream can inflate — one loud block a second before a silent one then
        reads as a second of speech, defeating the single-transient gate.
        """
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return 0.0
        if all(d > 0.0 for _, _, d in samples):
            return sum(d for _, rms, d in samples if rms > SILENCE_FLOOR_RMS)
        # Fallback for callers that do not report block durations.
        if len(samples) < 2:
            return 0.0
        span = samples[-1][0] - samples[0][0]
        if span <= 0:
            return 0.0
        per_sample = span / (len(samples) - 1)
        return sum(1 for _, rms, _ in samples if rms > SILENCE_FLOOR_RMS) * per_sample

    def verdict(self) -> str:
        """Advice band for the window, or VERDICT_SILENT when there is not
        enough signal in it to judge. The MIN_SIGNAL_S gate matters here too:
        without it a single loud block (a cough, a desk knock) reads as a
        healthy microphone."""
        with self._lock:
            empty = not self._samples
        if empty or self.signal_seconds() < MIN_SIGNAL_S:
            return VERDICT_SILENT
        return verdict_for_db(self.speech_db())
