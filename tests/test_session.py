"""Conversation-session machinery (modules/session.py).

Contracts:
- SessionNotes: an alert expires back to the state line; a decaying state
  clears only if nothing superseded it; reset() defuses a previous
  session's timers; a fresh user action (flush) always replaces an alert.
- ConversationSession: the preamble is pasted exactly once ahead of the
  first delivered chunk; phone chunks get headers; a failure while the
  session is live becomes an indicator note, after it ended an overlay, and
  during a newer recording only a log line; the end-of-session summary
  names the last failure and points retry at the last failed file; nothing
  paints over a dictation that is mid-pipeline.
"""
import os
import threading
import time
from typing import Callable, Dict, List, Tuple

from modules.session import ConversationSession, SessionNotes, build_preamble


# --- helpers -----------------------------------------------------------------

class ManualScheduler:
    """Collects (due_time, fn) and fires them when the fake clock passes."""

    def __init__(self) -> None:
        self.now = 100.0
        self.pending: List[Tuple[float, Callable[[], None]]] = []

    def clock(self) -> float:
        return self.now

    def schedule(self, delay_s: float, fn: Callable[[], None]) -> None:
        self.pending.append((self.now + delay_s, fn))

    def advance(self, seconds: float) -> None:
        self.now += seconds
        due = [fn for at, fn in self.pending if at <= self.now]
        self.pending = [(at, fn) for at, fn in self.pending if at > self.now]
        for fn in due:
            fn()


def make_notes() -> Tuple[SessionNotes, List[str], ManualScheduler]:
    shown: List[str] = []
    sched = ManualScheduler()
    notes = SessionNotes(shown.append, clock=sched.clock, schedule=sched.schedule)
    return notes, shown, sched


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)


class FakeHost:
    def __init__(self) -> None:
        self.current: object = None
        self.recording = False
        self.dictation_busy = False
        self.delivered: List[str] = []
        self.history: List[str] = []
        self.retry_candidate: List[str] = []
        self.warnings: List[str] = []
        self.failures: List[Tuple[str, str]] = []
        self.cleared = 0

    def is_current(self, session: object) -> bool:
        return self.current is session

    def is_recording(self) -> bool:
        return self.recording

    def dictation_in_progress(self) -> bool:
        return self.dictation_busy

    def deliver_text(self, paste: str, transcript: str) -> None:
        self.delivered.append(paste)
        self.history.append(transcript)

    def set_retry_candidate(self, path: str) -> None:
        self.retry_candidate.append(path)

    def show_warning(self, message: str, duration_ms: int) -> None:
        self.warnings.append(message)

    def show_failure(self, short: str, overlay: str) -> None:
        self.failures.append((short, overlay))

    def clear_post_session_status(self) -> None:
        self.cleared += 1


class GatedTranscriber:
    def __init__(self) -> None:
        self.gates: Dict[str, threading.Event] = {}
        self.failures: Dict[str, List[BaseException]] = {}
        self.results: Dict[str, str] = {}
        self._lock = threading.Lock()

    def fail_next(self, path: str, exc: BaseException) -> None:
        self.failures.setdefault(path, []).append(exc)

    def returns(self, path: str, text: str) -> None:
        """Make this path transcribe to exactly `text` (e.g. '' or '   ')."""
        self.results[path] = text

    def release(self, path: str) -> None:
        self.gates.setdefault(path, threading.Event()).set()

    def __call__(self, path: str) -> str:
        with self._lock:
            gate = self.gates.setdefault(path, threading.Event())
        gate.wait(5.0)
        with self._lock:
            queued = self.failures.get(path)
            if queued:
                raise queued.pop(0)
            if path in self.results:
                return self.results[path]
        return f"text:{path}"


SETTINGS = {
    'session_preamble': True, 'meeting_speaker_you': 'Me', 'meeting_speaker_them': 'Them',
    'phone_my_speaker_id': None, 'phone_speaker_labels': False,
}


def make_session(tmp_path, phone: bool = False, preamble: bool = True,
                 **overrides) -> Tuple[ConversationSession, FakeHost, GatedTranscriber, List[str]]:
    settings = {**SETTINGS, 'session_preamble': preamble, **overrides}
    host = FakeHost()
    t = GatedTranscriber()
    notes, shown, _ = make_notes()
    session = ConversationSession(phone=phone, settings_get=settings.get, notes=notes,
                                  host=host, transcribe_fn=t, retry_delay=0.0)
    host.current = session
    return session, host, t, shown


def chunk(tmp_path, name: str) -> str:
    p = tmp_path / name
    p.write_bytes(b"RIFF")
    return str(p)


# --- SessionNotes ------------------------------------------------------------

def test_alert_shows_over_state_and_expires_back_to_it() -> None:
    notes, shown, sched = make_notes()
    notes.set_state("📤 transcribing…")
    notes.set_alert("⚠️ chunk 2 retrying…", 6.0)
    assert shown[-1] == "⚠️ chunk 2 retrying…"
    sched.advance(6.2)
    assert shown[-1] == "📤 transcribing…"


def test_state_change_under_an_alert_does_not_hide_the_alert() -> None:
    notes, shown, sched = make_notes()
    notes.set_alert("⚠️ chunk 1 failed", 6.0)
    notes.set_state("⏳ 2 queued")
    assert shown[-1] == "⚠️ chunk 1 failed"
    sched.advance(6.2)
    assert shown[-1] == "⏳ 2 queued"


def test_flush_acknowledgement_replaces_an_active_alert() -> None:
    """A caps-press flush must always be visible, even mid-alert."""
    notes, shown, _ = make_notes()
    notes.set_alert("⚠️ chunk 1 failed", 6.0)
    notes.set_state("📤 transcribing…", clear_alert=True)
    assert shown[-1] == "📤 transcribing…"


def test_decaying_state_clears_only_if_not_superseded() -> None:
    notes, shown, sched = make_notes()
    notes.set_state("✅ pasted", decay_s=5.0)
    sched.advance(5.1)
    assert shown[-1] == ""
    notes.set_state("✅ pasted", decay_s=5.0)
    notes.set_state("📤 transcribing…")
    sched.advance(5.1)
    assert shown[-1] == "📤 transcribing…"


def test_reset_defuses_timers_from_a_previous_session() -> None:
    notes, shown, sched = make_notes()
    notes.set_state("✅ pasted", decay_s=5.0)
    notes.reset()
    notes.set_state("📤 transcribing…")  # next session
    sched.advance(5.1)
    assert shown[-1] == "📤 transcribing…"


def test_warning_outranks_the_routine_state_line() -> None:
    """A quiet mic is actionable and time-sensitive; the queue count is not."""
    notes, shown, _ = make_notes()
    notes.set_state("⏳ 2 queued")
    notes.set_warning("🔈 very quiet — check your mic")
    assert shown[-1] == "🔈 very quiet — check your mic"


def test_clearing_a_warning_restores_the_state_underneath() -> None:
    """The bug this layer exists for: the level warning and the session queue
    count shared one slot, so clearing one erased the other."""
    notes, shown, _ = make_notes()
    notes.set_state("⏳ 2 queued")
    notes.set_warning("🔈 very quiet — check your mic")
    notes.set_warning("")
    assert shown[-1] == "⏳ 2 queued"


def test_state_keeps_updating_underneath_a_warning() -> None:
    notes, shown, _ = make_notes()
    notes.set_warning("🔈 very quiet — check your mic")
    notes.set_state("⏳ 3 queued")
    assert shown[-1] == "🔈 very quiet — check your mic"
    notes.set_warning("")
    assert shown[-1] == "⏳ 3 queued"


def test_alert_outranks_a_warning_and_expires_back_to_it() -> None:
    notes, shown, sched = make_notes()
    notes.set_warning("🔈 very quiet — check your mic")
    notes.set_alert("⚠️ chunk 2 failed", 6.0)
    assert shown[-1] == "⚠️ chunk 2 failed"
    sched.advance(6.2)
    assert shown[-1] == "🔈 very quiet — check your mic"


def test_repeating_the_same_warning_does_not_repaint() -> None:
    """The watchdog calls this every 100ms; it must be free when unchanged."""
    notes, shown, _ = make_notes()
    notes.set_warning("🔈 very quiet — check your mic")
    before = len(shown)
    for _ in range(20):
        notes.set_warning("🔈 very quiet — check your mic")
    assert len(shown) == before


def test_reset_clears_the_warning_layer_too() -> None:
    notes, shown, _ = make_notes()
    notes.set_warning("🔈 very quiet — check your mic")
    notes.reset()
    assert shown[-1] == ""
    notes.set_state("📤 transcribing…")
    assert shown[-1] == "📤 transcribing…"


def test_blank_chunk_is_never_pasted_and_never_deleted(tmp_path) -> None:
    """A chunk the provider heard nothing in must not paste a bare newline,
    and must not be thrown away — the audio is the user's only copy."""
    session, host, t, shown = make_session(tmp_path)
    a = chunk(tmp_path, "a.wav")
    t.returns(a, "")
    session.submit(a)
    t.release(a)
    wait_for(lambda: host.retry_candidate == [a])
    assert host.delivered == []
    assert os.path.exists(a)


def test_whitespace_only_chunk_counts_as_blank(tmp_path) -> None:
    session, host, t, shown = make_session(tmp_path)
    a = chunk(tmp_path, "a.wav")
    t.returns(a, " " * 3 + chr(10) + "  ")
    session.submit(a)
    t.release(a)
    wait_for(lambda: host.retry_candidate == [a])
    assert host.delivered == []


def test_a_blank_chunk_does_not_burn_the_preamble(tmp_path) -> None:
    """The preamble is pasted once, ahead of the first real text. A silent
    first chunk must not consume it and leave the transcript unlabelled."""
    session, host, t, shown = make_session(tmp_path)
    a, b = chunk(tmp_path, "a.wav"), chunk(tmp_path, "b.wav")
    t.returns(a, "")
    session.submit(a)
    session.submit(b)
    t.release(a)
    t.release(b)
    wait_for(lambda: len(host.delivered) == 1)
    assert host.delivered[0].startswith(build_preamble(SETTINGS.get, False))
    assert "text:" + b in host.delivered[0]


def test_a_kept_blank_chunk_is_protected_from_the_snapshot_sweeper(tmp_path) -> None:
    """The queue treats a blank chunk as delivered and forgets it, so the
    session has to report it or _sweep_snapshots deletes the retry candidate."""
    session, host, t, _ = make_session(tmp_path)
    a = chunk(tmp_path, "a.wav")
    t.returns(a, "")
    session.submit(a)
    t.release(a)
    wait_for(lambda: host.retry_candidate == [a])
    assert a in session.active_paths()


def test_a_finished_drained_session_releases_its_blank_chunk(tmp_path) -> None:
    """Otherwise every session leaves one abandoned chunk sweep-protected for
    the rest of the process. Once drained, the file is protected only while it
    is still the app's retry candidate."""
    session, host, t, _ = make_session(tmp_path)
    a = chunk(tmp_path, "a.wav")
    t.returns(a, "")
    session.submit(a)
    t.release(a)
    wait_for(lambda: host.retry_candidate == [a])
    assert a in session.active_paths()      # still live
    session.close()
    session.live = False                    # what the app does at session end
    wait_for(lambda: session.active_paths() == [])


def test_only_the_newest_blank_chunk_is_kept(tmp_path) -> None:
    """Only one file can be the retry candidate, so a long meeting must not
    hoard every silent chunk."""
    session, host, t, _ = make_session(tmp_path)
    a, b = chunk(tmp_path, "a.wav"), chunk(tmp_path, "b.wav")
    t.returns(a, "")
    t.returns(b, "")
    session.submit(a)
    session.submit(b)
    t.release(a)
    t.release(b)
    wait_for(lambda: host.retry_candidate[-1:] == [b])
    assert not os.path.exists(a), "superseded blank chunk should be deleted"
    assert os.path.exists(b)
    assert session.active_paths() == [b]


# --- build_preamble ----------------------------------------------------------

def test_preamble_describes_attribution_per_mode() -> None:
    meeting = build_preamble(SETTINGS.get, phone=False)
    assert "'Me:' lines are me and 'Them:' is the other side" in meeting
    plain_phone = build_preamble(SETTINGS.get, phone=True)
    assert "unattributed" in plain_phone
    labeled = build_preamble({**SETTINGS, 'phone_speaker_labels': True}.get, phone=True)
    assert "Speaker N" in labeled
    matched = build_preamble({**SETTINGS, 'phone_my_speaker_id': 'me-1'}.get, phone=True)
    assert "matched by voice" in matched
    custom = build_preamble({**SETTINGS, 'meeting_speaker_you': 'Dimitri'}.get, phone=False)
    assert "'Dimitri:' lines are me" in custom


# --- ConversationSession -----------------------------------------------------

def test_preamble_once_then_plain_chunks_in_order(tmp_path) -> None:
    session, host, t, _ = make_session(tmp_path)
    a, b = chunk(tmp_path, "a.wav"), chunk(tmp_path, "b.wav")
    session.submit(a)
    session.submit(b)
    t.release(b)
    t.release(a)
    wait_for(lambda: len(host.delivered) == 2)
    assert host.delivered[0].startswith("[Transcript note:")
    assert host.delivered[0].endswith(f"text:{a}\n")
    assert host.delivered[1] == f"text:{b}\n"
    assert host.history == [f"text:{a}", f"text:{b}"]  # no preamble in history
    assert not (tmp_path / "a.wav").exists() and not (tmp_path / "b.wav").exists()


def test_preamble_can_be_disabled_and_phone_chunks_get_headers(tmp_path) -> None:
    session, host, t, _ = make_session(tmp_path, phone=True, preamble=False)
    a = chunk(tmp_path, "a.wav")
    session.submit(a)
    t.release(a)
    wait_for(lambda: host.delivered)
    assert host.delivered[0] == f"--- [chunk 1] ---\ntext:{a}\n"


def test_pending_counts_drive_the_indicator_note(tmp_path) -> None:
    session, host, t, shown = make_session(tmp_path)
    a, b = chunk(tmp_path, "a.wav"), chunk(tmp_path, "b.wav")
    session.submit(a)
    assert shown[-1] == "📤 transcribing…"
    session.submit(b)
    assert shown[-1] == "⏳ 2 queued"
    t.release(a)
    t.release(b)
    wait_for(lambda: shown and shown[-1] == "✅ pasted")


def test_failure_while_live_is_a_note_and_summary_names_the_cause(tmp_path) -> None:
    session, host, t, shown = make_session(tmp_path)
    a = chunk(tmp_path, "a.wav")
    t.fail_next(a, RuntimeError("blip"))
    t.fail_next(a, RuntimeError("You have 0 credits remaining"))
    session.submit(a)
    t.release(a)
    wait_for(lambda: any("failed" in s for s in shown))
    assert any("retrying" in s for s in shown)
    assert shown[-1] == "⚠️ chunk 1 failed — 💳 quota exhausted"
    assert host.retry_candidate == [a]
    assert (tmp_path / "a.wav").exists()  # kept for tray retry

    session.live = False  # app ended the session
    session.close()
    wait_for(lambda: host.failures)
    short, overlay = host.failures[0]
    assert short == "⚠️ 1 chunk(s) failed — 💳 quota exhausted"
    assert overlay.endswith("Add credits, or switch provider in the tray menu")
    assert host.retry_candidate[-1] == a


def test_failure_after_session_ended_is_an_overlay_and_during_new_recording_only_a_log(tmp_path) -> None:
    session, host, t, shown = make_session(tmp_path)
    a, b = chunk(tmp_path, "a.wav"), chunk(tmp_path, "b.wav")
    t.fail_next(a, RuntimeError("x")); t.fail_next(a, RuntimeError("x"))
    t.fail_next(b, RuntimeError("x")); t.fail_next(b, RuntimeError("x"))
    session.submit(a)
    session.submit(b)
    session.live = False
    session.close()
    t.release(a)
    wait_for(lambda: len(session.failed_paths) == 1)
    # Ended session, nothing else recording: retry and failure as overlays
    assert host.warnings == ["⚠️ Chunk 1 failed, retrying…",
                             "⚠️ Chunk 1 failed — ⚠️ Transcription failed\n(retry from tray)"]
    host.recording = True  # user started a fresh recording
    t.release(b)
    wait_for(lambda: len(session.failed_paths) == 2)
    time.sleep(0.05)
    assert len(host.warnings) == 2  # chunk 2's retry/failure only logged
    assert host.failures == []  # summary suppressed while recording


def test_clean_drain_clears_our_processing_status(tmp_path) -> None:
    session, host, t, _ = make_session(tmp_path)
    a = chunk(tmp_path, "a.wav")
    session.submit(a)
    session.live = False
    session.close()
    t.release(a)
    wait_for(lambda: host.cleared == 1)
    assert host.failures == []


def test_drain_never_paints_over_a_dictation_in_progress(tmp_path) -> None:
    session, host, t, _ = make_session(tmp_path)
    a = chunk(tmp_path, "a.wav")
    t.fail_next(a, RuntimeError("x")); t.fail_next(a, RuntimeError("x"))
    host.dictation_busy = True
    session.submit(a)
    session.live = False
    session.close()
    t.release(a)
    wait_for(lambda: session.failed_paths)
    time.sleep(0.05)
    assert host.failures == [] and host.cleared == 0
    assert host.retry_candidate == []  # the dictation's own retry candidate is kept


def test_superseded_session_stays_quiet(tmp_path) -> None:
    session, host, t, shown = make_session(tmp_path)
    a = chunk(tmp_path, "a.wav")
    session.submit(a)
    host.current = object()  # a newer session replaced this one
    session.live = False
    session.close()
    t.release(a)
    wait_for(lambda: host.delivered)  # ordered delivery still happens
    time.sleep(0.05)
    assert host.cleared == 0 and host.failures == []
    assert shown[-1] == "📤 transcribing…"  # no further notes from the old session


# --- countdown_note: the last-minute warning before the length limit acts ---

from modules.session import countdown_note  # noqa: E402


def test_countdown_is_silent_outside_the_last_minute_or_without_a_limit() -> None:
    assert countdown_note(None, session=False) == ''
    assert countdown_note(61.0, session=False) == ''
    assert countdown_note(3600.0, session=True) == ''


def test_countdown_names_the_action_for_the_mode() -> None:
    assert countdown_note(45.0, session=True) == '⏱ auto-send in 0:45 · Caps to send now'
    assert countdown_note(45.0, session=False) == '⏱ auto-stop in 0:45 · Caps to stop now'


def test_countdown_rounds_up_so_it_never_shows_zero_early() -> None:
    assert '1:00' in countdown_note(60.0, session=True)
    assert '0:01' in countdown_note(0.2, session=True)
    assert '0:00' in countdown_note(0.0, session=True)
    assert '0:00' in countdown_note(-3.0, session=True)  # limit already firing


def test_countdown_text_changes_only_once_per_second() -> None:
    assert countdown_note(10.9, session=False) == countdown_note(10.1, session=False)
    assert countdown_note(10.1, session=False) != countdown_note(9.9, session=False)
