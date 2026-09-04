"""Crash-safe JSON persistence (modules/fileutil.py).

Contract: settings.json and history.json are the only copies of the user's
configuration and dictations. A write either fully lands or leaves the
previous file byte-for-byte intact, the previous version survives as .bak,
and loading recovers from .bak when the primary is missing or corrupt.
"""
import json
import os
from pathlib import Path

import pytest

from modules.fileutil import backup_path, read_json_with_backup, write_json_atomic


def test_round_trip_creates_parents_and_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "settings.json"
    write_json_atomic(target, {"a": 1, "name": "Ünïcode"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "name": "Ünïcode"}
    assert list(target.parent.iterdir()) == [target]


def test_overwrite_keeps_previous_version_as_backup(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    write_json_atomic(target, {"v": 1})
    write_json_atomic(target, {"v": 2})
    assert json.loads(target.read_text()) == {"v": 2}
    assert json.loads(backup_path(target).read_text()) == {"v": 1}


def test_unserializable_data_raises_and_leaves_file_untouched(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    write_json_atomic(target, {"v": 1})
    before = target.read_bytes()
    with pytest.raises(TypeError):
        write_json_atomic(target, {"bad": object()})
    assert target.read_bytes() == before
    assert not (tmp_path / "settings.json.tmp").exists()


def test_failed_swap_leaves_file_untouched_and_cleans_temp(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crash (or lock) at the very last step: the old file must survive."""
    target = tmp_path / "settings.json"
    write_json_atomic(target, {"v": 1})
    before = target.read_bytes()

    def explode(src: str, dst: str) -> None:
        raise PermissionError("locked by another process")
    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(PermissionError):
        write_json_atomic(target, {"v": 2})
    assert target.read_bytes() == before
    assert not (tmp_path / "settings.json.tmp").exists()


def test_read_falls_back_to_backup_when_primary_is_corrupt(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    write_json_atomic(target, {"v": 1})
    write_json_atomic(target, {"v": 2})
    target.write_text("{ torn write", encoding="utf-8")
    assert read_json_with_backup(target, default={}) == {"v": 1}


def test_read_falls_back_to_backup_when_primary_is_missing(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    write_json_atomic(target, {"v": 1})
    write_json_atomic(target, {"v": 2})
    target.unlink()
    assert read_json_with_backup(target, default={}) == {"v": 1}


def test_read_returns_default_when_nothing_usable_exists(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    assert read_json_with_backup(target, default={"fresh": True}) == {"fresh": True}
    target.write_text("nope", encoding="utf-8")
    backup_path(target).write_text("also nope", encoding="utf-8")
    assert read_json_with_backup(target, default=[]) == []
