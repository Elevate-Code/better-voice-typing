"""How recordings self-describe their kind, and how to read it back.

Dictation, meeting and phone recordings need different transcription
pipelines, and the decision has to survive snapshots, retries and app
restarts, so it is carried by the WAV file itself rather than by app state:

- MEETING: 2 channels (mic + system audio). Dictation is always mono.
- PHONE: mono, tagged by the recorder with a WAV comment.
- DICTATION: everything else.

No app imports here; this is safe to use from tests with synthetic files.
"""

PHONE_RECORDING_COMMENT = 'voice_typing:phone'

DICTATION, MEETING, PHONE = 'dictation', 'meeting', 'phone'


def recording_kind(filename: str) -> str:
    """Classify a recording file as DICTATION, MEETING, or PHONE.

    Opens the file once. Unreadable files count as dictation and fail later
    with a real error from the provider.
    """
    try:
        import soundfile as sf
        with sf.SoundFile(filename) as f:
            if f.channels >= 2:
                return MEETING
            if (f.comment or '').startswith(PHONE_RECORDING_COMMENT):
                return PHONE
    except Exception:
        pass
    return DICTATION
