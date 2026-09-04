# Voice Typing Assistant

A lightweight Python desktop app for Windows that improves upon Windows Voice Typing (Win+H) by offering superior transcription accuracy and the ability to navigate between windows while recording, all while maintaining a simple, intuitive interface.

![Voice Typing Demo](voice-typing-demo.gif)

## Overview - How it works

- Press `Caps Lock` to begin recording your voice
- A recording indicator with audio level appears on your screen(s) (position and display options configurable)
- You can continue to navigate and type while recording, or click the recording indicator to cancel
- Press `Caps Lock` again to stop recording and process the audio
- The audio is sent to your chosen speech-to-text provider — ElevenLabs Scribe or OpenAI, picked automatically from the API keys you configure (ElevenLabs preferred when both are set; it benchmarked noticeably more accurate on dictation)
- (optional) The transcribed text can be further refined with a quick pass of an LLM model
- The transcribed text is inserted at your current cursor position in any text field or editor

**NOTE:** Hold `Ctrl` while pressing `Caps Lock` if you want to toggle Caps Lock on/off.

## Changelog

See the [CHANGELOG.json](CHANGELOG.json) file for latest changes or the [releases page](https://github.com/Elevate-Code/better-voice-typing/releases) for major releases.

## Features

#### Recording Controls
- **Toggle Recording**: Caps Lock (Ctrl+Caps Lock to toggle Caps Lock on/off)
- **Cancel Recording/Processing**: Click the recording indicator to cancel recording or transcription
- **Copy Last Transcription**: If your cursor was misplaced, left-click tray icon to copy last transcription
- The recording indicator shows elapsed recording time, and recordings auto-stop (and still transcribe) at a configurable maximum duration
- Only one instance of the app can run at a time; launching it again shows a notice instead of a second conflicting instance

### Conversation capture: Meeting Mode & Phone Mode

Beyond dictation, two tray-menu modes capture a **conversation** and paste back a speaker-separated transcript — great for pulling a live call into an AI chat mid-conversation. Both require an [ElevenLabs](https://elevenlabs.io) API key.

- **🎧 Meeting Mode** — for calls through your computer (Meet, Zoom, Teams…). Records your mic *plus* system audio as separate channels, so transcripts come back reliably labeled:

  ```
  Me: So walk me through the pricing concern again?
  Them: Well, mainly it's the onboarding fee that feels steep...
  ```

- **📞 Phone Mode** — for voices in the room (a phone on speaker, an in-person chat). Mic-only recording; speakers are separated onto their own lines by voice diarization, and you can optionally enroll your own voice for stable `Me:`/`Them:` labels.

Both record as a **continuous session**: press `Caps Lock` to send everything captured so far for transcription while recording keeps rolling; click the indicator to end the session. Sent chunks are transcribed in the background and inserted at your cursor strictly in order.

📖 **Full guide — setup, the session model, speaker labels, voice matching, and all related settings: [docs/conversation-modes.md](docs/conversation-modes.md)**

### Streaming Dictation (Beta)

Toggle **Streaming Dictation** under Settings to transcribe *while you speak* over an OpenAI Realtime websocket: text is ready ~1–3 seconds after you stop, regardless of how long the recording was (normally the wait grows with clip length).

- Normal dictation mode only — Meeting and Phone modes keep their batch pipelines (their multi-speaker transcription isn't available in realtime APIs).
- Realtime models trade a little accuracy for speed: each speech segment is transcribed as you go, without the full-recording context the batch model gets. Hence the Beta label — turn it off if you notice quality dips.
- Fail-safe by design: the audio file is still recorded in parallel, and any streaming failure (connection, mid-recording drop, quota) falls back to the normal batch upload automatically.

### Tray Options/Settings
- Retry Last Transcription: Attempts to re-process the last audio recording, useful if the first attempt failed or was inaccurate.
- Recent Transcriptions: Access previous transcriptions, copy to clipboard.
- Microphone Selection: Choose your preferred input device.
- Settings:
  - Clean Transcription: Enable/disable further refinement of the transcription using a configurable LLM.
  - Streaming Dictation (Beta): Transcribe while recording for near-instant results (see above).
  - Silent-Start Timeout: Cancels the recording if no sound is detected within the first few seconds, preventing accidental recordings.
  - Recording Indicator: Customize size, position, and multi-monitor display of the recording indicator.
  - Speech-to-Text: Select your STT provider (ElevenLabs Scribe, OpenAI, Custom/Local) and model.
  - Output Mode: Choose how text is inserted (see Plugins below).
  - Open Settings File / Open Logs Folder: Quick access to configuration and logs.
- Restart: Quickly restart the application, like when it's not responding to the keyboard shortcut.

### Tray History
- Keeps track of recent transcriptions
- Useful if your cursor was in the wrong place at the time of insertion
- Quick access to copy previous transcriptions from system tray
- The last 50 transcriptions are also saved (with timestamps) to `Documents\VoiceTyping\history.json`, so nothing is lost across restarts or crashes

### Fine-Tuning (Optional)

While most settings can be controlled from the tray menu, you can fine-tune the application's behavior by editing the settings file at `C:\Users\{YourUsername}\Documents\VoiceTyping\settings.json` (tray icon → Settings → Open Settings File). Older installs kept this file at `modules/settings.json`; it is migrated to the new location automatically on first run.

| Setting | Description | Default | Example Values |
| --- | --- | --- | --- |
| `silent_start_timeout` | Duration in seconds to wait for sound at the beginning of a recording before automatically canceling. Set to `null` to disable. | `4.0` | `2.0` to `5.0` |
| `silence_threshold` | The audio level (RMS) below which sound is considered silence. Lower values are more sensitive. | `0.01` | `0.005` (very quiet) to `0.02` (noisier) |
| `max_recording_duration` | Maximum recording length in seconds; when reached, recording stops automatically and the captured audio is still transcribed. Set to `null` to disable. | `900.0` | `300.0`, `1200.0`, `null` |
| `log_retention_days` | Number of days to keep log files. | `60` | `14`, `90`, `null` (indefinitely) |
| `log_transcript_text` | Whether log files include the transcript text itself. Set to `false` to keep dictated content out of logs. | `true` | `true`, `false` |
| `stt_provider` | The speech-to-text service to use. `null` picks automatically: ElevenLabs if `ELEVENLABS_API_KEY` is set, otherwise OpenAI. | `null` (auto) | `"elevenlabs"`, `"openai"`, `"custom"` |
| `stt_language` | Language for transcription (ISO-639-1 code). | `"en"` | `"en"`, `"es"`, `"de"` |
| `custom_stt_base_url` | Base URL for custom/local STT server. | `"http://localhost:8000"` | Any local or remote URL |
| `custom_stt_model` | Model name for custom STT server. | `"parakeet-tdt-0.6b-v2"` | Model supported by your server |
| `openai_stt_model` | The specific model to use for OpenAI's service. `gpt-4o-transcribe` is recommended for highest accuracy. | `"gpt-4o-transcribe"` | `"gpt-4o-transcribe"`, `"gpt-4o-mini-transcribe"` |
| `clipboard_restore_delay_ms` | How long after pasting to wait before restoring your previous clipboard contents. Increase if slow apps paste your old clipboard instead of the transcript. | `300` | `100` to `1000` |

## Technical Details
- Minimal UI built with Python tkinter
- Multi-provider Speech-to-Text support: ElevenLabs Scribe, OpenAI GPT-4o models, Whisper, and custom local/remote servers
- Extensible architecture for adding new STT providers (Azure, local models, etc.)
- Audio is uploaded as FLAC (lossless, roughly half the size of WAV) to reduce latency and stay under API upload limits
- User data (settings, transcription history, logs) lives in `Documents\VoiceTyping`, so app updates never touch it

## Known Issues/Limitations
- For now, only supporting Windows OS and Python 3.10 - 3.12
- When using `gpt-4o-transcribe`, the end of a transcription may occasionally be cut off - this is a [known model issue](https://community.openai.com/t/gpt-4o-transcribe-truncates-the-transcript/1148347). A workaround is in place to minimize this, but if it occurs, use the Retry Last Transcription and see the [Troubleshooting Guide](TROUBLESHOOTING.md).
- When using the `gpt-4o-transcribe` model to transcribe spoken instructions, sometimes it responds to them or carries them out.
- Update mechanism has had limited testing ([let me know if it doesn't work](https://github.com/Elevate-Code/better-voice-typing/issues))
- Recordings may not produce transcriptions if your microphone's audio level is too low
- OpenAI's API has a 25MB upload limit (roughly 20 minutes of audio with FLAC compression); recordings auto-stop at `max_recording_duration` (15 minutes by default) and are still transcribed

## Troubleshooting

For solutions to common problems, see the [**Troubleshooting Guide**](TROUBLESHOOTING.md).

You can find detailed application logs in `C:\Users\{YourUsername}\Documents\VoiceTyping\logs`.

## Using Custom/Local Speech-to-Text

The Voice Typing Assistant supports connecting to custom Speech-to-Text servers, whether local or remote. This allows you to:
- Use locally running models for privacy
- Connect to custom STT servers
- Use alternative STT providers not directly integrated
- Run OpenAI-compatible APIs locally

(As an example you can use parakeet+fastapi docker: `docker run -d -p 8000:8000 viktor742/openapi-parakeet-tdt-0.6b-v2:0.2.1`)

### Configuration

1. **Via Settings Menu**: Right-click the tray icon → Settings → Speech-to-Text → Provider → Select "Custom STT"

2. **Via settings.json**: Edit `Documents\VoiceTyping\settings.json` (tray icon → Settings → Open Settings File):
```json
{
  "stt_provider": "custom",
  "custom_stt_base_url": "http://localhost:8000",
  "custom_stt_model": "parakeet-tdt-0.6b-v2"
}
```

3. **Changing the URL and Model**:
   - `custom_stt_base_url`: Set this to your STT server's base URL (e.g., `http://localhost:8000`, `http://192.168.1.100:5000`)
   - `custom_stt_model`: Set this to the model name your server expects (optional, depends on server)

### Compatible Servers

The custom provider speaks the OpenAI audio API, which is what current local STT servers offer (speaches / faster-whisper-server, whisper.cpp server, the parakeet FastAPI images, LocalAI, vLLM):

- `POST {custom_stt_base_url}/v1/audio/transcriptions` — `/v1` is appended automatically if your base URL doesn't end with it
- Multipart form with a `file` field (sent as WAV) and a `model` field
- Response JSON `{"text": "transcribed text"}`

Servers with their own endpoint shapes (`/transcribe`, `{"segments": …}`) were supported before 1.0 and no longer are; put an OpenAI-compatible façade in front of them.

### Optional Authentication

If your server requires authentication, set the `CUSTOM_STT_API_KEY` environment variable in your `.env` file:
```
CUSTOM_STT_API_KEY="your-api-key-here"
```

## Installation

### Installer (recommended)

1. Download `BetterVoiceTyping-Setup-<version>.exe` from the [latest release](https://github.com/Elevate-Code/better-voice-typing/releases/latest).

   > **Windows will warn you the first time you run the installer.** It isn't code-signed: signing certificates cost money and require identity verification, and this is a free hobby project. You'll see "Windows protected your PC". Click **More info**, then **Run anyway**. Updates from inside the app won't show this again. Prefer to see the code first? Install from source below; it runs the same code without the one-click installer.

2. Run the installer. It installs for your user only (no admin prompt) into `%LOCALAPPDATA%\Programs\Better Voice Typing`, adds a Start menu entry, and by default starts the app when you sign in to Windows.
3. A microphone icon appears in the system tray. Right-click it → **Open API Keys (.env)**, add at least one speech-to-text API key (not needed only if you run a local Custom STT server — see below), save, then tray icon → **Restart**:
   - ElevenLabs API key ([get one here](https://elevenlabs.io/app/settings/api-keys)) — recommended: best dictation accuracy, and required for Meeting/Phone modes
   - and/or OpenAI API key ([get one here](https://platform.openai.com/api-keys)) — also enables Streaming Dictation and transcript cleaning

   Your keys, settings and history live in `Documents\VoiceTyping\`, so they survive updates and reinstalls.
4. 💡 Make the tray icon always visible: right-click the taskbar → "Taskbar settings" → "Other system tray icons" → toggle on Better Voice Typing.

### Updating

The app checks for a new release once a day and mentions it on the indicator. Tray icon → **Check for Updates** downloads the new installer, verifies its checksum against the release's `SHA256SUMS.txt`, installs it silently and relaunches the app. Uninstall via Windows Settings → Apps; your data in `Documents\VoiceTyping` is left in place.

### From source (developers, or if you'd rather not run an installer)

Requires [`uv`](https://docs.astral.sh/uv/getting-started/#installation); it fetches a suitable Python (3.10–3.12) by itself.

1. Clone the repo (or download and extract the ZIP)
2. Run `setup.bat` (creates the environment with `uv sync`), or run `uv sync` yourself
3. Launch with `run_voice_typing.bat`; right-click it → Send to → Desktop for a shortcut, or put a shortcut in `shell:startup` to launch at sign-in
4. Add API keys as in step 3 above. From source, **Check for Updates** opens the releases page; update with `git pull` and `uv sync`.

**(Optional) Fine-tune transcript cleaning**

Modern STT models are usually accurate enough that an extra cleaning pass isn't necessary.
If you still want to use the post-processing feature:

1. After the first run, open `settings.json`.
2. Set `"llm_model"` to an OpenAI chat model (default `gpt-4o-mini`). To use another provider, set `"llm_base_url"` to any OpenAI-compatible endpoint (Ollama, LM Studio, OpenRouter, …) and `"llm_model"` to a model that server offers. If that server needs its own key, put it in `.env` as `LLM_API_KEY` (otherwise `OPENAI_API_KEY` is sent).
   - Claude example: `"llm_base_url": "https://api.anthropic.com/v1/"`, `"llm_model": "claude-3-5-haiku-latest"`, and `LLM_API_KEY=<your Anthropic key>` in `.env`.
3. Save the file and restart the application.

## Development

1. Clone the repo and run `uv sync` (creates `.venv` with the locked dependencies, dev tools included)
2. Run the app once; it creates `Documents\VoiceTyping\.env` from `.env.example` (an app-folder `.env` from older versions is moved there automatically) — add your keys there
3. Run the app from the command line (`--debug` keeps the console and verbose logs):
   ```
   .\.venv\Scripts\python.exe .\voice_typing.pyw --debug
   ```
4. Tests: `uv run pytest` (see `tests/README.md` for what belongs there). Build the installer locally with `uv run pyinstaller BetterVoiceTyping.spec --noconfirm` then `iscc /DAppVersion=<version> installer\BetterVoiceTyping.iss` (Inno Setup 6). Releases are built by GitHub Actions on a `v*` tag; see `docs/release-checklist.md`.

## TODO/Roadmap

Want to request a feature or report a bug? [Create an issue](https://github.com/Elevate-Code/better-voice-typing/issues)

- [x] Review and validate setup and installation process
- [x] Add support for OpenAI's [new audio models](https://platform.openai.com/docs/guides/audio)
- [x] Update and improve README.md
- [x] Some warning or auto-stop if recording duration is going to be too long (due to 25MB API limits) — auto-stops at `max_recording_duration` and still transcribes
- [x] Support local whisper (or other) models via the Custom STT provider and a local API server
- [x] Performance profiling and lightweight audio compression — uploads are FLAC-encoded, deferred heavy imports at startup
- [ ] Add support for more speech-to-text providers (Azure, Deepgram, etc.)
- [ ] Customizable activation shortcuts for recording control
- [ ] Since text cleaning isn't needed with gpt-4o-transcribe, pivot it to be "post-processing" and allow user to customize the prompt
- [ ] Add user-configurable translation mode (eg. "English to {language}")
- [ ] Add user-configurable post-processing mode (eg. "Turn my rambling thoughts into an elegant email")
- [ ] Improved transcription accuracy via VLM for code variables, proper nouns and abbreviations using screenshot context and cursor position

## Contributing

TBD, for now, just create a pull request and start a conversation.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.