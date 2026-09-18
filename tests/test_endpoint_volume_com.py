"""COM ownership contract of read_input_volume, with fakes standing in for
pycaw/comtypes so no real COM is touched.

Why this exists: 1.0.1/1.0.2 turned the activated interface into an
IAudioEndpointVolume with comtypes `cast`, which creates a second owning
wrapper for the same reference without an AddRef. The extra Release landed
later, from the garbage collector, on whatever thread was running, into a
freed object: an access violation that crashed the packaged app twice. The
correct pattern is QueryInterface on the activated interface. This test
fails if the read goes back to `cast`, or leaks a COM wrapper past the call.
"""
import gc
import sys
import types
import weakref

import pytest

from modules import endpoint_volume as ev


class FakeVolume:
    def __init__(self, scalar: float, muted: bool) -> None:
        self._scalar, self._muted = scalar, muted

    def GetMasterVolumeLevelScalar(self) -> float:
        return self._scalar

    def GetMute(self) -> int:
        return int(self._muted)


class FakeActivated:
    """What IMMDevice.Activate returns: a wrapper that must be QueryInterface'd."""
    def __init__(self, volume: FakeVolume) -> None:
        self._volume = volume
        self.queried_for = []

    def QueryInterface(self, interface):  # noqa: N802 (COM naming)
        self.queried_for.append(interface)
        return self._volume


class FakeMMDevice:
    def __init__(self, activated: FakeActivated) -> None:
        self._activated = activated

    def Activate(self, iid, clsctx, params):  # noqa: N802
        return self._activated


class FakeAudioDevice:
    def __init__(self, name: str, dev_id: str, activated: FakeActivated, state: str = 'Active') -> None:
        self.FriendlyName = name
        self.id = dev_id
        self.state = types.SimpleNamespace(name=state)
        self._dev = FakeMMDevice(activated)


@pytest.fixture
def fake_com(monkeypatch: pytest.MonkeyPatch):
    """Install fake `comtypes` and `pycaw.pycaw` modules and return a
    registry the test fills with devices."""
    registry = {'devices': [], 'default': None}
    comtypes = types.ModuleType('comtypes')
    comtypes.CLSCTX_ALL = 23
    # `cast`/`POINTER` deliberately absent: importing them is the bug.
    pycaw_pkg = types.ModuleType('pycaw')
    pycaw = types.ModuleType('pycaw.pycaw')
    pycaw.IAudioEndpointVolume = type('IAudioEndpointVolume', (), {'_iid_': 'iid-volume'})

    class AudioUtilities:
        @staticmethod
        def GetAllDevices():
            return list(registry['devices'])

        @staticmethod
        def GetMicrophone():
            return registry['default']

        @staticmethod
        def CreateDevice(dev):
            return next(d for d in registry['devices'] if d._dev is dev)

    pycaw.AudioUtilities = AudioUtilities
    monkeypatch.setitem(sys.modules, 'comtypes', comtypes)
    monkeypatch.setitem(sys.modules, 'pycaw', pycaw_pkg)
    monkeypatch.setitem(sys.modules, 'pycaw.pycaw', pycaw)
    return registry


B1 = 'Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)'


def test_reads_volume_through_query_interface(fake_com):
    activated = FakeActivated(FakeVolume(0.42, False))
    fake_com['devices'] = [FakeAudioDevice(B1, '{0.0.1.00000000}.{b1}', activated)]

    reading = ev.read_input_volume(B1)

    assert reading == ev.EndpointVolume(B1, 42, False)
    assert len(activated.queried_for) == 1
    assert activated.queried_for[0].__name__ == 'IAudioEndpointVolume'


def test_mute_and_rounding(fake_com):
    activated = FakeActivated(FakeVolume(0.996, True))
    fake_com['devices'] = [FakeAudioDevice(B1, '{0.0.1.00000000}.{b1}', activated)]
    assert ev.read_input_volume(B1) == ev.EndpointVolume(B1, 100, True)


def test_render_and_inactive_endpoints_are_ignored(fake_com):
    live = FakeActivated(FakeVolume(1.0, False))
    fake_com['devices'] = [
        FakeAudioDevice(B1, '{0.0.0.00000000}.{render-twin}', FakeActivated(FakeVolume(0.1, False))),
        FakeAudioDevice(B1, '{0.0.1.00000000}.{unplugged}', FakeActivated(FakeVolume(0.2, False)), state='Unplugged'),
        FakeAudioDevice(B1, '{0.0.1.00000000}.{b1}', live),
    ]
    assert ev.read_input_volume(B1).percent == 100
    assert live.queried_for


def test_no_com_wrapper_outlives_the_read(fake_com):
    """Every wrapper the read creates must be released by plain reference
    counting before it returns; nothing may be left for the cyclic GC (which
    could run on another thread) to finalise. A `cast` would fail this: it
    ties the two wrappers into a cycle."""
    gc.disable()
    try:
        activated = FakeActivated(FakeVolume(1.0, False))
        ref = weakref.ref(activated)
        fake_com['devices'] = [FakeAudioDevice(B1, '{0.0.1.00000000}.{b1}', activated)]
        ev.read_input_volume(B1)
        fake_com['devices'].clear()
        del activated
        assert ref() is None, "activated interface survived the read (kept alive by a cycle)"
    finally:
        gc.enable()


def test_unknown_device_returns_none_without_touching_com(fake_com):
    activated = FakeActivated(FakeVolume(1.0, False))
    fake_com['devices'] = [FakeAudioDevice(B1, '{0.0.1.00000000}.{b1}', activated)]
    assert ev.read_input_volume('Some Other Mic') is None
    assert activated.queried_for == []
