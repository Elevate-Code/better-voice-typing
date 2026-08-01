# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Windows-only speech-to-text tray app (Python 3.10–3.12, tkinter + pynput). Caps Lock toggles recording; audio is transcribed by a configurable STT provider and pasted at the cursor. Two "conversation modes" (Meeting/Phone) capture multi-speaker sessions with speaker-separated transcripts (see `docs/conversation-modes.md`).

## Commands

```powershell
# Environment (uv-managed venv)
uv venv --python ">=3.10,<3.13"
uv pip install -r requirements.txt        # after adding a locked pin to requirements.txt

# Run (console, with logs visible)
.\.venv\Scripts\python.exe .\voice_typing.pyw --debug
# Run detached (what users use)
.\run_voice_typing.bat

# Quick syntax check of edited files
.\.venv\Scripts\python.exe -m py_compile voice_typing.pyw modules\*.py services\*.py

# Setup/update-flow test harness (no pytest suite; tests are manual scripts)
cd tests\setup_test; .\test_setup_simple.ps1
```

Only one app instance can run (named mutex in `modules/single_instance.py`). The `.venv\Scripts\pythonw.exe` shim plus the real interpreter appear as two `pythonw` processes for one instance — not a duplicate-instance bug.

## Architecture

`voice_typing.pyw` owns the `VoiceTypingApp` orchestration: hotkey listener, recording lifecycle, the conversation-session machinery (`_flush_chunk` / `_end_session` / `_make_chunk_queue`), a 100ms Tk-thread watchdog (`_check_recorder_status`), and the processing pipeline (analyze → transcribe → optional LLM clean → paste). Everything else is a module with one job:

- `modules/recorder.py` — `AudioRecorder`: mic capture thread writing `temp_audio.wav`. Flags consumed by the watchdog: `auto_stopped` (initial silence), `error` (device/stream failure — callers must keep captured audio), `max_duration_reached`. Meeting mode adds `modules/loopback_recorder.py` (WASAPI system-audio capture via `soundcard`), composed into a 2-channel WAV on stop.
- `modules/transcribe.py` — provider router. Recordings self-describe: 2 channels = meeting, WAV comment `voice_typing:phone` = phone (markers survive snapshots/retries/restarts); otherwise dictation via `stt_provider` setting, where `null` = auto (ElevenLabs if `ELEVENLABS_API_KEY` is set, else OpenAI). Transcriber instances in `services/` are cached by full config tuple; provider SDK imports are lazy for startup speed.
- `modules/chunk_queue.py` — `ChunkQueue`: session chunks transcribe concurrently but deliver strictly in order at the cursor; one auto-retry, then the file is kept for tray retry. Lock order is documented in the file (delivery lock → state lock); callbacks fire outside the state lock.
- `modules/settings.py` — `Settings` singleton; also loads `.env` at import (key-presence decisions happen in migrations and the provider router). One-shot migrations run at startup.
- `modules/ui.py` + `modules/status_manager.py` + `modules/tray.py` — recording indicator overlay(s), status state machine, pystray menu. tkinter is not thread-safe: all UI work must be marshalled through `UIFeedback` (queue → Tk main loop). Mid-recording warnings must use `UIFeedback.set_recording_note`, not `show_warning` — the pulse/elapsed-time ticker overwrites the warning overlay.
- `modules/output_providers.py` — pluggable paste strategies; users can drop custom providers in `Documents\VoiceTyping\plugins\`.
- `services/openai_realtime_stt.py` — streaming dictation (beta) over an OpenAI Realtime websocket while recording; any failure falls back to the batch upload (the WAV is always written in parallel).
- `check_update.py` — self-updater: downloads the latest GitHub release zipball and replaces app files, preserving `.env` and settings.

Concurrency invariants in `voice_typing.pyw`: recording start/stop/flush are serialized by `_toggle_lock`; `_recording_generation` stamps snapshot filenames (`temp_audio.wav.N.wav`) so a new recording can't clobber one mid-transcription; `_watchdog_token` ties the poll chain to the recording that started it. The snapshot sweeper must never delete files still referenced by a live `ChunkQueue` (`_recent_queues` registry).

User data (settings.json, logs, history.json, plugins) lives in `Documents\VoiceTyping\`, never in the repo — app updates replace repo files wholesale.

pynput gotcha: `listener.suppress_event()` raises an exception by design — code after it never runs (documented where used in `voice_typing.pyw`).

## Conventions

- Type hints on all function parameters and return values.
- No blocking operations on the Tk/main thread.
- Comments: preserve nearby `NOTE:`/`IMPORTANT:` comments when editing code around them.

## Changelog

`CHANGELOG.json` is the changelog of record (README links to it). When updating: check for an existing entry with the same category and today's date — append to its `changes` array if found, otherwise create a new object at the top of the `changelog` array. Each object requires `category`, `date` (YYYY-MM-DD), and a `changes` array of descriptive, full-sentence strings written for end users. Use the `General` category unless a better one exists. Entries that ship in a release also get a `version` field.

## Release process

1. Bump `version.txt` (bare number, e.g. `0.8.0`) and add/mark the `CHANGELOG.json` entry with that version.
2. Commit and push all changes to `master`.
3. Create and push a git tag: `git tag vX.Y.Z && git push origin vX.Y.Z` (tag carries the `v` prefix; `version.txt` does not — `check_update.py` normalizes when comparing).
4. On GitHub Releases, draft a release from the tag with notes from the changelog, and publish. No build artifacts — users install from source, and the in-app updater downloads the release zipball.
