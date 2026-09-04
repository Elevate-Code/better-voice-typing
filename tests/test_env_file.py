"""API key file editing (modules/env_file.py).

Contract: the settings window rewrites the user's .env in place. It must
change only the requested keys, keep comments and unrelated lines byte for
byte, treat template placeholders ('sk-...') as unset, and mirror the
change into the process environment so the new key is used immediately.
"""
import os
from pathlib import Path

import pytest

from modules import env_file


@pytest.fixture
def env_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / '.env'
    path.write_text(
        "# Keys for Better Voice Typing\n"
        "OPENAI_API_KEY=sk-...\n"
        "ELEVENLABS_API_KEY=\"sk_real\"\n"
        "UNRELATED=keep me\n",
        encoding='utf-8')
    for name in env_file.KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    return path


def test_read_strips_quotes_and_keeps_placeholders(env_path: Path) -> None:
    values = env_file.read_env(env_path)
    assert values['ELEVENLABS_API_KEY'] == 'sk_real'
    assert values['OPENAI_API_KEY'] == 'sk-...'
    assert env_file.is_placeholder(values['OPENAI_API_KEY'])
    assert not env_file.is_placeholder('sk_real')


def test_update_replaces_in_place_and_preserves_other_lines(env_path: Path) -> None:
    env_file.update_env({'OPENAI_API_KEY': 'sk-new', 'CUSTOM_STT_API_KEY': 'abc'}, env_path)
    lines = env_path.read_text(encoding='utf-8').splitlines()
    assert lines[0] == "# Keys for Better Voice Typing"
    assert lines[1] == "OPENAI_API_KEY=sk-new"
    assert lines[2] == 'ELEVENLABS_API_KEY="sk_real"'
    assert lines[3] == "UNRELATED=keep me"
    assert lines[4] == "CUSTOM_STT_API_KEY=abc"  # appended, not duplicated
    assert os.environ['OPENAI_API_KEY'] == 'sk-new'
    assert os.environ['CUSTOM_STT_API_KEY'] == 'abc'


def test_clearing_a_key_empties_line_and_environment(env_path: Path) -> None:
    os.environ['ELEVENLABS_API_KEY'] = 'sk_real'
    env_file.update_env({'ELEVENLABS_API_KEY': ''}, env_path)
    assert "ELEVENLABS_API_KEY=" in env_path.read_text(encoding='utf-8').splitlines()
    assert 'ELEVENLABS_API_KEY' not in os.environ


def test_update_creates_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    path = tmp_path / 'sub' / '.env'
    env_file.update_env({'OPENAI_API_KEY': 'sk-x'}, path)
    assert env_file.read_env(path) == {'OPENAI_API_KEY': 'sk-x'}
