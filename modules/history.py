import json
import logging
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import List, Deque

logger = logging.getLogger('voice_typing')

HISTORY_FILE = Path.home() / "Documents" / "VoiceTyping" / "history.json"
MAX_PERSISTED_ITEMS = 50

class TranscriptionHistory:
    """Recent transcriptions, persisted to disk so a crash, restart, or paste
    into the wrong window never loses a dictation."""

    def __init__(self, max_items: int = 5) -> None:
        self.history: Deque[str] = deque(maxlen=max_items)
        self._lock = threading.Lock()
        self._entries: List[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            self._entries = json.loads(HISTORY_FILE.read_text(encoding='utf-8'))[-MAX_PERSISTED_ITEMS:]
            for entry in self._entries[-self.history.maxlen:]:
                self.history.append(entry['text'])
        except FileNotFoundError:
            self._entries = []
        except Exception as e:
            logger.warning(f"Could not load transcription history: {e}")
            self._entries = []

    def _save(self) -> None:
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.write_text(
                json.dumps(self._entries, indent=2, ensure_ascii=False),
                encoding='utf-8'
            )
        except Exception as e:
            logger.warning(f"Could not save transcription history: {e}")

    def add(self, text: str) -> None:
        with self._lock:
            self.history.append(text)
            self._entries.append({
                'text': text,
                'timestamp': datetime.now().isoformat(timespec='seconds')
            })
            self._entries = self._entries[-MAX_PERSISTED_ITEMS:]
            self._save()

    def get_recent(self) -> List[str]:
        return list(reversed(self.history))

    def get_preview(self, text: str, max_length: int = 30) -> str:
        """Returns truncated preview of text for menu display"""
        if len(text) <= max_length:
            return text
        return text[:max_length] + "..."
