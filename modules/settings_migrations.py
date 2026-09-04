"""Pure, versioned settings-schema migrations.

``migrate(stored)`` takes the raw dict read from settings.json and returns a
copy brought up to ``SCHEMA_VERSION`` plus a list of human-readable notes
describing what changed (empty = nothing to save). It runs BEFORE defaults
are merged, never touches disk or hardware, and is idempotent, so
``tests/test_settings_migrations.py`` can pin every historical shape.

Anything that depends on the machine (resolving saved microphones against
the devices present right now) is NOT a migration; that lives in
``Settings._reconcile_devices`` and reruns every launch.

Versions:
- 0: every settings.json written before 1.0 (no ``schema_version`` key).
- 1: 1.0 — all pre-1.0 one-shot migrations folded into one step.
"""
from typing import Any, Dict, List, Tuple

SCHEMA_VERSION = 1

DEFAULT_LLM_MODEL = 'gpt-4o-mini'

OBSOLETE_KEYS = (
    'continuous_capture',
    'smart_capture',                # never-implemented feature stub, removed 2026-07
    'google_stt_language',          # Google STT provider removed 2026-07
    'output_mode',                  # output-provider plugins removed 2026-09 (1.0)
    'migrated_default_elevenlabs',  # pre-1.0 marker, superseded by schema_version
)


def migrate(stored: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Return ``(migrated copy, notes)``; ``notes`` is empty when nothing changed."""
    data = dict(stored)
    notes: List[str] = []
    version = data.get('schema_version')
    if not isinstance(version, int) or version < 0:
        version = 0
    if version < 1:
        _to_v1(data, notes)
    if data.get('schema_version') != SCHEMA_VERSION:
        data['schema_version'] = SCHEMA_VERSION
        notes.append(f"schema_version set to {SCHEMA_VERSION}")
    return data, notes


def _to_v1(data: Dict[str, Any], notes: List[str]) -> None:
    # 2025: 'silence_timeout' became 'silent_start_timeout'
    if 'silence_timeout' in data:
        data['silent_start_timeout'] = data.pop('silence_timeout')
        notes.append("renamed silence_timeout -> silent_start_timeout")

    # Google STT was removed (2026-07); its users fall back to OpenAI
    if data.get('stt_provider') == 'google':
        data['stt_provider'] = 'openai'
        notes.append("stt_provider 'google' is gone; using 'openai'")

    # 2026-07: the default STT provider became automatic (null = ElevenLabs
    # when its key is configured, else OpenAI). Settings still pinned to the
    # old hardcoded 'openai' default move to automatic, unless the pre-1.0
    # marker says that move already happened and 'openai' is a later, explicit
    # choice.
    already_moved = bool(data.get('migrated_default_elevenlabs'))
    if not already_moved and data.get('stt_provider') == 'openai':
        data['stt_provider'] = None
        notes.append("stt_provider 'openai' (old default) -> automatic")

    # 1.0: transcript cleaning uses the OpenAI API directly instead of
    # LiteLLM, so llm_model is a bare OpenAI model name
    model = data.get('llm_model')
    if isinstance(model, str) and '/' in model:
        provider, _, bare = model.partition('/')
        if provider == 'openai' and bare:
            data['llm_model'] = bare
            notes.append(f"llm_model '{model}' -> '{bare}'")
        else:
            data['llm_model'] = DEFAULT_LLM_MODEL
            notes.append(f"llm_model '{model}' is not supported any more (cleaning is "
                         f"OpenAI-only; set llm_base_url for a compatible server); "
                         f"using '{DEFAULT_LLM_MODEL}'")

    # 2025: microphones were saved as PortAudio device indexes, which shift
    # between launches. 1.0 no longer resolves them: the selection is cleared
    # (the default microphone is used until one is picked from the tray)
    if isinstance(data.get('selected_microphone'), int):
        data['selected_microphone'] = None
        notes.append("selected_microphone was a device index; cleared (pick it again in the tray)")
    favorites = data.get('favorite_microphones')
    if isinstance(favorites, list) and any(isinstance(f, int) for f in favorites):
        data['favorite_microphones'] = [f for f in favorites if not isinstance(f, int)]
        notes.append("dropped favorite microphones saved as device indexes")

    for key in OBSOLETE_KEYS:
        if key in data:
            data.pop(key)
            notes.append(f"removed obsolete '{key}'")
