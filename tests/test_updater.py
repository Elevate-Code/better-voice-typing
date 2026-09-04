"""Installer-based updater (modules/updater.py).

Contract: never run an installer whose checksum doesn't match the release's
published SHA256SUMS; compare versions numerically (so 1.10 > 1.9 and the
tag's 'v' is ignored); the update helper waits for our own PID before
installing and relaunches the installed executable afterwards.
"""
import hashlib
from pathlib import Path

import pytest

from modules import updater
from modules.updater import (
    Release, download_installer, expected_sha256, helper_script, is_newer,
    parse_version, parse_version_string,
)


@pytest.mark.parametrize("candidate, current, newer", [
    ("v1.0.1", "1.0.0", True),
    ("1.10.0", "1.9.9", True),
    ("v1.0.0", "1.0.0", False),
    ("0.9.0", "1.0.0", False),
    ("garbage", "1.0.0", False),
    ("1.0.0", "garbage", True),
])
def test_version_comparison(candidate: str, current: str, newer: bool) -> None:
    assert is_newer(candidate, current) is newer


def test_version_parsing_tolerates_prefix_and_suffix() -> None:
    assert parse_version("v1.2.3-beta") == (1, 2, 3)
    assert parse_version_string("v2.0.0") == "2.0.0"
    assert parse_version_string("nope") == "0.0.0"


def test_expected_sha256_reads_sha256sums_format() -> None:
    listing = ("abc123  BetterVoiceTyping-Setup-1.0.0.exe\n"
               "def456 *other.zip\n")
    assert expected_sha256(listing, "BetterVoiceTyping-Setup-1.0.0.exe") == "abc123"
    assert expected_sha256(listing, "other.zip") == "def456"
    assert expected_sha256(listing, "missing.exe") is None


def make_release() -> Release:
    return Release(version="1.0.1",
                   installer_url="https://example.invalid/installer",
                   installer_name="BetterVoiceTyping-Setup-1.0.1.exe",
                   checksums_url="https://example.invalid/sums",
                   notes="")


def fake_fetcher(payloads: dict):
    def _get(url: str, accept: str = "") -> bytes:
        return payloads[url]
    return _get


def test_download_verifies_checksum_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"MZ fake installer"
    digest = hashlib.sha256(body).hexdigest()
    release = make_release()
    monkeypatch.setattr(updater, "UPDATE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(updater, "_get", fake_fetcher({
        release.installer_url: body,
        release.checksums_url: f"{digest}  {release.installer_name}\n".encode(),
    }))
    path = download_installer(release)
    assert path == tmp_path / release.installer_name
    assert path.read_bytes() == body
    assert not (tmp_path / "BetterVoiceTyping-Setup-1.0.1.part").exists()

    # Second call: cached copy verified, no re-download (fetcher would KeyError)
    monkeypatch.setattr(updater, "_get", fake_fetcher({release.checksums_url:
                                                       f"{digest}  {release.installer_name}\n".encode()}))
    assert download_installer(release) == path


def test_download_rejects_checksum_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = make_release()
    monkeypatch.setattr(updater, "UPDATE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(updater, "_get", fake_fetcher({
        release.installer_url: b"tampered",
        release.checksums_url: f"{'0' * 64}  {release.installer_name}\n".encode(),
    }))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        download_installer(release)
    assert list(tmp_path.iterdir()) == []


def test_download_refuses_release_without_checksums(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = make_release()
    release.checksums_url = None
    monkeypatch.setattr(updater, "UPDATE_CACHE_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="unverified"):
        download_installer(release)


def test_helper_waits_for_our_pid_then_installs_silently_and_relaunches() -> None:
    script = helper_script(4242, Path(r"C:\cache\Setup.exe"), Path(r"C:\app\BetterVoiceTyping.exe"))
    assert 'PID eq 4242' in script
    assert "goto wait" in script
    assert '"C:\\cache\\Setup.exe" /VERYSILENT' in script
    assert script.index("goto wait") < script.index("/VERYSILENT") < script.index("BetterVoiceTyping.exe")
