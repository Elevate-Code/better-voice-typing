"""Main window: first-run setup, settings pages and transcription history.

One QMainWindow with a sidebar. On first run (``setup_completed`` unset) it
opens in setup mode: three pages (provider key, microphone test, try it)
with the sidebar hidden; finishing switches to the normal layout. Every
control writes straight to Settings and applies live through the app's
existing methods, so the tray menu and this window never disagree.

Runs on the Qt main thread only; the app calls ``show_main_window`` from
the tray (already main thread) or marshals through UIFeedback.
"""
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QFont, QIcon, QPainter, QShowEvent
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
                               QStackedWidget, QVBoxLayout, QWidget)

from modules import startup, transcribe
from modules.audio_manager import get_default_device_id, get_input_devices, names_match
from modules.env_file import KEY_NAMES, is_placeholder, read_env, update_env, validate_key
from modules.logger import get_log_dir
from modules.mic_test import MicMonitor
from modules.paths import APP_DIR, APP_NAME, app_version
from modules.settings import ENV_FILE, api_key_configured
from modules.sounds import play

logger = logging.getLogger('voice_typing')

STYLE = """
QMainWindow, QScrollArea, QWidget#page { background: palette(window); }
QListWidget#sidebar { background: palette(window); border: none; font-size: 14px; outline: 0; }
QListWidget#sidebar::item { padding: 9px 12px; border-radius: 8px; margin: 2px 8px; }
QListWidget#sidebar::item:selected { background: palette(midlight); color: palette(text); }
QFrame#card { background: palette(base); border: 1px solid palette(mid); border-radius: 10px; }
QLabel#h1 { font-size: 24px; font-weight: 600; }
QLabel#h2 { font-size: 15px; font-weight: 600; }
QLabel#muted { color: palette(placeholder-text); }
QLabel#ok { color: #1e8e3e; font-weight: 600; }
QLabel#bad { color: #c5221f; font-weight: 600; }
QPushButton#primary { background: palette(text); color: palette(base); border-radius: 8px;
                      padding: 9px 22px; font-weight: 600; }
QPushButton#primary:disabled { background: palette(mid); }
QLabel#kbd { border: 1px solid palette(mid); border-radius: 5px; padding: 1px 6px;
             background: palette(base); font-weight: 600; }
"""

LANGUAGES = [('English', 'en'), ('Auto-detect', None), ('German', 'de'), ('Spanish', 'es'),
             ('French', 'fr'), ('Italian', 'it'), ('Portuguese', 'pt'), ('Dutch', 'nl'),
             ('Russian', 'ru'), ('Japanese', 'ja'), ('Chinese', 'zh')]


# ---- small building blocks -------------------------------------------------

def h1(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName('h1')
    return lab


def muted(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName('muted')
    lab.setWordWrap(True)
    return lab


def kbd(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName('kbd')
    return lab


class Card(QFrame):
    """Rounded container of settings rows separated by thin lines."""

    def __init__(self, title: str = '') -> None:
        super().__init__()
        self.setObjectName('card')
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(18, 12, 18, 12)
        self._layout.setSpacing(0)
        if title:
            t = QLabel(title)
            t.setObjectName('h2')
            t.setContentsMargins(0, 4, 0, 10)
            self._layout.addWidget(t)
        self._rows = 0

    def add_row(self, label: str, description: str, control: Optional[QWidget]) -> QWidget:
        if self._rows:
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet('color: palette(mid);')
            self._layout.addWidget(line)
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 10, 0, 10)
        left = QVBoxLayout()
        left.setSpacing(2)
        left.addWidget(QLabel(label))
        if description:
            left.addWidget(muted(description))
        hl.addLayout(left, 1)
        if control is not None:
            hl.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        self._layout.addWidget(row)
        self._rows += 1
        return row

    def add_widget(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)
        self._rows += 1


class LevelMeter(QWidget):
    """Wispr-style row of bars that light up with the microphone level."""
    BARS = 14

    def __init__(self) -> None:
        super().__init__()
        self.level = 0.0
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_level(self, level: float) -> None:
        self.level = max(0.0, min(1.0, level))
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        on = QColor(self.palette().highlight().color())
        off = QColor(self.palette().mid().color())
        off.setAlphaF(0.7)
        w = self.width()
        bar_w = 10
        gap = (w - self.BARS * bar_w) / (self.BARS - 1) if self.BARS > 1 else 0
        lit = self.level * self.BARS
        for i in range(self.BARS):
            x = i * (bar_w + gap)
            strength = max(0.0, min(1.0, lit - i))
            color = QColor(on)
            if strength <= 0:
                color = off
            else:
                color.setAlphaF(0.35 + 0.65 * strength)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(x, 8, bar_w, self.height() - 16), 5, 5)
        p.end()


class _Worker(QObject):
    """Runs a callable on a thread and reports back on the main thread."""
    done = Signal(object)

    def run(self, fn: Callable[[], Any]) -> None:
        def go() -> None:
            try:
                self.done.emit(fn())
            except Exception as e:  # pragma: no cover - defensive
                self.done.emit(e)
        threading.Thread(target=go, daemon=True).start()


# ---- API keys panel (shared by setup and Dictation settings) ---------------

class KeysPanel(Card):
    """OpenAI and ElevenLabs key fields with live validation."""
    changed = Signal()

    PROVIDERS = [
        ('ELEVENLABS_API_KEY', 'elevenlabs', 'ElevenLabs API key',
         'Recommended: Scribe is the most accurate dictation, and Meeting and Phone modes need it.',
         'https://elevenlabs.io/app/settings/api-keys'),
        ('OPENAI_API_KEY', 'openai', 'OpenAI API key',
         'Alternative: gpt-4o-transcribe or Whisper. Also needed for Streaming Dictation (beta).',
         'https://platform.openai.com/api-keys'),
    ]

    def __init__(self) -> None:
        super().__init__('Speech-to-text provider keys')
        self._edits: Dict[str, QLineEdit] = {}
        self._status: Dict[str, QLabel] = {}
        self._workers: List[_Worker] = []
        for env_name, provider, label, desc, url in self.PROVIDERS:
            box = QWidget()
            vl = QVBoxLayout(box)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(4)
            hl = QHBoxLayout()
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText('paste key…')
            edit.setMinimumWidth(260)
            edit.editingFinished.connect(lambda n=env_name: self._save(n))
            show = QPushButton('Show')
            show.setCheckable(True)
            show.toggled.connect(lambda on, e=edit: e.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
            test = QPushButton('Test')
            test.clicked.connect(lambda _=False, n=env_name, p=provider: self._test(n, p))
            hl.addWidget(edit, 1)
            hl.addWidget(show)
            hl.addWidget(test)
            vl.addLayout(hl)
            status = QLabel('')
            status.setObjectName('muted')
            link = QLabel(f'<a href="{url}">Get a key</a>')
            link.setOpenExternalLinks(True)
            sl = QHBoxLayout()
            sl.addWidget(status, 1)
            sl.addWidget(link)
            vl.addLayout(sl)
            self._edits[env_name] = edit
            self._status[env_name] = status
            self.add_row(label, desc, None)
            self.add_widget(box)
        self.refresh()

    def refresh(self) -> None:
        values = read_env()
        for name, edit in self._edits.items():
            value = values.get(name) or os.environ.get(name) or ''
            edit.setText('' if is_placeholder(value) else value)
            self._status[name].setText('Configured' if api_key_configured(name) else 'Not set')
            self._status[name].setObjectName('muted')
            self._status[name].style().polish(self._status[name])

    def _save(self, name: str) -> None:
        value = self._edits[name].text().strip()
        current = os.environ.get(name, '')
        if value == current or (not value and is_placeholder(current)):
            return
        try:
            update_env({name: value})
            self._status[name].setText('Saved' if value else 'Cleared')
            logger.info(f"{name} {'updated' if value else 'cleared'} from the settings window")
        except Exception as e:
            self._status[name].setText(f'Could not save: {e}')
        self.changed.emit()

    def _test(self, name: str, provider: str) -> None:
        key = self._edits[name].text().strip()
        status = self._status[name]
        if not key:
            self._set_status(name, 'Paste a key first', ok=False)
            return
        status.setText('Testing…')
        worker = _Worker()
        self._workers.append(worker)

        def finished(result: Any) -> None:
            self._workers.remove(worker)
            if isinstance(result, tuple):
                ok, message = result
                if ok:
                    self._save(name)
                self._set_status(name, message, ok=ok)
            else:
                self._set_status(name, f'Test failed: {result}', ok=False)
        worker.done.connect(finished)
        worker.run(lambda: validate_key(provider, key))

    def _set_status(self, name: str, text: str, ok: bool) -> None:
        lab = self._status[name]
        lab.setText(('✓ ' if ok else '✗ ') + text)
        lab.setObjectName('ok' if ok else 'bad')
        lab.style().unpolish(lab)
        lab.style().polish(lab)

    def any_key_configured(self) -> bool:
        return any(api_key_configured(n) for n in ('OPENAI_API_KEY', 'ELEVENLABS_API_KEY'))


# ---- microphone picker + meter (shared by setup and General) ---------------

class MicPanel(QWidget):
    """Device combo, refresh, live meter and a plain-language verdict."""

    def __init__(self, app, with_meter: bool = True) -> None:
        super().__init__()
        self.app = app
        self.monitor: Optional[MicMonitor] = None
        self._devices: List[dict] = []
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        hl = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.setMinimumWidth(320)
        self.combo.currentIndexChanged.connect(self._chosen)
        refresh = QPushButton('Refresh')
        refresh.clicked.connect(self.reload)
        hl.addWidget(self.combo, 1)
        hl.addWidget(refresh)
        vl.addLayout(hl)
        self.meter: Optional[LevelMeter] = None
        self.verdict = QLabel('')
        self.verdict.setWordWrap(True)
        if with_meter:
            self.meter = LevelMeter()
            vl.addWidget(self.meter)
            vl.addWidget(self.verdict)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._loading = False

    def reload(self) -> None:
        self._loading = True
        self.combo.clear()
        try:
            self._devices = sorted(get_input_devices(), key=lambda d: d['name'].lower())
        except Exception as e:
            self._devices = []
            self.verdict.setText(f'Could not list microphones: {e}')
        try:
            default_id = get_default_device_id()
        except Exception:
            default_id = None
        selected = self.app.settings.get('selected_microphone')
        favorites = [f.get('name', '') for f in self.app.settings.get('favorite_microphones')
                     if isinstance(f, dict)]
        current = 0
        for i, d in enumerate(self._devices):
            tags = []
            if d['id'] == default_id:
                tags.append('Windows default')
            if any(names_match(f, d['name']) for f in favorites):
                tags.append('favorite')
            suffix = f"  ({', '.join(tags)})" if tags else ''
            self.combo.addItem(d['name'] + suffix, d['id'])
            if isinstance(selected, dict) and names_match(selected.get('name') or '', d['name']):
                current = i
        if self._devices:
            self.combo.setCurrentIndex(current)
        self._loading = False
        self._restart_monitor()

    def _chosen(self, index: int) -> None:
        if self._loading or index < 0:
            return
        device_id = self.combo.itemData(index)
        if device_id is not None:
            self.app.set_microphone(device_id)
        self._restart_monitor()

    def start(self) -> None:
        self.reload()
        if self.meter is not None:
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None

    def _restart_monitor(self) -> None:
        if self.meter is None:
            return
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None
        if self.app.recording:
            self.verdict.setText('Finish the current recording to test the microphone.')
            return
        index = self.combo.currentIndex()
        if index < 0:
            return
        self.monitor = MicMonitor(self.combo.itemData(index))
        self.monitor.start()
        self.verdict.setText('Say something — the bars should light up as you speak.')

    def _tick(self) -> None:
        if self.monitor is None or self.meter is None:
            return
        self.meter.set_level(self.monitor.level())
        verdict = self.monitor.verdict()
        if verdict == 'error':
            self._set_verdict(f'Could not open this microphone: {self.monitor.error}', 'bad')
        elif verdict == 'ok':
            self._set_verdict('Hearing you loud and clear.', 'ok')
        elif self.monitor.heard_speech:
            self._set_verdict('Heard you a moment ago — keep talking to check the level holds.', 'muted')
        elif verdict == 'quiet':
            self._set_verdict('Very quiet. Move closer, raise the input level in Windows sound '
                              'settings, or pick another microphone.', 'bad')
        else:
            self._set_verdict('Nothing yet. Speak normally; if the bars stay dark, check the mic '
                              'is not muted or choose another one. Wired and built-in mics are '
                              'more reliable than Bluetooth.', 'muted')

    def _set_verdict(self, text: str, kind: str) -> None:
        if self.verdict.text() == text:
            return
        self.verdict.setText(text)
        self.verdict.setObjectName(kind)
        self.verdict.style().unpolish(self.verdict)
        self.verdict.style().polish(self.verdict)


# ---- pages -------------------------------------------------------------------

class Page(QWidget):
    """Scrollable page with a title. Subclasses fill ``body`` and implement
    refresh() (called every time the page is shown)."""

    def __init__(self, title: str, subtitle: str = '') -> None:
        super().__init__()
        self.setObjectName('page')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner.setObjectName('page')
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(36, 28, 36, 28)
        self.body.setSpacing(14)
        self.body.addWidget(h1(title))
        if subtitle:
            self.body.addWidget(muted(subtitle))
        scroll.setWidget(inner)
        outer.addWidget(scroll)

    def finish(self) -> None:
        self.body.addStretch(1)

    def refresh(self) -> None:
        pass

    def leave(self) -> None:
        pass


def _bind_check(app, key: str, apply: Optional[Callable[[bool], None]] = None) -> QCheckBox:
    box = QCheckBox()
    box.setChecked(bool(app.settings.get(key)))

    def changed(state: int) -> None:
        value = bool(state)
        if apply is not None:
            apply(value)
        else:
            app.settings.set(key, value)
    box.stateChanged.connect(changed)
    return box


def _bind_text(app, key: str, width: int = 220, placeholder: str = '') -> QLineEdit:
    edit = QLineEdit(str(app.settings.get(key) or ''))
    edit.setMinimumWidth(width)
    edit.setPlaceholderText(placeholder)
    edit.editingFinished.connect(lambda: app.settings.set(key, edit.text().strip() or None))
    return edit


def _bind_spin(app, key: str, lo: float, hi: float, step: float = 1.0, decimals: int = 0,
               suffix: str = '', none_at_zero: bool = False, scale: float = 1.0) -> QWidget:
    spin = QDoubleSpinBox() if decimals else QSpinBox()
    if decimals:
        spin.setRange(lo, hi)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
    else:
        spin.setRange(int(lo), int(hi))
    spin.setSuffix(suffix)
    value = app.settings.get(key)
    shown = 0 if value is None else float(value) / scale
    spin.setValue(shown if decimals else int(shown))
    if none_at_zero:
        spin.setSpecialValueText('Off')

    def changed(v: float) -> None:
        app.settings.set(key, None if (none_at_zero and v == 0) else (v * scale if decimals else int(v * scale)))
    spin.valueChanged.connect(changed)
    return spin


def _combo(options: List[Tuple[str, Any]], current: Any, on_change: Callable[[Any], None]) -> QComboBox:
    combo = QComboBox()
    for label, value in options:
        combo.addItem(label, value)
    index = next((i for i, (_, v) in enumerate(options) if v == current), 0)
    combo.setCurrentIndex(index)
    combo.currentIndexChanged.connect(lambda i: on_change(combo.itemData(i)))
    return combo


class HomePage(Page):
    def __init__(self, app, window: 'MainWindow') -> None:
        super().__init__(f'Press Caps Lock to dictate anywhere',
                         'Text lands wherever your cursor is. Press Caps Lock again to stop.')
        self.app, self.window = app, window
        card = Card('Status')
        self.provider = QLabel()
        self.mic = QLabel()
        card.add_row('Transcribing with', '', self.provider)
        card.add_row('Microphone', '', self.mic)
        self.body.addWidget(card)

        card = Card('Controls')
        card.add_row('Start / stop dictation', 'Press Caps Lock. Ctrl + Caps Lock toggles real Caps Lock.', kbd('Caps Lock'))
        card.add_row('Cancel a recording', 'Click the recording indicator.', None)
        card.add_row('Meeting and Phone modes', 'Capture a conversation with speaker-separated transcripts. '
                     'Caps Lock sends a chunk while recording continues; click the indicator to end.', None)
        self.body.addWidget(card)

        row = QHBoxLayout()
        setup = QPushButton('Run setup again')
        setup.clicked.connect(window.start_setup)
        row.addWidget(setup)
        row.addStretch(1)
        self.body.addLayout(row)
        self.finish()

    def refresh(self) -> None:
        try:
            name = transcribe.get_current_provider()
            display = next((p['display_name'] for p in transcribe.get_available_providers()
                            if p['name'] == name), name)
            if name in ('openai', 'elevenlabs') and not api_key_configured(
                    'OPENAI_API_KEY' if name == 'openai' else 'ELEVENLABS_API_KEY'):
                display += '  (no key configured — see Dictation)'
        except Exception as e:
            display = f'unavailable ({e})'
        self.provider.setText(display)
        mic = self.app.settings.get('selected_microphone')
        self.mic.setText(mic.get('name', '') if isinstance(mic, dict) else 'Windows default')


class HistoryPage(Page):
    def __init__(self, app) -> None:
        super().__init__('History', 'Your last transcriptions, newest first. Stored locally in Documents\\VoiceTyping.')
        self.app = app
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setSpacing(2)
        self.body.addWidget(self.list, 1)
        row = QHBoxLayout()
        copy = QPushButton('Copy selected')
        copy.clicked.connect(self._copy)
        self.retry = QPushButton('Retry last recording')
        self.retry.clicked.connect(app.retry_transcription)
        row.addWidget(copy)
        row.addWidget(self.retry)
        row.addStretch(1)
        self.body.addLayout(row)

    def refresh(self) -> None:
        self.list.clear()
        for entry in self.app.history.entries():
            stamp = entry.get('timestamp', '').replace('T', '  ')
            item = QListWidgetItem(f"{stamp}\n{entry.get('text', '')}")
            item.setData(Qt.ItemDataRole.UserRole, entry.get('text', ''))
            self.list.addItem(item)
        if not self.list.count():
            self.list.addItem('No transcriptions yet.')
        self.retry.setEnabled(self.app.last_recording is not None)

    def _copy(self) -> None:
        item = self.list.currentItem()
        if item and item.data(Qt.ItemDataRole.UserRole):
            QApplication.clipboard().setText(item.data(Qt.ItemDataRole.UserRole))


class GeneralPage(Page):
    def __init__(self, app) -> None:
        super().__init__('General')
        self.app = app
        card = Card('Microphone')
        self.mic = MicPanel(app, with_meter=True)
        card.add_widget(self.mic)
        self.body.addWidget(card)

        card = Card('App')
        if startup.available():
            box = QCheckBox()
            box.setChecked(startup.is_enabled())
            box.stateChanged.connect(lambda s: startup.set_enabled(bool(s)))
            card.add_row('Start when I sign in to Windows', '', box)
        sounds = _bind_check(app, 'sounds_enabled')
        sounds.stateChanged.connect(lambda s: play('start') if s else None)
        card.add_row('Sounds', 'A soft cue when a recording starts and stops.', sounds)
        card.add_row('Language', 'Spoken language, for the transcription models.',
                     _combo(LANGUAGES, app.settings.get('stt_language'),
                            lambda v: app.settings.set('stt_language', v)))
        self.body.addWidget(card)
        self.finish()

    def refresh(self) -> None:
        self.mic.start()

    def leave(self) -> None:
        self.mic.stop()


class DictationPage(Page):
    def __init__(self, app, window: 'MainWindow') -> None:
        super().__init__('Dictation')
        self.app = app
        s = app.settings
        card = Card('Provider')
        self.provider = QComboBox()
        self.provider.setMinimumWidth(240)
        self.provider.currentIndexChanged.connect(self._provider_chosen)
        card.add_row('Speech-to-text provider', 'Automatic picks ElevenLabs when its key is set, else OpenAI.', self.provider)
        self.model = _combo([('GPT-4o (best)', 'gpt-4o-transcribe'), ('GPT-4o mini', 'gpt-4o-mini-transcribe'),
                             ('Whisper (legacy)', 'whisper-1')], s.get('openai_stt_model'),
                            lambda v: s.set('openai_stt_model', v))
        card.add_row('OpenAI model', '', self.model)
        card.add_row('Custom server URL', 'Any OpenAI-compatible transcription endpoint (local Whisper, Parakeet…).',
                     _bind_text(app, 'custom_stt_base_url', 260, 'http://localhost:8000'))
        card.add_row('Custom server model', '', _bind_text(app, 'custom_stt_model', 260))
        self.body.addWidget(card)

        self.keys = KeysPanel()
        self.keys.changed.connect(self._reload_providers)
        self.body.addWidget(self.keys)

        card = Card('Behaviour')
        card.add_row('Streaming dictation (beta)', 'Transcribe over an OpenAI Realtime connection while you speak, '
                     'so text is ready the moment you stop. Falls back to a normal upload on any problem.',
                     _bind_check(app, 'streaming_dictation', lambda v: self._toggle_via(app.toggle_streaming_dictation, 'streaming_dictation', v)))
        card.add_row('Silent-start timeout', 'Stop automatically if nothing is heard at the start of a recording.',
                     _bind_spin(app, 'silent_start_timeout', 0, 60, 0.5, 1, ' s', none_at_zero=True))
        card.add_row('Maximum recording length', 'Stop and transcribe automatically after this long.',
                     _bind_spin(app, 'max_recording_duration', 0, 180, 1, 1, ' min', none_at_zero=True, scale=60))
        self.body.addWidget(card)

        card = Card('Cleanup')
        card.add_row('Clean up transcripts with an LLM', 'Fix punctuation and filler words with an OpenAI chat model '
                     'after transcription. Uses LLM_API_KEY or OPENAI_API_KEY.',
                     _bind_check(app, 'clean_transcription', lambda v: self._toggle_via(app.toggle_clean_transcription, 'clean_transcription', v)))
        card.add_row('Model', '', _bind_text(app, 'llm_model', 200, 'gpt-4o-mini'))
        card.add_row('Server URL', 'Leave empty for api.openai.com.', _bind_text(app, 'llm_base_url', 260, 'optional'))
        card.add_row('Timeout', '', _bind_spin(app, 'cleaning_timeout', 1, 120, 1, 1, ' s'))
        self.body.addWidget(card)
        self.finish()
        self._loading = False

    def _toggle_via(self, toggle: Callable[[], None], key: str, wanted: bool) -> None:
        if bool(self.app.settings.get(key)) != wanted:
            toggle()

    def _reload_providers(self) -> None:
        self._loading = True
        self.provider.clear()
        stored = self.app.settings.get('stt_provider')
        current = transcribe.get_current_provider()
        available = transcribe.get_available_providers()
        resolved = next((p['display_name'] for p in available if p['name'] == current), current)
        self.provider.addItem(f'Automatic ({resolved})', None)
        for p in available:
            self.provider.addItem(p['display_name'], p['name'])
        index = next((i for i in range(self.provider.count()) if self.provider.itemData(i) == stored), 0)
        self.provider.setCurrentIndex(index)
        self.model.setEnabled(current == 'openai')
        self._loading = False

    def _provider_chosen(self, index: int) -> None:
        if self._loading or index < 0:
            return
        name = self.provider.itemData(index)
        try:
            if name is None:
                self.app.settings.set('stt_provider', None)
            else:
                transcribe.set_stt_provider(name)
        except Exception as e:
            self.app.ui_feedback.show_warning(f'⚠️ Could not switch provider: {str(e)[:60]}', 5000)
        self._reload_providers()

    def refresh(self) -> None:
        self.keys.refresh()
        self._reload_providers()


class ConversationPage(Page):
    def __init__(self, app) -> None:
        super().__init__('Conversation modes', 'Meeting mode records your mic and system audio on separate channels; '
                         'Phone mode records the room and separates voices. Both need an ElevenLabs key.')
        self.app = app
        s = app.settings
        card = Card('Mode')
        self.meeting = _bind_check(app, 'meeting_mode', lambda v: self._set_mode('meeting_mode', v))
        self.phone = _bind_check(app, 'phone_mode', lambda v: self._set_mode('phone_mode', v))
        card.add_row('🎧 Meeting mode', 'Two-channel capture: you on one channel, the call on the other.', self.meeting)
        card.add_row('📞 Phone mode', 'Mic-only capture of an in-room conversation, diarized by voice.', self.phone)
        card.add_row('Transcript preamble', 'Prepend a short note for the LLM reading the transcript to the first chunk.',
                     _bind_check(app, 'session_preamble'))
        self.body.addWidget(card)

        card = Card('Speaker labels')
        card.add_row('Your label', '', _bind_text(app, 'meeting_speaker_you', 160, 'Me'))
        card.add_row('Their label', '', _bind_text(app, 'meeting_speaker_them', 160, 'Them'))
        self.body.addWidget(card)

        card = Card('Phone mode')
        card.add_row('Expected speakers', 'Hint for diarization (0 = let Scribe decide).',
                     _bind_spin(app, 'phone_num_speakers', 0, 10, none_at_zero=True))
        card.add_row('Diarization threshold', 'Replaces the speaker hint when set (0 = off). 0.3 worked best in testing.',
                     _bind_spin(app, 'phone_diarization_threshold', 0, 0.4, 0.05, 2, none_at_zero=True))
        card.add_row('Label speakers per turn', 'Off by default: "Speaker N" identities can swap between chunks.',
                     _bind_check(app, 'phone_speaker_labels'))
        card.add_row('Match against speaker library', 'Use the ElevenLabs workspace speaker library to recognise voices.',
                     _bind_check(app, 'use_speaker_library'))
        card.add_row('Your speaker ID', 'Your registered ID in that library, so your turns get your label.',
                     _bind_text(app, 'phone_my_speaker_id', 200, 'optional'))
        self.body.addWidget(card)
        self.finish()

    def _set_mode(self, key: str, wanted: bool) -> None:
        if bool(self.app.settings.get(key)) != wanted:
            (self.app.toggle_meeting_mode if key == 'meeting_mode' else self.app.toggle_phone_mode)()
        self.refresh()

    def refresh(self) -> None:
        for box, key in ((self.meeting, 'meeting_mode'), (self.phone, 'phone_mode')):
            box.blockSignals(True)
            box.setChecked(bool(self.app.settings.get(key)))
            box.blockSignals(False)


class IndicatorPage(Page):
    def __init__(self, app) -> None:
        super().__init__('Recording indicator', 'The pill that shows while recording and transcribing.')
        self.app = app
        s = app.settings
        ui = app.ui_feedback
        card = Card()
        positions = [('Top left', 'top-left'), ('Top center', 'top-center'), ('Top right', 'top-right'),
                     ('Bottom left', 'bottom-left'), ('Bottom center', 'bottom-center'), ('Bottom right', 'bottom-right')]

        def set_position(v: str) -> None:
            s.set('ui_indicator_position', v)
            ui.set_position(v)
            self._preview()

        def set_size(v: str) -> None:
            s.set('ui_indicator_size', v)
            ui.set_size(v)
            self._preview()

        def set_all(v: bool) -> None:
            s.set('ui_indicator_all_displays', v)
            ui.set_all_displays(v)
            self._preview()
        card.add_row('Position', '', _combo(positions, s.get('ui_indicator_position'), set_position))
        card.add_row('Size', '', _combo([('Normal', 'normal'), ('Mini', 'mini')], s.get('ui_indicator_size'), set_size))
        card.add_row('Show on all displays', '', _bind_check(app, 'ui_indicator_all_displays', set_all))
        preview = QPushButton('Preview')
        preview.clicked.connect(self._preview)
        card.add_row('Preview', 'Flash the indicator where it will appear.', preview)
        self.body.addWidget(card)
        self.finish()

    def _preview(self) -> None:
        if not self.app.recording:
            self.app.ui_feedback.show_warning('🎤 Recording indicator preview', 2500)


class AdvancedPage(Page):
    def __init__(self, app, window: 'MainWindow') -> None:
        super().__init__('Advanced')
        self.app = app
        card = Card('Files')
        for label, desc, fn in (
                ('Settings file', 'settings.json in Documents\\VoiceTyping', lambda: os.startfile(app.settings.settings_file)),
                ('API keys file', str(ENV_FILE), lambda: os.startfile(str(ENV_FILE))),
                ('Logs folder', '', lambda: os.startfile(str(get_log_dir())))):
            btn = QPushButton('Open')
            btn.clicked.connect(lambda _=False, f=fn: f())
            card.add_row(label, desc, btn)
        self.body.addWidget(card)

        card = Card('Logging and output')
        card.add_row('Include transcript text in logs', '', _bind_check(app, 'log_transcript_text'))
        card.add_row('Keep logs for', '', _bind_spin(app, 'log_retention_days', 1, 365, suffix=' days'))
        card.add_row('Clipboard restore delay', 'How long after pasting to put your previous clipboard back.',
                     _bind_spin(app, 'clipboard_restore_delay_ms', 0, 5000, suffix=' ms'))
        self.body.addWidget(card)

        card = Card('About')
        upd = QPushButton('Check for updates')
        upd.clicked.connect(app.check_for_updates)
        card.add_row(f'{APP_NAME} v{app_version()}', 'Updates are checked once a day.', upd)
        link = QLabel('<a href="https://github.com/Elevate-Code/better-voice-typing/issues">Report a problem or suggest something</a>')
        link.setOpenExternalLinks(True)
        card.add_row('Feedback', '', link)
        restart = QPushButton('Restart app')
        restart.clicked.connect(app.restart_app)
        card.add_row('Restart', '', restart)
        self.body.addWidget(card)
        self.finish()


# ---- setup wizard --------------------------------------------------------------

class SetupWizard(QWidget):
    """Three pages: provider key → microphone → try it. Finishing records
    the app version in ``setup_completed``."""

    def __init__(self, app, window: 'MainWindow') -> None:
        super().__init__()
        self.app, self.window = app, window
        self.setObjectName('page')
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        self.steps = QLabel()
        self.steps.setObjectName('muted')
        self.steps.setContentsMargins(36, 18, 36, 0)
        vl.addWidget(self.steps)
        self.stack = QStackedWidget()
        vl.addWidget(self.stack, 1)
        nav = QHBoxLayout()
        nav.setContentsMargins(36, 0, 36, 24)
        self.back = QPushButton('Back')
        self.back.clicked.connect(lambda: self.go(self.stack.currentIndex() - 1))
        self.skip = QPushButton('Skip setup')
        self.skip.clicked.connect(self.finish)
        self.next = QPushButton('Continue')
        self.next.setObjectName('primary')
        self.next.clicked.connect(self._next)
        nav.addWidget(self.back)
        nav.addWidget(self.skip)
        nav.addStretch(1)
        nav.addWidget(self.next)
        vl.addLayout(nav)

        # Page 1: keys
        p1 = Page('Connect a speech-to-text provider',
                  f'{APP_NAME} sends your audio to a provider you choose and pay directly. '
                  'Paste one key (or both) and press Test.')
        self.keys = KeysPanel()
        p1.body.addWidget(self.keys)
        p1.body.addWidget(muted('Prefer a local or self-hosted server? Skip this and set it up later '
                                'under Settings › Dictation › Custom server.'))
        p1.finish()
        self.stack.addWidget(p1)

        # Page 2: microphone
        p2 = Page('Test your microphone', 'Built-in and wired microphones are the most reliable; '
                  'Bluetooth headsets sometimes report odd formats or drop the first second.')
        card = Card()
        self.mic = MicPanel(app, with_meter=True)
        card.add_widget(self.mic)
        p2.body.addWidget(card)
        p2.finish()
        self.stack.addWidget(p2)

        # Page 3: try it
        p3 = Page('Try it', 'Click into the box, press Caps Lock, say something, then press Caps Lock again.')
        card = Card()
        self.tryout = QPlainTextEdit()
        self.tryout.setPlaceholderText('Your words will appear here…')
        self.tryout.setFixedHeight(64)  # room for a sentence or two, no more
        card.add_widget(self.tryout)
        rec_row = QWidget()
        hl = QHBoxLayout(rec_row)
        hl.setContentsMargins(0, 8, 0, 0)
        self.rec_button = QPushButton('Start recording')
        self.rec_button.clicked.connect(self._toggle_recording)
        hl.addWidget(muted('No Caps Lock handy? '))
        hl.addWidget(self.rec_button)
        hl.addStretch(1)
        card.add_widget(rec_row)
        p3.body.addWidget(card)

        card = Card('Good to know')
        card.add_row('Ctrl + Caps Lock', 'Toggles real Caps Lock when you need it.', kbd('Ctrl + Caps Lock'))
        card.add_row('Cancel', 'Click the red indicator to discard a recording.', None)
        card.add_row('Tray icon', f'Look for the 🎤 icon next to the clock — that is where {APP_NAME} lives. '
                     'Windows sometimes hides new icons behind the ^ arrow; drag it out to keep it visible.', None)
        sounds = _bind_check(app, 'sounds_enabled')
        sounds.stateChanged.connect(lambda s: play('start') if s else None)
        card.add_row('Sounds', 'Play a soft cue when a recording starts and stops.', sounds)
        p3.body.addWidget(card)
        p3.finish()
        self.stack.addWidget(p3)
        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(300)
        self._rec_timer.timeout.connect(self._sync_rec_button)

    def go(self, index: int) -> None:
        index = max(0, min(self.stack.count() - 1, index))
        self._leave(self.stack.currentIndex())
        self.stack.setCurrentIndex(index)
        self.steps.setText(f'Step {index + 1} of {self.stack.count()}')
        self.back.setVisible(index > 0)
        self.next.setText('Finish' if index == self.stack.count() - 1 else 'Continue')
        if index == 0:
            self.keys.refresh()
        elif index == 1:
            self.mic.start()
        elif index == 2:
            self.tryout.setFocus()
            self._rec_timer.start()

    def _leave(self, index: int) -> None:
        if index == 1:
            self.mic.stop()
        elif index == 2:
            self._rec_timer.stop()

    def _next(self) -> None:
        if self.stack.currentIndex() == self.stack.count() - 1:
            self.finish()
        else:
            self.go(self.stack.currentIndex() + 1)

    def _toggle_recording(self) -> None:
        # Keep the caret in the text box so the paste lands there
        self.tryout.setFocus()
        threading.Thread(target=self.app.toggle_recording, daemon=True).start()

    def _sync_rec_button(self) -> None:
        self.rec_button.setText('Stop recording' if self.app.recording else 'Start recording')

    def finish(self) -> None:
        self._leave(self.stack.currentIndex())
        self.app.settings.set('setup_completed', app_version())
        logger.info("First-run setup finished")
        self.window.show_normal()


# ---- window ----------------------------------------------------------------------

class MainWindow(QMainWindow):
    SECTIONS = ['Home', 'History', 'General', 'Dictation', 'Conversation', 'Indicator', 'Advanced']

    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self.setWindowTitle(APP_NAME)
        icon = APP_DIR / 'assets' / 'app.ico'
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(960, 660)
        self.setStyleSheet(STYLE)

        root = QWidget()
        hl = QHBoxLayout(root)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        self.sidebar = QListWidget()
        self.sidebar.setObjectName('sidebar')
        self.sidebar.setFixedWidth(190)
        for name in self.SECTIONS:
            self.sidebar.addItem(QListWidgetItem(name))
        self.sidebar.currentRowChanged.connect(self._section_changed)
        hl.addWidget(self.sidebar)
        self.outer = QStackedWidget()
        hl.addWidget(self.outer, 1)
        self.setCentralWidget(root)

        self.pages = QStackedWidget()
        self._page_widgets: List[Page] = [
            HomePage(app, self), HistoryPage(app), GeneralPage(app), DictationPage(app, self),
            ConversationPage(app), IndicatorPage(app), AdvancedPage(app, self),
        ]
        for page in self._page_widgets:
            self.pages.addWidget(page)
        self.wizard = SetupWizard(app, self)
        self.outer.addWidget(self.pages)
        self.outer.addWidget(self.wizard)
        self._current: Optional[Page] = None
        self.sidebar.setCurrentRow(0)

    def _section_changed(self, row: int) -> None:
        if self._current is not None:
            self._current.leave()
        page = self._page_widgets[row]
        self.pages.setCurrentIndex(row)
        page.refresh()
        self._current = page

    def show_normal(self) -> None:
        self.outer.setCurrentWidget(self.pages)
        self.sidebar.show()
        if self._current is not None:
            self._current.refresh()

    def start_setup(self) -> None:
        if self._current is not None:
            self._current.leave()
        self.sidebar.hide()
        self.outer.setCurrentWidget(self.wizard)
        self.wizard.go(0)

    def open(self, setup: bool = False) -> None:
        if setup:
            self.start_setup()
        elif self._current is not None:
            self._current.refresh()
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def _stop_live(self) -> None:
        """Release anything that runs only while visible (the mic monitor)."""
        if self.outer.currentWidget() is self.wizard:
            self.wizard._leave(self.wizard.stack.currentIndex())
        elif self._current is not None:
            self._current.leave()

    def closeEvent(self, event: QCloseEvent) -> None:
        # Closing hides; the app keeps running in the tray
        event.ignore()
        self.hide()

    def hideEvent(self, event) -> None:
        self._stop_live()
        super().hideEvent(event)
