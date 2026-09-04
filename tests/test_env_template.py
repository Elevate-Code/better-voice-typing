"""The shipped .env template and real-world .env shapes (modules/env_file.py,
modules/settings.py loading rules).

Contract: the settings window must read the template the app itself
creates as four *empty* keys (not the quotes-plus-comment text), an empty
template entry must never blank out a key set in the Windows environment,
and editing a key with duplicate definitions must not leave a stale copy
that wins again on the next launch. Line endings are preserved.
"""
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from modules import env_file

TEMPLATE = Path(__file__).resolve().parents[1] / '.env.example'


def test_template_reads_as_empty_keys() -> None:
    values = env_file.read_env(TEMPLATE)
    assert set(values) == set(env_file.KEY_NAMES)
    assert all(v == '' for v in values.values())
    assert all(env_file.is_placeholder(v) for v in values.values())


def test_empty_template_entries_do_not_override_environment(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    # Mirrors Settings._load_env_files: only non-empty file values win
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-from-windows')
    for name, value in dotenv_values(TEMPLATE).items():
        if value:
            os.environ[name] = value
    assert os.environ['OPENAI_API_KEY'] == 'sk-from-windows'


def test_duplicate_definitions_collapse_to_the_new_value(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    path = tmp_path / '.env'
    path.write_text("OPENAI_API_KEY=first\nOTHER=1\nOPENAI_API_KEY=old\n", encoding='utf-8')
    env_file.update_env({'OPENAI_API_KEY': 'new'}, path)
    text = path.read_text(encoding='utf-8')
    assert text.count('OPENAI_API_KEY') == 1
    assert dotenv_values(path)['OPENAI_API_KEY'] == 'new'
    assert 'OTHER=1' in text


def test_line_endings_are_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    path = tmp_path / '.env'
    path.write_bytes(b"# keys\r\nOPENAI_API_KEY=\r\n")
    env_file.update_env({'OPENAI_API_KEY': 'sk-x'}, path)
    assert path.read_bytes() == b"# keys\r\nOPENAI_API_KEY=sk-x\r\n"
