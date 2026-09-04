"""Recording self-description (modules/audio_markers.py).

Contract: a recording file alone says whether it is dictation, a meeting
(2-channel) or a phone call (mono + WAV comment), so routing is correct after
snapshots, retries and restarts. Unreadable files route as dictation.
"""
from pathlib import Path

import numpy as np
import soundfile as sf

from modules.audio_markers import (
    DICTATION, MEETING, PHONE, PHONE_RECORDING_COMMENT, recording_kind,
)


def write_wav(path: Path, channels: int, comment: str = "") -> Path:
    data = np.zeros((2205, channels), dtype="float32")
    with sf.SoundFile(str(path), mode="w", samplerate=22050, channels=channels,
                      subtype="PCM_16") as f:
        if comment:
            f.comment = comment
        f.write(data)
    return path


def test_mono_without_marker_is_dictation(tmp_path: Path) -> None:
    assert recording_kind(str(write_wav(tmp_path / "a.wav", 1))) == DICTATION


def test_two_channels_is_meeting(tmp_path: Path) -> None:
    assert recording_kind(str(write_wav(tmp_path / "m.wav", 2))) == MEETING


def test_mono_with_phone_comment_is_phone(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "p.wav", 1, comment=PHONE_RECORDING_COMMENT)
    assert recording_kind(str(path)) == PHONE


def test_marker_survives_a_snapshot_rename(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "temp_audio.wav", 1, comment=PHONE_RECORDING_COMMENT)
    snapshot = tmp_path / "temp_audio.wav.7.wav"
    path.rename(snapshot)
    assert recording_kind(str(snapshot)) == PHONE


def test_unrelated_comment_does_not_count(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "c.wav", 1, comment="phone call notes")
    assert recording_kind(str(path)) == DICTATION


def test_missing_or_garbage_file_is_dictation(tmp_path: Path) -> None:
    assert recording_kind(str(tmp_path / "nope.wav")) == DICTATION
    garbage = tmp_path / "g.wav"
    garbage.write_bytes(b"not a wav")
    assert recording_kind(str(garbage)) == DICTATION
