import ctypes
import os
import sys
import threading
import time
import subprocess
from typing import Any, Callable, Optional, Tuple, Union
import logging
from pathlib import Path
import json

from pynput import keyboard
import pyperclip

from modules.clean_text import clean_transcription
from modules.error_messages import (
    CANCELLED, NO_RECORDING, TranscriptionFailure, describe_transcription_error,
)
from modules import updater
from modules.history import TranscriptionHistory
from modules.hotkey import CapsLockHotkey, HotkeyAction
from modules.paths import RECORDINGS_DIR
from modules.recorder import AudioRecorder, DEFAULT_SILENT_START_TIMEOUT
from modules.session import ConversationSession, SessionNotes
from modules.settings import Settings, api_key_configured
from modules.settings import startup_notes as settings_startup_notes
from modules.transcribe import transcribe_audio, is_conversation_recording
from modules.tray import setup_tray_icon
from modules.tray_pin import promote_tray_icon
from modules.ui import UIFeedback
from modules.audio_manager import set_input_device, get_default_device_id, DeviceIdentifier, find_device_by_identifier
from modules.status_manager import StatusManager, AppStatus, RECORDING_STATUSES
from modules.screen_utils import hide_console_window
from modules.logger import setup_logging
from modules.single_instance import acquire_single_instance_lock, release_single_instance_lock

class VoiceTypingApp:
    def __init__(self) -> None:
        # Initialize settings first
        self.settings = Settings()

        # Setup logging
        self.logger = setup_logging(self.settings)
        self.logger.info("Starting Voice Typing application")
        # Settings loads before logging exists; replay what it did on the way in
        for note in settings_startup_notes:
            self.logger.info(note)

        # Qt handles per-monitor DPI itself; just make sure no console shows
        if os.name == 'nt':
            hide_console_window()

        # Initialize attributes that will be set later by other modules
        self.update_tray_tooltip: Optional[Callable] = None
        self.update_icon_menu: Optional[Callable] = None

        # Initialize last_recording before tray setup
        self.last_recording: Optional[str] = None

        silent_start_timeout = self.settings.get('silent_start_timeout')
        ui_position = self.settings.get('ui_indicator_position')
        ui_size = self.settings.get('ui_indicator_size')
        ui_all_displays = self.settings.get('ui_indicator_all_displays')
        self.ui_feedback = UIFeedback(position=ui_position, size=ui_size, all_displays=ui_all_displays)
        # In-progress recordings live under %LOCALAPPDATA%, never in the
        # program folder (which the installer replaces wholesale)
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        self.recorder = AudioRecorder(
            filename=str(RECORDINGS_DIR / 'temp_audio.wav'),
            level_callback=self.ui_feedback.update_audio_level,
            silent_start_timeout=silent_start_timeout
        )
        self.ui_feedback.set_click_callback(self.handle_ui_click)
        self.recording = False

        # Continuous conversation session (meeting/phone mode): caps lock
        # flushes a chunk and keeps recording; indicator click ends the session.
        # The last session is kept after it ends (its queue may still be
        # draining); recent sessions stay sweep-protected while they hold files.
        # (Initialized before _recover_last_recording, which sweeps snapshots.)
        self._session: Optional[ConversationSession] = None
        self._recent_sessions: list[ConversationSession] = []
        # Recording-indicator note (session state / timed alerts); see
        # modules/session.py. Also used for the odd mid-recording warning.
        self._notes = SessionNotes(self.ui_feedback.set_recording_note)
        # Scopes the recorder watchdog to the recording that scheduled it, so
        # a leftover poll from a just-stopped recording can't start a second
        # concurrent chain (which could double-fire stop/flush actions)
        self._watchdog_token = 0
        # Per-recording generation counter to handle overlapping processing;
        # _recover_last_recording seeds it past surviving snapshot numbers
        self._recording_generation = 0

        # Recover the most recent recording for retry-after-restart, then sweep stale snapshots
        self.last_recording = self._recover_last_recording()
        # Interprets raw keyboard-hook messages (repeat, Ctrl chord, injected)
        # into one toggle per physical Caps Lock press; see modules/hotkey.py
        self._hotkey = CapsLockHotkey()
        self.clean_transcription_enabled = self.settings.get('clean_transcription')
        self.history = TranscriptionHistory()

        self.processing_thread: Optional[threading.Thread] = None
        self.cancel_flag = threading.Event()
        # Live streaming-transcription session for the current recording
        # (normal dictation mode with streaming_dictation enabled)
        self._streaming_session = None
        # Which recording status the current recording uses (varies by mode)
        self._active_recording_status = AppStatus.RECORDING
        # Serializes start/stop transitions (hotkey presses arrive on separate threads)
        self._toggle_lock = threading.RLock()
        # Held for the process lifetime; released explicitly only on restart
        self._instance_mutex: Optional[int] = None

        # Log settings information
        self.logger.info(f"Application settings:\n{json.dumps(self.settings.current_settings)}")

        # Initialize microphone
        self._initialize_microphone()

        # Initialize status manager first
        self.status_manager = StatusManager()

        # Setup single tray icon instance
        setup_tray_icon(self)

        # Now set the callbacks
        self.status_manager.set_callbacks(
            ui_callback=self.ui_feedback.update_status,
            tray_callback=self.update_tray_tooltip
        )

        # Set initial status
        self.status_manager.set_status(AppStatus.IDLE)

        # Store last recording for retry functionality
        self.ui_feedback.set_retry_callback(self.retry_transcription)

        def win32_event_filter(msg: int, data: Any) -> bool:
            # Thin adapter: the decision lives in CapsLockHotkey (pure, tested);
            # this callback only performs the side effects. Keep it fast —
            # Windows silently drops a low-level hook that stalls.
            action = self._hotkey.handle(msg, data.vkCode, data.flags)
            if action is HotkeyAction.TOGGLE:
                threading.Thread(target=self._on_caps_lock_press, daemon=True).start()
            if action in (HotkeyAction.TOGGLE, HotkeyAction.SUPPRESS):
                # suppress_event() RAISES (exiting this filter) by design, so
                # the OS never sees the key and nothing after this line runs
                self.listener.suppress_event()
            return True

        self.listener = keyboard.Listener(
            win32_event_filter=win32_event_filter,
            suppress=False
        )

    def _initialize_microphone(self) -> None:
        """Resolve the saved microphone (or the system default) to a live device.

        A saved microphone that can't be resolved right now — unplugged, or a
        legacy truncated name matching several devices — is KEPT in settings
        and the default used temporarily, so it takes over again once it
        resolves (next launch or Refresh Devices)."""
        try:
            device = None
            saved_identifier = self.settings.get('selected_microphone')
            if saved_identifier is not None:
                try:
                    identifier = DeviceIdentifier(**saved_identifier)
                    device = find_device_by_identifier(identifier)
                except Exception:
                    self.logger.error("Saved microphone setting unusable; using default",
                                      exc_info=True)
            if device:
                set_input_device(device['id'])
                self.logger.info(f"Using saved microphone: {device['name']} (ID: {device['id']}, Channels: {device['max_input_channels']}, Sample Rate: {device['default_samplerate']} Hz)")
                return
            default_id = get_default_device_id()
            set_input_device(default_id)
            if saved_identifier is not None:
                self.logger.warning(f"Saved microphone not available; using default "
                                    f"(ID: {default_id}) until it can be resolved")
            else:
                self.logger.info(f"No saved microphone, using default device (ID: {default_id})")
        except Exception:
            # Even the default couldn't be determined; drop any stale runtime
            # selection so recording streams fall back to the system default
            self.logger.error("Microphone initialization failed", exc_info=True)
            set_input_device(None)

    def set_microphone(self, device_id: int) -> None:
        """Change the active microphone device.

        An in-progress recording keeps its already-open stream — the new
        device applies from the next recorder start. In a conversation
        session that next start is forced immediately by flushing the current
        chunk, so the switch takes effect mid-session."""
        try:
            # Get device info for proper identifier storage
            from modules.audio_manager import get_device_by_id, create_device_identifier
            device = get_device_by_id(device_id)
            if not device:
                raise ValueError(f"Device with ID {device_id} not found")
            identifier = create_device_identifier(device)
            previous = self.settings.get('selected_microphone')
            changed = not (isinstance(previous, dict) and
                           previous.get('name') == device['name'])
            set_input_device(device_id)
            self.settings.set('selected_microphone', identifier._asdict())
            self.logger.info(f"Microphone changed to: {device['name']} (ID: {device_id}, Channels: {device['max_input_channels']}, Sample Rate: {device['default_samplerate']} Hz)")
            if changed and self._session_active:
                threading.Thread(target=self._flush_chunk, daemon=True).start()
        except Exception as e:
            self.logger.error(f"Error setting microphone: {e}", exc_info=True)
            self.logger.debug(f"Failed device_id: {device_id}")
            self.ui_feedback.show_warning("⚠️ Error changing microphone")

    def refresh_microphones(self) -> None:
        """Rescan audio devices and rebuild the tray menu.

        PortAudio snapshots the device list at init, so a real rescan needs a
        reinitialization — which must not happen while a stream is open, and
        shifts device IDs, so the saved selection is re-resolved afterwards."""
        from modules.audio_manager import refresh_devices
        with self._toggle_lock:
            if self.recording:
                # show_warning would be repainted over by the recording
                # pulse/ticker; the recording note is the visible channel here
                self._notes.set_alert("⚠️ can't refresh devices while recording", 3.0)
                return
            try:
                refresh_devices()
            except Exception:
                # Backend state is unknown; don't re-resolve devices or
                # rebuild the menu against it
                self.logger.error("Audio device rescan failed", exc_info=True)
                self.ui_feedback.show_warning("⚠️ Device rescan failed", 4000)
                return
            self._initialize_microphone()
        if self.update_icon_menu:
            self.update_icon_menu()

    def _on_caps_lock_press(self) -> None:
        """Handle Caps Lock press off the hook thread, keeping the hook callback fast."""
        self.toggle_recording()
        time.sleep(0.05)
        self._correct_caps_lock_state()

    def _correct_caps_lock_state(self) -> None:
        """Force Caps Lock off if it was accidentally toggled on."""
        VK_CAPITAL = 0x14
        if ctypes.windll.user32.GetKeyState(VK_CAPITAL) & 1:
            KEYEVENTF_KEYUP = 0x0002
            ctypes.windll.user32.keybd_event(VK_CAPITAL, 0x3A, 0, 0)
            ctypes.windll.user32.keybd_event(VK_CAPITAL, 0x3A, KEYEVENTF_KEYUP, 0)
            self.logger.debug("Corrected accidental Caps Lock activation")

    def _snapshot_paths(self) -> list[Path]:
        """All snapshot files (temp_audio.wav.N.wav), newest first."""
        def mtime(p: Path) -> float:
            # Chunk deliveries delete their files concurrently; a snapshot
            # vanishing between glob and stat must not blow up the caller
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0
        base = Path(self.recorder.filename).resolve()
        snapshots = list(base.parent.glob(base.name + '.*.wav'))
        return sorted(snapshots, key=mtime, reverse=True)

    def _sweep_snapshots(self, keep: Optional[str] = None) -> None:
        """Delete snapshot files, keeping the current retry candidate plus any
        chunk files a conversation session's queue still needs."""
        keep_paths = {Path(keep).resolve()} if keep else set()
        for session in self._recent_sessions:
            keep_paths.update(Path(p).resolve() for p in session.active_paths())
        for snapshot in self._snapshot_paths():
            if snapshot.resolve() in keep_paths:
                continue
            try:
                snapshot.unlink()
            except OSError as e:
                self.logger.warning(f"Could not delete old snapshot {snapshot}: {e}")

    def _recover_last_recording(self) -> Optional[str]:
        """Find the most recent recording after a restart and clean up the rest."""
        snapshots = self._snapshot_paths()
        # Seed the generation counter past any surviving snapshot numbers so
        # this run's snapshots can never os.replace over a kept retry
        # candidate from a previous run
        for snap in snapshots:
            try:
                self._recording_generation = max(
                    self._recording_generation, int(snap.suffixes[-2].lstrip('.')))
            except (ValueError, IndexError):
                continue
        newest_snapshot = str(snapshots[0]) if snapshots else None
        # Always keep the newest snapshot, even when a bare temp_audio.wav
        # exists: the bare file may be a partial recording from a crash, and
        # the snapshot is the only good retry candidate if it turns out bad.
        # (The next completed recording sweeps it away.)
        self._sweep_snapshots(keep=newest_snapshot)
        if os.path.exists(self.recorder.filename):
            return self.recorder.filename
        return newest_snapshot

    def _seal_recording(self, gen: int) -> Optional[str]:
        """Rename the recorder's working file to its generation-stamped
        snapshot (temp_audio.wav.N.wav) so a new recording can't overwrite it
        mid-transcription. Returns the snapshot path, or None when there is
        no working file. Raises OSError if the rename fails."""
        path = self.recorder.filename
        if not os.path.exists(path):
            return None
        snapshot = path + f".{gen}.wav"
        os.replace(path, snapshot)
        return snapshot

    def toggle_recording(self) -> None:
        with self._toggle_lock:
            if not self.recording:
                # Cancel any in-flight processing before starting a new recording
                if self.processing_thread and self.processing_thread.is_alive():
                    self.cancel_flag.set()
                    self.logger.info("Cancelled in-flight processing for new recording")
                self._recording_generation += 1
                self.recorder.meeting_mode = bool(self.settings.get('meeting_mode'))
                self.recorder.phone_mode = (not self.recorder.meeting_mode and
                                            bool(self.settings.get('phone_mode')))
                if self.recorder.meeting_mode:
                    mode_note = " (meeting mode)"
                    self._active_recording_status = AppStatus.RECORDING_MEETING
                elif self.recorder.phone_mode:
                    mode_note = " (phone mode)"
                    self._active_recording_status = AppStatus.RECORDING_PHONE
                else:
                    mode_note = ""
                    self._active_recording_status = AppStatus.RECORDING

                # Streaming dictation (beta): open a realtime session so the
                # transcript is ready ~immediately on stop. Normal mode only;
                # meeting/phone need their multi-speaker batch pipelines.
                if self._streaming_session is not None:
                    self._streaming_session.abort()  # stale leftover
                    self._streaming_session = None
                if (not self.recorder.meeting_mode and not self.recorder.phone_mode
                        and self.settings.get('streaming_dictation')):
                    self._streaming_session = self._start_streaming_session()
                if self._streaming_session is not None:
                    from services.openai_realtime_stt import REALTIME_SAMPLE_RATE
                    self.recorder.samplerate = REALTIME_SAMPLE_RATE
                    self.recorder.stream_callback = self._streaming_session.feed
                    mode_note = " (streaming)"
                else:
                    self.recorder.samplerate = 22050
                    self.recorder.stream_callback = None

                # Meeting/phone recordings run as a continuous session: caps
                # flushes chunks into an ordered transcription queue while
                # recording continues; clicking the indicator ends the session
                self.recorder.continuation_chunk = False
                if self.recorder.meeting_mode or self.recorder.phone_mode:
                    self._session = self._start_session(phone=self.recorder.phone_mode)

                self.logger.info(f"🎙️ Starting recording...{mode_note}")
                self.last_recording = None
                self.recording = True
                self.recorder.start()
                self.status_manager.set_status(self._active_recording_status)
                self._watchdog_token += 1
                token = self._watchdog_token
                self.ui_feedback.call_on_main(lambda: self._check_recorder_status(token))
            elif self._session_active:
                self._flush_chunk()
            else:
                self._stop_recording()

    def _stop_recording(self, expected_gen: Optional[int] = None) -> None:
        """Stop the current dictation and hand it to processing.

        Idempotent: a second caller for the same recording (a caps press
        racing a watchdog auto-stop) returns without touching the snapshot
        the first caller is already transcribing. Callers that decide to
        stop asynchronously (the watchdog) pass the generation they observed,
        so a stop meant for a finished recording can't kill the one that
        replaced it in the meantime."""
        with self._toggle_lock:
            if not self.recording:
                return
            gen = self._recording_generation
            if expected_gen is not None and expected_gen != gen:
                self.logger.info("Ignoring stale stop request for a superseded recording")
                return
            self.recording = False
            self.recorder.stop()
            self.logger.info("Recording stopped")

            # Detach the streaming session from app state; from here it either
            # travels with this recording's processing or gets aborted
            stream_session, self._streaming_session = self._streaming_session, None

            if self.recorder.was_auto_stopped():
                if stream_session is not None:
                    stream_session.abort()
                self.status_manager.set_status(
                    AppStatus.ERROR,
                    "⚠️ Recording stopped: No audio detected"
                )
                self.logger.warning("Recording auto-stopped due to initial silence")
                self.recorder.auto_stopped = False
                return

            if self.recorder.max_duration_reached:
                self.logger.warning("Recording hit max duration; transcribing what was captured")
                self.recorder.max_duration_reached = False

            if self.recorder.error is not None:
                self.logger.warning(f"Recording ended by device error: {self.recorder.error}; "
                                    "transcribing what was captured")
                self.recorder.error = None

            # A missing or un-renamable file leaves the bare path, which the
            # processing thread then reports as a missing recording
            try:
                self.last_recording = self._seal_recording(gen) or self.recorder.filename
            except OSError:
                self.logger.error("Could not snapshot recording", exc_info=True)
                self.last_recording = self.recorder.filename
            # Older snapshots are no longer retry candidates; drop them
            self._sweep_snapshots(keep=self.last_recording)
            self.status_manager.set_status(AppStatus.PROCESSING)
            self.process_audio(stream_session)

    def _flush_chunk(self, expected_gen: Optional[int] = None) -> None:
        """Seal the current chunk, queue it for transcription, resume recording.

        The gap between stop and restart is the quick-restart cost the session
        design accepts (~0.3s mic-only, up to ~1-2s in meeting mode where the
        loopback thread is rejoined and the 2-channel file composed).

        expected_gen: asynchronous callers (the max-duration watchdog) pass
        the generation they saw; if a caps press already sealed that chunk,
        the request is dropped instead of producing a near-empty extra one."""
        with self._toggle_lock:
            if not (self.recording and self._session_active):
                return
            if expected_gen is not None and expected_gen != self._recording_generation:
                self.logger.info("Ignoring stale flush request; the chunk was already sealed")
                return
            # Acknowledge the caps press right away — the stop/restart and
            # chunk analysis below can take a moment, and the user needs to
            # see the flush registered (recording continues throughout)
            self._notes.set_state("📤 transcribing…", clear_alert=True)
            self.recorder.stop()
            self._recording_generation += 1
            try:
                snapshot = self._seal_recording(self._recording_generation)
            except OSError:
                snapshot = None
                self.logger.error("Could not snapshot chunk; skipping it", exc_info=True)
                self._notes.set_alert("⚠️ chunk could not be saved", 5.0)
            # Restart capture before analyzing/queueing the sealed chunk: the
            # snapshot is a closed file, so this shrinks the not-recording gap
            # (where spoken words are lost) to just the stop/restart itself
            self.recorder.continuation_chunk = True
            self.recorder.start()
            if snapshot:
                is_valid, reason = self.recorder.analyze_recording(snapshot)
                if is_valid:
                    index = self._session.submit(snapshot)
                    self.logger.info(f"Chunk {index} queued for transcription")
                else:
                    # Quiet flush (nothing said since the last one): drop it
                    # without the error flash a failed dictation would get
                    self.logger.info(f"Skipping chunk: {reason}")
                    self._notes.set_alert("🔇 nothing new to send", 3.0)
                    try:
                        os.remove(snapshot)
                    except OSError:
                        pass

    def _end_session(self, auto_stopped: bool = False,
                     error: Optional[str] = None,
                     expected_gen: Optional[int] = None) -> None:
        """End the conversation session, discarding the unflushed tail.

        Audio since the last flush is dropped by design (press caps to flush
        before ending if you want it); chunks already queued keep delivering
        in order. Exception: when a recording ERROR ends the session (mic
        unplugged, driver failure), the user didn't choose to end it, so the
        tail is salvaged into the queue instead of discarded.

        expected_gen: asynchronous callers (the watchdog) pass the generation
        they saw. A flush or a new session in the meantime restarts capture,
        so a stale end request is dropped; if the fault persists the watchdog
        sees it again on its next tick."""
        with self._toggle_lock:
            session = self._session
            if session is None or not session.live:
                return
            if expected_gen is not None and expected_gen != self._recording_generation:
                self.logger.info("Ignoring stale end-session request for a superseded capture")
                return
            session.live = False
            self.recording = False
            self.recorder.continuation_chunk = False
            try:
                self.recorder.stop()
            except Exception:
                self.logger.error("Error stopping recorder", exc_info=True)
            self.recorder.auto_stopped = False
            self.recorder.error = None
            if error:
                # Salvage audio captured before the device failed
                self._recording_generation += 1
                try:
                    snapshot = self._seal_recording(self._recording_generation)
                    if snapshot and self.recorder.analyze_recording(snapshot)[0]:
                        index = session.submit(snapshot)
                        self.logger.info(f"Salvaged session tail as chunk {index} after recording error")
                    elif snapshot:
                        os.remove(snapshot)
                except OSError:
                    self.logger.warning("Could not salvage session tail", exc_info=True)
            try:
                if os.path.exists(self.recorder.filename):
                    os.remove(self.recorder.filename)
            except OSError:
                self.logger.warning("Could not delete session tail", exc_info=True)
            self._notes.reset()
            if auto_stopped:
                # First chunk never made a sound; nothing was queued
                session.cancel()
                self.status_manager.set_status(
                    AppStatus.ERROR,
                    "⚠️ Recording stopped: No audio detected"
                )
                self.logger.warning("Session auto-stopped due to initial silence")
                return
            if error:
                # Queued chunks (and any salvaged tail) still deliver in
                # order; only new recording is dead
                self.logger.error(f"Session ended by recording error: {error}")
                self.status_manager.set_status(
                    AppStatus.ERROR,
                    "⚠️ Recording error — session ended"
                )
                session.close()
                return
            self.logger.info("Conversation session ended")
            # PROCESSING first, then close(): if the queue is already empty
            # the drained callback immediately corrects this to IDLE/ERROR
            self.status_manager.set_status(AppStatus.PROCESSING)
            session.close()

    # --- Conversation sessions (modules/session.py) ---------------------------

    @property
    def _session_active(self) -> bool:
        """A meeting/phone session is recording (caps = flush, click = end)."""
        return self._session is not None and self._session.live

    def _start_session(self, phone: bool) -> ConversationSession:
        session = ConversationSession(
            phone=phone, settings_get=self.settings.get, notes=self._notes,
            host=self, transcribe_fn=transcribe_audio)
        # Sweep protection: keep sessions that still hold files (draining, or
        # failures kept for retry) rather than capping by count, so a
        # slow-draining session can't lose its files to a sweep
        self._recent_sessions = [s for s in self._recent_sessions if s.active_paths()]
        self._recent_sessions.append(session)
        return session

    # SessionHost protocol — the app surfaces a session may touch from its
    # queue-worker threads. All of these are thread-safe.

    def is_current(self, session: ConversationSession) -> bool:
        return self._session is session

    def is_recording(self) -> bool:
        return self.recording

    def dictation_in_progress(self) -> bool:
        return bool(self.processing_thread and self.processing_thread.is_alive())

    def deliver_text(self, paste: str, transcript: str) -> None:
        self.history.add(transcript)
        self.ui_feedback.insert_text(paste)
        if self.update_icon_menu:
            self.update_icon_menu()

    def set_retry_candidate(self, path: str) -> None:
        self.last_recording = path

    def show_warning(self, message: str, duration_ms: int) -> None:
        self.ui_feedback.show_warning(message, duration_ms)

    def show_failure(self, short: str, overlay: str) -> None:
        # Status first, retry overlay second (see _report_failure)
        self.status_manager.set_status(AppStatus.ERROR, short)
        self.ui_feedback.show_error_with_retry(overlay)

    def clear_post_session_status(self) -> None:
        # Clear our own post-session PROCESSING state — including the case
        # where the recording watchdog reasserted a stale "Recording" status
        # in the instant the session ended (with self.recording False that
        # status can only be stale). A newer dictation's transcribing/
        # cleaning status is left alone.
        if (self.status_manager.current_status == AppStatus.PROCESSING or
                self.status_manager.current_status in RECORDING_STATUSES):
            self.status_manager.set_status(AppStatus.IDLE)

    # Add this method to check recorder status periodically
    def _check_recorder_status(self, token: int) -> None:
        """Periodically check if recorder has auto-stopped and guard recording UI.

        Runs on the Tk thread as a 100ms after() chain. The token ties the
        chain to the recording that started it: a newer recording bumps the
        token, so a stale pending callback exits instead of spawning a second
        chain that could double-fire stop/flush actions."""
        if token != self._watchdog_token:
            return

        # Every action below runs on its own thread and takes the toggle lock
        # later; it carries the generation seen now so that, if a caps press
        # stops/flushes/restarts first, the stale action is dropped instead of
        # double-processing or stopping the recording that replaced this one
        gen = self._recording_generation

        if self.recording and self.recorder.error is not None:
            # Device/stream failure (mic unplugged, driver error) — unlike the
            # silent-start case below, audio already captured must be kept
            if self._session_active:
                threading.Thread(target=self._end_session,
                                 kwargs={'error': self.recorder.error, 'expected_gen': gen},
                                 daemon=True).start()
            else:
                threading.Thread(target=self._stop_recording,
                                 kwargs={'expected_gen': gen}, daemon=True).start()
            return

        if self.recording and self.recorder.was_auto_stopped():
            if self._session_active:
                threading.Thread(target=self._end_session,
                                 kwargs={'auto_stopped': True, 'expected_gen': gen},
                                 daemon=True).start()
            else:
                threading.Thread(target=self._stop_recording,
                                 kwargs={'expected_gen': gen}, daemon=True).start()
            return

        if self.recording and self.recorder.max_duration_reached:
            if self._session_active:
                # Roll into a new chunk instead of ending the session
                self.recorder.max_duration_reached = False
                self.logger.warning("Max chunk duration reached; auto-flushing")
                threading.Thread(target=self._flush_chunk,
                                 kwargs={'expected_gen': gen}, daemon=True).start()
            else:
                threading.Thread(target=self._stop_recording,
                                 kwargs={'expected_gen': gen}, daemon=True).start()
                return

        if self.recording:
            # Self-heal: if a stale processing thread overwrote our status, reassert it
            if self.status_manager.current_status != self._active_recording_status:
                self.status_manager.set_status(self._active_recording_status)
            self.ui_feedback.after(100, lambda: self._check_recorder_status(token))

    def process_audio(self, stream_session=None) -> None:
        try:
            self.cancel_flag.clear()
            gen = self._recording_generation
            self.processing_thread = threading.Thread(
                target=self._process_audio_thread, args=(gen, stream_session))
            self.processing_thread.start()
        except Exception as e:
            if stream_session is not None:
                stream_session.abort()
            self.logger.error("Failed to start processing thread", exc_info=True)
            self.logger.debug(f"Thread state: {threading.current_thread().name}")
            self.ui_feedback.insert_text(f"Error: {str(e)[:50]}...")

    def _is_stale(self, gen: int) -> bool:
        """Check if this processing run has been superseded by a newer recording."""
        return gen != self._recording_generation or self.cancel_flag.is_set()

    def _process_audio_thread(self, gen: int, stream_session=None) -> None:
        try:
            self.logger.info("Starting audio processing")
            is_valid, reason = self.recorder.analyze_recording(self.last_recording)

            if self._is_stale(gen):
                if stream_session is not None:
                    stream_session.abort()
                self.logger.info("Processing cancelled (stale generation).")
                return

            if not is_valid:
                if stream_session is not None:
                    stream_session.abort()
                if self._is_stale(gen):
                    return
                self.logger.warning(f"Skipping transcription: {reason}")
                self.status_manager.set_status(
                    AppStatus.ERROR,
                    "⛔ Skipped: " + ("too short" if "short" in reason.lower() else "mostly silence")
                )
                return

            # Streaming path: the realtime session already has the audio; just
            # flush and collect. Any failure falls through to the batch upload.
            streamed_text = None
            if stream_session is not None:
                try:
                    if not self.cancel_flag.is_set():
                        self.status_manager.set_status(AppStatus.TRANSCRIBING)
                    streamed_text = stream_session.finish()
                    self.logger.info(f"Streaming transcription ready ({len(streamed_text)} chars)")
                except Exception as e:
                    self.logger.warning(f"Streaming transcription failed, falling back to batch: {e}")
                    stream_session.abort()

            self.logger.info("Starting transcription")
            success, result = self._attempt_transcription(streamed_text=streamed_text)

            if self._is_stale(gen):
                self.logger.info("Processing cancelled (stale generation).")
                return

            if not success:
                if self._is_stale(gen):
                    return
                self._report_failure(result)
            elif result:
                if self._is_stale(gen):
                    return
                self.history.add(result)
                self.ui_feedback.insert_text(result)
                if self.update_icon_menu:
                    self.update_icon_menu()
                self.status_manager.set_status(AppStatus.IDLE)
                if self.settings.get('log_transcript_text'):
                    preview_len = 50
                    preview = result[:preview_len] + "..." if len(result) > preview_len else result
                    self.logger.info(f"Transcription completed ({len(result)} chars): {preview}")
                else:
                    self.logger.info(f"Transcription completed ({len(result)} chars)")

        except Exception as e:
            if self._is_stale(gen):
                return
            self.logger.error("Error in _process_audio_thread:", exc_info=True)
            self._report_failure(describe_transcription_error(e))

    def _report_failure(self, failure: Union[str, TranscriptionFailure, None],
                        prefix: str = "") -> None:
        """Surface a classified failure on the indicator and the tray tooltip.

        Silent failures (cancelled, nothing recorded) are control flow, not
        errors — they leave the current status alone."""
        if not isinstance(failure, TranscriptionFailure):
            failure = TranscriptionFailure("⚠️ Transcription failed")
        if failure.silent:
            return
        # Order matters: both calls repaint the indicator label via the UI
        # queue, and the ERROR status only knows the short line. The retry
        # overlay must land last so the hint and "🔄 Click to retry" survive.
        self.status_manager.set_status(AppStatus.ERROR, prefix + failure.short)
        self.ui_feedback.show_error_with_retry(prefix + failure.overlay)

    def _attempt_transcription(self, recording_path: Optional[str] = None,
                               streamed_text: Optional[str] = None
                               ) -> Tuple[bool, Union[str, TranscriptionFailure, None]]:
        """Attempt transcription and return (success, result or failure).

        On success the second element is the transcript. On failure it is a
        TranscriptionFailure carrying display text for the caller — silent
        ones (cancelled, nothing recorded) should produce no error UI.

        Pass recording_path explicitly when the caller may run concurrently
        with new recordings (retry), since self.last_recording is mutable.
        If streamed_text is provided (realtime streaming already transcribed
        the recording), the batch upload is skipped but cleaning still runs."""
        try:
            path = recording_path or self.last_recording
            if not path:
                self.logger.error("Attempted transcription with no recording available.")
                return False, NO_RECORDING

            # Update status to show we're transcribing (skip if already cancelled,
            # so a cancel can't be overwritten by a stale pulsing status)
            if not self.cancel_flag.is_set():
                self.status_manager.set_status(AppStatus.TRANSCRIBING)
            text = streamed_text if streamed_text else transcribe_audio(path)

            if self.cancel_flag.is_set():
                return False, CANCELLED

            # Meeting/phone transcripts are speaker-labeled; LLM cleaning would
            # mangle the labels, so skip it for those recordings
            if self.clean_transcription_enabled and not is_conversation_recording(path):
                try:
                    # Update status to show we're cleaning
                    if not self.cancel_flag.is_set():
                        self.status_manager.set_status(AppStatus.CLEANING)

                    cleaned_text = clean_transcription(
                        text,
                        model=self.settings.get('llm_model'),
                        timeout=self.settings.get('cleaning_timeout'),
                        base_url=self.settings.get('llm_base_url') or None)
                    self.logger.info("Transcription cleaned successfully")
                    return True, cleaned_text
                except Exception as e:
                    self.logger.warning(f"LLM cleaning failed, falling back to raw transcription. Error: {e}")
                    # Show a brief warning that we're using the fallback
                    self.ui_feedback.show_warning("⚠️ Using raw transcript (cleaning failed)", 2000)
                    return True, text  # Fallback to original text

            return True, text
        except Exception as e:
            self.logger.error(f"Transcription error: {e}", exc_info=True)
            return False, describe_transcription_error(e)

    def retry_transcription(self) -> None:
        """Retry transcription of the last failed recording; the result goes
        to the clipboard rather than the cursor.

        Runs as the tracked processing job: starting a new recording cancels
        it exactly like an in-flight dictation, and a retry that outlives its
        generation discards its result instead of copying text, rewriting
        history, or repainting the status over the newer recording."""
        with self._toggle_lock:
            # Capture the path now: self.last_recording can be cleared/replaced
            # by a new recording while the retry is in flight
            recording_path = self.last_recording
            if not recording_path:
                return
            if self.recording:
                self.logger.info("Retry ignored: a recording is in progress")
                return
            if self.processing_thread and self.processing_thread.is_alive():
                self.logger.info("Retry ignored: a transcription is already in progress")
                return
            gen = self._recording_generation
            self.cancel_flag.clear()

            def retry_thread() -> None:
                self.status_manager.set_status(AppStatus.PROCESSING)
                success, result = self._attempt_transcription(recording_path)
                if self._is_stale(gen):
                    self.logger.info("Retry result discarded (superseded by a newer recording)")
                    return
                if success and result:
                    self.history.add(result)
                    pyperclip.copy(result)  # Copy to clipboard instead of direct insertion
                    self.status_manager.set_status(AppStatus.IDLE)
                    self.ui_feedback.show_warning("✅ Transcription copied to clipboard", 3000)
                    # Update the menu to reflect the new transcription in history
                    if self.update_icon_menu:
                        self.update_icon_menu()
                else:
                    # Keep the reason (quota, key, network) visible on the retry
                    self._report_failure(result, prefix="🔄 Retry failed — ")

            self.processing_thread = threading.Thread(target=retry_thread, daemon=True)
            self.processing_thread.start()

    def toggle_clean_transcription(self) -> None:
        self.clean_transcription_enabled = not self.clean_transcription_enabled
        self.settings.set('clean_transcription', self.clean_transcription_enabled)
        status = 'enabled' if self.clean_transcription_enabled else 'disabled'
        self.logger.info(f"Clean transcription {status}")

    # Meeting and phone mode are mutually exclusive capture strategies: enabling
    # one disables the other. Both need ElevenLabs (multichannel / diarization).
    _CONVERSATION_MODES = {
        'meeting_mode': ('🎧', 'Meeting mode', 'phone_mode'),
        'phone_mode': ('📞', 'Phone mode', 'meeting_mode'),
    }

    def toggle_meeting_mode(self) -> None:
        """Toggle meeting mode (mic + system audio with speaker-labeled transcripts)."""
        self._toggle_conversation_mode('meeting_mode')

    def toggle_phone_mode(self) -> None:
        """Toggle phone mode (mic-only conversation with diarized transcripts).

        For conversations happening in the room — a call on speakerphone, an
        in-person chat — where all voices reach the microphone. Speakers are
        separated by voice diarization instead of by channel."""
        self._toggle_conversation_mode('phone_mode')

    def _toggle_conversation_mode(self, key: str) -> None:
        """Runs under the toggle lock so a caps press can't start a session in
        the gap between ending the current one and flipping the setting."""
        icon, label, other = self._CONVERSATION_MODES[key]
        other_label = self._CONVERSATION_MODES[other][1]
        with self._toggle_lock:
            if self._session_active:
                self._end_session()
            enabling = not self.settings.get(key)

            if enabling:
                if not api_key_configured('ELEVENLABS_API_KEY'):
                    self.ui_feedback.show_warning(
                        f"⚠️ {label} needs ELEVENLABS_API_KEY in .env", 5000)
                    self.logger.warning(f"{label} not enabled: ELEVENLABS_API_KEY missing")
                    return
                if self.settings.get(other):
                    self.settings.set(other, False)
                    self.logger.info(f"{other_label} disabled ({label.lower()} enabled)")
                self.ui_feedback.show_warning(*self._mode_on_notice(key))
            else:
                self.ui_feedback.show_warning(f"{icon} {label} off", 2000)

            self.settings.set(key, enabling)
            self.logger.info(f"{label} {'enabled' if enabling else 'disabled'}")
        if self.update_icon_menu:
            self.update_icon_menu()

    def _mode_on_notice(self, key: str) -> Tuple[str, int]:
        """(message, duration_ms) confirming a conversation mode was enabled."""
        if key == 'phone_mode':
            return "📞 Phone mode on — Caps Lock sends a chunk, click the indicator to end", 4000
        from modules.loopback_recorder import loopback_available
        available, detail = loopback_available()
        if available:
            return f"🎧 Meeting mode on ({detail}) — Caps Lock sends, click to end", 4000
        # Allow enabling anyway: capture falls back to mic-only per
        # recording, and the output device may change before next use
        self.logger.warning(f"Loopback unavailable at toggle time: {detail}")
        return "⚠️ Meeting mode on, but system audio capture unavailable", 5000

    def toggle_streaming_dictation(self) -> None:
        """Toggle streaming dictation (beta): transcribe over a realtime
        websocket while recording, so text is ready ~immediately on stop.
        Applies to normal dictation only; falls back to batch on any failure."""
        enabling = not self.settings.get('streaming_dictation')

        if enabling:
            if not api_key_configured('OPENAI_API_KEY'):
                self.ui_feedback.show_warning(
                    "⚠️ Streaming dictation needs OPENAI_API_KEY in .env", 5000)
                self.logger.warning("Streaming dictation not enabled: OPENAI_API_KEY missing")
                return
            self.ui_feedback.show_warning("⚡ Streaming dictation on (beta)", 3000)
        else:
            self.ui_feedback.show_warning("⚡ Streaming dictation off", 2000)

        self.settings.set('streaming_dictation', enabling)
        self.logger.info(f"Streaming dictation {'enabled' if enabling else 'disabled'}")
        if self.update_icon_menu:
            self.update_icon_menu()

    def check_for_updates(self, startup: bool = False) -> None:
        """Tray "Check for Updates", and the once-daily startup check.

        Installed app: newer release → download, verify, hand off to the
        installer and exit (the startup variant only announces it). Source
        checkout: open the releases page."""
        def worker() -> None:
            try:
                if not updater.running_as_installed():
                    if not startup:
                        updater.open_releases_page()
                    return
                release = updater.fetch_latest_release()
                current = updater.current_version()
                if release is None or not updater.is_newer(release.version, current):
                    if not startup:
                        self.ui_feedback.show_warning(f"✅ Up to date (v{current})", 3000)
                    return
                if startup:
                    self.ui_feedback.show_warning(
                        f"⬆️ v{release.version} is available — tray → Check for Updates", 6000)
                    return
                with self._toggle_lock:
                    if self.recording:
                        self.ui_feedback.show_warning("⚠️ Finish recording before updating", 3000)
                        return
                self.ui_feedback.show_warning(f"⬇️ Downloading v{release.version}…", 15000)
                installer = updater.download_installer(release)
                self.ui_feedback.show_warning("🔄 Installing update — the app will restart", 5000)
                time.sleep(1.5)  # let the notice paint before the process goes away
                updater.launch_installer_and_exit(installer)
            except Exception as e:
                self.logger.error("Update check failed", exc_info=True)
                if not startup:
                    self.ui_feedback.show_warning(f"⚠️ Update failed: {str(e)[:70]}", 6000)
        threading.Thread(target=worker, daemon=True).start()

    def _schedule_startup_update_check(self) -> None:
        """At most once a day, a little after startup, only for installed builds."""
        if not updater.running_as_installed():
            return
        last = self.settings.get('last_update_check') or 0
        if time.time() - float(last) < 24 * 3600:
            return
        self.settings.set('last_update_check', time.time())
        self.ui_feedback.after(15000, lambda: self.check_for_updates(startup=True))

    def _pin_tray_icon(self, attempt: int = 0) -> None:
        """Promote the tray icon out of the Windows overflow, once per
        executable path (so a user who later hides it on purpose isn't
        overruled at every launch). Explorer registers the icon a moment
        after it appears, so retry a few times."""
        exe = sys.executable
        if self.settings.get('tray_pinned_exe') == exe:
            return
        result = promote_tray_icon(exe)
        if result:
            self.settings.set('tray_pinned_exe', exe)
        elif result is False and attempt < 5:
            self.ui_feedback.after(5000, lambda: self._pin_tray_icon(attempt + 1))

    def run(self) -> None:
        # Start keyboard listener
        self.listener.start()
        self.ui_feedback.after(4000, self._pin_tray_icon)
        self._schedule_startup_update_check()

        # Qt main loop, on the main thread
        try:
            self.ui_feedback.run()
        finally:
            self.cleanup()
            sys.exit(0)

    def cleanup(self) -> None:
        """Ensure proper cleanup of all resources"""
        self.logger.info("Cleaning up application resources")
        self.listener.stop()
        if self.recording:
            self.recorder.stop()
        self.ui_feedback.cleanup()

    def handle_ui_click(self) -> None:
        """Handle clicks on the UI feedback window."""
        status = self.status_manager.current_status
        if status in RECORDING_STATUSES:
            if self._session_active:
                self.logger.info("Ending conversation session (indicator click)...")
                threading.Thread(target=self._end_session, daemon=True).start()
            else:
                self.logger.info("Canceling recording...")
                threading.Thread(target=self._cancel_recording, daemon=True).start()
                self.status_manager.set_status(AppStatus.IDLE)
        elif status in (AppStatus.PROCESSING, AppStatus.TRANSCRIBING, AppStatus.CLEANING):
            self.logger.info("Canceling processing...")
            if self.processing_thread and self.processing_thread.is_alive():
                self.cancel_flag.set()
            elif self._session is not None:
                # Only when no dictation is processing is the visible activity
                # the session queue's post-end drain; cancelling the dictation
                # must not silently discard delivered-in-order session chunks
                self._session.cancel()
            # The processing thread exits silently once it notices the flag;
            # reset the UI here so it can't be left stuck on a pulsing status
            self.status_manager.set_status(AppStatus.IDLE)

    def _cancel_recording(self) -> None:
        """Stop and discard the current recording, serialized against hotkey toggles."""
        with self._toggle_lock:
            if not self.recording:
                return
            # Retire the watchdog chain here, not just via recording=False: a
            # tick between the click (which already showed IDLE) and this lock
            # would reassert the recording status, and nothing after the chain
            # exits would ever restore IDLE
            self._watchdog_token += 1
            self.recording = False
            if self._streaming_session is not None:
                self._streaming_session.abort()
                self._streaming_session = None
            try:
                self.recorder.stop()
            except Exception:
                self.logger.error("Error stopping recorder", exc_info=True)
            self.status_manager.set_status(AppStatus.IDLE)

    def _start_streaming_session(self):
        """Open a realtime transcription session, or None if unavailable.

        Failure is non-fatal: recording proceeds normally and transcription
        happens via the regular batch upload on stop."""
        try:
            from services.openai_realtime_stt import RealtimeDictationSession
            model = self.settings.get('openai_stt_model') or 'gpt-4o-transcribe'
            if not str(model).startswith('gpt-4o'):
                model = 'gpt-4o-transcribe'  # realtime doesn't support whisper-1
            language = self.settings.get('stt_language') or 'en'
            session = RealtimeDictationSession(model=model, language=language)
            session.start()
            return session
        except Exception as e:
            self.logger.warning(f"Streaming session unavailable, using batch: {e}")
            return None

    def toggle_silence_detection(self) -> None:
        """Toggle silence detection on/off"""
        current_timeout = self.settings.get('silent_start_timeout')
        # Toggle between None and default timeout
        new_timeout = None if current_timeout is not None else DEFAULT_SILENT_START_TIMEOUT
        self.settings.set('silent_start_timeout', new_timeout)

        # Update recorder's silence timeout
        self.recorder.silent_start_timeout = new_timeout

        status = "enabled" if new_timeout is not None else "disabled"
        self.logger.info(f"Silence detection {status}")

    def show_main_window(self) -> None:
        """Open (or raise) the main window. Called from the tray on the main thread."""
        self.logger.info("Main window requested (not implemented yet)")

    def restart_app(self) -> None:
        """Restart the application by launching a new instance and closing the current one."""
        self.logger.info("Attempting to restart application...")
        try:
            # Use subprocess.Popen to ensure the correct python executable from the venv is used.
            # sys.executable is the path to the python interpreter running the script.
            # We pass sys.argv to the new process to restart with the same arguments.
            # This is more reliable than os.startfile as it doesn't depend on file associations.
            self.logger.debug(f"Restarting with command: {[sys.executable] + sys.argv}")
            # Hand off the single-instance mutex so the new instance can acquire it
            release_single_instance_lock(self._instance_mutex)
            self._instance_mutex = None
            subprocess.Popen([sys.executable] + sys.argv)

            # Exit current instance
            self.logger.info("New instance started. Exiting current instance.")
            # Ensure all logs are written before exiting
            logging.shutdown()
            os._exit(0)
        except Exception as e:
            # Restart failed and we're staying alive: retake the single-instance
            # guard that was released for the hand-off
            if self._instance_mutex is None:
                self._instance_mutex = acquire_single_instance_lock()
            self.logger.error(f"Failed to restart application: {e}", exc_info=True)
            self.status_manager.set_status(AppStatus.ERROR, "⚠️ Failed to restart")

if __name__ == "__main__":
    mutex = acquire_single_instance_lock()
    if mutex is None:
        # Another instance is already running; tell the user and bail out
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning(
                "Voice Typing",
                "Voice Typing is already running.\nCheck the system tray for the microphone icon."
            )
            root.destroy()
        except Exception:
            pass
        sys.exit(0)

    app = VoiceTypingApp()
    app._instance_mutex = mutex
    app.run()