"""Ordered transcription queue for continuous conversation sessions.

In meeting/phone mode, recording is continuous: each caps-lock press flushes
the audio captured so far into a snapshot file and recording resumes
immediately. Chunks transcribe concurrently on their own threads, but results
are delivered strictly in chunk order — chunk N+1's text waits until chunk N
has been delivered or permanently failed — so the assembled transcript always
reads chronologically.

Callbacks receive all data as arguments and run WITHOUT the queue's state
lock held (so slow work — disk I/O, tray menu rebuilds — can't block
submit/close/cancel); delivery callbacks are serialized by a dedicated lock
to preserve chunk order. They still must not call back into the queue.
"""
import logging
import threading
import time
from typing import Callable, List

logger = logging.getLogger('voice_typing')

# Wait before the single automatic retry of a failed chunk (transient API
# errors and timeouts usually clear quickly; recording continues meanwhile)
RETRY_DELAY_S = 2.0

_PENDING, _DONE, _FAILED = 'pending', 'done', 'failed'


class ChunkQueue:
    def __init__(self,
                 transcribe_fn: Callable[[str], str],
                 on_result: Callable[[int, str, str], None],
                 on_retrying: Callable[[int], None],
                 on_failed: Callable[[int, str], None],
                 on_pending: Callable[[int], None],
                 on_drained: Callable[[List[str]], None]) -> None:
        """
        Args:
            transcribe_fn: (path) -> transcript text; raises on failure.
            on_result: (chunk_index, text, path) — delivered strictly in order.
            on_retrying: (chunk_index) — first attempt failed, retry starting.
            on_failed: (chunk_index, path) — chunk permanently failed; its file
                is kept on disk for manual retry.
            on_pending: (count) — number of undelivered chunks changed.
            on_drained: (failed_paths) — queue closed and fully delivered.
        """
        self._transcribe = transcribe_fn
        self._on_result = on_result
        self._on_retrying = on_retrying
        self._on_failed = on_failed
        self._on_pending = on_pending
        self._on_drained = on_drained
        self._lock = threading.RLock()
        # Serializes deliverers so results leave in order even when two
        # workers finish near-simultaneously; never held while _lock is taken
        # first by someone else (single acquisition order: deliver -> state)
        self._deliver_lock = threading.Lock()
        self._chunks: List[dict] = []  # undelivered, in submission order
        self._next_index = 1
        self._closed = False
        self._cancelled = False
        self._drained_notified = False
        self.failed_paths: List[str] = []

    def submit(self, path: str) -> int:
        """Queue a chunk file for transcription; returns its 1-based chunk number."""
        with self._lock:
            if self._closed or self._cancelled:
                raise RuntimeError("ChunkQueue is closed")
            index = self._next_index
            self._next_index += 1
            chunk = {'index': index, 'path': path, 'state': _PENDING, 'text': None}
            self._chunks.append(chunk)
            pending = len(self._chunks)
        try:
            self._on_pending(pending)
        except Exception:
            logger.exception("Error in queue status callback")
        threading.Thread(target=self._worker, args=(chunk,), daemon=True).start()
        return index

    def close(self) -> None:
        """No more submissions; on_drained fires once everything is delivered."""
        with self._lock:
            self._closed = True
        self._drain()  # fires on_drained now if the queue is already empty

    def cancel(self) -> None:
        """Drop all undelivered results. Running transcriptions finish silently."""
        with self._lock:
            self._cancelled = True
            self._closed = True
            self._chunks.clear()

    def active_paths(self) -> List[str]:
        """Files the queue still needs (pending chunks + kept failures)."""
        with self._lock:
            return [c['path'] for c in self._chunks] + list(self.failed_paths)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._chunks)

    def _worker(self, chunk: dict) -> None:
        for attempt in (1, 2):
            try:
                chunk['text'] = self._transcribe(chunk['path'])
                chunk['state'] = _DONE
                break
            except Exception as e:
                if self._cancelled:
                    chunk['state'] = _FAILED
                    break
                if attempt == 1:
                    logger.warning(f"Chunk {chunk['index']} transcription failed, retrying: {e}")
                    try:
                        self._on_retrying(chunk['index'])
                    except Exception:
                        logger.exception("Error in retrying callback")
                    time.sleep(RETRY_DELAY_S)
                    if self._cancelled:
                        # Cancelled during the backoff; don't burn an API call
                        chunk['state'] = _FAILED
                        break
                else:
                    logger.error(f"Chunk {chunk['index']} transcription failed permanently: {e}")
                    chunk['state'] = _FAILED
        self._drain()

    def _drain(self) -> None:
        """Deliver ready chunks from the head, strictly in order.

        State transitions happen under _lock; callbacks run outside it so
        slow delivery work can't block submit/close/cancel."""
        with self._deliver_lock:
            while True:
                with self._lock:
                    if self._cancelled:
                        return
                    if not self._chunks or self._chunks[0]['state'] == _PENDING:
                        pending = len(self._chunks)
                        drained = (self._closed and not self._chunks
                                   and not self._drained_notified)
                        if drained:
                            self._drained_notified = True
                        failed = list(self.failed_paths)
                        break
                    chunk = self._chunks.pop(0)
                    if chunk['state'] == _FAILED:
                        self.failed_paths.append(chunk['path'])
                try:
                    if chunk['state'] == _DONE:
                        self._on_result(chunk['index'], chunk['text'], chunk['path'])
                    else:
                        self._on_failed(chunk['index'], chunk['path'])
                except Exception:
                    logger.exception(f"Error delivering chunk {chunk['index']}")
            try:
                self._on_pending(pending)
                if drained:
                    self._on_drained(failed)
            except Exception:
                logger.exception("Error in queue status callback")
