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
| `test_session.py` | Meeting/Phone sessions: preamble once, chunk headers, notes vs. overlays vs. log depending on whether the session is live, end-of-session failure summary, never painting over a dictation in progress. |
| `test_fileutil.py` | Settings and history writes are atomic and recover from `.bak` after a torn write. |
| `test_audio_markers.py` | A recording file alone says dictation / meeting / phone, surviving snapshots and restarts. |
| `test_custom_stt.py` | Pre-1.0 custom STT base URLs keep working (`/v1` appended), no OpenAI key needed. |
| `test_tray_menu.py` | Tray menu items are accepted by pystray at construction (one bad item kills the whole icon). |
| `test_settings_migrations.py` | Every historical settings.json shape loads into the current schema, idempotently, keeping the user's choices and unknown keys. |
| `test_realtime_stt.py` | Streaming dictation raises (so batch takes over) after any lost turn or server error, and commits after all buffered audio. |
| `test_updater.py` | The updater never runs an installer whose checksum doesn't match the release, compares versions numerically, and the helper waits for our PID. |
| `test_version.py` | `version.txt` and `pyproject.toml` agree. |

## What is deliberately *not* automated

Real `pynput` suppression and Caps Lock LED state, PortAudio/WASAPI device
behaviour, clipboard paste across applications, tray/overlay rendering, and the
install/update flow. `manual/keyboard_test.py` is an interactive tool for the
first of those; the rest is the release smoke test in
`docs/release-checklist.md`, run by a human on a real Windows machine.
