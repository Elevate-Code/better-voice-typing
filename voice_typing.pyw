import os
import sys
import threading
import subprocess
import traceback
from typing import Any, Callable, Optional, Tuple
import logging
from datetime import datetime
from pathlib import Path
import httpx
import json

from pynput import keyboard
import pyperclip

from modules.clean_text import clean_transcription
from modules.history import TranscriptionHistory
from modules.recorder import AudioRecorder, DEFAULT_SILENT_START_TIMEOUT
from modules.settings import Settings
from modules.transcribe import transcribe_audio
from modules.tray import setup_tray_icon
from modules.ui import UIFeedback
from modules.audio_manager import set_input_device, get_default_device_id, DeviceIdentifier, find_device_by_identifier
from modules.status_manager import StatusManager, AppStatus
from modules.screen_utils import set_process_dpi_awareness, hide_console_window
from modules.logger import setup_logging

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
        self.ui_feedback = UIFeedback(position=ui_position, size=ui_size)
        self.recorder = AudioRecorder(
            level_callback=self.ui_feedback.update_audio_level,
            silent_start_timeout=silent_start_timeout
        )
        self.ui_feedback.set_click_callback(self.handle_ui_click)
        self.recording = False

        # Initialize last_recording from existing temp file if present (allows retry after restart)
        self.last_recording = self.recorder.filename if os.path.exists(self.recorder.filename) else None
        self.ctrl_pressed = False
        self.clean_transcription_enabled = self.settings.get('clean_transcription')
        self.history = TranscriptionHistory()

        # Add a flag for canceling processing
        self.processing_thread: Optional[threading.Thread] = None
        self.cancel_flag = threading.Event()

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
            # Key codes and messages
            VK_CONTROL = 0x11
            VK_LCONTROL = 0xA2
            VK_RCONTROL = 0xA3
            VK_CAPITAL = 0x14

            WM_KEYDOWN = 0x0100
            WM_KEYUP = 0x0101

            if data.vkCode in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL):
                if msg == WM_KEYDOWN:
                    self.ctrl_pressed = True
                elif msg == WM_KEYUP:
                    self.ctrl_pressed = False
                return True

            # Handle Caps Lock
            if data.vkCode == VK_CAPITAL and msg == WM_KEYDOWN:
                if self.ctrl_pressed:
                    # Allow normal Caps Lock behavior when Ctrl is pressed
                    return True
                else:
                    # Toggle recording and suppress default Caps Lock behavior. Returning False is not always sufficient
                    # to prevent the OS from toggling the Caps Lock state, so suppress_event() is used.
                    # TODO: watch this as it still seems to be a bit flaky
                    self.toggle_recording()
                    self.listener.suppress_event()
                    return False

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

    def toggle_recording(self) -> None:
        if not self.recording:
            self.logger.info("🎙️ Starting recording...")
            # Clear last recording when starting a new one
            self.last_recording = None
            self.recording = True
            self.recorder.start()
            self.status_manager.set_status(AppStatus.RECORDING)
            # Start periodic status checks
            self._check_recorder_status()
        else:
            self._stop_recording()

    def _stop_recording(self) -> None:
        """Helper method to handle recording stop logic"""
        self.recording = False
        self.recorder.stop()
        self.logger.info("Recording stopped via keyboard shortcut")

        if self.recorder.was_auto_stopped():
            self.status_manager.set_status(
                AppStatus.ERROR,
                "⚠️ Recording stopped: No audio detected"
            )
            self.logger.warning("Recording auto-stopped due to initial silence")
            # Clear the auto-stopped flag
            self.recorder.auto_stopped = False
        else:
            self.status_manager.set_status(AppStatus.PROCESSING)
            self.process_audio()

    # Add this method to check recorder status periodically
    def _check_recorder_status(self) -> None:
        """Periodically check if recorder has auto-stopped"""
        if self.recording and self.recorder.was_auto_stopped():
            self._stop_recording()

        if self.recording:
            # Schedule next check in 100ms
            self.ui_feedback.root.after(100, self._check_recorder_status)

    def process_audio(self) -> None:
        try:
            self.cancel_flag.clear()  # Reset flag before starting
            self.processing_thread = threading.Thread(target=self._process_audio_thread)
            self.processing_thread.start()
        except Exception as e:
            self.logger.error("Failed to start processing thread", exc_info=True)
            self.logger.debug(f"Thread state: {threading.current_thread().name}")
            self.ui_feedback.insert_text(f"Error: {str(e)[:50]}...")

    def _process_audio_thread(self) -> None:
        try:
            self.logger.info("Starting audio processing")
            is_valid, reason = self.recorder.analyze_recording()

            if self.cancel_flag.is_set():
                self.logger.info("Processing cancelled before transcription.")
                self.status_manager.set_status(AppStatus.IDLE)
                return

            if not is_valid:
                self.logger.warning(f"Skipping transcription: {reason}")
                self.status_manager.set_status(
                    AppStatus.ERROR,
                    "⛔ Skipped: " + ("too short" if "short" in reason.lower() else "mostly silence")
                )
                return

            # Store recording path for retry functionality
            self.last_recording = self.recorder.filename

            self.logger.info("Starting transcription")
            success, result = self._attempt_transcription()

            if self.cancel_flag.is_set():
                self.logger.info("Processing cancelled after transcription.")
                self.status_manager.set_status(AppStatus.IDLE)
                return

            if not success:
                # Check if it was a timeout error
                if result == "timeout":
                    self.ui_feedback.show_error_with_retry("⏱️ Request timed out - try again")
                    self.status_manager.set_status(AppStatus.ERROR, "⏱️ Request timed out")
                else:
                    self.ui_feedback.show_error_with_retry("⚠️ Transcription failed")
                    self.status_manager.set_status(AppStatus.ERROR, "⚠️ Error processing audio")
            elif result:
                self.history.add(result)
                self.ui_feedback.insert_text(result)
                if self.update_icon_menu:
                    self.update_icon_menu()
                self.status_manager.set_status(AppStatus.IDLE)
                # Log transcription result with preview
                preview_len = 50
                preview = result[:preview_len] + "..." if len(result) > preview_len else result
                self.logger.info(f"Transcription completed ({len(result)} chars): {preview}")

        except Exception as e:
            self.logger.error("Error in _process_audio_thread:", exc_info=True)
            # Check if it's a timeout exception
            if 'timeout' in str(e).lower():
                self.ui_feedback.show_error_with_retry("⏱️ Request timed out - try again")
                self.status_manager.set_status(AppStatus.ERROR, "⏱️ Request timed out")
            else:
                self.ui_feedback.show_error_with_retry("⚠️ Transcription failed")
                self.status_manager.set_status(AppStatus.ERROR, "⚠️ Error processing audio")

    def _attempt_transcription(self) -> Tuple[bool, Optional[str]]:
        """Attempt transcription and return (success, result or error_type)"""
        try:
            if not self.last_recording:
                self.logger.error("Attempted transcription with no recording available.")
                return False, "no_recording"

            # Update status to show we're transcribing
            self.status_manager.set_status(AppStatus.TRANSCRIBING)
            text = transcribe_audio(self.last_recording)

            if self.cancel_flag.is_set():
                return False, "cancelled"

            if self.clean_transcription_enabled:
                try:
                    # Update status to show we're cleaning
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
        if not self.last_recording:
            return

        def retry_thread():
            self.status_manager.set_status(AppStatus.PROCESSING)
            success, result = self._attempt_transcription()

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

        threading.Thread(target=retry_thread).start()

    def toggle_clean_transcription(self) -> None:
        self.clean_transcription_enabled = not self.clean_transcription_enabled
        self.settings.set('clean_transcription', self.clean_transcription_enabled)
        status = 'enabled' if self.clean_transcription_enabled else 'disabled'
        self.logger.info(f"Clean transcription {status}")

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
        if status == AppStatus.RECORDING:
            self.logger.info("Canceling recording...")
            self.recording = False
            threading.Thread(target=self._stop_recorder).start()
            self.status_manager.set_status(AppStatus.IDLE)
        elif status in (AppStatus.PROCESSING, AppStatus.TRANSCRIBING, AppStatus.CLEANING):
            self.logger.info("Canceling processing...")
            if self.processing_thread and self.processing_thread.is_alive():
                self.cancel_flag.set()
                # The processing thread will set the status to IDLE upon graceful exit

    def _stop_recorder(self) -> None:
        """Helper method to stop recorder in a separate thread"""
        try:
            self.recorder.stop()
        except Exception as e:
            self.logger.error("Error stopping recorder", exc_info=True)
            self.logger.debug(f"Recorder state: recording={self.recording}")

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
            subprocess.Popen([sys.executable] + sys.argv)

            # Exit current instance
            self.logger.info("New instance started. Exiting current instance.")
            # Ensure all logs are written before exiting
            logging.shutdown()
            os._exit(0)
        except Exception as e:
            self.logger.error(f"Failed to restart application: {e}", exc_info=True)
            self.status_manager.set_status(AppStatus.ERROR, "⚠️ Failed to restart")

if __name__ == "__main__":
    app = VoiceTypingApp()
    app.run()