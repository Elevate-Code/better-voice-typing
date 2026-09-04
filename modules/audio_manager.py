from typing import List, Dict, Optional, NamedTuple
import sounddevice as sd

class DeviceIdentifier(NamedTuple):
    """Unique identifier for an audio device that persists across sessions"""
    name: str
    channels: int
    default_samplerate: float

def create_device_identifier(device: Dict[str, any]) -> DeviceIdentifier:
    """Creates a persistent identifier for a device"""
    return DeviceIdentifier(
        name=device['name'],
        channels=device['max_input_channels'],  # Match the key used in device info
        default_samplerate=device['default_samplerate']
    )

def find_device_by_identifier(identifier: DeviceIdentifier) -> Optional[Dict[str, any]]:
    """Finds the best matching device for a saved identifier"""
    devices = get_input_devices()

    # First try exact match
    for device in devices:
        if create_device_identifier(device) == identifier:
            return device

    # Fall back to name match with best specs (truncation-tolerant: the
    # saved name may be a legacy MME-truncated form of the full name)
    matching_devices = [
        d for d in devices
        if names_match(d['name'], identifier.name)
    ]

    # A truncated saved name matching several DIFFERENT device names is
    # unresolvable — guessing could silently record from the wrong
    # microphone. Report it unresolved; callers keep the saved setting and
    # use the default until it can be resolved.
    if len({d['name'] for d in matching_devices}) > 1:
        return None

    if matching_devices:
        return max(
            matching_devices,
            key=lambda d: (d['max_input_channels'], d['default_samplerate'])
        )

    return None

def get_device_by_id(device_id: int) -> Optional[Dict[str, any]]:
    """Gets device info by ID, returns None if device not found"""
    try:
        device = sd.query_devices(device_id)
        if device['max_input_channels'] > 0:
            return {
                'id': device_id,
                'name': device['name'],
                'max_input_channels': device['max_input_channels'],
                'hostapi': device['hostapi'],
                'default_samplerate': device['default_samplerate']
            }
        return None
    except:
        return None

# Windows MME truncates device names to 31 characters (MAXPNAMELEN minus the
# terminator); every other host API reports the full endpoint name.
MME_NAME_LIMIT = 31

def names_match(a: str, b: str) -> bool:
    """True when two device names plausibly refer to the same endpoint,
    tolerating MME's 31-char truncation of longer names. Only a name of
    exactly the truncation length is treated as a prefix — anything shorter
    is a complete name and must match exactly."""
    if a == b:
        return True
    if len(a) == MME_NAME_LIMIT and b.startswith(a):
        return True
    if len(b) == MME_NAME_LIMIT and a.startswith(b):
        return True
    return False

def _all_input_devices() -> List[Dict[str, any]]:
    """Every PortAudio input device across all host APIs."""
    devices = []
    for i, device in enumerate(sd.query_devices()):
        if device['max_input_channels'] > 0:
            devices.append({
                'id': i,
                'name': device['name'],
                'max_input_channels': device['max_input_channels'],
                'hostapi': device['hostapi'],
                'default_samplerate': device['default_samplerate']
            })
    return devices

def _dedupe_by_name(devices: List[Dict[str, any]]) -> List[Dict[str, any]]:
    """Collapse exact-name duplicates, keeping the best-spec variant."""
    seen_devices: Dict[str, Dict] = {}
    for device_info in devices:
        name = device_info['name']
        if name not in seen_devices or (
            device_info['max_input_channels'] > seen_devices[name]['max_input_channels'] or
            (device_info['max_input_channels'] == seen_devices[name]['max_input_channels'] and
             device_info['default_samplerate'] > seen_devices[name]['default_samplerate'])
        ):
            seen_devices[name] = device_info
    return list(seen_devices.values())

def get_input_devices() -> List[Dict[str, any]]:
    """Returns a list of available input (microphone) devices.

    PortAudio lists every endpoint once per Windows host API: MME (names
    truncated to 31 chars), DirectSound, WASAPI, and WDM-KS (hardware-pin
    names that don't even match the endpoint name), plus meta devices like
    'Microsoft Sound Mapper'. WASAPI enumerates each real endpoint exactly
    once under its full name — the same list Windows Sound settings shows —
    so only WASAPI devices are returned. Nothing is deduped within WASAPI:
    distinct endpoints with identical names stay distinct. If WASAPI has no
    input devices at all (broken/ancient audio stack), fall back to
    best-variant-per-name dedup across all host APIs."""
    devices = _all_input_devices()
    try:
        hostapis = sd.query_hostapis()
        wasapi = [d for d in devices
                  if 'WASAPI' in hostapis[d['hostapi']]['name']]
    except Exception:
        wasapi = []
    if wasapi:
        return wasapi
    return _dedupe_by_name(devices)

def get_default_device_id() -> int:
    """Returns the system default input device ID.

    Prefers the WASAPI host API's default entry so the ID corresponds to a
    device get_input_devices() actually returns (PortAudio's global default
    is the MME variant of the same endpoint)."""
    try:
        for api in sd.query_hostapis():
            if 'WASAPI' in api['name'] and api['default_input_device'] >= 0:
                return api['default_input_device']
    except Exception:
        pass
    device = sd.query_devices(None, kind='input')
    return device['index']

# Active input device for recording streams. Held here instead of in
# sd.default.device: overriding that made the system default unrecoverable
# (get_default_device_id would just echo the selection back).
_selected_device_id: Optional[int] = None

def set_input_device(device_id: Optional[int]) -> None:
    """Sets the active input device for recording (None = system default)"""
    global _selected_device_id
    _selected_device_id = device_id

def get_input_device() -> Optional[int]:
    """The selected device ID as chosen in the UI (None = system default)."""
    return _selected_device_id

def get_capture_device_id(samplerate: int) -> Optional[int]:
    """Device ID a recording stream should open for the current selection.

    The visible device list is WASAPI (one clean entry per endpoint), but
    WASAPI shared mode only accepts an endpoint's native sample rate, while
    the MME/DirectSound variants of the same endpoint resample to anything.
    So: probe the selected device and its name-matched variants with
    check_input_settings at the requested rate and return the first that
    works (MME first, then DirectSound). A truncated MME name that could
    belong to more than one endpoint is skipped — never guess which physical
    device a stream would actually open. None = system default (PortAudio's
    default is the MME variant of the Windows default endpoint, which
    resamples)."""
    if _selected_device_id is None:
        return None
    try:
        target_name = sd.query_devices(_selected_device_id)['name']
        hostapis = sd.query_hostapis()
        wasapi_names = [d['name'] for d in get_input_devices()]
        variants = [d for d in _all_input_devices()
                    if d['id'] != _selected_device_id and
                    names_match(d['name'], target_name)]
        api_order = {'MME': 0, 'Windows DirectSound': 1}
        variants.sort(key=lambda d: api_order.get(
            hostapis[d['hostapi']]['name'], 2))

        def unambiguous(variant: Dict[str, any]) -> bool:
            name = variant['name']
            if len(name) != MME_NAME_LIMIT or name == target_name:
                return True  # full name — identifies exactly one endpoint
            return sum(1 for n in wasapi_names if n.startswith(name)) <= 1

        candidates = [_selected_device_id] + [
            v['id'] for v in variants if unambiguous(v)]
    except Exception:
        return _selected_device_id
    for device_id in candidates:
        try:
            sd.check_input_settings(device=device_id, samplerate=samplerate,
                                    channels=1)
            return device_id
        except Exception:
            continue
    # Nothing passed the probe; let the stream open surface the real error
    return _selected_device_id

def refresh_devices() -> None:
    """Rescan the audio device list. PortAudio snapshots devices at init, so
    hot-plugged/removed devices are invisible until it is reinitialized.
    Callers must ensure no audio stream is open, and must re-resolve any
    stored device IDs afterwards — indices can shift across a rescan.
    Raises if PortAudio could not be reinitialized (after one retry): a dead
    backend disables all audio, so callers must not proceed as if the rescan
    succeeded."""
    import time
    sd._terminate()
    try:
        sd._initialize()
    except Exception:
        time.sleep(0.2)
        sd._initialize()
