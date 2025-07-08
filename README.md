# Voice Typing Assistant

A lightweight Python desktop app for Windows that improves upon Windows Voice Typing (Win+H) by offering superior transcription accuracy and the ability to navigate between windows while recording, all while maintaining a simple, intuitive interface.

![Voice Typing Demo](voice-typing-demo.gif)

## Overview - How it works

- Press `Caps Lock` to begin recording your voice
- A recording indicator with audio level appears (top-right corner by default)
- You can continue to navigate and type while recording, or click the recording indicator to cancel
- Press `Caps Lock` again to stop recording and process the audio
- The audio is sent to your chosen speech-to-text provider (OpenAI `gpt-4o-transcribe` by default)
- (optional) The transcribed text can be further refined with a quick pass of an LLM model
- The transcribed text is inserted at your current cursor position in any text field or editor

**NOTE:** Hold `Ctrl` while pressing `Caps Lock` if you want to toggle Caps Lock on/off.

## Changelog

✨ New major version, v0.6.0, see [docs/stt-upgrade-migration.md](docs/stt-upgrade-migration.md) for more details.

For a detailed history of changes, see the [CHANGELOG.json](CHANGELOG.json) file.

## Features

### Recording Controls
- **Toggle Recording**: Caps Lock (Ctrl+Caps Lock to toggle Caps Lock on/off)
- **Cancel Recording/Processing**: Click the recording indicator to cancel recording or transcription
- **Copy Last Transcription**: If your cursor was misplaced, left-click tray icon to copy last transcription

### Tray Options/Settings
- Recent Transcriptions: Access previous transcriptions, copy to clipboard
- Microphone Selection: Choose your preferred input device
- Settings:
  - Continuous Capture: Default recording mode. Record audio until the user stops it, send it all at once to the STT provider
  - Speech-to-Text: Select your STT provider (OpenAI, Google Cloud) and model (Whisper, GPT-4o, GPT-4o Mini)
  - Silent-Start Timeout: Cancels the recording if no sound is detected within the first few seconds, preventing accidental recordings
  - Clean Transcription: Enable/disable further refinement of the transcription using a configurable LLM
  - UI Indicator Position: Change the corner of the screen where the recording indicator appears.
- Restart: Quickly restart the application, like when its not responding to the keyboard shortcut
- Exit: Exit the application

### Tray History
- Keeps track of recent transcriptions
- Useful if your cursor was in the wrong place at the time of insertion
- Quick access to copy previous transcriptions from system tray

### Fine-Tuning (Optional)

While most settings can be controlled from the tray menu, you can fine-tune the application's behavior by editing the `settings.json` file.

| Setting | Description | Default | Example Values |
| --- | --- | --- | --- |
| `silent_start_timeout` | Duration in seconds to wait for sound at the beginning of a recording before automatically canceling. Set to `null` to disable. | `4.0` | `2.0` to `5.0` |
| `silence_threshold` | The audio level (RMS) below which sound is considered silence. Lower values are more sensitive. | `0.01` | `0.005` (very quiet) to `0.02` (noisier) |
| `stt_provider` | The speech-to-text service to use. | `"openai"` | `"openai"`, `"google"` |
| `openai_stt_model` | The specific model to use for OpenAI's service. `gpt-4o-transcribe` is recommended for highest accuracy. | `"gpt-4o-transcribe"` | `"gpt-4o-transcribe"`, `"gpt-4o-mini-transcribe"` |

## Technical Details
- Minimal UI built with Python tkinter
- Multi-provider Speech-to-Text support with OpenAI GPT-4o models and Whisper
- Extensible architecture for adding new STT providers (Google Cloud, Azure, etc.)

## Known Issues/Limitations
- For now, only supporting Windows OS and Python 3.10+
- Brittle start script, `run_voice_typing.bat` sometimes just stops working silently so I fall back to running `voice_typing.pyw` from the command line
- Untested update mechanism ([let me know if it doesn't work](https://github.com/jason-m-hicks/better-voice-typing/issues))
- Recordings may not produce transcriptions if your microphone's audio level is too low
- Maximum recording duration of ~10 minutes per transcription due to OpenAI Whisper API's 25MB file size limit

## Troubleshooting

For solutions to common problems, see the [**Troubleshooting Guide**](TROUBLESHOOTING.md).

## Setup/Installation - For Users

### Quick Start (Windows)

* Requires Python 3.10 or higher (check with `python --version`) - get from [python.org](https://python.org)
* Requires `uv` CLI tool (check with `uv --version`) - get from [uv installation guide](https://docs.astral.sh/uv/getting-started/#installation)

1. Download this project by clicking the green "Code" button at top of page → "Download ZIP" or clone the repo
2. Extract the ZIP file to a location of your choice
3. Run `setup.bat` from Command Prompt or PowerShell:
   - Open Command Prompt or PowerShell (run `cmd` or `powershell` in the search bar)
   - Navigate to the folder: `cd "path\to\extracted\better-voice-typing"`
   - Run: `setup.bat` (Command Prompt) or `.\setup.bat` (PowerShell)
   - This will create a virtual environment, install packages, and set up default configuration
   - If you encounter any instillation issues, please [report them](https://github.com/Elevate-Code/better-voice-typing/issues)
4. Open the `.env` file in Notepad, update the following and save:
   - OpenAI API key ([get one here](https://platform.openai.com/api-keys))
   - (Optional) Anthropic API key for text cleaning
5. Launch the application by double-clicking the `run_voice_typing.bat` file in the application folder
6. ⚠️ Ensure the app's tray icon is visible by right-clicking the taskbar → "Taskbar settings" → "Select which icons appear on the taskbar" → Toggle on for Voice Typing Assistant
7. Right-click `run_voice_typing.bat` → Send to → Desktop to create a shortcut

**(Optional) Fine-tune transcript cleaning**

GPT-4o-transcribe is usually accurate enough that an extra cleaning pass isn't necessary.
If you still want to use the post-processing feature:

1. After the first run, open `settings.json`.
2. Update the `"llm_model"` value to any provider/model [supported by LiteLLM](https://docs.litellm.ai/docs/providers) (eg. `anthropic/claude-3-5-haiku-latest`).
3. Save the file and restart the application.

### Auto-start with Windows
To make the app start automatically when Windows boots:
1. Press `Win + R` on your keyboard
2. Type `shell:startup` and press Enter
3. Create a shortcut to `run_voice_typing.bat` in this folder:
   - Right-click `run_voice_typing.bat` → "Copy"
   - Navigate to the startup folder
   - Right-click in an empty area → "Paste shortcut" (might be under more options)

### Updating the App
To update to the latest version:
1. Open Command Prompt or PowerShell
2. Navigate to the folder: `cd "path\to\better-voice-typing"`
3. Run: `setup.bat` (Command Prompt) or `.\setup.bat` (PowerShell)
4. Choose 'Y' when asked to check for updates
5. The tool will automatically:
   - Download the latest version
   - Preserve your settings and API keys
   - Update all dependencies
6. Restart the app if it was running

## Setup/Installation - For Developers

1. Clone the repo
2. Ensure you have `uv` installed (see [uv installation guide](https://docs.astral.sh/uv/getting-started/#installation))
3. Create a virtual environment with `uv venv --python ">=3.8"`
4. Activate with `.venv\Scripts\activate`
5. Install dependencies with `uv pip install -r requirements.txt`
6. Create a `.env` file based on `.env.example` by running `cp .env.example .env`
7. Set up your API keys:
   - Get an OpenAI API key from [OpenAI's API Keys page](https://platform.openai.com/api-keys)
   - (Optional) Get an Anthropic API key if you want to use the text cleaning feature
   - Add these keys to your `.env` file
8. Run the app from the command line:
   ```
   .\.venv\Scripts\python.exe .\voice_typing.pyw
   ```
9. For debugging: Add the `--debug` flag when executing:
   ```
   .\.venv\Scripts\python.exe .\voice_typing.pyw --debug
   ```

## TODO

Want to request a feature or report a bug? [Create an issue](https://github.com/Elevate-Code/better-voice-typing/issues)

- [x] Review and validate setup and installation process
- [x] Add support for OpenAI's [new audio models](https://platform.openai.com/docs/guides/audio)
- [x] Update and improve README.md
- [ ] Some warning or auto-stop if recording duration is going to be too long (due to 25MB API limits)
- [ ] Add support for more speech-to-text providers (Google Cloud implementation in progress)
- [ ] Since text cleaning isn't needed with gpt-4o-transcribe, pivot it to be "post-processing" and allow user to customize the prompt
- [ ] Customizable activation shortcuts for recording control
- [ ] Improved transcription accuracy via VLM for code variables, proper nouns and abbreviations using screenshot context and cursor position
- [ ] Add support for translation?

## Contributing

TBD, for now, just create a pull request and start a conversation.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.