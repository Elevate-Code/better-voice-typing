# Meeting Mode & Phone Mode — capturing conversations

Normal dictation assumes one voice: yours. These two tray-menu modes instead capture a **conversation** and give you a speaker-separated transcript you can paste anywhere — typically into an AI chat mid-call ("here's the conversation so far, what should I ask next?").

Both modes require an [ElevenLabs](https://elevenlabs.io) API key (`ELEVENLABS_API_KEY` in your `.env`); transcription uses their Scribe v2 model. They are mutually exclusive — enabling one turns the other off — and LLM transcript cleaning is skipped so speaker structure is preserved.

## Which mode do I want?

| | 🎧 Meeting Mode | 📞 Phone Mode |
| --- | --- | --- |
| **Use for** | Calls through your computer (Meet, Zoom, Teams…) | Voices in the room: a phone on speaker, an in-person chat |
| **What's recorded** | Your mic **plus** system audio (whatever you hear), as separate channels | Your mic only |
| **Speaker labels** | `Me:` / `Them:` — always correct, attributed by channel | Unlabeled turns by default; optional labels (see below) |
| **Indicator color** | Crimson-pink | Violet |

Rule of thumb: if the other person's voice comes out of your speakers, use Meeting Mode; if it travels through the air to your mic, use Phone Mode.

## How a session works (both modes)

Recording runs as a **continuous session** — you never lose audio while waiting for a transcript:

1. **Start**: press `Caps Lock`. The indicator shows the mode color and `caps=send · click=end`.
2. **Send**: press `Caps Lock` again anytime. Everything captured since the last send is queued for transcription, and **recording keeps rolling** (a sub-second gap mic-only; up to a couple of seconds in Meeting Mode while the 2-channel file is composed).
3. **End**: click the recording indicator, or toggle the mode off in the tray. Audio since your last send is **discarded by design** — press `Caps Lock` first if you want it.

Behind the scenes:

- Sent chunks transcribe concurrently in the background but are inserted at your cursor **strictly in order**, so the assembled transcript always reads chronologically.
- A failed chunk retries once automatically; if it fails again, its audio is kept for **Retry Last Transcription** in the tray and the session carries on.
- Chunks where nothing was said are skipped silently.
- The first chunk of each session is prefixed with a short bracketed note addressed to whoever reads the paste (typically an AI assistant), warning that proper nouns and speaker attribution may be imperfect. Disable with `session_preamble: false`.
- If a recording error ends the session (e.g. the mic disappears), audio captured up to the failure is salvaged and queued rather than lost.

## Meeting Mode details

Your mic is channel 0 and system audio is channel 1, so attribution is by **channel**, not voice-guessing — it's reliable even with similar voices. Everyone on the remote side shares the `Them:` label.

```
Me: So walk me through the pricing concern again?
Them: Well, mainly it's the onboarding fee that feels steep...
```

- If system-audio capture fails (e.g. output device issues), the recording falls back to normal mic-only dictation automatically.
- Tip: wear headphones. If your mic can hear your speakers, faint echoes of the other side can bleed onto your channel.

## Phone Mode details

All voices arrive through one microphone, so speakers are separated by **voice diarization**. Each speaker turn gets its own line:

```
So walk me through the pricing concern again?
Well, mainly it's the onboarding fee that feels steep...
```

Turns are deliberately **unlabeled** by default. Diarization can't know which voice is yours, and its generic "Speaker 1/2" labels are assigned fresh for every chunk — so "Speaker 1" in one chunk can be a different person in the next. Rather than mislead, the app drops the labels; an AI reading the transcript infers who's who from context. (Set `phone_speaker_labels: true` if you want the generic labels anyway.)

Each chunk is prefixed with a `--- [chunk N] ---` header marking the discontinuity.

### Stable "Me:" / "Them:" labels via voice matching (optional)

You can get reliable labels by enrolling your own voice in ElevenLabs' workspace speaker library:

1. In the [ElevenLabs dashboard](https://elevenlabs.io): **Speech to Text → Speakers → Add speaker**. Upload 10–60 seconds of you speaking alone (a solo dictation recording works) and pick a Speaker ID, e.g. `jane-doe`.
2. In `settings.json`, set `"phone_my_speaker_id": "jane-doe"`.

Words matched to your enrolled voice now come back tagged with your ID instead of a generic label, so your turns are labeled `Me:` and everyone else `Them:` — consistently across every chunk of a session. Other people enrolled in your workspace library are labeled by their prettified ID; in chunks where no voice matches, output falls back to unlabeled turns.

## Settings reference

All in `Documents\VoiceTyping\settings.json` (tray → Settings → Open Settings File):

| Setting | Description | Default |
| --- | --- | --- |
| `meeting_speaker_you` / `meeting_speaker_them` | The two speaker labels (used by both modes). | `"Me"` / `"Them"` |
| `session_preamble` | Prefix the first chunk of a session with a transcript-limitations note for the reader. | `true` |
| `phone_speaker_labels` | Show generic per-chunk `Speaker N:` labels in Phone Mode. | `false` |
| `phone_num_speakers` | Hint for the expected speaker count; `null` lets Scribe decide. Ignored while `phone_diarization_threshold` is set (the API accepts only one of the two). | `2` |
| `phone_diarization_threshold` | Diarization sensitivity (0.1–0.4). Also gates how willing the API is to match voices against the speaker library — the default was chosen empirically so enrolled-voice matching works reliably. Set `null` to use `phone_num_speakers` instead (disables tuned matching). | `0.3` |
| `use_speaker_library` | Match diarized voices against your workspace speaker library. Harmless no-op if the library is empty. | `true` |
| `phone_my_speaker_id` | Your registered Speaker ID for stable `Me:`/`Them:` labels (see above). | `null` |

## Notes & limitations

- Speaker attribution is good but not perfect: overlapping speech can land on the wrong side in Meeting Mode, and diarization quality varies with audio conditions in Phone Mode. The session preamble exists precisely so downstream readers treat labels as approximate.
- Chunks are independent API requests; very frequent sends make more (small) requests, infrequent sends make fewer, larger ones. Recordings auto-send at `max_recording_duration` (default 15 min) and keep rolling.
- Meeting Mode's system-audio capture uses WASAPI loopback on the default output device — if you switch output devices mid-session, the old device keeps being captured until the next chunk.
