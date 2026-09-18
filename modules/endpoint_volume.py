"""Windows input volume of the recording device, read from its audio endpoint.

Why this exists
---------------
The level analysis in ``modules/audio_level.py`` judges how loud the user
*sounds* and can only advise; a microphone that is merely quieter than usual
looks like a soft-spoken user. But one specific failure keeps recurring and
is invisible to that analysis: some other application (meeting apps with
"automatically adjust microphone volume", Chromium's WebRTC auto-gain) turns
the *Windows* input volume of the endpoint down — seen at 24% and 42% — and
every recording from then on is attenuated by 13-22 dB before this app ever
sees a sample. The speaking-level bands cannot catch that without also
nagging every genuinely quiet user, so this module checks the cause directly:
it reads the endpoint's master volume and mute flag once per recording and the
indicator says "Windows mic volume is 42%" while the user is still speaking.

Everything COM lives in ``read_input_volume``; ``match_endpoint`` and
``volume_note`` are pure and tested. The read runs on its own thread
(``check_async``) because COM must be initialised per thread and a recording
start must never wait on it.

NOTE: this reads the endpoint (Settings > Sound > <device> > Input volume),
not the per-app mixer slider, which does not exist for capture streams.
"""
from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from modules.audio_manager import names_match

logger = logging.getLogger('voice_typing')

# Below this the note appears. Windows shows the volume as a whole percent;
# 100 is the only setting that does not attenuate, but a user who deliberately
# runs at 98 should not be nagged.
LOW_VOLUME_PCT = 95

# Endpoint IDs from IMMDeviceEnumerator start with the data-flow marker:
# {0.0.0.…} render, {0.0.1.…} capture.
_CAPTURE_ID_PREFIX = '{0.0.1.'


@dataclass(frozen=True)
class EndpointVolume:
    device_name: str      # the endpoint's Windows friendly name
    percent: int          # 0-100, master volume scalar as Windows shows it
    muted: bool


def volume_note(reading: Optional[EndpointVolume]) -> str:
    """Indicator warning text for a reading; '' when fine or unknown."""
    if reading is None:
        return ''
    if reading.muted:
        return '🔇 mic is muted in Windows sound settings'
    if reading.percent < LOW_VOLUME_PCT:
        return f'🔉 Windows mic volume is {reading.percent}% — set it to 100'
    return ''


def match_endpoint(device_name: str, endpoints: List[Tuple[str, str]]) -> Optional[int]:
    """Index of the endpoint the recording device corresponds to, or None.

    ``endpoints`` are (friendly name, endpoint ID) pairs. The device name
    comes from PortAudio and may be MME-truncated; the endpoint names are
    complete. Anything that fits more than one distinct endpoint ID is
    unresolvable — two identical USB microphones share a friendly name, and
    reporting the wrong device's volume would be worse than saying nothing.
    """
    hits = [i for i, (name, _) in enumerate(endpoints) if names_match(device_name, name)]
    if not hits:
        return None
    if len({endpoints[i][1] for i in hits}) > 1:
        return None
    return hits[0]


def read_input_volume(device_name: Optional[str]) -> Optional[EndpointVolume]:
    """Read the Windows volume of the capture endpoint behind ``device_name``
    (None = the system default input). Returns None when it cannot be
    determined; never raises. Call from a thread that owns COM (see
    ``check_async``)."""
    try:
        import warnings
        from comtypes import CLSCTX_ALL, POINTER, cast
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        if device_name is None:
            dev = AudioUtilities.GetMicrophone()
            if dev is None:
                return None
            name = _friendly_name(AudioUtilities.CreateDevice(dev)) or 'default microphone'
        else:
            with warnings.catch_warnings():
                # pycaw warns for every property it cannot read on any device
                warnings.simplefilter('ignore')
                devices = AudioUtilities.GetAllDevices()
            capture = [d for d in devices
                       if (d.id or '').startswith(_CAPTURE_ID_PREFIX)
                       and getattr(d.state, 'name', '') == 'Active']
            index = match_endpoint(device_name, [(d.FriendlyName or '', d.id or '') for d in capture])
            if index is None:
                return None
            dev = capture[index]._dev
            name = capture[index].FriendlyName or device_name
        iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(iface, POINTER(IAudioEndpointVolume))
        percent = int(round(volume.GetMasterVolumeLevelScalar() * 100))
        return EndpointVolume(name, percent, bool(volume.GetMute()))
    except Exception:
        logger.debug("Could not read the Windows input volume", exc_info=True)
        return None


def _friendly_name(device: object) -> Optional[str]:
    try:
        return getattr(device, 'FriendlyName', None)
    except Exception:
        return None


def check_async(device_name: Optional[str],
                on_result: Callable[[Optional[EndpointVolume]], None]) -> None:
    """Read the volume on a short daemon thread and hand the result to
    ``on_result`` from that thread. The callback must be thread-safe."""
    def run() -> None:
        # comtypes calls CoInitializeEx on whichever thread first imports it
        # (its __init__ does so at import time). When that is this thread,
        # the import IS our initialisation and a second call would leave the
        # count unbalanced; when it was imported earlier, this thread still
        # needs its own.
        first_import = 'comtypes' not in sys.modules
        import comtypes
        initialised = first_import
        if not first_import:
            try:
                comtypes.CoInitialize()
                initialised = True
            except Exception:
                pass  # already initialised on this thread in another mode
        try:
            on_result(read_input_volume(device_name))
        finally:
            if initialised:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    threading.Thread(target=run, name='endpoint-volume', daemon=True).start()
