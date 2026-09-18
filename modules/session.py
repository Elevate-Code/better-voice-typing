"""Conversation-session machinery for Meeting and Phone mode.

A session is a continuous recording where each caps press seals the audio so
far into a chunk file and recording resumes. Chunks transcribe concurrently
(ChunkQueue) and are delivered strictly in order at the cursor. This module
owns everything about that flow that does not touch the recorder:

- ``build_preamble``: the transcript-limitations note pasted once ahead of
  the first chunk (pure settings → text).
- ``SessionNotes``: the three-layer status note on the recording indicator —
  a routine STATE line (transcribing / queued / pasted), a sticky WARNING
  above it (the mic is too quiet) and a timed ALERT above both (chunk
  retrying, failed, quiet flush) that expires back to what is underneath.
- ``ConversationSession``: one session's queue, preamble, failure summary
  and delivery callbacks. It talks to the app only through ``SessionHost``,
  so tests drive it with fakes and no Tk, recorder, or provider.

The recorder side (sealing chunks, restarting capture, salvaging the tail on
device errors) stays in voice_typing.pyw, which calls ``submit`` / ``close``
/ ``cancel`` here.
"""
import logging
import os
import threading
import time
from typing import Callable, List, Optional, Protocol

from modules.chunk_queue import RETRY_DELAY_S, ChunkQueue
from modules.error_messages import TranscriptionFailure, describe_transcription_error

logger = logging.getLogger('voice_typing')

SettingsGetter = Callable[[str], object]


def build_preamble(get: SettingsGetter, phone: bool) -> str:
    """Note for the LLM reading the pasted transcript, sent once per session.

    Explains how speaker attribution works for this capture mode so the
    reader can silently compensate for its limits."""
    you = get('meeting_speaker_you') or 'Me'
    them = get('meeting_speaker_them') or 'Them'
    if phone:
        if get('phone_my_speaker_id'):
            speakers = (f"'{you}:' lines are me (matched by voice) and "
                        f"'{them}:' is anyone else; in chunks with "
                        "unlabeled lines the voice match failed, so each "
                        "line is just one unattributed speaker turn")
        elif get('phone_speaker_labels'):
            speakers = ("Speaker turns are labeled 'Speaker N' per chunk, and "
                        "labels can swap identities between chunks")
        else:
            speakers = ("Each line is one speaker turn, but turns are "
                        "unattributed — quietly infer who's speaking")
    else:
        speakers = (f"'{you}:' lines are me and '{them}:' is the other "
                    "side, though attribution can err on overlapping speech")
    return (
        "[Transcript note: a live conversation transcribed by "
        "voice-to-text, arriving in chunks as the call happens. "
        f"{speakers}. Proper nouns and abbreviations are often "
        "mistranscribed — quietly interpret them from context; you "
        "don't need to surface these corrections to me. I'm in this "
        "conversation live, so act as my silent advisor: as it "
        "progresses, feel free to quickly explore for docs or "
        "context relevant to what's being discussed. I can only "
        "glance at your replies briefly — keep them short, and put "
        "anything you want me to actually say or ask in **bold** so "
        "my eyes are drawn to it.]"
    )


COUNTDOWN_WINDOW_S = 60.0


def countdown_note(remaining_s: Optional[float], session: bool,
                   window_s: float = COUNTDOWN_WINDOW_S) -> str:
    """Indicator note for the last minute before the length limit acts.

    An auto-stop (dictation) or auto-send (session chunk) that fires without
    warning pastes text wherever the cursor happens to be at that moment.
    The countdown gives the user the moment back: they see it coming and
    press Caps Lock to send when it suits them. Empty outside the window or
    when there is no limit. The text changes once per second, so the sticky
    warning layer repaints once per second and never more.
    """
    if remaining_s is None or remaining_s > window_s:
        return ''
    secs = max(0, int(remaining_s + 0.999))  # 0:01 stays visible until it fires
    clock = f"{secs // 60}:{secs % 60:02d}"
    if session:
        return f"⏱ auto-send in {clock} · Caps to send now"
    return f"⏱ auto-stop in {clock} · Caps to stop now"


def _timer_schedule(delay_s: float, fn: Callable[[], None]) -> None:
    timer = threading.Timer(delay_s, fn)
    timer.daemon = True
    timer.start()


class SessionNotes:
    """Recording-indicator note, in three layers of decreasing priority:

    * ALERT — a timed message about something that just happened ("chunk 3
      failed"). Expires back to whatever is underneath.
    * WARNING — a sticky condition that stays true until it stops being true
      ("your mic is very quiet"). Outranks the routine state because it is
      actionable and the user is mid-recording; cleared explicitly.
    * STATE — the routine line ("2 queued", "transcribing...").

    The warning layer exists because the mid-recording level warning and the
    session's queue count both want the indicator: sharing one slot meant
    whichever wrote last erased the other, so clearing the level warning also
    wiped the queue count.

    A decaying state clears itself unless superseded, so the label can never
    be left stale. One lock serializes all writers (hook thread, queue
    workers, expiry timers), which also keeps UI-queue ordering consistent
    with note ordering.

    ``show`` receives the text to display ('' clears). ``clock`` and
    ``schedule(delay_s, fn)`` are injectable for tests.
    """

    def __init__(self, show: Callable[[str], None],
                 clock: Callable[[], float] = time.monotonic,
                 schedule: Callable[[float, Callable[[], None]], None] = _timer_schedule) -> None:
        self._show = show
        self._clock = clock
        self._schedule = schedule
        self._lock = threading.Lock()
        self._state = ''
        self._state_seq = 0
        self._alert = ''
        self._alert_until = 0.0
        self._warning = ''

    def _push_locked(self) -> None:
        """Recompute and push the visible note. Caller holds the lock."""
        if self._alert and self._clock() < self._alert_until:
            note = self._alert
        else:
            self._alert = ''
            note = self._warning or self._state
        self._show(note)

    def set_state(self, note: str, decay_s: float = 0.0, clear_alert: bool = False) -> None:
        """Set the routine state line. decay_s clears it back to '' after that
        long unless superseded. clear_alert also dismisses an active alert —
        for fresh user actions (a caps-press flush must always be
        acknowledged, even mid-alert)."""
        with self._lock:
            self._state_seq += 1
            seq = self._state_seq
            self._state = note
            if clear_alert:
                self._alert = ''
            self._push_locked()
        if decay_s and note:
            def decay() -> None:
                with self._lock:
                    if seq == self._state_seq:
                        self._state = ''
                        self._push_locked()
            self._schedule(decay_s, decay)

    def set_warning(self, note: str) -> None:
        """Set (or clear, with '') the sticky condition line. Idempotent, so
        a poller can call it every tick without repainting the indicator."""
        with self._lock:
            if note == self._warning:
                return
            self._warning = note
            self._push_locked()

    def set_alert(self, note: str, duration_s: float) -> None:
        """Show a transient alert over the state line; it expires back to
        whatever the state line says then."""
        with self._lock:
            self._alert = note
            self._alert_until = self._clock() + duration_s
            self._push_locked()

        def expire() -> None:
            # Re-evaluates under the lock: a newer/extended alert keeps
            # showing, an expired one falls back to the warning or state
            with self._lock:
                self._push_locked()
        self._schedule(duration_s + 0.1, expire)

    def reset(self) -> None:
        """Clear all three layers and invalidate outstanding decay timers, so
        a previous session's timers can't touch a later session's notes."""
        with self._lock:
            self._state_seq += 1
            self._state = ''
            self._alert = ''
            self._alert_until = 0.0
            self._warning = ''
            self._show('')


class SessionHost(Protocol):
    """What a ConversationSession needs from the application."""

    def is_current(self, session: "ConversationSession") -> bool:
        """True while no newer session has replaced this one."""
    def is_recording(self) -> bool: ...
    def dictation_in_progress(self) -> bool:
        """A normal dictation is mid-pipeline (it owns status and retry)."""
    def deliver_text(self, paste: str, transcript: str) -> None:
        """Paste ``paste`` at the cursor; ``transcript`` (no preamble or
        chunk header) goes to history."""
    def set_retry_candidate(self, path: str) -> None: ...
    def show_warning(self, message: str, duration_ms: int) -> None: ...
    def show_failure(self, short: str, overlay: str) -> None:
        """ERROR status (tray) plus the retryable overlay."""
    def clear_post_session_status(self) -> None:
        """Our own PROCESSING (or a stale Recording) → IDLE; a newer
        dictation's transcribing/cleaning status must be left alone."""


class ConversationSession:
    """One meeting/phone session: ordered chunk delivery plus its notes.

    Callbacks run on queue worker threads (outside the queue's state lock,
    serialized in delivery order), so they only touch thread-safe host
    surfaces and never call back into the queue (data arrives as arguments).
    """

    def __init__(self, *, phone: bool, settings_get: SettingsGetter,
                 notes: SessionNotes, host: SessionHost,
                 transcribe_fn: Callable[[str], str],
                 retry_delay: float = RETRY_DELAY_S) -> None:
        self.phone = phone
        # True until the app ends the session (recording stopped); queued
        # chunks may still be delivering afterwards
        self.live = True
        self._get = settings_get
        self._notes = notes
        self._host = host
        # Most recent classified chunk failure, so the end-of-session summary
        # can name the cause instead of just counting casualties
        self._last_failure: Optional[TranscriptionFailure] = None
        # Sent once, ahead of whichever chunk is delivered first
        self._preamble_pending = bool(settings_get('session_preamble'))
        # The one blank chunk kept for retry. Only one can be the retry
        # candidate, so keeping every blank chunk of a long meeting would
        # hoard minutes of audio per chunk for no benefit; superseding one
        # deletes it. Touched only from the queue's single delivery thread.
        self._blank_path: Optional[str] = None
        self._queue = ChunkQueue(
            transcribe_fn=transcribe_fn,
            on_result=self._on_result,
            on_retrying=self._on_retrying,
            on_failed=self._on_failed,
            on_pending=self._on_pending,
            on_drained=self._on_drained,
            retry_delay=retry_delay,
        )

    # --- queue surface used by the app -------------------------------------

    def submit(self, path: str) -> int:
        return self._queue.submit(path)

    def close(self) -> None:
        """No more chunks; on_drained fires once everything is delivered."""
        self._queue.close()

    def cancel(self) -> None:
        """Drop undelivered results; nothing fires afterwards."""
        self._queue.cancel()

    def active_paths(self) -> List[str]:
        """Files this session still needs: pending chunks, kept failures, and
        — while the session is live or still draining — the blank chunk held
        for retry (the queue counts that one as delivered and forgets it, so
        the sweeper would otherwise delete it mid-session).

        The blank is released once the session is finished and drained: from
        then on it is protected only if it is still the app's retry candidate,
        which `_sweep_snapshots(keep=last_recording)` handles. Holding it
        forever would keep one abandoned chunk per session alive for the rest
        of the process.
        """
        paths = self._queue.active_paths()
        blank = self._blank_path
        if blank and (self.live or paths):
            paths.append(blank)
        return paths

    @property
    def failed_paths(self) -> List[str]:
        return self._queue.failed_paths

    # --- delivery callbacks -------------------------------------------------

    def _on_screen(self) -> bool:
        """The session's own notes own the indicator label right now."""
        return self.live and self._host.is_current(self)

    def _on_result(self, index: int, text: str, path: str) -> None:
        if not text or not text.strip():
            # The provider heard nothing in this chunk. Delivering would
            # paste a bare newline (and, for the first chunk, burn the
            # preamble on it); deleting would throw away audio the user
            # may want to retry. So do neither: keep this file and drop the
            # blank one it supersedes.
            logger.info(f"Chunk {index} transcribed to nothing; kept for retry")
            previous, self._blank_path = self._blank_path, path
            if previous and previous != path:
                try:
                    os.remove(previous)
                except OSError:
                    pass
            if not self._host.dictation_in_progress():
                self._host.set_retry_candidate(path)
            if self._on_screen():
                self._notes.set_alert(f"🔇 nothing heard in chunk {index}", 4.0)
            return
        prefix = ""
        if self._preamble_pending:
            self._preamble_pending = False
            prefix = build_preamble(self._get, self.phone) + "\n\n"
        # Chunk headers mark discontinuities (mid-sentence cuts, and in
        # labeled phone transcripts, where speaker labels reset)
        header = f"--- [chunk {index}] ---\n" if self.phone else ""
        self._host.deliver_text(prefix + header + text + "\n", text)
        logger.info(f"Chunk {index} delivered ({len(text)} chars)")
        try:
            os.remove(path)
        except OSError:
            pass

    def _on_retrying(self, index: int) -> None:
        if self._on_screen():
            self._notes.set_alert(f"⚠️ chunk {index} retrying…", 6.0)
        elif not self._host.is_recording():
            self._host.show_warning(f"⚠️ Chunk {index} failed, retrying…", 3000)

    def _on_failed(self, index: int, path: str, error: Optional[BaseException]) -> None:
        # Keep the file and point the retry machinery at it (tray "Retry
        # Last Transcription" copies the result to the clipboard) — unless
        # a newer dictation is mid-processing, whose own retry candidate
        # must not be clobbered
        if not self._host.dictation_in_progress():
            self._host.set_retry_candidate(path)
        failure = describe_transcription_error(error) if error else None
        if failure and not failure.silent:
            self._last_failure = failure
        reason = f" — {failure.short}" if failure else ""
        if self._on_screen():
            self._notes.set_alert(f"⚠️ chunk {index} failed{reason}", 6.0)
        elif not self._host.is_recording():
            self._host.show_warning(
                f"⚠️ Chunk {index} failed{reason}\n(retry from tray)", 5000)
        else:
            # A newer recording owns the indicator; the warning overlay
            # would hide it when it auto-dismisses, so just log
            logger.warning(f"Chunk {index} from an earlier session failed")

    def _on_pending(self, count: int) -> None:
        if not self._on_screen():
            return
        if count == 0:
            # The last outstanding chunk was just delivered at the cursor
            self._notes.set_state("✅ pasted", decay_s=5.0)
        elif count == 1:
            self._notes.set_state("📤 transcribing…")
        else:
            self._notes.set_state(f"⏳ {count} queued")

    def _on_drained(self, failed_paths: List[str]) -> None:
        if not self._host.is_current(self) or self._host.is_recording():
            return
        if self._host.dictation_in_progress():
            # It owns the status and will set IDLE/ERROR itself when done
            return
        if failed_paths:
            self._host.set_retry_candidate(failed_paths[-1])
            count = f"⚠️ {len(failed_paths)} chunk(s) failed"
            # Name the cause from the most recent failure; when every chunk
            # died the same way (quota, key, network) that's the whole story
            failure = self._last_failure
            short = f"{count} — {failure.short}" if failure else count
            overlay = f"{short}\n{failure.hint}".rstrip() if failure else count
            self._host.show_failure(short, overlay)
        else:
            self._host.clear_post_session_status()
