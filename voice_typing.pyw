import os
import sys
import threading
import traceback
from typing import Any, Callable, Optional

from pynput import keyboard

from modules.clean_text import clean_transcription
from modules.history import TranscriptionHistory
from modules.recorder import AudioRecorder
from modules.settings import Settings
from modules.transcribe import transcribe_audio
from modules.tray import setup_tray_icon
from modules.ui import UIFeedback

class VoiceTypingApp:
    def __init__(self) -> None:
        # Hide console in Windows if running as .pyw
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.ShowWindow(
                ctypes.windll.kernel32.GetConsoleWindow(), 0)

        self.settings = Settings()
        self.ui_feedback = UIFeedback()
        self.recorder = AudioRecorder(level_callback=self.ui_feedback.update_audio_level)
        self.ui_feedback.set_click_callback(self.cancel_recording)
        self.recording = False
        self.ctrl_pressed = False
        self.clean_transcription_enabled = self.settings.get('clean_transcription')
        self.history = TranscriptionHistory()

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
                    # Toggle recording and suppress default Caps Lock behavior
                    self.toggle_recording()
                    self.listener.suppress_event()
                    return False

            return True

        self.listener = keyboard.Listener(
            win32_event_filter=win32_event_filter,
            suppress=False
        )

    def toggle_recording(self) -> None:
        if not self.recording:
            print("🎙️ Starting recording...")
            self.recording = True
            self.recorder.start()
            self.ui_feedback.start_listening_animation()
        else:
            self.recording = False
            self.recorder.stop()
            self.ui_feedback.stop_listening_animation()
            self.process_audio()

    def process_audio(self) -> None:
        try:
            # Run transcription in a separate thread to prevent UI blocking
            threading.Thread(target=self._process_audio_thread).start()
        except Exception as e:
            print(f"Error starting process_audio thread: {str(e)}")
            self.ui_feedback.insert_text(f"Error: {str(e)[:50]}...")

    def _process_audio_thread(self) -> None:
        try:
            print("✍️ Starting transcription...")
            text = transcribe_audio(self.recorder.filename)
            if self.clean_transcription_enabled:
                text = clean_transcription(text)
            self.history.add(text)
            self.ui_feedback.insert_text(text)
            self.update_icon_menu()
            print("Transcription completed and inserted")
        except Exception as e:
            print("Error in _process_audio_thread:")
            traceback.print_exc()
            self.ui_feedback.insert_text(f"Error: {str(e)[:50]}...")

    def toggle_clean_transcription(self) -> None:
        self.clean_transcription_enabled = not self.clean_transcription_enabled
        self.settings.set('clean_transcription', self.clean_transcription_enabled)
        print(f"Clean transcription {'enabled' if self.clean_transcription_enabled else 'disabled'}")

    def run(self) -> None:
        # Start keyboard listener
        self.listener.start()

        # Setup tray icon with self reference
        setup_tray_icon(self)

        # Start the UI feedback's tkinter mainloop in the main thread
        try:
            self.ui_feedback.root.mainloop()
        finally:
            self.cleanup()
            sys.exit(0)

    def cleanup(self) -> None:
        self.listener.stop()
        if self.recording:
            self.recorder.stop()
            self.ui_feedback.stop_listening_animation()

    def cancel_recording(self) -> None:
        """Cancel recording without attempting transcription"""
        if self.recording:
            print("Canceling recording...")
            self.recording = False
            # Start a separate thread for stopping the recorder
            threading.Thread(target=self._stop_recorder).start()
            self.ui_feedback.stop_listening_animation()

    def _stop_recorder(self) -> None:
        """Helper method to stop recorder in a separate thread"""
        try:
            self.recorder.stop()
        except Exception as e:
            print(f"Error stopping recorder: {e}")
            traceback.print_exc()

if __name__ == "__main__":
    app = VoiceTypingApp()
    app.run()