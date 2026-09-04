"""Ordered chunk delivery for conversation sessions (modules/chunk_queue.py).

Contract: chunks transcribe concurrently but reach the cursor strictly in
submission order; a failed chunk retries exactly once, then its file is kept
and later chunks are not blocked; nothing fires after cancel; on_drained
fires exactly once. The transcriber is a callable that blocks on per-chunk
events so tests control completion order without sleeping.
"""
import threading
import time
from typing import Callable, Dict, List, Optional

import pytest

from modules.chunk_queue import ChunkQueue


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)


class Recorder:
    """Collects every callback the queue makes, in the order it made them."""

    def __init__(self) -> None:
        self.results: List[tuple] = []
        self.retrying: List[int] = []
        self.failed: List[tuple] = []
        self.pending: List[int] = []
        self.drained: List[List[str]] = []
        self.on_result_hook: Optional[Callable[[int], None]] = None

    def on_result(self, index: int, text: str, path: str) -> None:
        self.results.append((index, text, path))
        if self.on_result_hook:
            self.on_result_hook(index)

    def on_retrying(self, index: int) -> None:
        self.retrying.append(index)

    def on_failed(self, index: int, path: str, error: Optional[BaseException]) -> None:
        self.failed.append((index, path, error))

    def on_pending(self, count: int) -> None:
        self.pending.append(count)

    def on_drained(self, failed_paths: List[str]) -> None:
        self.drained.append(list(failed_paths))


class GatedTranscriber:
    """transcribe(path) blocks until release(path); outcomes are scripted."""

    def __init__(self) -> None:
        self.gates: Dict[str, threading.Event] = {}
        self.calls: List[str] = []
        self.failures: Dict[str, List[BaseException]] = {}  # per path, consumed in order
        self._lock = threading.Lock()

    def fail_next(self, path: str, exc: BaseException) -> None:
        self.failures.setdefault(path, []).append(exc)

    def release(self, path: str) -> None:
        self.gates.setdefault(path, threading.Event()).set()

    def __call__(self, path: str) -> str:
        with self._lock:
            self.calls.append(path)
            gate = self.gates.setdefault(path, threading.Event())
        gate.wait(5.0)
        with self._lock:
            queued = self.failures.get(path)
            if queued:
                raise queued.pop(0)
        return f"text:{path}"


def make_queue(transcriber: Callable[[str], str], rec: Recorder,
               retry_delay: float = 0.0) -> ChunkQueue:
    return ChunkQueue(transcriber, rec.on_result, rec.on_retrying, rec.on_failed,
                      rec.on_pending, rec.on_drained, retry_delay=retry_delay)


def test_chunks_deliver_in_submission_order_even_when_later_ones_finish_first() -> None:
    """Chunk 2 finishes before chunk 1: nothing is delivered until 1 lands."""
    t, rec = GatedTranscriber(), Recorder()
    q = make_queue(t, rec)
    assert q.submit("a") == 1
    assert q.submit("b") == 2
    assert q.submit("c") == 3
    t.release("c")
    t.release("b")
    wait_for(lambda: len(t.calls) == 3)
    time.sleep(0.05)
    assert rec.results == []
    t.release("a")
    wait_for(lambda: len(rec.results) == 3)
    assert [r[0] for r in rec.results] == [1, 2, 3]
    assert [r[2] for r in rec.results] == ["a", "b", "c"]


def test_first_failure_retries_exactly_once_then_delivers() -> None:
    t, rec = GatedTranscriber(), Recorder()
    t.fail_next("a", RuntimeError("blip"))
    q = make_queue(t, rec)
    q.submit("a")
    t.release("a")
    wait_for(lambda: len(rec.results) == 1)
    assert t.calls == ["a", "a"]
    assert rec.retrying == [1]
    assert rec.failed == []


def test_permanent_failure_keeps_file_reports_error_and_unblocks_later_chunks() -> None:
    t, rec = GatedTranscriber(), Recorder()
    boom = RuntimeError("quota")
    t.fail_next("a", RuntimeError("first"))
    t.fail_next("a", boom)
    q = make_queue(t, rec)
    q.submit("a")
    q.submit("b")
    t.release("a")
    t.release("b")
    q.close()
    wait_for(lambda: rec.drained)
    assert t.calls.count("a") == 2
    assert rec.failed == [(1, "a", boom)]
    assert [r[0] for r in rec.results] == [2]
    assert q.failed_paths == ["a"]
    assert rec.drained == [["a"]]
    assert q.active_paths() == ["a"]


def test_cancel_during_retry_backoff_makes_no_second_call_and_no_callbacks() -> None:
    t, rec = GatedTranscriber(), Recorder()
    t.fail_next("a", RuntimeError("blip"))
    q = make_queue(t, rec, retry_delay=0.3)
    q.submit("a")
    t.release("a")
    wait_for(lambda: rec.retrying == [1])
    q.cancel()
    time.sleep(0.6)
    assert t.calls == ["a"]
    assert rec.results == [] and rec.failed == [] and rec.drained == []


def test_cancel_drops_results_that_finish_afterwards() -> None:
    """Session ended by click while a chunk is still uploading."""
    t, rec = GatedTranscriber(), Recorder()
    q = make_queue(t, rec)
    q.submit("a")
    q.cancel()
    t.release("a")
    time.sleep(0.1)
    assert rec.results == [] and rec.drained == []
    assert q.pending_count == 0


def test_drained_fires_exactly_once() -> None:
    t, rec = GatedTranscriber(), Recorder()
    q = make_queue(t, rec)
    q.submit("a")
    q.close()
    q.close()
    assert rec.drained == []  # chunk still in flight
    t.release("a")
    wait_for(lambda: rec.drained)
    time.sleep(0.05)
    assert rec.drained == [[]]


def test_closing_an_empty_queue_drains_immediately() -> None:
    t, rec = GatedTranscriber(), Recorder()
    q = make_queue(t, rec)
    q.close()
    assert rec.drained == [[]]


def test_submit_after_close_or_cancel_is_refused() -> None:
    t, rec = GatedTranscriber(), Recorder()
    q = make_queue(t, rec)
    q.close()
    with pytest.raises(RuntimeError):
        q.submit("late")
    q2 = make_queue(t, rec)
    q2.cancel()
    with pytest.raises(RuntimeError):
        q2.submit("late")


def test_pending_count_reflects_undelivered_chunks() -> None:
    t, rec = GatedTranscriber(), Recorder()
    q = make_queue(t, rec)
    q.submit("a")
    q.submit("b")
    assert q.pending_count == 2
    assert rec.pending == [1, 2]
    t.release("a")
    wait_for(lambda: len(rec.results) == 1)
    assert q.pending_count == 1
    t.release("b")
    wait_for(lambda: q.pending_count == 0)
    assert rec.pending[-1] == 0


def test_a_crashing_delivery_callback_does_not_block_the_next_chunk() -> None:
    t, rec = GatedTranscriber(), Recorder()

    def explode(index: int) -> None:
        if index == 1:
            raise ValueError("paste failed")
    rec.on_result_hook = explode
    q = make_queue(t, rec)
    q.submit("a")
    q.submit("b")
    t.release("a")
    t.release("b")
    q.close()
    wait_for(lambda: rec.drained)
    assert [r[0] for r in rec.results] == [1, 2]


def test_many_concurrent_chunks_finishing_in_random_order_stay_ordered() -> None:
    import random
    t, rec = GatedTranscriber(), Recorder()
    q = make_queue(t, rec)
    paths = [f"p{i}" for i in range(25)]
    for p in paths:
        q.submit(p)
    wait_for(lambda: len(t.calls) == len(paths))
    shuffled = paths[:]
    random.shuffle(shuffled)
    for p in shuffled:
        t.release(p)
    q.close()
    wait_for(lambda: rec.drained)
    assert [r[0] for r in rec.results] == list(range(1, len(paths) + 1))
