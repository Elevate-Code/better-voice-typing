# Tests

```powershell
uv pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

## What belongs here

This suite protects the parts of the app that are **brittle, load-bearing, and
easy to break without noticing**: the pieces where a refactor that "looks
right" ships a race or a lost recording. There is no coverage target and there
never will be. A test earns its place by naming a concrete failure mode a user
could hit — mashing Caps Lock, holding it down, a chunk finishing out of
order, a retry that shouldn't fire after cancel — and failing when that
behaviour regresses.

If you can't say which user-visible contract a test guards in one sentence,
it doesn't belong here.

## Ground rules

- **No hardware, no network, no Tk, no global keyboard hooks.** Everything a
  test touches is driven by fakes: raw hook messages instead of pynput,
  callables instead of provider SDKs, an injected clock instead of sleeping.
- **Every test names its race.** The docstring says which sequence of events
  it replays and what must (not) happen.
- **A hang is a failure.** `pytest-timeout` is on for every test (see
  `pytest.ini`); tests that need to wait use short bounded polls.
- **Refactors add seams, not mocks.** If production code can't be tested
  without patching internals, the fix is an injectable parameter (clock,
  delay, transcribe function), not `monkeypatch` of private attributes.

## Map

| File | Guards |
|---|---|
| `test_hotkey.py` | One physical Caps Lock press is exactly one toggle: auto-repeat, mashing, Ctrl chords, injected keystrokes, lost key-ups. |
| `test_chunk_queue.py` | Conversation-session chunks deliver strictly in order, retry exactly once, keep failed files, and never fire after cancel. |
| `test_error_messages.py` | Provider exceptions classify into the right user-facing reason (quota vs. bad key vs. offline), and the raw text never leaks beyond the fallback. |

## What is deliberately *not* automated

Real `pynput` suppression and Caps Lock LED state, PortAudio/WASAPI device
behaviour, clipboard paste across applications, tray/overlay rendering, and the
install/update flow. Those live in `manual/` and `setup_test/` as interactive
tools and checklists, and are run by a human on a real Windows machine before
a release.
