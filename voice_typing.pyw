import ctypes
import os
import sys
import threading
import time
import subprocess
from typing import Any, Callable, Optional, Tuple
import logging
from pathlib import Path
import json

from pynput import keyboard
import pyperclip

from modules.chunk_queue import ChunkQueue
from modules.clean_text import clean_transcription
from modules.history import TranscriptionHistory
from modules.output_providers import initialize_providers
from modules.recorder import AudioRecorder, DEFAULT_SILENT_START_TIMEOUT
from modules.settings import Settings
from modules.transcribe import transcribe_audio, is_conversation_recording
from modules.tray import setup_tray_icon
from modules.ui import UIFeedback
from modules.audio_manager import set_input_device, get_default_device_id, DeviceIdentifier, find_device_by_identifier
from modules.status_manager import StatusManager, AppStatus, RECORDING_STATUSES
from modules.screen_utils import set_process_dpi_awareness, hide_console_window
from modules.logger import setup_logging
from modules.single_instance import acquire_single_instance_lock, release_single_instance_lock

class VoiceTypingApp:
    def __init__(self) -> None:
        # Initialize settings first
        self.settings = Settings()

        # Setup logging
        self.logger = setup_logging(self.settings)
        self.logger.info("Starting Voice Typing application")

        # Windows specific tweaks (DPI awareness & hiding console)
        if os.name == 'nt':
            if not set_process_dpi_awareness():
                self.logger.debug("DPI awareness could not be set or is already configured.")
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
        self.recorder = AudioRecorder(
            level_callback=self.ui_feedback.update_audio_level,
            silent_start_timeout=silent_start_timeout
        )
        self.ui_feedback.set_click_callback(self.handle_ui_click)
        self.recording = False

        # Continuous conversation session (meeting/phone mode): caps lock
        # flushes a chunk and keeps recording; indicator click ends the session.
        # Recent queues stay sweep-protected while their deliveries drain.
        # (Initialized before _recover_last_recording, which sweeps snapshots.)
        self._session_active = False
        self._chunk_queue: Optional[ChunkQueue] = None
        self._recent_queues: list[ChunkQueue] = []
        self._note_hold_until = 0.0
        # Scopes the recorder watchdog to the recording that scheduled it, so
        # a leftover poll from a just-stopped recording can't start a second
        # concurrent chain (which could double-fire stop/flush actions)
        self._watchdog_token = 0

        # Recover the most recent recording for retry-after-restart, then sweep stale snapshots
        self.last_recording = self._recover_last_recording()
        self.ctrl_pressed = False
        self.caps_down = False
        self.caps_passthrough = False
        self.clean_transcription_enabled = self.settings.get('clean_transcription')
        self.history = TranscriptionHistory()

        # Initialize output providers and show any plugin errors
        plugin_errors = initialize_providers()
        if plugin_errors:
            # Show first error briefly, log all
            self.ui_feedback.show_warning(plugin_errors[0], duration_ms=5000)
            for error in plugin_errors:
                self.logger.warning(f"Plugin error: {error}")

        # Per-recording generation counter to handle overlapping processing
        self.processing_thread: Optional[threading.Thread] = None
        self.cancel_flag = threading.Event()
        self._recording_generation = 0
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
            VK_CONTROL = 0x11
            VK_LCONTROL = 0xA2
            VK_RCONTROL = 0xA3
            VK_CAPITAL = 0x14

            WM_KEYDOWN = 0x0100
            WM_KEYUP = 0x0101

            LLKHF_INJECTED = 0x10

            if data.vkCode in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL):
                if msg == WM_KEYDOWN:
                    self.ctrl_pressed = True
                elif msg == WM_KEYUP:
                    self.ctrl_pressed = False
                return True

            if data.vkCode == VK_CAPITAL:
                # Let our own corrective keystrokes pass through
                if data.flags & LLKHF_INJECTED:
                    return True

                if msg == WM_KEYDOWN:
                    if self.ctrl_pressed:
                        self.caps_passthrough = True
                        return True

                    if self.caps_down:
                        # suppress_event() RAISES (exiting this filter), so OS
                        # key-repeat stops here and never re-toggles recording
                        self.listener.suppress_event()

                    self.caps_down = True
                    self.caps_passthrough = False
                    threading.Thread(target=self._on_caps_lock_press, daemon=True).start()
                    self.listener.suppress_event()

                elif msg == WM_KEYUP:
                    self.caps_down = False
                    if self.caps_passthrough:
                        self.caps_passthrough = False
                        return True
                    self.listener.suppress_event()

            return True

        self.listener = keyboard.Listener(
            win32_event_filter=win32_event_filter,
            suppress=False
        )

    def _initialize_microphone(self) -> None:
        """Initialize microphone device from settings or default"""
        try:
            saved_identifier = self.settings.get('selected_microphone')
            if saved_identifier is not None:
                try:
                    # Convert dictionary back to DeviceIdentifier
                    identifier = DeviceIdentifier(**saved_identifier)
                    device = find_device_by_identifier(identifier)
                    if device:
                        set_input_device(device['id'])
                        self.logger.info(f"Using saved microphone: {device['name']} (ID: {device['id']}, Channels: {device['max_input_channels']}, Sample Rate: {device['default_samplerate']} Hz)")
                    else:
                        # Fallback to default if saved device not found
                        self.settings.set('selected_microphone', None)
                        default_id = get_default_device_id()
                        set_input_device(default_id)
                        self.logger.warning(f"Saved microphone not found, using default device (ID: {default_id})")
                except Exception as e:
                    self.logger.error(f"Error setting saved microphone: {e}")
                    # Fallback to default
                    self.settings.set('selected_microphone', None)
                    default_id = get_default_device_id()
                    set_input_device(default_id)
                    self.logger.info(f"Using default microphone (ID: {default_id}) due to error")
            else:
                # No saved microphone, use default
                default_id = get_default_device_id()
                set_input_device(default_id)
                self.logger.info(f"No saved microphone, using default device (ID: {default_id})")
        except Exception as e:
            self.logger.error(f"Error setting saved microphone: {e}", exc_info=True)
            # Fallback to default
            self.settings.set('selected_microphone', None)
            default_id = get_default_device_id()
            set_input_device(default_id)
            self.logger.info(f"Using default microphone (ID: {default_id}) due to initialization error")

    def set_microphone(self, device_id: int) -> None:
        """Change the active microphone device"""
        try:
            # Get device info for proper identifier storage
            from modules.audio_manager import get_device_by_id, create_device_identifier
            device = get_device_by_id(device_id)
            if device:
                identifier = create_device_identifier(device)
                set_input_device(device_id)
                self.settings.set('selected_microphone', identifier._asdict())
                self.logger.info(f"Microphone changed to: {device['name']} (ID: {device_id}, Channels: {device['max_input_channels']}, Sample Rate: {device['default_samplerate']} Hz)")
            else:
                raise ValueError(f"Device with ID {device_id} not found")
            # Stop any ongoing recording when changing microphone
            if self.recording:
                self.handle_ui_click()
        except Exception as e:
            self.logger.error(f"Error setting microphone: {e}", exc_info=True)
            self.logger.debug(f"Failed device_id: {device_id}")
            self.ui_feedback.show_warning("⚠️ Error changing microphone")

    def refresh_microphones(self) -> None:
        """Refresh the microphone list and update the tray menu"""
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
        for queue in self._recent_queues:
            keep_paths.update(Path(p).resolve() for p in queue.active_paths())
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
        newest_snapshot = str(snapshots[0]) if snapshots else None
        # Always keep the newest snapshot, even when a bare temp_audio.wav
        # exists: the bare file may be a partial recording from a crash, and
        # the snapshot is the only good retry candidate if it turns out bad.
        # (The next completed recording sweeps it away.)
        self._sweep_snapshots(keep=newest_snapshot)
        if os.path.exists(self.recorder.filename):
            return self.recorder.filename
        return newest_snapshot

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
                    self._chunk_queue = self._make_chunk_queue(
                        phone=self.recorder.phone_mode)
                    self._session_active = True

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

    def _stop_recording(self) -> None:
        """Helper method to handle recording stop logic"""
        with self._toggle_lock:
            self.recording = False
            gen = self._recording_generation
            self.recorder.stop()
            self.logger.info("Recording stopped")

            # Detach the streaming session from app state; from here it either
            # travels with this recording's processing or gets aborted
            stream_session, self._streaming_session = self._streaming_session, None

            # If a new recording started while we were stopping, bail out entirely
            if gen != self._recording_generation:
                if stream_session is not None:
                    stream_session.abort()
                self.logger.info("Skipping processing — superseded by new recording")
                return

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

            # Snapshot path so a new recording can't overwrite the file mid-transcription
            recording_path = self.recorder.filename
            if os.path.exists(recording_path):
                snapshot_path = recording_path + f".{gen}.wav"
                try:
                    os.replace(recording_path, snapshot_path)
                    self.last_recording = snapshot_path
                except OSError:
                    self.last_recording = recording_path
            else:
                self.last_recording = recording_path
            # Older snapshots are no longer retry candidates; drop them
            self._sweep_snapshots(keep=self.last_recording)
            self.status_manager.set_status(AppStatus.PROCESSING)
            self.process_audio(stream_session)

    def _flush_chunk(self) -> None:
        """Seal the current chunk, queue it for transcription, resume recording.

        The gap between stop and restart is the quick-restart cost the session
        design accepts (~0.3s mic-only, up to ~1-2s in meeting mode where the
        loopback thread is rejoined and the 2-channel file composed)."""
        with self._toggle_lock:
            if not (self.recording and self._session_active):
                return
            self.recorder.stop()
            self._recording_generation += 1
            gen = self._recording_generation
            path = self.recorder.filename
            if os.path.exists(path):
                snapshot = path + f".{gen}.wav"
                try:
                    os.replace(path, snapshot)
                except OSError:
                    snapshot = None
                    self.logger.error("Could not snapshot chunk; skipping it", exc_info=True)
                if snapshot:
                    is_valid, reason = self.recorder.analyze_recording(snapshot)
                    if is_valid:
                        index = self._chunk_queue.submit(snapshot)
                        self.logger.info(f"Chunk {index} queued for transcription")
                    else:
                        # Quiet flush (nothing said since the last one): drop it
                        # without the error flash a failed dictation would get
                        self.logger.info(f"Skipping chunk: {reason}")
                        try:
                            os.remove(snapshot)
                        except OSError:
                            pass
            self.recorder.continuation_chunk = True
            self.recorder.start()

    def _end_session(self, auto_stopped: bool = False) -> None:
        """End the conversation session, discarding the unflushed tail.

        Audio since the last flush is dropped by design (press caps to flush
        before ending if you want it); chunks already queued keep delivering
        in order."""
        with self._toggle_lock:
            if not self._session_active:
                return
            self._session_active = False
            self.recording = False
            self.recorder.continuation_chunk = False
            try:
                self.recorder.stop()
            except Exception:
                self.logger.error("Error stopping recorder", exc_info=True)
            self.recorder.auto_stopped = False
            try:
                if os.path.exists(self.recorder.filename):
                    os.remove(self.recorder.filename)
            except OSError:
                self.logger.warning("Could not delete session tail", exc_info=True)
            self.ui_feedback.set_recording_note('')
            self._note_hold_until = 0.0
            queue = self._chunk_queue
            if auto_stopped:
                # First chunk never made a sound; nothing was queued
                if queue is not None:
                    queue.cancel()
                self.status_manager.set_status(
                    AppStatus.ERROR,
                    "⚠️ Recording stopped: No audio detected"
                )
                self.logger.warning("Session auto-stopped due to initial silence")
                return
            self.logger.info("Conversation session ended")
            if queue is not None:
                # PROCESSING first, then close(): if the queue is already empty
                # the drained callback immediately corrects this to IDLE/ERROR
                self.status_manager.set_status(AppStatus.PROCESSING)
                queue.close()
            else:
                self.status_manager.set_status(AppStatus.IDLE)

    def _make_chunk_queue(self, phone: bool) -> ChunkQueue:
        """Build the ordered delivery queue for a conversation session.

        Callbacks run on queue worker threads (outside the queue's state lock,
        serialized in delivery order), so they only touch thread-safe app
        surfaces and never call back into the queue (data arrives as
        arguments)."""
        queue_ref: list = []
        # Transcript-limitations note for the LLM reading the paste, sent once
        # per session ahead of whichever chunk is delivered first
        preamble_pending = [bool(self.settings.get('session_preamble'))]

        def build_preamble() -> str:
            if phone:
                you = self.settings.get('meeting_speaker_you') or 'Me'
                them = self.settings.get('meeting_speaker_them') or 'Them'
                if self.settings.get('phone_my_speaker_id'):
                    speakers = (f"'{you}:' lines are me (matched by voice) and "
                                f"'{them}:' is anyone else; in chunks with "
                                "unlabeled lines the voice match failed, so each "
                                "line is just one unattributed speaker turn")
                elif self.settings.get('phone_speaker_labels'):
                    speakers = ("Speaker turns are labeled 'Speaker N' per chunk, and "
                                "labels can swap identities between chunks")
                else:
                    speakers = ("Each line is one speaker turn, but turns are "
                                "unattributed — quietly infer who's speaking")
            else:
                you = self.settings.get('meeting_speaker_you') or 'Me'
                them = self.settings.get('meeting_speaker_them') or 'Them'
                speakers = (f"'{you}:' lines are me and '{them}:' is the other "
                            "side, though attribution can err on overlapping speech")
            return (
                "[Transcript note: a live conversation transcribed by "
                "voice-to-text, arriving in chunks as the call happens. "
                f"{speakers}. Proper nouns and abbreviations are often "
                "mistranscribed — quietly interpret them from context; you "
                "don't need to surface these corrections to me.]"
            )

        def is_current() -> bool:
            return queue_ref and queue_ref[0] is self._chunk_queue

        def on_result(index: int, text: str, path: str) -> None:
            prefix = ""
            if preamble_pending[0]:
                preamble_pending[0] = False
                prefix = build_preamble() + "\n\n"
            # Chunk headers mark discontinuities (mid-sentence cuts, and in
            # labeled phone transcripts, where speaker labels reset)
            header = f"--- [chunk {index}] ---\n" if phone else ""
            self.history.add(text)
            self.ui_feedback.insert_text(prefix + header + text + "\n",
                                         output_mode=self.settings.get('output_mode'))
            if self.update_icon_menu:
                self.update_icon_menu()
            self.logger.info(f"Chunk {index} delivered ({len(text)} chars)")
            try:
                os.remove(path)
            except OSError:
                pass

        def on_retrying(index: int) -> None:
            if self._session_active and is_current():
                self._set_session_note(f"⚠️ chunk {index} retrying…", hold_s=6.0)
            elif not self.recording:
                self.ui_feedback.show_warning(f"⚠️ Chunk {index} failed, retrying…", 3000)

        def on_failed(index: int, path: str) -> None:
            # Keep the file and point the retry machinery at it (tray "Retry
            # Last Transcription" copies the result to the clipboard) — unless
            # a newer dictation is mid-processing, whose own retry candidate
            # must not be clobbered
            if not (self.processing_thread and self.processing_thread.is_alive()):
                self.last_recording = path
            if self._session_active and is_current():
                self._set_session_note(f"⚠️ chunk {index} failed", hold_s=6.0)
            elif not self.recording:
                self.ui_feedback.show_warning(f"⚠️ Chunk {index} failed (retry from tray)", 5000)
            else:
                # A newer recording owns the indicator; the warning overlay
                # would hide it when it auto-dismisses, so just log
                self.logger.warning(f"Chunk {index} from an earlier session failed")

        def on_pending(count: int) -> None:
            if not (self._session_active and is_current()):
                return
            # A backlog of 1 is the normal state right after a flush; only
            # surface it once chunks start stacking up
            self._set_session_note(f"⏳ {count} queued" if count >= 2 else "")

        def on_drained(failed_paths: list) -> None:
            if not is_current() or self.recording:
                return
            if self.processing_thread and self.processing_thread.is_alive():
                # A normal dictation is mid-pipeline; it owns the status and
                # will set IDLE/ERROR itself when it finishes
                return
            if failed_paths:
                self.last_recording = failed_paths[-1]
                message = f"⚠️ {len(failed_paths)} chunk(s) failed"
                self.ui_feedback.show_error_with_retry(message)
                self.status_manager.set_status(AppStatus.ERROR, message)
            elif (self.status_manager.current_status == AppStatus.PROCESSING or
                  self.status_manager.current_status in RECORDING_STATUSES):
                # Clear our own post-session PROCESSING state — including the
                # case where the recording watchdog reasserted a stale
                # "Recording" status in the instant the session ended (with
                # self.recording False that status can only be stale). A newer
                # dictation's transcribing/cleaning status is left alone.
                self.status_manager.set_status(AppStatus.IDLE)

        queue = ChunkQueue(
            transcribe_fn=transcribe_audio,
            on_result=on_result,
            on_retrying=on_retrying,
            on_failed=on_failed,
            on_pending=on_pending,
            on_drained=on_drained,
        )
        queue_ref.append(queue)
        # Registry for sweep protection: prune queues that no longer hold any
        # files (drained with no kept failures) rather than capping by count,
        # so a slow-draining queue can't lose its files to a sweep
        self._recent_queues = [q for q in self._recent_queues if q.active_paths()]
        self._recent_queues.append(queue)
        return queue

    def _set_session_note(self, note: str, hold_s: float = 0.0) -> None:
        """Show a note in the recording label; hold_s protects it from being
        overwritten by routine pending-count updates for that long."""
        now = time.monotonic()
        if hold_s:
            self._note_hold_until = now + hold_s
        elif now < self._note_hold_until:
            return
        self.ui_feedback.set_recording_note(note)

    # Add this method to check recorder status periodically
    def _check_recorder_status(self, token: int) -> None:
        """Periodically check if recorder has auto-stopped and guard recording UI.

        Runs on the Tk thread as a 100ms after() chain. The token ties the
        chain to the recording that started it: a newer recording bumps the
        token, so a stale pending callback exits instead of spawning a second
        chain that could double-fire stop/flush actions."""
        if token != self._watchdog_token:
            return

        if self.recording and self.recorder.was_auto_stopped():
            if self._session_active:
                threading.Thread(target=self._end_session,
                                 kwargs={'auto_stopped': True}, daemon=True).start()
            else:
                threading.Thread(target=self._stop_recording, daemon=True).start()
            return

        if self.recording and self.recorder.max_duration_reached:
            if self._session_active:
                # Roll into a new chunk instead of ending the session
                self.recorder.max_duration_reached = False
                self.logger.warning("Max chunk duration reached; auto-flushing")
                threading.Thread(target=self._flush_chunk, daemon=True).start()
            else:
                threading.Thread(target=self._stop_recording, daemon=True).start()
                return

        if self.recording:
            # Self-heal: if a stale processing thread overwrote our status, reassert it
            if self.status_manager.current_status != self._active_recording_status:
                self.status_manager.set_status(self._active_recording_status)
            self.ui_feedback.root.after(100, lambda: self._check_recorder_status(token))

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
                if result == "timeout":
                    self.ui_feedback.show_error_with_retry("⏱️ Request timed out - try again")
                    self.status_manager.set_status(AppStatus.ERROR, "⏱️ Request timed out")
                else:
                    self.ui_feedback.show_error_with_retry("⚠️ Transcription failed")
                    self.status_manager.set_status(AppStatus.ERROR, "⚠️ Error processing audio")
            elif result:
                if self._is_stale(gen):
                    return
                self.history.add(result)
                output_mode = self.settings.get('output_mode')
                self.ui_feedback.insert_text(result, output_mode=output_mode)
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
            if 'timeout' in str(e).lower():
                self.ui_feedback.show_error_with_retry("⏱️ Request timed out - try again")
                self.status_manager.set_status(AppStatus.ERROR, "⏱️ Request timed out")
            else:
                self.ui_feedback.show_error_with_retry("⚠️ Transcription failed")
                self.status_manager.set_status(AppStatus.ERROR, "⚠️ Error processing audio")

    def _attempt_transcription(self, recording_path: Optional[str] = None,
                               streamed_text: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """Attempt transcription and return (success, result or error_type).

        Pass recording_path explicitly when the caller may run concurrently
        with new recordings (retry), since self.last_recording is mutable.
        If streamed_text is provided (realtime streaming already transcribed
        the recording), the batch upload is skipped but cleaning still runs."""
        try:
            path = recording_path or self.last_recording
            if not path:
                self.logger.error("Attempted transcription with no recording available.")
                return False, "no_recording"

            # Update status to show we're transcribing (skip if already cancelled,
            # so a cancel can't be overwritten by a stale pulsing status)
            if not self.cancel_flag.is_set():
                self.status_manager.set_status(AppStatus.TRANSCRIBING)
            text = streamed_text if streamed_text else transcribe_audio(path)

            if self.cancel_flag.is_set():
                return False, "cancelled"

            # Meeting/phone transcripts are speaker-labeled; LLM cleaning would
            # mangle the labels, so skip it for those recordings
            if self.clean_transcription_enabled and not is_conversation_recording(path):
                try:
                    # Update status to show we're cleaning
                    if not self.cancel_flag.is_set():
                        self.status_manager.set_status(AppStatus.CLEANING)

                    # Get the configured LLM model and timeout from settings
                    llm_model = self.settings.get('llm_model')
                    cleaning_timeout = self.settings.get('cleaning_timeout')

                    cleaned_text = clean_transcription(text, model=llm_model, timeout=cleaning_timeout)
                    self.logger.info("Transcription cleaned successfully")
                    return True, cleaned_text
                except Exception as e:
                    self.logger.warning(f"LLM cleaning failed, falling back to raw transcription. Error: {e}")
                    # Show a brief warning that we're using the fallback
                    self.ui_feedback.show_warning("⚠️ Using raw transcript (cleaning failed)", 2000)
                    return True, text  # Fallback to original text

            return True, text
        except Exception as e:
            # Check if it's a timeout exception
            if 'timeout' in str(e).lower():
                self.logger.error(f"Transcription timeout: Request took too long", exc_info=True)
                return False, "timeout"
            else:
                self.logger.error(f"Transcription error: {e}", exc_info=True)
                return False, None

    def retry_transcription(self) -> None:
        """Retry transcription of last failed recording"""
        # Capture the path now: self.last_recording can be cleared/replaced by
        # a new recording while the retry is in flight
        recording_path = self.last_recording
        if not recording_path:
            return

        def retry_thread():
            self.status_manager.set_status(AppStatus.PROCESSING)
            success, result = self._attempt_transcription(recording_path)

            if success and result:
                self.history.add(result)
                pyperclip.copy(result)  # Copy to clipboard instead of direct insertion
                self.status_manager.set_status(AppStatus.IDLE)
                self.ui_feedback.show_warning("✅ Transcription copied to clipboard", 3000)
                # Update the menu to reflect the new transcription in history
                if self.update_icon_menu:
                    self.update_icon_menu()
            else:
                self.ui_feedback.show_error_with_retry("⚠️ Retry failed")
                self.status_manager.set_status(AppStatus.ERROR)

        threading.Thread(target=retry_thread, daemon=True).start()

    def toggle_clean_transcription(self) -> None:
        self.clean_transcription_enabled = not self.clean_transcription_enabled
        self.settings.set('clean_transcription', self.clean_transcription_enabled)
        status = 'enabled' if self.clean_transcription_enabled else 'disabled'
        self.logger.info(f"Clean transcription {status}")

    def toggle_meeting_mode(self) -> None:
        """Toggle meeting mode (mic + system audio with speaker-labeled transcripts).

        Runs under the toggle lock so a caps press can't start a session in the
        gap between ending the current one and flipping the setting."""
        with self._toggle_lock:
            if self._session_active:
                self._end_session()
            enabling = not self.settings.get('meeting_mode')

            if enabling:
                if not os.environ.get('ELEVENLABS_API_KEY'):
                    self.ui_feedback.show_warning(
                        "⚠️ Meeting mode needs ELEVENLABS_API_KEY in .env", 5000)
                    self.logger.warning("Meeting mode not enabled: ELEVENLABS_API_KEY missing")
                    return
                # Meeting and phone mode are mutually exclusive capture strategies
                if self.settings.get('phone_mode'):
                    self.settings.set('phone_mode', False)
                    self.logger.info("Phone mode disabled (meeting mode enabled)")
                from modules.loopback_recorder import loopback_available
                available, detail = loopback_available()
                if available:
                    self.ui_feedback.show_warning(f"🎧 Meeting mode on ({detail})", 3000)
                else:
                    # Allow enabling anyway: capture falls back to mic-only per
                    # recording, and the output device may change before next use
                    self.ui_feedback.show_warning(
                        "⚠️ Meeting mode on, but system audio capture unavailable", 5000)
                    self.logger.warning(f"Loopback unavailable at toggle time: {detail}")
            else:
                self.ui_feedback.show_warning("🎧 Meeting mode off", 2000)

            self.settings.set('meeting_mode', enabling)
            self.logger.info(f"Meeting mode {'enabled' if enabling else 'disabled'}")
        if self.update_icon_menu:
            self.update_icon_menu()

    def toggle_phone_mode(self) -> None:
        """Toggle phone mode (mic-only conversation with diarized transcripts).

        For conversations happening in the room — a call on speakerphone, an
        in-person chat — where all voices reach the microphone. Speakers are
        separated by voice diarization instead of by channel.
        """
        with self._toggle_lock:
            if self._session_active:
                self._end_session()
            enabling = not self.settings.get('phone_mode')

            if enabling:
                if not os.environ.get('ELEVENLABS_API_KEY'):
                    self.ui_feedback.show_warning(
                        "⚠️ Phone mode needs ELEVENLABS_API_KEY in .env", 5000)
                    self.logger.warning("Phone mode not enabled: ELEVENLABS_API_KEY missing")
                    return
                # Meeting and phone mode are mutually exclusive capture strategies
                if self.settings.get('meeting_mode'):
                    self.settings.set('meeting_mode', False)
                    self.logger.info("Meeting mode disabled (phone mode enabled)")
                self.ui_feedback.show_warning("📞 Phone mode on (diarized transcripts)", 3000)
            else:
                self.ui_feedback.show_warning("📞 Phone mode off", 2000)

            self.settings.set('phone_mode', enabling)
            self.logger.info(f"Phone mode {'enabled' if enabling else 'disabled'}")
        if self.update_icon_menu:
            self.update_icon_menu()

    def toggle_streaming_dictation(self) -> None:
        """Toggle streaming dictation (beta): transcribe over a realtime
        websocket while recording, so text is ready ~immediately on stop.
        Applies to normal dictation only; falls back to batch on any failure."""
        enabling = not self.settings.get('streaming_dictation')

        if enabling:
            if not os.environ.get('OPENAI_API_KEY'):
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

    def run(self) -> None:
        # Start keyboard listener
        self.listener.start()

        # Start the UI feedback's tkinter mainloop in the main thread
        try:
            self.ui_feedback.root.mainloop()
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
            elif self._chunk_queue is not None:
                # Only when no dictation is processing is the visible activity
                # the session queue's post-end drain; cancelling the dictation
                # must not silently discard delivered-in-order session chunks
                self._chunk_queue.cancel()
            # The processing thread exits silently once it notices the flag;
            # reset the UI here so it can't be left stuck on a pulsing status
            self.status_manager.set_status(AppStatus.IDLE)

    def _cancel_recording(self) -> None:
        """Stop and discard the current recording, serialized against hotkey toggles."""
        with self._toggle_lock:
            self.recording = False
            if self._streaming_session is not None:
                self._streaming_session.abort()
                self._streaming_session = None
            try:
                self.recorder.stop()
            except Exception:
                self.logger.error("Error stopping recorder", exc_info=True)

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

    def toggle_favorite_microphone(self, device_id: int) -> None:
        """Toggle favorite status for a microphone device"""
        favorites = self.settings.get('favorite_microphones')
        if device_id in favorites:
            favorites.remove(device_id)
        else:
            favorites.append(device_id)
        self.settings.set('favorite_microphones', favorites)

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