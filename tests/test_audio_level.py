"""Contract tests for modules/audio_level.py.

This is brittle, load-bearing logic: it decides whether a recording the user
just spent a minute making is transcribed or thrown away. The bug it replaced
was a whole-file RMS threshold that counted pauses as signal, so the cases
below are mostly "the same speech, judged the same way regardless of how much
silence surrounds it" plus the genuine-failure cases that must still reject.
"""
import numpy as np
import pytest

from modules.audio_level import (
    GOOD_DB,
    LiveLevelTracker,
    MIN_SIGNAL_S,
    QUIET_DB,
    SILENCE_FLOOR_DB,
    VERDICT_OK,
    VERDICT_QUIET,
    VERDICT_SILENT,
    VERDICT_VERY_QUIET,
    analyze,
    analyze_blocks,
    frame_length,
    frame_rms,
    speech_level_db,
    to_db,
    verdict_for_db,
)

SR = 22050


def speech(seconds: float, db: float, samplerate: int = SR, seed: int = 0) -> np.ndarray:
    """Speech-like noise at a given RMS level, in bursts with gaps between —
    the shape that broke the old whole-file average."""
    rng = np.random.default_rng(seed)
    amplitude = 10 ** (db / 20.0)
    n = int(samplerate * seconds)
    signal = rng.normal(0.0, amplitude, n)
    # Syllable-ish envelope so the frames are not all identical.
    t = np.arange(n) / samplerate
    return signal * (0.6 + 0.4 * np.sin(2 * np.pi * 3.0 * t))


def silence(seconds: float, samplerate: int = SR) -> np.ndarray:
    return np.zeros(int(samplerate * seconds))


# ---- frame_rms ------------------------------------------------------------

def test_frame_rms_returns_one_value_per_frame():
    levels = frame_rms(silence(1.0), SR)
    assert len(levels) == pytest.approx(1000 / 30, abs=1)


def test_frame_rms_empty_audio():
    assert frame_rms(np.array([]), SR).size == 0


def test_frame_rms_clip_shorter_than_one_frame_is_one_frame():
    assert frame_rms(np.ones(10) * 0.5, SR).size == 1


def test_frame_rms_scores_multichannel_on_the_loudest_channel():
    """Meeting recordings are mic + system audio; a silent far side must not
    drag a perfectly good recording down."""
    loud = speech(1.0, -20.0)
    stereo = np.stack([loud, np.zeros_like(loud)], axis=1)
    assert frame_rms(stereo, SR) == pytest.approx(frame_rms(loud, SR))


# ---- speaking level is independent of pauses ------------------------------

@pytest.mark.parametrize('pause_s', [0.0, 5.0, 30.0, 120.0])
def test_speech_level_ignores_surrounding_silence(pause_s):
    """The regression that caused the bug: padding a take with pauses must not
    change how loud the speaker is judged to have been."""
    talk = speech(5.0, -25.0)
    bare = analyze(talk, SR).speech_db
    padded = analyze(np.concatenate([talk, silence(pause_s)]), SR).speech_db
    assert padded == pytest.approx(bare, abs=1.0)


def test_speech_level_tracks_actual_loudness():
    quiet = analyze(speech(3.0, -40.0), SR).speech_db
    loud = analyze(speech(3.0, -20.0), SR).speech_db
    assert loud - quiet == pytest.approx(20.0, abs=2.0)


def test_speech_level_of_no_frames_is_the_floor():
    assert speech_level_db([]) == to_db(0.0)


# ---- has_signal: only a dead stream is rejected ---------------------------

@pytest.mark.parametrize('pause_s', [0.0, 10.0, 60.0, 300.0])
def test_real_speech_is_always_worth_sending(pause_s):
    report = analyze(np.concatenate([speech(4.0, -30.0), silence(pause_s)]), SR)
    assert report.has_signal


def test_very_quiet_speech_is_still_sent():
    """Quiet is advice, never a reason to discard what the user said."""
    report = analyze(speech(4.0, -48.0), SR)
    assert report.has_signal
    assert report.verdict == VERDICT_VERY_QUIET


@pytest.mark.parametrize('db', [-96.0, -80.0, -70.0, -60.0])
def test_dead_stream_is_rejected(db):
    assert not analyze(speech(10.0, db), SR).has_signal


def test_digital_silence_is_rejected():
    report = analyze(silence(10.0), SR)
    assert not report.has_signal
    assert report.verdict == VERDICT_SILENT
    assert report.signal_seconds == 0.0


def test_single_click_is_not_speech():
    """An accidental key tap in an otherwise empty recording is loud but far
    too short to be worth transcribing."""
    click = np.concatenate([silence(2.0), speech(0.02, -12.0), silence(2.0)])
    report = analyze(click, SR)
    assert report.signal_seconds < MIN_SIGNAL_S
    assert not report.has_signal


def test_one_short_word_is_speech():
    report = analyze(np.concatenate([silence(2.0), speech(0.6, -28.0), silence(2.0)]), SR)
    assert report.signal_seconds >= MIN_SIGNAL_S
    assert report.has_signal


def test_empty_audio_reports_silent():
    report = analyze(np.array([]), SR)
    assert not report.has_signal
    assert report.verdict == VERDICT_SILENT


# ---- advice bands ---------------------------------------------------------

@pytest.mark.parametrize('db,expected', [
    (-10.0, VERDICT_OK),
    (GOOD_DB, VERDICT_OK),
    (GOOD_DB - 0.1, VERDICT_QUIET),
    (QUIET_DB, VERDICT_QUIET),
    (QUIET_DB - 0.1, VERDICT_VERY_QUIET),
    (SILENCE_FLOOR_DB, VERDICT_VERY_QUIET),
    (SILENCE_FLOOR_DB - 0.1, VERDICT_SILENT),
])
def test_verdict_bands(db, expected):
    assert verdict_for_db(db) == expected


def test_report_describe_is_loggable():
    text = analyze(speech(2.0, -25.0), SR).describe()
    assert 'level' in text and 'signal' in text


# ---- LiveLevelTracker -----------------------------------------------------

def test_tracker_is_silent_before_any_audio():
    assert LiveLevelTracker().verdict() == VERDICT_SILENT


def test_tracker_reports_the_speaking_level():
    tracker = LiveLevelTracker(window_s=10.0)
    for i in range(100):
        tracker.add(10 ** (-25.0 / 20.0), i * 0.05)
    assert tracker.speech_db() == pytest.approx(-25.0, abs=0.5)
    assert tracker.verdict() == VERDICT_OK


def test_tracker_forgets_outside_its_window():
    """Someone who starts too far from the mic and moves closer must stop
    being warned."""
    tracker = LiveLevelTracker(window_s=5.0)
    for i in range(100):          # 5s of far-away speech
        tracker.add(10 ** (-48.0 / 20.0), i * 0.05)
    assert tracker.verdict() == VERDICT_VERY_QUIET
    for i in range(100, 220):     # 6s of close speech pushes the old out
        tracker.add(10 ** (-22.0 / 20.0), i * 0.05)
    assert tracker.verdict() == VERDICT_OK


def test_tracker_pauses_do_not_make_the_speaker_quiet():
    tracker = LiveLevelTracker(window_s=30.0)
    now = 0.0
    for _ in range(20):           # 1s of speech
        tracker.add(10 ** (-24.0 / 20.0), now)
        now += 0.05
    for _ in range(400):          # 20s of held silence
        tracker.add(0.0, now)
        now += 0.05
    assert tracker.verdict() == VERDICT_OK


def test_tracker_elapsed_counts_from_the_first_sample():
    tracker = LiveLevelTracker()
    assert tracker.elapsed(100.0) == 0.0
    tracker.add(0.1, 100.0)
    assert tracker.elapsed(106.0) == pytest.approx(6.0)


def test_tracker_reset_clears_everything():
    tracker = LiveLevelTracker()
    tracker.add(0.1, 1.0)
    tracker.reset()
    assert tracker.verdict() == VERDICT_SILENT
    assert tracker.elapsed(50.0) == 0.0


# ---- cases the Codex review found missing ---------------------------------

def test_short_real_word_is_not_rejected():
    """A one-word answer ("yes") can be under 200ms of sound. Rejecting it
    loses the user's words, so the presence gate must sit below that."""
    clip = np.concatenate([silence(0.4), speech(0.2, -26.0), silence(0.5)])
    report = analyze(clip, SR)
    assert report.has_signal, report.describe()


@pytest.mark.parametrize('samplerate', [16000, 22050, 24000, 44100, 48000])
def test_verdict_is_stable_across_sample_rates(samplerate):
    """24000 is used for streaming dictation, 22050 for everything else."""
    report = analyze(speech(3.0, -25.0, samplerate=samplerate), samplerate)
    assert report.speech_db == pytest.approx(-25.0, abs=1.5)
    assert report.total_seconds == pytest.approx(3.0, abs=0.05)
    assert report.has_signal


def test_sustained_hiss_is_accepted_because_this_is_an_energy_detector():
    """Documents a deliberate limitation rather than a wish: hiss above the
    floor passes. Callers must tolerate an empty or nonsense transcript —
    see the blank-chunk guard in ConversationSession._on_result."""
    report = analyze(speech(10.0, -45.0), SR)
    assert report.has_signal
    assert report.verdict == VERDICT_VERY_QUIET


def test_dc_offset_alone_does_not_read_as_loud_speech():
    report = analyze(np.full(SR * 3, 0.01), SR)
    assert report.speech_db == pytest.approx(-40.0, abs=1.0)


def test_clipped_audio_reports_a_hot_level():
    report = analyze(np.sign(speech(3.0, -10.0)) * 0.999, SR)
    assert report.verdict == VERDICT_OK
    assert report.peak_db > -1.0


def test_noisy_pauses_drag_the_estimate_but_never_the_decision():
    """The honest limit of pause-independence: silence is ignored exactly,
    a noise floor above SILENCE_FLOOR_DB is not."""
    talk = speech(1.0, -30.0)
    quiet_pause = analyze(np.concatenate([talk, silence(30.0)]), SR)
    noisy_pause = analyze(np.concatenate([talk, speech(30.0, -45.0)]), SR)
    assert quiet_pause.speech_db == pytest.approx(-30.0, abs=1.0)
    assert noisy_pause.speech_db < quiet_pause.speech_db - 3.0
    assert quiet_pause.has_signal and noisy_pause.has_signal


@pytest.mark.parametrize('db,expect_signal', [
    (SILENCE_FLOOR_DB + 2.0, True),
    (SILENCE_FLOOR_DB - 2.0, False),
])
def test_behaviour_at_the_floor(db, expect_signal):
    assert analyze(speech(5.0, db), SR).has_signal is expect_signal


@pytest.mark.parametrize('block_size', [
    frame_length(SR),           # exactly one frame
    frame_length(SR) * 7,
    frame_length(SR) * 512,     # what the recorder actually uses
    frame_length(SR) + 1,       # deliberately not a frame multiple
    1000,
    4096,
    7,                          # smaller than a frame
])
@pytest.mark.parametrize('channels', [1, 2])
def test_analyze_blocks_matches_whole_file_at_any_block_size(block_size, channels):
    """Block reading exists to bound memory on long recordings; it must not
    change the verdict. Framing each block independently would drop up to a
    frame per block (a third of the audio at some sizes), and the undercount
    lands on signal_seconds — the number that decides whether the recording
    is kept."""
    audio = np.concatenate([speech(4.0, -28.0), silence(3.0), speech(4.0, -28.0)])
    if channels == 2:
        audio = np.stack([audio, np.zeros_like(audio)], axis=1)
    whole = analyze(audio, SR)
    blocks = [audio[i:i + block_size] for i in range(0, len(audio), block_size)]
    streamed = analyze_blocks(blocks, SR)
    assert streamed.speech_db == pytest.approx(whole.speech_db, abs=0.01)
    # Tight on purpose: one frame is 0.03s, so a looser bound would hide
    # exactly the dropped-or-doubled frame this test exists to catch.
    assert streamed.signal_seconds == pytest.approx(whole.signal_seconds, abs=0.0001)
    assert streamed.total_seconds == pytest.approx(whole.total_seconds, abs=0.0001)
    assert streamed.has_signal == whole.has_signal


def test_analyze_blocks_handles_an_empty_block_mid_stream():
    audio = speech(3.0, -28.0)
    frame_len = frame_length(SR)
    blocks = [audio[:frame_len * 4], np.array([]), audio[frame_len * 4:]]
    assert analyze_blocks(blocks, SR).speech_db == pytest.approx(
        analyze(audio, SR).speech_db, abs=0.01)


def test_analyze_blocks_of_nothing_is_silent():
    assert not analyze_blocks([], SR).has_signal


def test_tracker_forgets_loud_audio_once_it_leaves_the_window():
    """The reverse of the other window test: a loud start must not keep
    reporting 'ok' after the user drifts away from the mic. This direction
    actually fails without pruning."""
    tracker = LiveLevelTracker(window_s=5.0)
    for i in range(100):          # 5s close to the mic
        tracker.add(10 ** (-22.0 / 20.0), i * 0.05)
    assert tracker.verdict() == VERDICT_OK
    for i in range(100, 240):     # 7s far away pushes the loud samples out
        tracker.add(10 ** (-46.0 / 20.0), i * 0.05)
    assert tracker.verdict() == VERDICT_VERY_QUIET


def test_tracker_ignores_a_single_loud_transient():
    """One desk knock in an otherwise dead window is not a working mic."""
    tracker = LiveLevelTracker(window_s=10.0)
    for i in range(200):
        tracker.add(0.5 if i == 100 else 0.0, i * 0.05)
    assert tracker.verdict() == VERDICT_SILENT


def test_analyze_blocks_matches_analyze_for_a_sub_frame_clip():
    """analyze() treats a clip shorter than one frame as a single frame; the
    block form must not report silence for audio that exists."""
    tiny = np.full(10, 0.5)
    assert analyze_blocks([tiny], SR).speech_db == pytest.approx(analyze(tiny, SR).speech_db)


def test_tracker_with_real_durations_rejects_a_transient_after_a_stall():
    """Inferring block duration from timestamps let one loud block a second
    before a silent one read as a second of speech."""
    tr = LiveLevelTracker(window_s=15.0)
    tr.add(0.05, 100.0, 0.046)
    tr.add(0.0, 101.0, 0.046)          # a stalled callback, one second later
    assert tr.signal_seconds() == pytest.approx(0.046, abs=0.001)
    assert tr.verdict() == VERDICT_SILENT


def test_tracker_with_real_durations_still_sees_real_speech():
    tr = LiveLevelTracker(window_s=15.0)
    for i in range(100):
        tr.add(0.05, 100.0 + i * 0.046, 0.046)
    assert tr.signal_seconds() == pytest.approx(4.6, abs=0.1)
    assert tr.verdict() == VERDICT_OK
