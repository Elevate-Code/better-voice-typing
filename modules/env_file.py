"""Read and update API keys in the user's .env (Documents\\VoiceTyping\\.env).

The file is edited in place so comments and unrelated lines survive; the
process environment is updated at the same time so a key pasted in the
setup window works immediately, without a restart.
"""
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from modules.fileutil import write_text_atomic
from modules.settings import ENV_FILE

KEY_NAMES = ('OPENAI_API_KEY', 'ELEVENLABS_API_KEY', 'CUSTOM_STT_API_KEY', 'LLM_API_KEY')
_LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def read_env(path: Path = ENV_FILE) -> Dict[str, str]:
    """Key/value pairs from the file (unfilled '...' placeholders included)."""
    values: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.lstrip().startswith('#'):
                continue
            m = _LINE.match(line)
            if m:
                values[m.group(1)] = _strip_quotes(m.group(2))
    except OSError:
        pass
    return values


def is_placeholder(value: Optional[str]) -> bool:
    value = (value or '').strip()
    return not value or value.endswith('...')


def update_env(changes: Dict[str, str], path: Path = ENV_FILE) -> None:
    """Set (or add) each key; an empty value clears it. Other lines are kept
    verbatim. Also applies the change to os.environ."""
    try:
        lines: List[str] = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        lines = []
    pending = dict(changes)
    out: List[str] = []
    for line in lines:
        m = _LINE.match(line) if not line.lstrip().startswith('#') else None
        if m and m.group(1) in pending:
            key = m.group(1)
            value = pending.pop(key)
            out.append(f'{key}={value}' if value else f'{key}=')
        else:
            out.append(line)
    for key, value in pending.items():
        out.append(f'{key}={value}' if value else f'{key}=')
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, '\n'.join(out) + '\n')
    for key, value in changes.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


def validate_key(provider: str, key: str, timeout: float = 10.0) -> Tuple[bool, str]:
    """Cheap authenticated call against the provider. Returns (ok, message)."""
    import httpx
    key = key.strip()
    if not key:
        return False, "No key entered"
    try:
        if provider == 'openai':
            r = httpx.get('https://api.openai.com/v1/models',
                          headers={'Authorization': f'Bearer {key}'}, timeout=timeout)
        elif provider == 'elevenlabs':
            r = httpx.get('https://api.elevenlabs.io/v1/user',
                          headers={'xi-api-key': key}, timeout=timeout)
        else:
            return False, f"Unknown provider {provider}"
    except httpx.HTTPError as e:
        return False, f"Could not reach the API: {type(e).__name__}"
    if r.status_code == 200:
        return True, "Key works"
    if r.status_code in (401, 403):
        return False, "Key rejected (check for typos or a revoked key)"
    return False, f"Unexpected response {r.status_code}"
