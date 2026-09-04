"""Release plumbing stays consistent.

Contract: version.txt (what the app reports and the updater compares) and
pyproject.toml agree, and the release workflow refuses a tag that doesn't
match. A mismatch here would ship an installer that thinks it is a
different version than its release.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_version_txt_matches_pyproject() -> None:
    version = (ROOT / "version.txt").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert match and match.group(1) == version
