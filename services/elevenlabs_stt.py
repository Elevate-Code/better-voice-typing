"""ElevenLabs Scribe Speech-to-Text service for conversation recordings.

Two transcribers share the request/formatting machinery and differ only in
how words are attributed to speakers:

- ElevenLabsMeetingTranscriber: 2-channel recordings (ch0 = user's mic,
  ch1 = system loopback) via Scribe v2 multichannel mode. Attribution comes
  from the channel itself, so "you" vs "them" is always correct.
- ElevenLabsDiarizedTranscriber: mono recordings where all speakers share
  the mic (phone-on-speaker, in-person). Attribution comes from Scribe's
  voice diarization, labeled "Speaker 1", "Speaker 2", ... in order of
  first appearance.
"""
import io
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, Union

import requests
import soundfile as sf

logger = logging.getLogger('voice_typing')

ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"

# Words from the same speaker closer than this (seconds) are grouped into one
# utterance; larger gaps start a new line. Keeps overlapping backchannels
# ("yeah", "right") from chopping the other speaker's sentence mid-word.
UTTERANCE_GAP_S = 1.2


def _prepare_upload(filename: Union[str, Path]) -> io.BytesIO:
    """Re-encode the recording as FLAC to roughly halve upload size/latency."""
    data, samplerate = sf.read(filename, dtype='float32')
    buffer = io.BytesIO()
    sf.write(buffer, data, samplerate, format='FLAC', subtype='PCM_16')
    buffer.seek(0)
    buffer.name = "audio.flac"
    return buffer


class _ScribeTranscriberBase:
    """Shared Scribe v2 request handling and speaker-labeled formatting."""

    def __init__(self, timeout: float = 120.0):
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY not found in environment variables")
        self.api_key = api_key
        self.timeout = timeout
        self.model = "scribe_v2"
        self.session = requests.Session()
        # When False, utterances keep their one-line-per-turn structure but
        # drop the "Name: " prefix (used when labels would be unreliable)
        self.include_labels = True

    def _request_data(self) -> dict:
        """Mode-specific request parameters (multichannel vs diarization)."""
        raise NotImplementedError

    def _speaker_key(self, word: dict):
        """Attribution key for a word (channel index or diarized speaker id)."""
        raise NotImplementedError

    def _speaker_labels(self, keys_in_order: list) -> dict:
        """Map attribution keys (in order of first appearance) to display labels."""
        raise NotImplementedError

    def _labels_enabled(self, keys_in_order: list) -> bool:
        """Whether this result's lines get 'Name: ' prefixes (may depend on keys)."""
        return self.include_labels

    def transcribe(self, filename: Union[str, Path]) -> str:
        start_time = time.time()
        buffer = _prepare_upload(filename)

        response = self.session.post(
            ELEVENLABS_STT_URL,
            headers={"xi-api-key": self.api_key},
            files={"file": (buffer.name, buffer, "audio/flac")},
            data={
                "model_id": self.model,
                "language_code": "eng",
                "tag_audio_events": "false",
                "no_verbatim": "true",
                **self._request_data(),
            },
            timeout=self.timeout,
        )
        if not response.ok:
            # ElevenLabs returns 401 for quota exhaustion — surface the real reason
            try:
                detail = response.json()["detail"]["message"]
            except Exception:
                detail = response.text[:300] or response.reason
            raise RuntimeError(f"ElevenLabs API error {response.status_code}: {detail}")

        result = response.json()
        transcript = self._build_labeled_transcript(result)
        logger.info(
            f"ElevenLabs transcription ({type(self).__name__}) completed in "
            f"{time.time() - start_time:.1f}s ({len(transcript)} chars)"
        )
        return transcript

    def _build_labeled_transcript(self, result: dict) -> str:
        """Group words into per-speaker utterances, interleave by start time."""
        words = [w for w in result.get("words", []) if w.get("type") == "word"]
        if not words:
            return result.get("text", "")

        keys_in_order: list = []
        utterances = []
        for w in words:
            key = self._speaker_key(w)
            if key not in keys_in_order:
                keys_in_order.append(key)

        for key in keys_in_order:
            current = None
            for w in (w for w in words if self._speaker_key(w) == key):
                if current and w["start"] - current["end"] <= UTTERANCE_GAP_S:
                    current["text"].append(w["text"])
                    current["end"] = w["end"]
                else:
                    if current:
                        utterances.append(current)
                    current = {"key": key, "start": w["start"],
                               "end": w["end"], "text": [w["text"]]}
            if current:
                utterances.append(current)

        names = self._speaker_labels(keys_in_order)
        labels_on = self._labels_enabled(keys_in_order)
        utterances.sort(key=lambda u: u["start"])
        lines = []
        for u in utterances:
            prefix = names[u["key"]] + ": " if labels_on else ""
            lines.append(prefix + " ".join(u["text"]))
        return "\n".join(lines)


class ElevenLabsMeetingTranscriber(_ScribeTranscriberBase):
    """Multichannel transcriber: speaker attribution by recording channel."""

    def __init__(self, you_label: str = "Me", them_label: str = "Them",
                 timeout: float = 120.0):
        super().__init__(timeout=timeout)
        self.you_label = you_label
        self.them_label = them_label

    def _request_data(self) -> dict:
        return {
            "diarize": "false",
            "use_multi_channel": "true",
            "multichannel_output_style": "combined",
        }

    def _speaker_key(self, word: dict):
        return word.get("channel_index")

    def _speaker_labels(self, keys_in_order: list) -> dict:
        names = {0: self.you_label, 1: self.them_label}
        return {k: names.get(k, f"Speaker {k}") for k in keys_in_order}


class ElevenLabsDiarizedTranscriber(_ScribeTranscriberBase):
    """Diarizing transcriber for mono recordings with multiple speakers.

    Voice diarization can't know which voice is the user on its own, so by
    default (labeled=False) output is one unattributed line per speaker turn —
    generic per-request labels would swap identities between session chunks.

    The exception is the ElevenLabs workspace speaker library: with
    use_speaker_library, words from voices enrolled in the library come back
    with the registered speaker ID (e.g. 'dimitri-sudomoin') instead of
    'speaker_N' (verified empirically 2026-07-22). When my_speaker_id is set
    and matched, labels turn on: that voice is you_label ('Me') and unmatched
    voices are them_label ('Them'), stable across chunks.
    """

    _GENERIC_ID = re.compile(r"speaker_\d+")

    def __init__(self, num_speakers: Optional[int] = 2, labeled: bool = True,
                 my_speaker_id: Optional[str] = None,
                 you_label: str = "Me", them_label: str = "Them",
                 use_speaker_library: bool = True,
                 diarization_threshold: Optional[float] = None,
                 timeout: float = 120.0):
        super().__init__(timeout=timeout)
        self.num_speakers = num_speakers
        self.include_labels = labeled
        self.my_speaker_id = my_speaker_id
        self.you_label = you_label
        self.them_label = them_label
        self.use_speaker_library = use_speaker_library
        self.diarization_threshold = diarization_threshold

    def _request_data(self) -> dict:
        data = {"diarize": "true"}
        if self.use_speaker_library:
            data["use_speaker_library"] = "true"
        # The API treats num_speakers and diarization_threshold as mutually
        # exclusive; threshold wins when set. Empirically (2026-07-22, seeded
        # sweep 0.1-0.4 on a 4-speaker meeting recording) the threshold also
        # gates speaker-library match ACCEPTANCE: clustering was identical at
        # every value, but the enrolled speaker's cluster only matched at
        # >=0.26 (~98% word accuracy, plateau through 0.4) while the default
        # ~0.22 rejected the match. 0.3 sits mid-plateau with margin, without
        # maxing the knob (docs say higher risks merging distinct speakers).
        if self.diarization_threshold is not None:
            data["diarization_threshold"] = str(self.diarization_threshold)
        elif self.num_speakers:
            data["num_speakers"] = str(self.num_speakers)
        return data

    def _speaker_key(self, word: dict):
        return word.get("speaker_id")

    def _is_library_match(self, key) -> bool:
        return key is not None and not self._GENERIC_ID.fullmatch(str(key))

    def _labels_enabled(self, keys_in_order: list) -> bool:
        # A library match makes labels meaningful even when generic labels
        # are configured off
        return (self.include_labels or
                any(self._is_library_match(k) for k in keys_in_order))

    def _speaker_labels(self, keys_in_order: list) -> dict:
        me_matched = (self.my_speaker_id is not None and
                      self.my_speaker_id in keys_in_order)
        unmatched = [k for k in keys_in_order if not self._is_library_match(k)]
        labels = {}
        generic_count = 0
        for k in keys_in_order:
            if me_matched and k == self.my_speaker_id:
                labels[k] = self.you_label
            elif self._is_library_match(k):
                # Some other enrolled speaker: prettify their registered slug
                labels[k] = str(k).replace('-', ' ').replace('_', ' ').title()
            elif me_matched:
                generic_count += 1
                labels[k] = (self.them_label if len(unmatched) == 1
                             else f"{self.them_label} {generic_count}")
            else:
                generic_count += 1
                labels[k] = f"Speaker {generic_count}"
        return labels
