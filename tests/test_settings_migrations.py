"""Settings-schema migrations (modules/settings_migrations.py).

Contract: every settings.json shape a real user may still have on disk
loads into the current schema without losing their choices, the result is
idempotent (a migrated file migrates to itself with no changes), and unknown
keys survive so a downgrade-then-upgrade never drops data.
"""
import copy

from modules.settings_migrations import SCHEMA_VERSION, migrate

# A real pre-1.0 file (schema 0) with every legacy shape at once
LEGACY = {
    "silence_timeout": 3.0,
    "stt_provider": "google",
    "google_stt_language": "en-US",
    "continuous_capture": True,
    "smart_capture": False,
    "output_mode": "chunked_terminal",
    "llm_model": "openai/gpt-4o-mini",
    "selected_microphone": 3,
    "favorite_microphones": [3, {"name": "Headset", "channels": 1, "default_samplerate": 44100.0}],
    "ui_indicator_position": "bottom-center",
    "some_future_key": {"kept": True},
}


def test_legacy_file_lands_on_current_schema_with_choices_intact() -> None:
    data, notes = migrate(LEGACY)
    assert notes
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["silent_start_timeout"] == 3.0 and "silence_timeout" not in data
    # google -> openai (removed provider) -> automatic (old hardcoded default)
    assert data["stt_provider"] is None
    assert data["llm_model"] == "gpt-4o-mini"
    assert data["selected_microphone"] is None
    assert data["favorite_microphones"] == [{"name": "Headset", "channels": 1, "default_samplerate": 44100.0}]
    assert data["ui_indicator_position"] == "bottom-center"
    assert data["some_future_key"] == {"kept": True}
    for gone in ("google_stt_language", "continuous_capture", "smart_capture", "output_mode"):
        assert gone not in data


def test_migration_is_idempotent_and_does_not_mutate_input() -> None:
    original = copy.deepcopy(LEGACY)
    once, _ = migrate(LEGACY)
    assert LEGACY == original
    twice, notes = migrate(once)
    assert twice == once
    assert notes == []


def test_old_openai_default_moves_to_automatic_unless_already_moved() -> None:
    data, _ = migrate({"stt_provider": "openai"})
    assert data["stt_provider"] is None
    explicit, _ = migrate({"stt_provider": "openai", "migrated_default_elevenlabs": True})
    assert explicit["stt_provider"] == "openai"
    assert "migrated_default_elevenlabs" not in explicit
    eleven, _ = migrate({"stt_provider": "elevenlabs"})
    assert eleven["stt_provider"] == "elevenlabs"


def test_unsupported_llm_provider_falls_back_with_a_note() -> None:
    data, notes = migrate({"llm_model": "anthropic/claude-3-5-haiku-latest"})
    assert data["llm_model"] == "gpt-4o-mini"
    assert any("not supported" in n for n in notes)
    bare, _ = migrate({"llm_model": "gpt-4.1-mini"})
    assert bare["llm_model"] == "gpt-4.1-mini"


def test_fresh_or_garbage_version_is_treated_as_unversioned() -> None:
    data, notes = migrate({})
    assert data == {"schema_version": SCHEMA_VERSION}
    assert notes == [f"schema_version set to {SCHEMA_VERSION}"]
    weird, _ = migrate({"schema_version": "one", "silence_timeout": 2.0})
    assert weird["silent_start_timeout"] == 2.0


def test_current_schema_needs_no_save() -> None:
    _, notes = migrate({"schema_version": SCHEMA_VERSION, "stt_provider": "openai"})
    assert notes == []
