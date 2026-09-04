"""Crash-safe JSON persistence for user data (settings, history).

A plain ``open(path, 'w')`` + ``json.dump`` truncates the file first and
writes afterwards, so a crash, power loss, or a second writer in between
leaves an empty or half-written file — and settings.json / history.json are
the only copies. ``write_json_atomic`` serializes fully before touching disk,
writes to a sibling temp file, fsyncs, keeps the previous version as ``.bak``,
and swaps with ``os.replace`` (atomic on NTFS). ``read_json_with_backup``
falls back to the ``.bak`` when the primary is missing or unparseable.
"""
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Union

logger = logging.getLogger('voice_typing')

PathLike = Union[str, Path]


def backup_path(path: PathLike) -> Path:
    path = Path(path)
    return path.with_name(path.name + '.bak')


def write_json_atomic(path: PathLike, data: Any, *, indent: int = 4,
                      ensure_ascii: bool = True) -> None:
    """Replace ``path`` with ``data`` as JSON, or leave it exactly as it was.

    Raises whatever serialization or I/O error occurred; on any failure the
    previous file content is untouched and no temp file is left behind.
    """
    path = Path(path)
    # Serialize first: a non-serializable value must not cost the old file
    payload = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            try:
                shutil.copyfile(path, backup_path(path))
            except OSError:
                logger.warning(f"Could not refresh backup for {path.name}", exc_info=True)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_text_atomic(path: PathLike, text: str) -> None:
    """Replace ``path`` with ``text`` (temp file + fsync + rename), keeping a
    .bak of the previous content like write_json_atomic."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            try:
                shutil.copyfile(path, backup_path(path))
            except OSError:
                logger.warning(f"Could not refresh backup for {path.name}", exc_info=True)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_json_with_backup(path: PathLike, default: Any) -> Any:
    """Parse ``path``; if it is missing or corrupt, try its ``.bak``; else ``default``.

    A corrupt primary is logged as an error (that is data loss being averted,
    not a routine event); a missing primary is silent.
    """
    path = Path(path)
    candidates = [(path, logging.ERROR), (backup_path(path), logging.WARNING)]
    for candidate, level in candidates:
        try:
            with open(candidate, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if candidate is not path:
                logger.warning(f"Recovered {path.name} from backup {candidate.name}")
            return data
        except FileNotFoundError:
            continue
        except Exception:
            logger.log(level, f"Could not parse {candidate.name}", exc_info=True)
    return default
