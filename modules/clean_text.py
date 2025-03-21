import logging
import litellm

# Get logger
logger = logging.getLogger('voice_typing')

# Aggressive silencing of LiteLLM logging
# To work around a compatibility issue: LiteLLM and Python 3.12 (__annotations__ Access Error)
logging.getLogger('LiteLLM').setLevel(logging.CRITICAL + 1)
# see: https://github.com/BerriAI/litellm/issues/9424
# and: https://github.com/BerriAI/litellm/issues/9432

def clean_transcription(text: str, model: str) -> str:
    """
    Cleans and corrects voice-to-text transcription using LLM models.

    Args:
        text: The raw transcription text to clean
        model: The LLM model to use for cleaning
    """
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
ORIGINAL: My wife is a software, parenthesis, web development, engineer, and we occasionally share an account.
IMPROVED: My wife is a software (web development) engineer, and we occasionally share an account.

6. Selectively fixing run-on sentences, while avoiding over-correction on unclear statements:
ORIGINAL: I guess I'm more so looking something that includes the word clockify at the start And then says essentially casual Description to Entry something give me variations on that
IMPROVED: I guess I'm more looking for something that includes the word "Clockify" at the start and then says essentially casual description to entry something. Give me variations on that.

**Transcription Text**

<transcription_text>
{}
</transcription_text>

IMPORTANT: Respond only with the corrected transcription text, nothing else.
    """.strip().format(text)

    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        num_retries=2
    )

    cleaned_text = response.choices[0].message.content

    logger.info("IMPROVED: %s", cleaned_text)
    return cleaned_text