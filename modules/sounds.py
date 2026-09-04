"""Soft start/stop cues for recording (``sounds_enabled``; off by default and
not exposed in the UI: on 2026-09-04 the default output device — a virtual
mixer — swallowed the first ~300 ms of every clip, so the cues were inaudible
or clipped. Left in for users who set the key in settings.json).

The two tones are synthesized once with numpy and played from memory through
winsound, so there are no audio assets to license or bundle and playback
never blocks (SND_ASYNC). Any failure is swallowed: a missing cue must never
affect a recording.
"""
import io
import logging
import struct
import threading
import wave
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('voice_typing')

_RATE = 44100
_cache: Dict[str, bytes] = {}
_lock = threading.Lock()


def _tone(notes: List[Tuple[float, float]], gap_s: float = 0.02, volume: float = 0.4,
          lead_in_s: float = 0.08) -> bytes:
    """WAV bytes for a sequence of (frequency_hz, duration_s) sine notes with
    a gentle attack/release envelope so they don't click. A short silent
    lead-in absorbs output-device start-up latency (virtual mixers and
    Bluetooth devices can swallow the first ~100 ms of a clip)."""
    import numpy as np
    parts = [np.zeros(int(_RATE * lead_in_s))]
    for freq, dur in notes:
        n = int(_RATE * dur)
        t = np.arange(n) / _RATE
        env = np.minimum(1.0, np.minimum(t / 0.012, (dur - t) / 0.06))
        # A touch of the second harmonic makes it sound less like a beep
        wave_data = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * freq * 2 * t)
        parts.append(wave_data * env * volume)
        parts.append(np.zeros(int(_RATE * gap_s)))
    samples = np.concatenate(parts)
    pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _cue(name: str) -> Optional[bytes]:
    with _lock:
        data = _cache.get(name)
        if data is None:
            try:
                if name == 'start':
                    data = _tone([(660.0, 0.14), (880.0, 0.20)])
                else:
                    data = _tone([(880.0, 0.14), (587.0, 0.22)])
            except Exception:
                logger.warning("Could not synthesize sound cue", exc_info=True)
                return None
            _cache[name] = data
    return data


def play(name: str) -> None:
    """Play the 'start' or 'stop' cue asynchronously (no-op off Windows)."""
    try:
        import winsound
    except ImportError:
        return
    data = _cue(name)
    if data is None:
        return
    # winsound cannot play from memory asynchronously (RuntimeError), so the
    # blocking call runs on its own short-lived thread instead
    def go() -> None:
        try:
            winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except Exception:
            logger.warning("Sound cue playback failed", exc_info=True)
    threading.Thread(target=go, name='sound-cue', daemon=True).start()


def play_if_enabled(settings, name: str) -> None:
    if settings.get('sounds_enabled'):
        play(name)
