# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Windows-only speech-to-text tray app (Python 3.10–3.12, tkinter + pynput). Caps Lock toggles recording; audio is transcribed by a configurable STT provider and pasted at the cursor. Two "conversation modes" (Meeting/Phone) capture multi-speaker sessions with speaker-separated transcripts (see `docs/conversation-modes.md`).

## Commands

```powershell
# Environment: pyproject.toml + uv.lock are the source of truth (dev group included by default)
uv sync                                    # after editing pyproject.toml: uv lock, then uv sync

# Run (console, with logs visible)
.\.venv\Scripts\python.exe .\voice_typing.pyw --debug
# Run detached (source users)
.\run_voice_typing.bat

# Quick syntax check of edited files
.\.venv\Scripts\python.exe -m py_compile voice_typing.pyw modules\*.py services\*.py

# Unit tests (pytest config lives in pyproject.toml; see tests/README.md for what belongs there)
uv run pytest

# Frozen build + installer (what users get; CI does this on a v* tag)
uv run pyinstaller BetterVoiceTyping.spec --noconfirm        # -> dist\BetterVoiceTyping\
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" /DAppVersion=1.0.0 installer\BetterVoiceTyping.iss   # -> dist\installer\

# Manual tool: real-keyboard hotkey check
.\.venv\Scripts\python.exe tests\manual\keyboard_test.py
```

Only one app instance can run (named mutex in `modules/single_instance.py`), so a frozen build and the dev instance can't run at once. Installing the built setup with `/VERYSILENT /CURRENTUSER /MERGETASKS=!startup,!desktopicon` and uninstalling with `unins000.exe /VERYSILENT` leaves the machine as it was (user data is never touched).

Tests cover only brittle, load-bearing contracts (hotkey message sequences, chunk ordering/retry/cancel, error classification) — no coverage target. Hardware, network, Tk, and the global keyboard hook are never touched; if a change can't be tested without patching internals, add an injectable seam (clock, delay, callable) instead.

Only one app instance can run (named mutex in `modules/single_instance.py`). The `.venv\Scripts\pythonw.exe` shim plus the real interpreter appear as two `pythonw` processes for one instance — not a duplicate-instance bug.

## Architecture

`voice_typing.pyw` owns the `VoiceTypingApp` orchestration: hotkey listener, recording lifecycle, the recorder side of conversation sessions (`_flush_chunk` / `_end_session`: sealing chunks, restarting capture, salvaging the tail), a 100ms Tk-thread watchdog (`_check_recorder_status`), and the processing pipeline (analyze → transcribe → optional LLM clean → paste). Everything else is a module with one job:

- `modules/session.py` — `ConversationSession` (one meeting/phone session: its `ChunkQueue`, preamble, failure summary, delivery callbacks) talks to the app only through the `SessionHost` protocol, which `VoiceTypingApp` implements (`is_current`, `deliver_text`, `show_failure`, …). `SessionNotes` is the indicator's state/alert note machine (app-level, `self._notes`). `build_preamble` is pure settings → text. All three are tested with fakes.

- `modules/hotkey.py` — `CapsLockHotkey`: pure state machine turning raw keyboard-hook messages (auto-repeat, Ctrl+Caps chord, injected keystrokes, lost key-ups) into one `TOGGLE` per physical press. The pynput `win32_event_filter` in `voice_typing.pyw` is a thin adapter over it; put new hotkey rules here with a test, never in the adapter.
- `modules/recorder.py` — `AudioRecorder`: mic capture thread writing `temp_audio.wav`. Flags consumed by the watchdog: `auto_stopped` (initial silence), `error` (device/stream failure — callers must keep captured audio), `max_duration_reached`. Meeting mode adds `modules/loopback_recorder.py` (WASAPI system-audio capture via `soundcard`), composed into a 2-channel WAV on stop.
- `modules/transcribe.py` — provider router. Recordings self-describe via `modules/audio_markers.py` (`recording_kind`: 2 channels = meeting, WAV comment `voice_typing:phone` = phone; markers survive snapshots/retries/restarts); otherwise dictation via `stt_provider` setting, where `null` = auto (ElevenLabs if `ELEVENLABS_API_KEY` is set, else OpenAI). The `custom` provider is `OpenAITranscriber` pointed at an OpenAI-compatible base URL (`services/custom_stt.py`). Transcriber instances in `services/` are cached by full config tuple; provider SDK imports are lazy for startup speed.
- `modules/clean_text.py` — optional LLM cleanup of dictation via the OpenAI chat API (`llm_model`, optional `llm_base_url` for compatible servers). Any failure falls back to the raw transcript.
- `modules/chunk_queue.py` — `ChunkQueue`: session chunks transcribe concurrently but deliver strictly in order at the cursor; one auto-retry, then the file is kept for tray retry. Lock order is documented in the file (delivery lock → state lock); callbacks fire outside the state lock. `retry_delay` is injectable for tests.
- `modules/settings.py` — `Settings` singleton; also loads API keys at import from `Documents\VoiceTyping\.env` (moved there from the app folder on first 1.0 run; the app-folder `.env` is still read as a fallback). Load order: raw JSON → `modules/settings_migrations.py` (`migrate`: pure, versioned by `schema_version`, tested with fixture dicts; add a new version there for any schema change) → merge defaults → `_reconcile_devices` (runtime hardware matching, reruns every launch, never a migration). Settings and history are written through `modules/fileutil.py` (`write_json_atomic`: temp file + fsync + `os.replace`, previous version kept as `.bak`; `read_json_with_backup` recovers from the `.bak`). Never write user JSON with a bare `open(..., 'w')`.
- `modules/ui.py` + `modules/status_manager.py` + `modules/tray.py` — recording indicator overlay(s), status state machine, pystray menu. tkinter is not thread-safe: all UI work must be marshalled through `UIFeedback` (queue → Tk main loop). Mid-recording warnings must use `UIFeedback.set_recording_note`, not `show_warning` — the pulse/elapsed-time ticker overwrites the warning overlay. Always-on-top is re-asserted (toggle off/on, `_assert_topmost`) on every show and every pulse tick because Tk skips the Win32 call when it thinks the flag is unchanged while Windows may have demoted the window. When reporting a retryable error, call `status_manager.set_status(ERROR, …)` *before* `show_error_with_retry(…)`: both repaint the label in UI-queue order and only the overlay carries the hint and the "click to retry" line.
- `modules/paste.py` — the one delivery strategy: clipboard + Ctrl+V (pynput) + delayed clipboard restore, serialized by a lock. The pre-1.0 output-provider plugin system is gone by decision; don't reintroduce a plugin folder.
- `services/openai_realtime_stt.py` — streaming dictation (beta) over an OpenAI Realtime websocket while recording; any failure falls back to the batch upload (the WAV is always written in parallel).
- `modules/paths.py` — every path comes from here: `APP_DIR` (bundled resources; `sys._MEIPASS` when frozen), `INSTALL_DIR`, `USER_DATA_DIR` (`Documents\VoiceTyping`: settings, `.env`, history, logs), `LOCAL_DATA_DIR` (`%LOCALAPPDATA%\BetterVoiceTyping`: in-progress recordings, update downloads), `app_version()` from the bundled `version.txt`.
- `modules/updater.py` — installer-based self-update for frozen builds only: GitHub latest release → `BetterVoiceTyping-Setup-<v>.exe` asset verified against `SHA256SUMS.txt` → detached helper script waits for our PID, runs setup `/VERYSILENT`, relaunches. Source checkouts get the releases page. Never overwrite the program folder from inside the app.

Concurrency invariants in `voice_typing.pyw`: recording start/stop/flush are serialized by `_toggle_lock`; `_recording_generation` stamps snapshot filenames (`temp_audio.wav.N.wav`) so a new recording can't clobber one mid-transcription; `_watchdog_token` ties the poll chain to the recording that started it. The snapshot sweeper must never delete files still referenced by a live `ChunkQueue` (`_recent_queues` registry).

User data (settings.json, `.env`, logs, history.json) lives in `Documents\VoiceTyping\` and in-progress recordings in `%LOCALAPPDATA%\BetterVoiceTyping\recordings\`, never in the program folder — the installer replaces it wholesale.

pynput gotcha: `listener.suppress_event()` raises an exception by design — code after it never runs (documented where used in `voice_typing.pyw`).

## Conventions

- Type hints on all function parameters and return values.
- No blocking operations on the Tk/main thread.
- Comments: preserve nearby `NOTE:`/`IMPORTANT:` comments when editing code around them.

## Changelog

`CHANGELOG.json` is the changelog of record (README links to it). When updating: check for an existing entry with the same category and today's date — append to its `changes` array if found, otherwise create a new object at the top of the `changelog` array. Each object requires `category`, `date` (YYYY-MM-DD), and a `changes` array of descriptive, full-sentence strings written for end users. Use the `General` category unless a better one exists. Entries that ship in a release also get a `version` field.

## Release process

1. Bump `version.txt` AND `pyproject.toml` (bare number, e.g. `1.0.0`; `tests/test_version.py` checks they match) and give the `CHANGELOG.json` entry that `version`.
2. Commit and push to `master`; CI (`.github/workflows/ci.yml`) runs tests and a PyInstaller build.
3. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`. The Release workflow refuses a tag that doesn't match `version.txt`, then builds the installer, writes `SHA256SUMS.txt`, and publishes the GitHub Release with notes from `scripts/release_notes.py`.
4. Smoke-test the installer per `docs/release-checklist.md`. Installed apps pick the release up via the daily check / tray "Check for Updates".

The installer is unsigned (documented in README: users click "More info → Run anyway"). Revisit Azure Artifact Signing (~$10/month, individual identity validation) once download counts justify it.
