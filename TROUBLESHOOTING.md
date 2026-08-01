# Troubleshooting Guide

This guide provides solutions to common issues you might encounter with the Voice Typing Assistant.

## Recordings Not Producing Transcriptions

A known issue is that the recorder activates and may show voice activity, but no transcription is produced after you stop recording. This usually means the recorded audio's volume (RMS value) is below the configured silence threshold. This can happen if you are using a laptop microphone with the lid closed or a microphone that is far away.

### Solution: Adjust the Silence Threshold

You can make the app more sensitive to quiet audio by adjusting the `silence_threshold` in your `settings.json` file.

1.  Open the `settings.json` file located in your `Documents\VoiceTyping\` folder.
2.  Find or add the `"silence_threshold"` setting (eg. `"silence_threshold": 0.005,`).
3.  Lower the value to make the silence detection more sensitive. See the table below for recommended values.

| Threshold | Equivalent dB | Recommended For                                |
| :-------- | :------------ | :--------------------------------------------- |
| `0.01`    | -40 dB        | **(Default)** Quiet room, close microphone.    |
| `0.005`   | -46 dB        | Normal room or a standard laptop microphone.   |
| `0.003`   | -50 dB        | A distant microphone or a quiet speaker.       |
| `0.001`   | -60 dB        | Very distant mic or a noisy environment.       |

**Debugging Tip**: To see exactly why a recording was discarded, check the latest log file in your `Documents\VoiceTyping\logs\` folder and listen to the `temp_audio.wav` audio file. The log will contain a warning with the calculated audio level of your recording, which can help you fine-tune the threshold.

## Transcription is Inaccurate or Cut Off

With certain models like OpenAI's `gpt-4o-transcribe`, a known issue can cause the end of a transcription to be cut off. While the application includes a workaround to minimize this, it can still happen occasionally.

### Solution: Retry Transcription

If you notice a transcription is cut off or inaccurate, the most effective solution is to use the **Retry Last Transcription** feature from the tray icon menu. This re-processes the same audio, which often produces the correct result on the second attempt.