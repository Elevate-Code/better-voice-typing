"""Optional LLM pass that tidies a raw dictation transcript.

Talks to the OpenAI chat completions API directly (or any OpenAI-compatible
server via ``base_url``). Before 1.0 this went through LiteLLM, which cost
~60 MB of dependencies and a multi-second first import for one call.
"""
import logging
import os
from typing import Optional

from modules.settings import Settings

logger = logging.getLogger('voice_typing')

CLEANING_PROMPT = """
Improve transcription clarity by making minimal edits to fix:
- Fragmented sentences
- Filler words ("uh", "um")
- Obvious grammatical errors
It's crucial to preserve the original meaning and speaker's intent. When in doubt, keep the original text.

**Examples of Acceptable Edits**

1. Removing filler words while preserving meaning:
ORIGINAL: Is there a way to programmatically add, uh, that is to say using the Slack API or something like that, um, add or invite people using their email as guests to a specific private channel?
IMPROVED: Is there a way to programmatically (that is to say using the Slack API or something like that) add or invite people using their email as guests to a specific private channel?

ORIGINAL: So like how we're logging starting voice typing application can we also log out like the text cleaning or model that's gonna be used like LLM model
IMPROVED: So, how we're logging starting voice typing application can we also log out the text cleaning or the LLM model that's gonna be used?

2. Preserving uncertainty while improving clarity:
ORIGINAL: Okay, so I want to add logging, I guess. I'm not sure. Yeah, let's add logging as a feature to this app. Okay.
IMPROVED: I want to add logging, I guess. Yeah, let's add logging as a feature to this app.

3. Handling sentence fragments:
ORIGINAL: Or sometime soon.
IMPROVED: or sometime soon

4. Minimal punctuation fixes:
ORIGINAL: If you think we need the cheese then go to the store.
IMPROVED: If you think we need the cheese, then go to the store.

5. Adding dictated punctuation (parentheses, dot dot dot, quotes, etc.):
ORIGINAL: His wife is a software, parenthesis, web development, engineer, and they occasionally share an account, dot, dot, dot.
IMPROVED: His wife is a software (web development) engineer, and they occasionally share an account...

6. Selectively fixing run-on sentences, while avoiding over-correction on unclear statements:
ORIGINAL: I guess I'm more so looking something that includes the word clockify at the start And then says essentially casual Description to Entry something give me variations on that
IMPROVED: I guess I'm more looking for something that includes the word "Clockify" at the start and then says essentially casual description to entry something. Give me variations on that.

**Transcription Text**

<transcription_text>
{}
</transcription_text>

IMPORTANT: Respond only with the corrected transcription text, nothing else. So the first word of your response should be the first word of the transcription, and the last word of your response should be the last word of the transcription.
""".strip()


def clean_transcription(text: str, model: str, timeout: float = 45.0,
                        base_url: Optional[str] = None) -> str:
    """Return ``text`` with minimal LLM cleanup, or raise so the caller can
    fall back to the raw transcript.

    Args:
        text: raw transcription
        model: OpenAI chat model name, e.g. 'gpt-4o-mini'
        timeout: seconds for the whole request (two retries inside the SDK)
        base_url: OpenAI-compatible server root; None = api.openai.com
    """
    # Deferred import: the OpenAI SDK is a heavy import and cleaning is
    # optional, so don't pay for it at startup
    from openai import OpenAI

    # LLM_API_KEY lets a compatible server (or Anthropic's OpenAI-compatible
    # endpoint) use its own key without touching the STT key
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key and not base_url:
        raise ValueError("OPENAI_API_KEY environment variable not set")

    log_text = Settings().get('log_transcript_text')
    if log_text:
        logger.info("ORIGINAL: %s", text)

    client = OpenAI(api_key=api_key or "not-needed", base_url=base_url,
                    timeout=timeout, max_retries=2)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": CLEANING_PROMPT.format(text)}],
        temperature=0.2,
    )
    cleaned_text = response.choices[0].message.content if response.choices else None
    if not cleaned_text:
        logger.warning("Empty LLM response – falling back to raw text")
        cleaned_text = text

    if log_text:
        logger.info("IMPROVED: %s", cleaned_text)
    return cleaned_text
