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
``volume_note`` are pure and tested. Reads run on one persistent COM thread
(``check_async`` → ``_ComWorker``): a recording start must never wait on
COM, and COM objects must live and die on the thread that created them —
see ``_ComWorker`` for the crash that taught us that.

NOTE: this reads the endpoint (Settings > Sound > <device> > Input volume),
not the per-app mixer slider, which does not exist for capture streams.
"""
from __future__ import annotations

import logging
import queue
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
        from comtypes import CLSCTX_ALL
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
        # IMPORTANT: QueryInterface, never ctypes/comtypes `cast`. `cast`
        # makes a second owning wrapper for the SAME reference without an
        # AddRef (comtypes' own source says so), and ties the two wrappers
        # into a reference cycle: one Release happens on scope exit, the
        # other whenever the cyclic GC finds the cycle, on any thread, into
        # an already-freed object. That double release was the access
        # violation in _ctypes.pyd that crashed 1.0.1/1.0.2 on the second
        # recording, or 79 s later on the transcription thread. This is the
        # pattern pycaw itself uses (AudioDevice.EndpointVolume).
        volume = iface.QueryInterface(IAudioEndpointVolume)
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


class _ComWorker:
    """The one thread that ever touches COM for this module.

    This replaced a short-lived thread per read that initialised COM, did
    the job, uninitialised COM and exited. The crash that prompted it (an
    access violation in _ctypes on the second recording, or a minute later
    on the transcription thread) turned out to be the double release
    described in ``read_input_volume``, but the per-read thread made it
    worse and is wrong on its own terms: a COM interface pointer belongs to
    the apartment of the thread that created it, and that apartment was
    being torn down while Python could still release the pointer later,
    from whatever thread the garbage collector ran on. One persistent thread
    keeps every COM object's apartment alive for the life of the process;
    objects are created, used and released here by ordinary reference
    counting. comtypes initialises COM on whichever thread first imports
    it, so the import happens here and counts as this thread's one
    CoInitialize; it is never balanced with CoUninitialize on purpose (the
    thread lives until exit).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: 'queue.Queue[Tuple[Optional[str], Callable[[Optional[EndpointVolume]], None]]]' = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    def submit(self, device_name: Optional[str],
               on_result: Callable[[Optional[EndpointVolume]], None]) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name='endpoint-volume',
                                                daemon=True)
                self._thread.start()
        self._queue.put((device_name, on_result))

    def _run(self) -> None:
        try:
            # Exactly one CoInitialize for this thread: the import does it
            # when comtypes has not been imported anywhere yet (its module
            # body calls CoInitializeEx), otherwise we do. Calling both
            # would leave the per-thread count at two, which is harmless
            # for a thread that never uninitialises but is still wrong.
            already_imported = 'comtypes' in sys.modules
            import comtypes
            if already_imported:
                comtypes.CoInitialize()
        except Exception:
            logger.debug("COM unavailable; input volume checks disabled", exc_info=True)
        while True:
            device_name, on_result = self._queue.get()
            try:
                reading = read_input_volume(device_name)
            except Exception:
                reading = None
            try:
                on_result(reading)
            except Exception:
                logger.error("Input volume callback failed", exc_info=True)


_worker = _ComWorker()


def check_async(device_name: Optional[str],
                on_result: Callable[[Optional[EndpointVolume]], None]) -> None:
    """Read the volume on the module's COM thread and hand the result to
    ``on_result`` from that thread. The callback must be thread-safe and
    quick: it runs before the next queued read."""
    _worker.submit(device_name, on_result)
