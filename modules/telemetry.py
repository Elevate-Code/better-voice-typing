"""Opt-in anonymous usage counts. This file is all of it.

Nothing happens unless the user said yes (setting ``telemetry_consent`` is
True). Then the app counts how much it is used, and at most once a day sends
exactly the fields in PAYLOAD_KEYS to ENDPOINT:

    v                 payload format version
    id                random ID made when the user opted in (not derived
                      from the machine or the user); lets the server count
                      one install once instead of once per ping
    app               app version
    build             "installer" or "source"
    provider          speech-to-text provider name: openai / elevenlabs /
                      custom (never its URL or key)
    dictations        dictations pasted since the last ping
    meeting_sessions  Meeting-mode sessions since the last ping
    phone_sessions    Phone-mode sessions since the last ping
    minutes           minutes of audio transcribed since the last ping
    failures          transcriptions that failed since the last ping

The server (server/telemetry in this repo) keeps one row per ID per day plus
the 2-letter country Cloudflare derives from the connection; the IP address
is not stored.

Never sent: audio, transcript text, API keys, device names, file paths,
settings, or anything else.

Every payload that was sent is appended to SENT_LOG (telemetry_sent.jsonl in
Documents\\VoiceTyping), so the user can see exactly what left the machine.
Turning it off deletes the ID and any unsent counts; nothing is counted
while it is off.
"""
import json
import logging
import threading
import time
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Callable, Dict, Optional

from modules.fileutil import read_json_with_backup, write_json_atomic
from modules.paths import USER_DATA_DIR

logger = logging.getLogger('voice_typing')

ENDPOINT = "https://bvt-telemetry.elevatecode.io/v1/ping"
SOURCE_URL = "https://github.com/Elevate-Code/better-voice-typing/blob/master/modules/telemetry.py"
STATE_FILE = USER_DATA_DIR / 'telemetry.json'
SENT_LOG = USER_DATA_DIR / 'telemetry_sent.jsonl'

PAYLOAD_VERSION = 1
COUNTERS = ('dictations', 'meeting_sessions', 'phone_sessions', 'failures')
PAYLOAD_KEYS = ('v', 'id', 'app', 'build', 'provider', *COUNTERS, 'minutes')
SEND_INTERVAL_S = 24 * 3600
TIMEOUT_S = 5.0


def build_payload(install_id: str, app: str, build: str, provider: str,
                  counts: Dict[str, int], seconds: float) -> Dict[str, object]:
    """The complete ping. Pure; its keys are exactly PAYLOAD_KEYS."""
    payload: Dict[str, object] = {
        'v': PAYLOAD_VERSION,
        'id': install_id,
        'app': app,
        'build': build,
        'provider': provider,
    }
    for key in COUNTERS:
        payload[key] = int(counts.get(key, 0))
    payload['minutes'] = int(round(seconds / 60))
    return payload


def wav_seconds(path: Optional[str]) -> float:
    """Length of a WAV file from its header; 0 when it cannot be read."""
    if not path:
        return 0.0
    try:
        with wave.open(str(path), 'rb') as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else 0.0
    except (OSError, EOFError, wave.Error):
        return 0.0


def _post(payload: Dict[str, object]) -> None:
    """POST the payload as JSON; raises on any failure."""
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/json', 'User-Agent': 'BetterVoiceTyping'})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        if resp.status >= 300:
            raise OSError(f"HTTP {resp.status}")


def _empty_state() -> Dict[str, object]:
    return {'id': None, 'counts': {k: 0 for k in COUNTERS}, 'seconds': 0.0, 'last_sent': 0.0}


class Telemetry:
    """Counts usage while consent is True and sends it at most once a day.

    Thread-safe: counting happens on worker threads. ``send_if_due`` blocks on
    the network, so callers run it off the Qt main thread. Counts that arrive
    while a ping is in flight are kept for the next one."""

    def __init__(self, consent: Callable[[], Optional[bool]],
                 describe: Callable[[], Dict[str, str]],
                 post: Callable[[Dict[str, object]], None] = _post,
                 clock: Callable[[], float] = time.time,
                 state_file: Path = STATE_FILE, sent_log: Path = SENT_LOG) -> None:
        """describe() returns the 'app', 'build' and 'provider' fields, read
        at send time so they reflect the current version and settings."""
        self._consent = consent
        self._describe = describe
        self._post = post
        self._clock = clock
        self._state_file = Path(state_file)
        self._sent_log = Path(sent_log)
        self._lock = threading.Lock()
        self._sending = False

    @property
    def enabled(self) -> bool:
        return self._consent() is True

    def _load(self) -> Dict[str, object]:
        state = _empty_state()
        data = read_json_with_backup(self._state_file, None)
        if isinstance(data, dict):
            state.update({k: v for k, v in data.items() if k in state})
            counts = data.get('counts') if isinstance(data.get('counts'), dict) else {}
            state['counts'] = {k: int(counts.get(k, 0) or 0) for k in COUNTERS}
        return state

    def _save(self, state: Dict[str, object]) -> None:
        write_json_atomic(self._state_file, state)

    def record(self, counter: str, seconds: float = 0.0) -> None:
        """Add one to ``counter`` (and seconds of transcribed audio)."""
        self._add({counter: 1}, seconds)

    def add_audio(self, seconds: float) -> None:
        self._add({}, seconds)

    def _add(self, increments: Dict[str, int], seconds: float) -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                state = self._load()
                for key, n in increments.items():
                    if key in COUNTERS:
                        state['counts'][key] += n
                state['seconds'] = float(state['seconds']) + max(0.0, float(seconds))
                self._save(state)
        except Exception:
            logger.debug("Telemetry count failed", exc_info=True)

    def forget(self) -> None:
        """Delete the install ID and unsent counts (called on opt-out)."""
        with self._lock:
            for path in (self._state_file, self._state_file.with_name(self._state_file.name + '.bak')):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning(f"Could not delete {path.name}", exc_info=True)

    def due(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            last = float(self._load()['last_sent'] or 0)
        return self._clock() - last >= SEND_INTERVAL_S

    def send_if_due(self) -> bool:
        """Send one ping if consent is on and a day has passed. Blocking;
        never raises. Returns True when a ping went out."""
        if not self.due():
            return False
        with self._lock:
            if self._sending:
                return False
            self._sending = True
        try:
            with self._lock:
                state = self._load()
                if not state['id']:
                    state['id'] = str(uuid.uuid4())
                    self._save(state)
                counts = dict(state['counts'])
                seconds = float(state['seconds'])
                install_id = str(state['id'])
            info = self._describe()
            payload = build_payload(install_id, info.get('app', ''), info.get('build', ''),
                                    info.get('provider', ''), counts, seconds)
            self._post(payload)
            self._log_sent(payload)
            logger.info(f"Usage ping sent: {json.dumps(payload)}")
            with self._lock:
                state = self._load()
                if state['id'] != install_id:
                    return False  # opted out (and maybe back in) mid-flight
                for key in COUNTERS:
                    state['counts'][key] = max(0, state['counts'][key] - counts[key])
                state['seconds'] = max(0.0, float(state['seconds']) - seconds)
                state['last_sent'] = self._clock()
                self._save(state)
            return True
        except Exception as e:
            logger.info(f"Usage ping not sent ({e}); will retry later")
            return False
        finally:
            with self._lock:
                self._sending = False

    def _log_sent(self, payload: Dict[str, object]) -> None:
        try:
            self._sent_log.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self._clock()))
            with open(self._sent_log, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'sent_at': stamp, 'payload': payload}) + '\n')
        except OSError:
            logger.warning("Could not append to the usage ping log", exc_info=True)
