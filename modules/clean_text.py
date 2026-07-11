import logging
from typing import Any, cast

from modules.settings import Settings

# Get logger
logger = logging.getLogger('voice_typing')

# Aggressive silencing of LiteLLM logging
# To work around a compatibility issue: LiteLLM and Python 3.12 (__annotations__ Access Error)
logging.getLogger('LiteLLM').setLevel(logging.CRITICAL + 1)
# see: https://github.com/BerriAI/litellm/issues/9424
# and: https://github.com/BerriAI/litellm/issues/9432

def clean_transcription(text: str, model: str, timeout: float = 45.0) -> str:
    """
    Cleans and corrects voice-to-text transcription using LLM models.

    Args:
        text: The raw transcription text to clean
        model: The LLM model to use for cleaning
        timeout: Maximum time to wait for cleaning (in seconds)
    """
    # Deferred import: litellm is one of the heaviest imports in the app and
    # cleaning is optional, so don't pay for it at startup
    import litellm

    log_text = Settings().get('log_transcript_text')
    if log_text:
        logger.info("ORIGINAL: %s", text)

    prompt = """
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
    """.strip().format(text)

    response_any: Any = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        num_retries=2,
        timeout=timeout
    )

    try:
        # Safely grab the content while satisfying the type checker
        cleaned_text = cast(str, response_any.choices[0].message.content)
    except (AttributeError, IndexError, TypeError):
        logger.warning("Unexpected LLM response shape – falling back to raw text")
        cleaned_text = text

    if log_text:
        logger.info("IMPROVED: %s", cleaned_text)
    return cleaned_text