"""The pure half of modules/endpoint_volume.py: which Windows endpoint a
PortAudio device name maps to, and what the indicator says about a reading.
The COM read itself is never touched here."""
from modules.audio_manager import MME_NAME_LIMIT
from modules.endpoint_volume import (EndpointVolume, LOW_VOLUME_PCT,
                                     match_endpoint, volume_note)

FULL = 'Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)'
TRUNCATED = FULL[:MME_NAME_LIMIT]
OTHER = 'Microphone Array (Realtek(R) Audio)'
B1 = (FULL, '{0.0.1.00000000}.{b1}')
REALTEK = (OTHER, '{0.0.1.00000000}.{realtek}')


def test_exact_name_matches_its_endpoint():
    assert match_endpoint(FULL, [REALTEK, B1]) == 1


def test_mme_truncated_name_matches_the_full_endpoint_name():
    assert len(TRUNCATED) == MME_NAME_LIMIT
    assert match_endpoint(TRUNCATED, [REALTEK, B1]) == 1


def test_unknown_name_matches_nothing():
    assert match_endpoint('Nope Mic', [REALTEK, B1]) is None
    assert match_endpoint(FULL, []) is None


def test_truncated_name_fitting_two_different_endpoints_is_unresolved():
    # Same first 31 characters, different devices: reporting either one's
    # volume could be wrong, so no endpoint is chosen.
    twin = (FULL[:MME_NAME_LIMIT] + 'icemeeter B2)', '{0.0.1.00000000}.{b2}')
    assert match_endpoint(TRUNCATED, [B1, twin]) is None


def test_two_distinct_endpoints_with_the_same_name_are_unresolved():
    # Two identical USB microphones: same friendly name, different IDs.
    twin = (FULL, '{0.0.1.00000000}.{second-unit}')
    assert match_endpoint(FULL, [B1, twin]) is None
    assert match_endpoint(TRUNCATED, [B1, twin]) is None


def test_one_endpoint_listed_twice_is_still_one_device():
    assert match_endpoint(TRUNCATED, [B1, B1]) == 0


def test_note_is_silent_when_volume_is_fine_or_unknown():
    assert volume_note(None) == ''
    assert volume_note(EndpointVolume(FULL, 100, False)) == ''
    assert volume_note(EndpointVolume(FULL, LOW_VOLUME_PCT, False)) == ''


def test_note_names_the_low_percentage():
    note = volume_note(EndpointVolume(FULL, 42, False))
    assert '42%' in note
    assert note == volume_note(EndpointVolume(OTHER, 42, False))


def test_mute_outranks_a_low_percentage():
    note = volume_note(EndpointVolume(FULL, 42, True))
    assert 'muted' in note
    assert '42%' not in note
