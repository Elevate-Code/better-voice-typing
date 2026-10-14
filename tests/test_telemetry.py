"""Opt-in usage counts (modules/telemetry.py).

Contracts:
- The payload is exactly PAYLOAD_KEYS. This is the promise made to users in
  the consent text; adding a field must be a deliberate edit here too.
- Nothing is counted or sent unless consent is True (None = not asked yet).
- At most one ping a day; a failed ping keeps its counts for the next one,
  and counts arriving while a ping is in flight are not lost.
- Opting out deletes the ID and unsent counts, including mid-flight.
- Every ping that went out is in the local sent log.
"""
import json
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from modules import telemetry
from modules.telemetry import PAYLOAD_KEYS, Telemetry, build_payload

INFO = {'app': '1.1.0', 'build': 'installer', 'provider': 'elevenlabs'}


class Rig:
    def __init__(self, tmp_path: Path, consent: Optional[bool] = True) -> None:
        self.consent = consent
        self.now = 1_000_000.0
        self.sent: List[Dict[str, object]] = []
        self.fail = False
        self.during_post = None
        self.t = Telemetry(consent=lambda: self.consent, describe=lambda: dict(INFO),
                           post=self._post, clock=lambda: self.now,
                           state_file=tmp_path / 'telemetry.json',
                           sent_log=tmp_path / 'telemetry_sent.jsonl')
        self.log = tmp_path / 'telemetry_sent.jsonl'
        self.state = tmp_path / 'telemetry.json'

    def _post(self, payload: Dict[str, object]) -> None:
        if self.during_post:
            self.during_post()
        if self.fail:
            raise OSError("offline")
        self.sent.append(payload)


def test_payload_is_exactly_the_promised_fields() -> None:
    payload = build_payload('id', '1.1.0', 'source', 'openai',
                            {'dictations': 3, 'meeting_sessions': 1}, 150.0)
    assert tuple(payload) == PAYLOAD_KEYS
    assert payload['dictations'] == 3 and payload['phone_sessions'] == 0
    assert payload['minutes'] == 2  # 2.5 rounds to even


@pytest.mark.parametrize('consent', [None, False])
def test_nothing_counted_or_sent_without_consent(tmp_path: Path, consent: Optional[bool]) -> None:
    rig = Rig(tmp_path, consent)
    rig.t.record('dictations', 30)
    assert not rig.t.send_if_due()
    assert rig.sent == [] and not rig.state.exists() and not rig.log.exists()


def test_first_ping_goes_out_at_once_then_daily(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.t.record('dictations', 90)
    rig.t.record('dictations', 30)
    rig.t.record('failures')
    assert rig.t.send_if_due()
    first = rig.sent[0]
    assert (first['dictations'], first['failures'], first['minutes']) == (2, 1, 2)
    assert first['app'] == '1.1.0' and first['id']

    rig.t.record('phone_sessions')
    rig.now += 23 * 3600
    assert not rig.t.send_if_due()
    rig.now += 3600
    assert rig.t.send_if_due()
    second = rig.sent[1]
    assert second['id'] == first['id']
    assert (second['dictations'], second['phone_sessions'], second['minutes']) == (0, 1, 0)


def test_failed_ping_keeps_counts(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.t.record('dictations')
    rig.fail = True
    assert not rig.t.send_if_due()
    rig.fail = False
    rig.t.record('dictations')
    assert rig.t.send_if_due()
    assert rig.sent[0]['dictations'] == 2


def test_counts_during_flight_carry_over(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.t.record('dictations')
    rig.during_post = lambda: rig.t.record('dictations')
    assert rig.t.send_if_due()
    rig.during_post = None
    assert rig.sent[0]['dictations'] == 1
    rig.now += 24 * 3600
    rig.t.send_if_due()
    assert rig.sent[1]['dictations'] == 1


def test_opt_out_forgets_id_and_counts(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.t.send_if_due()
    old_id = rig.sent[0]['id']
    rig.t.record('dictations')
    rig.consent = False
    rig.t.forget()
    assert not rig.state.exists()
    rig.consent = True
    assert rig.t.send_if_due()
    assert rig.sent[1]['id'] != old_id and rig.sent[1]['dictations'] == 0


def test_opt_out_mid_flight_does_not_resurrect_state(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    def opt_out() -> None:
        rig.consent = False
        rig.t.forget()
    rig.during_post = opt_out
    rig.t.send_if_due()
    assert not rig.state.exists()


def test_every_sent_ping_is_logged_locally(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.t.send_if_due()
    rig.now += 24 * 3600
    rig.t.send_if_due()
    lines = [json.loads(line) for line in rig.log.read_text(encoding='utf-8').splitlines()]
    assert [entry['payload'] for entry in lines] == rig.sent


def test_wav_seconds_tolerates_missing_files(tmp_path: Path) -> None:
    assert telemetry.wav_seconds(None) == 0.0
    assert telemetry.wav_seconds(str(tmp_path / 'nope.wav')) == 0.0
