import logging
import queue
import threading
import time
import tkinter as tk
from typing import Optional, Callable, Any, Tuple

from pynput import keyboard
import pyautogui
import pyperclip

from modules.status_manager import StatusConfig
from modules.screen_utils import get_primary_monitor_geometry, get_all_monitor_geometries, MonitorGeometry
from modules.output_providers import get_output_provider

logger = logging.getLogger('voice_typing')

# Tkinter is not thread-safe, but this app updates the UI from the audio
# callback thread, the processing thread, the hotkey hook thread and the tray
# thread. All public UIFeedback methods therefore marshal their work onto the
# Tk main thread via a queue drained by a root.after() poller.
# 30ms keeps status changes and the audio level bar feeling immediate (~33fps).
UI_QUEUE_POLL_MS = 30

class UIFeedback:
    pyautogui_lock = threading.Lock()

    def __init__(self, position: str = 'top-right', size: str = 'normal', all_displays: bool = False):
        # Store desired position; fallback to default if invalid
        valid_positions = {'top-right', 'top-left', 'bottom-right', 'bottom-left', 'top-center', 'bottom-center'}
        self.position = position if position in valid_positions else 'top-right'

        # Store desired size; fallback to default if invalid
        valid_sizes = {'normal', 'mini'}
        self.size = size if size in valid_sizes else 'normal'

        # Store all_displays setting
        self.all_displays = all_displays

        # Configure dimensions based on size
        self._configure_size_attributes()

        # Create the root window
        self.root = tk.Tk()
        self.root.withdraw()

        # Lists to hold all indicator windows and their components
        self.indicators: list[tk.Toplevel] = []
        self.frames: list[tk.Frame] = []
        self.labels: list[tk.Label] = []
        self.level_canvases: list[tk.Canvas] = []
        self.level_bars: list[int] = []  # Canvas item IDs

        # Create indicator window(s) based on all_displays setting
        self._create_all_windows()

        # Backward compatibility: reference to first window's components
        self.indicator = self.indicators[0]
        self.frame = self.frames[0]
        self.label = self.labels[0]
        self.level_canvas = self.level_canvases[0]
        self.level_bar = self.level_bars[0]

        # Add pulsing state variables
        self.pulsing = False
        self.RECORDING_COLORS = ['red', 'darkred']
        self.pulse_colors = self.RECORDING_COLORS
        self.current_color = 0

        # Add click callback placeholder
        self.on_click_callback = None

        # Add retry callback placeholder
        self.on_retry_callback: Optional[Callable[[], None]] = None
        self.retry_available = False

        # Position windows initially
        self._position_window()

        # Add warning state variables
        self.warning_color = '#FFA500'  # Orange warning color
        self.warning_timer: Optional[str] = None

        # Update label text color to be more visible on warning background
        for label in self.labels:
            label.configure(fg='black')  # Will be dynamically changed based on state

        # Cross-thread marshalling (must be created on the main thread)
        self._main_thread = threading.current_thread()
        self._ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._pending_level: Optional[float] = None  # Latest audio level, applied coalesced by the poller
        self._pulse_after_id: Optional[str] = None
        self._snap_after_id: Optional[str] = None
        self._snap_passes = 0
        self._timer_after_id: Optional[str] = None
        self._recording_started: Optional[float] = None
        self._recording_base_text: str = ''
        self.root.after(UI_QUEUE_POLL_MS, self._process_ui_queue)

    def _process_ui_queue(self) -> None:
        """Drain marshalled UI work and apply the latest audio level. Runs on the main thread."""
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except tk.TclError:
                    pass
                except Exception:
                    logger.exception("Error running marshalled UI task")
        except queue.Empty:
            pass

        level = self._pending_level
        if level is not None:
            self._pending_level = None
            if self.pulsing:
                try:
                    for level_canvas, level_bar in zip(self.level_canvases, self.level_bars):
                        width = level_canvas.winfo_width()
                        bar_width = int(width * min(1.0, max(0.0, level)))
                        level_canvas.coords(level_bar, 0, 0, bar_width, self.level_height)
                except tk.TclError:
                    pass

        self.root.after(UI_QUEUE_POLL_MS, self._process_ui_queue)

    def _call_on_ui_thread(self, fn: Callable[[], None]) -> None:
        """Run fn now if on the main thread, otherwise queue it for the poller."""
        if threading.current_thread() is self._main_thread:
            fn()
        else:
            self._ui_queue.put(fn)

    def call_on_main(self, fn: Callable[[], None]) -> None:
        """Public: schedule fn to run on the Tk main thread (always queued)."""
        self._ui_queue.put(fn)

    def _create_indicator_window(self) -> Tuple[tk.Toplevel, tk.Frame, tk.Label, tk.Canvas, int]:
        """Creates a single indicator window with all its components."""
        indicator = tk.Toplevel(self.root)
        indicator.withdraw()

        # Configure the indicator window
        indicator.overrideredirect(True)  # Remove window decorations
        indicator.attributes('-topmost', True)  # Keep on top
        indicator.attributes('-alpha', 0.85)  # Make window semi-transparent
        indicator.configure(bg='red')

        # Create main frame
        frame = tk.Frame(indicator, bg='red', borderwidth=0, highlightthickness=0)
        frame.pack(fill='both', padx=self.frame_padding, pady=self.frame_padding)

        # Create label with click binding
        font_config = ('TkDefaultFont', self.font_size) if self.font_size else None
        label = tk.Label(frame, text=self.label_text,
                        fg='white', bg='red', padx=self.label_padx, pady=self.label_pady,
                        cursor="hand2", font=font_config)
        label.pack()

        # Create audio level indicator
        level_canvas = tk.Canvas(frame, width=1, height=self.level_height, bg='darkred',
                                highlightthickness=0, borderwidth=0)
        level_canvas.pack(fill='x', padx=self.level_padx, pady=self.level_pady)
        level_bar = level_canvas.create_rectangle(0, 0, 0, self.level_height,
                                                  fill='white', width=0)

        # Bind click events
        label.bind('<Button-1>', self._handle_click)
        indicator.bind('<Button-1>', self._handle_click)
        level_canvas.bind('<Button-1>', self._handle_click)

        return indicator, frame, label, level_canvas, level_bar

    def _create_all_windows(self) -> None:
        """Creates all indicator windows based on all_displays setting."""
        # Clear existing windows
        for indicator in self.indicators:
            indicator.destroy()
        self.indicators.clear()
        self.frames.clear()
        self.labels.clear()
        self.level_canvases.clear()
        self.level_bars.clear()

        if self.all_displays:
            monitors = get_all_monitor_geometries()
            # Create one window per monitor (at least one if enumeration fails)
            num_windows = max(1, len(monitors))
        else:
            num_windows = 1

        for _ in range(num_windows):
            indicator, frame, label, level_canvas, level_bar = self._create_indicator_window()
            self.indicators.append(indicator)
            self.frames.append(frame)
            self.labels.append(label)
            self.level_canvases.append(level_canvas)
            self.level_bars.append(level_bar)

        # Update backward compatibility references
        if self.indicators:
            self.indicator = self.indicators[0]
            self.frame = self.frames[0]
            self.label = self.labels[0]
            self.level_canvas = self.level_canvases[0]
            self.level_bar = self.level_bars[0]

    def _configure_size_attributes(self) -> None:
        """Sets UI dimension attributes based on self.size."""
        if self.size == 'mini':
            self.label_padx = 5
            self.label_pady = 3
            self.level_height = 3
            self.level_padx = 2
            self.level_pady = (0, 2)
            self.font_size = 9
            self.frame_padding = 0
            self.label_text = "🎤 Recording"
        else:  # normal
            self.label_padx = 10
            self.label_pady = 5
            self.level_height = 4
            self.level_padx = 4
            self.level_pady = (0, 4)
            self.font_size = None
            self.frame_padding = 0
            self.label_text = "🎤 Recording (click to cancel)"

    def _show_on_top(self) -> None:
        """Show all indicator windows and ensure they stay on top."""
        for indicator in self.indicators:
            indicator.deiconify()
            indicator.attributes('-topmost', True)
            indicator.lift()

    def _position_single_window(self, indicator: tk.Toplevel, monitor_geometry: Optional[MonitorGeometry]) -> None:
        """Positions a single indicator window on the given monitor."""
        indicator.update_idletasks()
        win_w = indicator.winfo_width()
        win_h = indicator.winfo_height()

        # Default coordinates if monitor info fails
        if monitor_geometry:
            mon_x = monitor_geometry.left
            mon_y = monitor_geometry.top
            mon_w = monitor_geometry.width
            mon_h = monitor_geometry.height
        else:
            mon_x = 0
            mon_y = 0
            mon_w = self.root.winfo_screenwidth()
            mon_h = self.root.winfo_screenheight()

        margin = 15
        taskbar_offset = 40  # Offset to clear the Windows taskbar

        # Compute x
        if 'right' in self.position:
            pos_x = mon_x + mon_w - win_w - margin
        elif 'left' in self.position:
            pos_x = mon_x + margin
        else:  # center
            pos_x = mon_x + (mon_w - win_w) // 2

        # Compute y
        if 'bottom' in self.position:
            pos_y = mon_y + mon_h - win_h - margin - taskbar_offset
        else:  # top
            pos_y = mon_y + margin

        indicator.geometry(f'+{pos_x}+{pos_y}')

    def _position_window(self) -> None:
        """Positions all indicator windows based on the configured corner."""
        if self.all_displays:
            monitors = get_all_monitor_geometries()
            # Check if monitor count changed
            if len(monitors) != len(self.indicators):
                self._create_all_windows()
            # Position each window on its respective monitor
            for i, indicator in enumerate(self.indicators):
                monitor = monitors[i] if i < len(monitors) else None
                self._position_single_window(indicator, monitor)
        else:
            # Single display mode - use primary monitor
            monitor_geometry = get_primary_monitor_geometry()
            self._position_single_window(self.indicators[0], monitor_geometry)

    # Public method to allow position change at runtime
    def set_position(self, position: str) -> None:
        """Update the indicator corner position and reposition it immediately. Thread-safe."""
        valid_positions = {'top-right', 'top-left', 'bottom-right', 'bottom-left', 'top-center', 'bottom-center'}
        if position in valid_positions:
            self.position = position
            self._call_on_ui_thread(self._position_window)

    def set_size(self, size: str) -> None:
        """Update the indicator size and reconfigure UI elements. Thread-safe."""
        self._call_on_ui_thread(lambda: self._set_size_impl(size))

    def _set_size_impl(self, size: str) -> None:
        valid_sizes = {'normal', 'mini'}
        if size in valid_sizes and self.size != size:
            self.size = size

            # Reconfigure dimensions based on new size
            self._configure_size_attributes()

            # Update UI elements with new dimensions on all windows
            font_config = ('TkDefaultFont', self.font_size) if self.font_size else None
            for i, (frame, label, level_canvas) in enumerate(zip(self.frames, self.labels, self.level_canvases)):
                label.configure(padx=self.label_padx, pady=self.label_pady, font=font_config)
                frame.configure(padx=self.frame_padding, pady=self.frame_padding)
                level_canvas.configure(height=self.level_height)
                level_canvas.pack_configure(padx=self.level_padx, pady=self.level_pady)

                # Update label text based on current status
                current_text = label.cget('text')
                if '🎤 Recording' in current_text:
                    label.configure(text=self.label_text)

            # Reposition windows with new size
            self._position_window()

    def set_all_displays(self, enabled: bool) -> None:
        """Enable or disable showing indicator on all displays. Thread-safe."""
        def impl() -> None:
            if self.all_displays != enabled:
                self.all_displays = enabled
                self._create_all_windows()
                self._position_window()
        self._call_on_ui_thread(impl)

    def update_audio_level(self, level: float) -> None:
        """Update the audio level indicator (level should be between 0.0 and 1.0).
        Thread-safe: called from the audio callback thread; only stores the value.
        The UI queue poller applies the latest level (coalesced to ~20fps)."""
        self._pending_level = level

    def _pulse(self) -> None:
        # At most one pulse chain exists: _pulse_after_id is set iff a tick is pending
        self._pulse_after_id = None
        if not self.pulsing:
            return
        self.current_color = (self.current_color + 1) % 2
        color = self.pulse_colors[self.current_color]
        try:
            for indicator, frame, label in zip(self.indicators, self.frames, self.labels):
                indicator.configure(bg=color)
                frame.configure(bg=color)
                label.configure(bg=color)
        except tk.TclError:
            return
        self._pulse_after_id = self.root.after(500, self._pulse)  # Pulse every 500ms

    def _start_pulse(self) -> None:
        """Mark pulsing active and start the pulse chain if one isn't already running."""
        self.pulsing = True
        if self._pulse_after_id is None:
            self._pulse()

    def start_listening_animation(self) -> None:
        """Start the recording animation on all windows"""
        # Cancel any existing warning state
        if self.warning_timer:
            self.root.after_cancel(self.warning_timer)
            self.warning_timer = None

        self.pulse_colors = self.RECORDING_COLORS
        for label, level_canvas in zip(self.labels, self.level_canvases):
            label.configure(
                text=self.label_text,
                fg='white'
            )
            level_canvas.pack(fill='x', padx=self.level_padx, pady=self.level_pady)
        self._position_window()
        self._show_on_top()
        self._start_pulse()
        self._schedule_snap()

    def stop_listening_animation(self) -> None:
        """Stop the recording animation on all windows"""
        self.pulsing = False
        # Only hide if no warning is active
        if not self.warning_timer:
            for indicator in self.indicators:
                indicator.withdraw()
        # Reset colors to recording state
        self.current_color = 0
        for indicator, frame, label, level_canvas, level_bar in zip(
            self.indicators, self.frames, self.labels, self.level_canvases, self.level_bars
        ):
            indicator.configure(bg=self.RECORDING_COLORS[0])
            frame.configure(bg=self.RECORDING_COLORS[0])
            label.configure(bg=self.RECORDING_COLORS[0])
            # Reset audio level
            level_canvas.coords(level_bar, 0, 0, 0, self.level_height)

    def _handle_click(self, event: tk.Event) -> None:
        if self.retry_available and self.on_retry_callback:
            self.retry_available = False
            self.on_retry_callback()
        elif self.on_click_callback:
            self.on_click_callback()

    def set_click_callback(self, callback: Callable[[], None]) -> None:
        """Set the function to be called when the indicator is clicked"""
        self.on_click_callback = callback

    def set_retry_callback(self, callback: Callable[[], None]) -> None:
        """Set the function to be called when retry is clicked"""
        self.on_retry_callback = callback

    def insert_text(self, text: str, output_mode: str = 'standard') -> None:
        """Insert text at the current cursor position using the configured output provider.
        Thread-safe: runs on the Tk main thread (providers use root.after and the clipboard)."""
        self._call_on_ui_thread(lambda: self._insert_text_impl(text, output_mode))

    def _insert_text_impl(self, text: str, output_mode: str) -> None:
        try:
            provider = get_output_provider(output_mode)
            provider.insert_text(text, self.pyautogui_lock, self.root.after)
        except Exception as e:
            logger.error(f"UIFeedback: Error during text insertion: {e}", exc_info=True)

    def show_warning(self, message: str, duration_ms: int = 5000) -> None:
        """Show a warning message in all indicators for a specified duration. Thread-safe."""
        self._call_on_ui_thread(lambda: self._show_warning_impl(message, duration_ms))

    def _show_warning_impl(self, message: str, duration_ms: int) -> None:
        # Cancel any existing warning timer
        if self.warning_timer:
            self.root.after_cancel(self.warning_timer)

        # Update appearance for warning state on all windows
        self._show_on_top()
        for indicator, frame, label, level_canvas in zip(
            self.indicators, self.frames, self.labels, self.level_canvases
        ):
            indicator.configure(bg=self.warning_color)
            frame.configure(bg=self.warning_color)
            label.configure(
                bg=self.warning_color,
                fg='black',  # Dark text for warning state
                text=message
            )
            # Hide the level indicator during warning
            level_canvas.pack_forget()

        self._position_window()
        self._schedule_snap()

        # Schedule auto-dismiss
        self.warning_timer = self.root.after(
            duration_ms,
            self._reset_and_hide
        )

    def show_error_with_retry(self, message: str, duration_ms: int = 7000) -> None:
        """Show error message with retry option on all windows. Thread-safe."""
        self._call_on_ui_thread(lambda: self._show_error_with_retry_impl(message, duration_ms))

    def _show_error_with_retry_impl(self, message: str, duration_ms: int) -> None:
        # Cancel any existing warning timer
        if self.warning_timer:
            self.root.after_cancel(self.warning_timer)

        self.retry_available = True

        # Update appearance for error state on all windows
        self._show_on_top()
        for indicator, frame, label, level_canvas in zip(
            self.indicators, self.frames, self.labels, self.level_canvases
        ):
            indicator.configure(bg=self.warning_color)
            frame.configure(bg=self.warning_color)
            label.configure(
                bg=self.warning_color,
                fg='black',
                text=f"{message}\n🔄 Click to retry"
            )
            # Hide the level indicator during warning
            level_canvas.pack_forget()

        self._position_window()
        self._schedule_snap()

        # Schedule auto-dismiss
        self.warning_timer = self.root.after(
            duration_ms,
            self._reset_and_hide
        )

    def _reset_and_hide(self) -> None:
        """Reset UI state and hide all indicators"""
        self.warning_timer = None
        self.retry_available = False
        for indicator, frame, label, level_canvas in zip(
            self.indicators, self.frames, self.labels, self.level_canvases
        ):
            level_canvas.pack(fill='x', padx=self.level_padx, pady=self.level_pady)  # Restore level indicator
            indicator.withdraw()
            # Reset to recording state colors
            indicator.configure(bg=self.RECORDING_COLORS[0])
            frame.configure(bg=self.RECORDING_COLORS[0])
            label.configure(
                bg=self.RECORDING_COLORS[0],
                fg='white'  # Reset to white text for recording state
            )

    def update_status(self, config: StatusConfig, error_message: Optional[str] = None) -> None:
        """Update UI appearance based on status configuration on all windows. Thread-safe."""
        self._call_on_ui_thread(lambda: self._update_status_impl(config, error_message))

    def _update_status_impl(self, config: StatusConfig, error_message: Optional[str] = None) -> None:
        # Update colors and text
        text = error_message if error_message else config.ui_text

        # Override text for recording status in mini mode
        if self.size == 'mini' and config.ui_text == "🎤 Recording (click to cancel)":
            text = "🎤 Recording"

        for indicator, frame, label in zip(self.indicators, self.frames, self.labels):
            indicator.configure(bg=config.ui_color)
            frame.configure(bg=config.ui_color)
            label.configure(
                bg=config.ui_color,
                fg=config.ui_fg_color,
                text=text
            )

        # Handle visibility and animation
        if config.pulse:
            if self.warning_timer:
                self.root.after_cancel(self.warning_timer)
                self.warning_timer = None
                # The dismissed warning had hidden the level bars; restore them
                for level_canvas in self.level_canvases:
                    level_canvas.pack(fill='x', padx=self.level_padx, pady=self.level_pady)
            self.retry_available = False
            self.pulse_colors = [config.ui_color, self._darken_color(config.ui_color)]
            self._show_on_top()
            self._start_pulse()
            self._schedule_snap()

            # Elapsed-time display while recording
            if config.ui_text.startswith("🎤 Recording"):
                self._recording_base_text = text
                if self._recording_started is None:
                    self._recording_started = time.monotonic()
                    self._start_recording_timer()
            else:
                self._recording_started = None
        else:
            self.pulsing = False
            self._recording_started = None
            if error_message:
                self._show_on_top()
                # Auto-hide after 5 seconds for errors
                if self.warning_timer:
                    self.root.after_cancel(self.warning_timer)
                self.warning_timer = self.root.after(5000, self._reset_and_hide)
            else:
                for indicator in self.indicators:
                    indicator.withdraw()

    def _start_recording_timer(self) -> None:
        """Start the once-per-second elapsed-time label update, if not already running."""
        if self._timer_after_id is None:
            self._timer_after_id = self.root.after(1000, self._tick_recording_timer)

    def _tick_recording_timer(self) -> None:
        self._timer_after_id = None
        if self._recording_started is None or not self.pulsing:
            return
        elapsed = int(time.monotonic() - self._recording_started)
        minutes, seconds = divmod(elapsed, 60)
        try:
            for label in self.labels:
                label.configure(text=f"{self._recording_base_text}  {minutes}:{seconds:02d}")
        except tk.TclError:
            return
        # Text width changes as the timer advances; re-fit the window once
        self._schedule_snap(passes=1)
        self._timer_after_id = self.root.after(1000, self._tick_recording_timer)

    def _darken_color(self, color: str) -> str:
        """Create a darker version of the given color for pulsing effect"""
        try:
            # Handle invalid or empty color values
            if not color or len(color) != 7 or not color.startswith('#'):
                return '#000000'  # Default to black if invalid color

            # Convert hex to RGB, darken, convert back to hex
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)

            factor = 0.7  # Darken by 30%
            r = int(r * factor)
            g = int(g * factor)
            b = int(b * factor)

            return f'#{r:02x}{g:02x}{b:02x}'
        except ValueError:
            logger.warning(f"Invalid color format: {color}")
            return '#000000'  # Fallback color

    def cleanup(self) -> None:
        """Ensure proper cleanup of UI resources for all windows"""
        if self.warning_timer:
            self.root.after_cancel(self.warning_timer)
        self.pulsing = False
        for indicator in self.indicators:
            indicator.withdraw()
        self.root.quit()

    def _schedule_snap(self, passes: int = 2) -> None:
        """Snap windows to their content now-ish, plus follow-up passes to catch
        layout that settles late. Event-driven (called when content changes)
        rather than a continuous loop: constantly re-measuring and re-setting
        geometry on visible windows caused perceptible UI stutter."""
        self._snap_passes = max(self._snap_passes, passes)
        if self._snap_after_id is None:
            self._snap_after_id = self.root.after(10, self._snap_to_content)

    def _snap_to_content(self) -> None:
        """
        Adjusts all window sizes to fit their content.
        Forces windows to "shrink-wrap" their contents by measuring the
        required space and resizing windows to match. This prevents "mysterious margins".
        """
        self._snap_after_id = None
        self._snap_passes = max(0, self._snap_passes - 1)
        try:
            for indicator in self.indicators:
                indicator.update_idletasks()
                w = indicator.winfo_reqwidth()
                h = indicator.winfo_reqheight()
                indicator.geometry(f"{w}x{h}")

            # Reposition after resizing to ensure correct placement
            self._position_window()

            # Follow-up pass, if any remain
            if self._snap_passes > 0:
                self._snap_after_id = self.root.after(100, self._snap_to_content)
        except tk.TclError:
            # This can happen if the window is destroyed while the after() call is pending
            pass


if __name__ == "__main__":
    import time

    class UITester:
        def __init__(self) -> None:
            print("Starting UI feedback test...")
            print("Press Caps Lock to toggle recording indicator")
            print("Press Ctrl+C to exit")

            self.ui = UIFeedback()
            self.recording = False
            self.listener = None

        def on_press(self, key: Any) -> None:
            if key == keyboard.Key.caps_lock:
                self.recording = not self.recording
                if self.recording:
                    print("Recording started")
                    self.ui.start_listening_animation()
                else:
                    print("Recording stopped")
                    self.ui.stop_listening_animation()

        def run(self) -> None:
            self.listener = keyboard.Listener(on_press=self.on_press)
            self.listener.start()

            try:
                self.ui.root.mainloop()
            except KeyboardInterrupt:
                self.cleanup()

        def cleanup(self) -> None:
            if self.listener:
                self.listener.stop()
            if self.recording:
                self.ui.stop_listening_animation()
            self.ui.root.destroy()
            print("\nTest ended")

    # Create and run the tester
    tester = UITester()
    tester.run()