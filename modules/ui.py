"""Recording indicator overlay(s) and the Qt main loop.

``UIFeedback`` owns the QApplication and one frameless, always-on-top
indicator pill per display. Qt widgets may only be touched from the main
thread, but this app reports from the audio callback thread, the processing
thread, the hotkey hook thread and provider threads — so every public method
here is thread-safe: work is run directly when already on the main thread and
otherwise posted to it through a queued signal.

The pill has a fixed width for the standard statuses (measured once from the
widest status text plus the elapsed-time counter) so it never resizes while
recording; only long notices (warnings, retry hints) can widen it.
"""
import ctypes
import logging
import threading
import time
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPaintEvent, QScreen
from PySide6.QtWidgets import QApplication, QWidget

from modules.paste import paste_text
from modules.settings import Settings
from modules.status_manager import StatusConfig

logger = logging.getLogger('voice_typing')

VALID_POSITIONS = frozenset({
    'top-right', 'top-left', 'bottom-right', 'bottom-left', 'top-center', 'bottom-center',
})
VALID_SIZES = frozenset({'normal', 'mini'})

# The level meter eases toward the latest audio level at this rate (the audio
# callback only stores the target), so the bar moves smoothly regardless of
# the capture block size
LEVEL_APPLY_MS = 16
LEVEL_ATTACK = 0.55   # fraction of the gap closed per tick when rising
LEVEL_RELEASE = 0.22  # ...and when falling
PULSE_MS = 500
SWEEP_MS = 33
NOTICE_COLOR = '#FFA500'
NOTICE_FG = '#000000'
# The pill is sized so this fits without resizing (mode texts are shorter)
_WIDEST_STATUS = "🎤 Recording 00:00"


def format_recording_label(base: str, note: str, elapsed_s: int) -> str:
    """Label text while recording: base text, optional note, elapsed time."""
    minutes, seconds = divmod(max(0, int(elapsed_s)), 60)
    middle = f"  {note}" if note else ""
    return f"{base}{middle}  {minutes}:{seconds:02d}"


def darken(color: str, factor: float = 0.72) -> str:
    """Darker shade of a '#rrggbb' color (used for pulse and level track)."""
    c = QColor(color)
    if not c.isValid():
        return '#000000'
    return QColor(int(c.red() * factor), int(c.green() * factor), int(c.blue() * factor)).name()


class _Bridge(QObject):
    """Queued signal carrying callables to the main thread."""
    invoke = Signal(object)


class IndicatorWindow(QWidget):
    """One rounded, translucent status pill. Pure view: all state is pushed
    in by UIFeedback; clicks are reported through ``on_click``."""

    def __init__(self, on_click: Callable[[], None]) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        # Never steal focus from the window the user is dictating into
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setWindowOpacity(0.92)
        self._on_click = on_click
        self.text = ''
        self.color = '#FF0000'
        self.fg = '#FFFFFF'
        self.level = 0.0           # 0..1 filled level bar
        self.show_level = True
        self.sweep: Optional[float] = None  # 0..1 phase of the indeterminate sweep
        self.radius = 8
        self.pad_x = 10
        self.pad_y = 4
        self.bar_h = 3
        self.min_width = 0

    def apply_metrics(self, font: QFont, radius: int, pad_x: int, pad_y: int, bar_h: int,
                      min_width: int) -> None:
        self.setFont(font)
        self.radius, self.pad_x, self.pad_y, self.bar_h = radius, pad_x, pad_y, bar_h
        self.min_width = min_width
        self.refit()

    def refit(self) -> None:
        fm = QFontMetrics(self.font())
        lines = self.text.split('\n') or ['']
        text_w = max(fm.horizontalAdvance(line) for line in lines)
        w = max(self.min_width, text_w + 2 * self.pad_x)
        h = fm.height() * len(lines) + 2 * self.pad_y + (self.bar_h + 3 if self.show_level else 0)
        self.setFixedSize(QSize(w, h))
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self.color))
        p.drawRoundedRect(QRectF(self.rect()), self.radius, self.radius)

        fm = QFontMetrics(self.font())
        p.setPen(QColor(self.fg))
        lines = self.text.split('\n')
        y = self.pad_y
        for line in lines:
            p.drawText(QRect(self.pad_x, y, self.width() - 2 * self.pad_x, fm.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)
            y += fm.height()

        if self.show_level:
            track = QRect(self.pad_x, self.height() - self.pad_y - self.bar_h,
                          self.width() - 2 * self.pad_x, self.bar_h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(darken(self.color)))
            p.drawRoundedRect(QRectF(track), self.bar_h / 2, self.bar_h / 2)
            fill = QColor(self.fg)
            fill.setAlphaF(0.9)
            p.setBrush(fill)
            if self.sweep is not None:
                seg_w = max(12, track.width() // 4)
                span = track.width() + seg_w
                x = track.left() - seg_w + int(span * self.sweep)
                seg = QRect(x, track.top(), seg_w, self.bar_h).intersected(track)
                if seg.width() > 0:
                    p.drawRoundedRect(QRectF(seg), self.bar_h / 2, self.bar_h / 2)
            elif self.level > 0:
                filled = QRect(track.left(), track.top(),
                               int(track.width() * min(1.0, max(0.0, self.level))), self.bar_h)
                p.drawRoundedRect(QRectF(filled), self.bar_h / 2, self.bar_h / 2)
        p.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click()

    def assert_topmost(self) -> None:
        """Re-issue SetWindowPos(HWND_TOPMOST): Windows quietly demotes
        topmost windows (another topmost window raised over it, fullscreen
        apps, UAC prompts, display changes) and Qt only sets the flag once."""
        if not self.isVisible():
            return
        try:
            hwnd = int(self.winId())
            HWND_TOPMOST = -1
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
            ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                              SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        except Exception:
            pass


class UIFeedback:
    def __init__(self, position: str = 'top-right', size: str = 'normal', all_displays: bool = False):
        self.app = QApplication.instance() or QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        self._main_thread = threading.current_thread()
        self._bridge = _Bridge()
        self._bridge.invoke.connect(self._run_marshalled, Qt.ConnectionType.QueuedConnection)

        self.position = position if position in VALID_POSITIONS else 'top-right'
        self.size = size if size in VALID_SIZES else 'normal'
        self.all_displays = all_displays

        self.indicators: List[IndicatorWindow] = []
        self.on_click_callback: Optional[Callable[[], None]] = None
        self.on_retry_callback: Optional[Callable[[], None]] = None
        self.retry_available = False

        # Live status (what the pill shows when no notice overrides it)
        self._status: Optional[StatusConfig] = None
        self._status_text = ''
        self._visible = False
        self.pulsing = False
        self._pulse_phase = 0
        self._sweep_phase = 0.0
        self._pending_level: Optional[float] = None
        self._level_target = 0.0
        self._level_shown = 0.0
        self._recording_started: Optional[float] = None
        self._recording_note = ''
        self._notice_active = False

        self._level_timer = QTimer()
        self._level_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._level_timer.setInterval(LEVEL_APPLY_MS)
        self._level_timer.timeout.connect(self._apply_level)
        self._pulse_timer = QTimer()
        self._pulse_timer.setInterval(PULSE_MS)
        self._pulse_timer.timeout.connect(self._pulse)
        self._sweep_timer = QTimer()
        self._sweep_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._sweep_timer.setInterval(SWEEP_MS)
        self._sweep_timer.timeout.connect(self._sweep)
        self._tick_timer = QTimer()
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._refresh_recording_label)
        self._notice_timer = QTimer()
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._end_notice)

        self._create_all_windows()
        self.app.screenAdded.connect(lambda _s: self._screens_changed())
        self.app.screenRemoved.connect(lambda _s: self._screens_changed())

    # ---- threading -------------------------------------------------------

    def _run_marshalled(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            logger.exception("Error running marshalled UI task")

    def _call_on_ui_thread(self, fn: Callable[[], None]) -> None:
        """Run fn now if on the main thread, otherwise post it there."""
        if threading.current_thread() is self._main_thread:
            self._run_marshalled(fn)
        else:
            self._bridge.invoke.emit(fn)

    def call_on_main(self, fn: Callable[[], None]) -> None:
        """Public: run fn on the main thread (always posted, never inline)."""
        self._bridge.invoke.emit(fn)

    def after(self, delay_ms: int, fn: Callable[[], None]) -> None:
        """Run fn on the main thread after delay_ms. Thread-safe."""
        self._call_on_ui_thread(lambda: QTimer.singleShot(int(delay_ms), fn))

    def run(self) -> None:
        """Enter the Qt main loop (blocks until quit)."""
        self.app.exec()

    def cleanup(self) -> None:
        self._call_on_ui_thread(self._cleanup_impl)

    def _cleanup_impl(self) -> None:
        for t in (self._level_timer, self._pulse_timer, self._sweep_timer,
                  self._tick_timer, self._notice_timer):
            t.stop()
        for w in self.indicators:
            w.hide()
        self.app.quit()

    # ---- windows ---------------------------------------------------------

    def _metrics(self) -> dict:
        if self.size == 'mini':
            font = QFont('Segoe UI', 9)
            return dict(font=font, radius=6, pad_x=7, pad_y=2, bar_h=2)
        font = QFont('Segoe UI', 10)
        return dict(font=font, radius=8, pad_x=10, pad_y=4, bar_h=3)

    def _apply_metrics(self, w: IndicatorWindow) -> None:
        m = self._metrics()
        min_width = QFontMetrics(m['font']).horizontalAdvance(_WIDEST_STATUS) + 2 * m['pad_x']
        w.apply_metrics(m['font'], m['radius'], m['pad_x'], m['pad_y'], m['bar_h'], min_width)

    def _target_screens(self) -> List[QScreen]:
        screens = self.app.screens()
        if self.all_displays and screens:
            return list(screens)
        primary = self.app.primaryScreen()
        return [primary] if primary else screens[:1]

    def _create_all_windows(self) -> None:
        for w in self.indicators:
            w.hide()
            w.deleteLater()
        self.indicators = []
        for _ in self._target_screens():
            w = IndicatorWindow(self._handle_click)
            self._apply_metrics(w)
            self.indicators.append(w)
        self._paint_all()
        self._position_windows()

    def _screens_changed(self) -> None:
        was_visible = self._visible
        self._create_all_windows()
        if was_visible:
            self._show_all()

    def _position_windows(self) -> None:
        margin = 12
        for w, screen in zip(self.indicators, self._target_screens()):
            area = screen.availableGeometry()  # excludes the taskbar
            size = w.size()
            if 'right' in self.position:
                x = area.right() - size.width() - margin
            elif 'left' in self.position:
                x = area.left() + margin
            else:
                x = area.left() + (area.width() - size.width()) // 2
            if 'bottom' in self.position:
                y = area.bottom() - size.height() - margin
            else:
                y = area.top() + margin
            w.move(QPoint(x, y))

    def _show_all(self) -> None:
        self._visible = True
        self._position_windows()
        for w in self.indicators:
            if not w.isVisible():
                w.show()
            w.assert_topmost()

    def _hide_all(self) -> None:
        self._visible = False
        for w in self.indicators:
            w.hide()

    def _paint_all(self) -> None:
        """Push the current model (status or notice) into every window."""
        for w in self.indicators:
            if self._notice_active:
                pass  # notice painted by _show_notice
            elif self._status is not None:
                w.color = self._pulse_color()
                w.fg = self._status.ui_fg_color
                w.text = self._status_text
                w.show_level = True
                w.sweep = self._sweep_phase if self._is_sweeping() else None
            w.refit()
        self._position_windows()

    def _is_sweeping(self) -> bool:
        return (self._status is not None and self._status.pulse
                and self._recording_started is None)

    def _pulse_color(self) -> str:
        if self._status is None:
            return '#FF0000'
        if self.pulsing and self._pulse_phase:
            return darken(self._status.ui_color, 0.8)
        return self._status.ui_color

    # ---- public settings --------------------------------------------------

    def set_position(self, position: str) -> None:
        if position in VALID_POSITIONS:
            self.position = position
            self._call_on_ui_thread(self._position_windows)

    def set_size(self, size: str) -> None:
        def impl() -> None:
            if size in VALID_SIZES and size != self.size:
                self.size = size
                for w in self.indicators:
                    self._apply_metrics(w)
                self._paint_all()
        self._call_on_ui_thread(impl)

    def set_all_displays(self, enabled: bool) -> None:
        def impl() -> None:
            if self.all_displays != enabled:
                self.all_displays = enabled
                self._screens_changed()
        self._call_on_ui_thread(impl)

    def set_click_callback(self, callback: Callable[[], None]) -> None:
        self.on_click_callback = callback

    def set_retry_callback(self, callback: Callable[[], None]) -> None:
        self.on_retry_callback = callback

    def _handle_click(self) -> None:
        if self.retry_available and self.on_retry_callback:
            self.retry_available = False
            self.on_retry_callback()
        elif self.on_click_callback:
            self.on_click_callback()

    # ---- audio level / animation -----------------------------------------

    def update_audio_level(self, level: float) -> None:
        """Thread-safe: called from the audio callback; only stores the value."""
        self._pending_level = level

    def _apply_level(self) -> None:
        if not self.pulsing or self._notice_active:
            return
        pending = self._pending_level
        if pending is not None:
            self._pending_level = None
            self._level_target = min(1.0, max(0.0, pending))
        gap = self._level_target - self._level_shown
        if abs(gap) < 0.003:
            return
        self._level_shown += gap * (LEVEL_ATTACK if gap > 0 else LEVEL_RELEASE)
        for w in self.indicators:
            w.level = self._level_shown
            w.update()

    def _pulse(self) -> None:
        if not self.pulsing:
            return
        self._pulse_phase ^= 1
        color = self._pulse_color()
        for w in self.indicators:
            if not self._notice_active:
                w.color = color
                w.update()
            # While anything is in progress keep re-asserting topmost: the
            # indicator sporadically ended up behind other windows
            w.assert_topmost()

    def _sweep(self) -> None:
        if not self._is_sweeping() or self._notice_active:
            return
        self._sweep_phase = (self._sweep_phase + 0.02) % 1.0
        for w in self.indicators:
            w.sweep = self._sweep_phase
            w.update()

    def _start_animation(self) -> None:
        self.pulsing = True
        if not self._pulse_timer.isActive():
            self._pulse_timer.start()
        if not self._level_timer.isActive():
            self._level_timer.start()
        if self._is_sweeping():
            if not self._sweep_timer.isActive():
                self._sweep_timer.start()
        else:
            self._sweep_timer.stop()

    def _stop_animation(self) -> None:
        self.pulsing = False
        self._pulse_phase = 0
        self._pulse_timer.stop()
        self._level_timer.stop()
        self._sweep_timer.stop()
        self._level_target = self._level_shown = 0.0
        self._pending_level = None
        for w in self.indicators:
            w.level = 0.0
            w.sweep = None

    # ---- status ------------------------------------------------------------

    def update_status(self, config: StatusConfig, error_message: Optional[str] = None) -> None:
        """Thread-safe."""
        self._call_on_ui_thread(lambda: self._update_status_impl(config, error_message))

    def _update_status_impl(self, config: StatusConfig, error_message: Optional[str]) -> None:
        self._status = config
        self._status_text = error_message or config.ui_text
        if config.pulse:
            recording = "Recording" in config.ui_text or config.ui_text.startswith(("🎤", "🎧", "📞"))
            if recording:
                if self._recording_started is None:
                    self._recording_started = time.monotonic()
                    self._recording_note = ''
                    self._tick_timer.start()
                self._status_text = format_recording_label(
                    config.ui_text, self._recording_note,
                    int(time.monotonic() - self._recording_started))
            else:
                self._recording_started = None
                self._tick_timer.stop()
            if self._notice_active:
                # A notice fired around a status change: drop it, the live
                # status is what the user needs to see now
                self._notice_timer.stop()
                self._notice_active = False
                self.retry_available = False
            self._start_animation()
            self._paint_all()
            self._show_all()
        else:
            self._recording_started = None
            self._tick_timer.stop()
            self._stop_animation()
            if error_message:
                self._paint_all()
                self._show_all()
                self._notice_timer.start(5000)
            elif not self._notice_active:
                self._hide_all()

    def _refresh_recording_label(self) -> None:
        if self._recording_started is None or self._status is None:
            self._tick_timer.stop()
            return
        self._status_text = format_recording_label(
            self._status.ui_text, self._recording_note,
            int(time.monotonic() - self._recording_started))
        if not self._notice_active:
            for w in self.indicators:
                w.text = self._status_text
                w.update()

    def set_recording_note(self, note: str) -> None:
        """Short note appended to the recording label (queued-chunk count, a
        chunk failure during a session); '' clears. Thread-safe. Warnings
        can't be used mid-recording: the ticker would repaint over them."""
        def impl() -> None:
            self._recording_note = note
            self._refresh_recording_label()
            for w in self.indicators:
                w.refit()
            self._position_windows()
        self._call_on_ui_thread(impl)

    # ---- notices -----------------------------------------------------------

    def show_warning(self, message: str, duration_ms: int = 5000) -> None:
        """Orange notice on every indicator for duration_ms. Thread-safe."""
        self._call_on_ui_thread(lambda: self._show_notice(message, duration_ms, retry=False))

    def show_error_with_retry(self, message: str, duration_ms: int = 7000) -> None:
        """Notice whose click retries the last recording until dismissed. Thread-safe."""
        self._call_on_ui_thread(lambda: self._show_notice(message, duration_ms, retry=True))

    def _show_notice(self, message: str, duration_ms: int, retry: bool) -> None:
        self._notice_timer.stop()
        self._notice_active = True
        self.retry_available = retry
        text = f"{message}\n🔄 Click to retry" if retry else message
        for w in self.indicators:
            w.color = NOTICE_COLOR
            w.fg = NOTICE_FG
            w.text = text
            w.show_level = False
            w.sweep = None
            w.refit()
        self._show_all()
        self._notice_timer.start(int(duration_ms))

    def _end_notice(self) -> None:
        self._notice_active = False
        self.retry_available = False
        if self.pulsing:
            # A live status (recording/processing) continues underneath
            self._paint_all()
            return
        self._hide_all()
        self._paint_all()

    # ---- text delivery ------------------------------------------------------

    def insert_text(self, text: str) -> None:
        """Paste text at the cursor. Thread-safe (the clipboard restore is
        scheduled on the main thread)."""
        self._call_on_ui_thread(lambda: self._insert_text_impl(text))

    def _insert_text_impl(self, text: str) -> None:
        try:
            paste_text(text, Settings().get('clipboard_restore_delay_ms'), self.after)
        except Exception as e:
            logger.error(f"UIFeedback: Error during text insertion: {e}", exc_info=True)
