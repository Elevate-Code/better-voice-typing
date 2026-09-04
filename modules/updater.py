"""Installer-based self-update for the packaged app.

Flow (frozen builds only):
1. Ask the GitHub Releases API for the latest release.
2. If its version is newer than ours, download the versioned installer asset
   to the update cache and verify its SHA-256 against the SHA256SUMS asset
   the release workflow publishes next to it.
3. Write a tiny helper script that waits for this process to exit, runs the
   installer silently, and relaunches the app; start it detached and exit.

The app never overwrites its own program folder — Inno Setup does, once the
process (and its single-instance mutex) is gone. Source checkouts don't
self-update; "Check for Updates" opens the releases page instead.

Network I/O uses urllib so the app needs no HTTP library beyond what the
STT providers already bring.
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from modules.paths import EXE_NAME, FROZEN, INSTALL_DIR, UPDATE_CACHE_DIR, app_version

logger = logging.getLogger('voice_typing')

GITHUB_REPO = "Elevate-Code/better-voice-typing"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"
LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
INSTALLER_PATTERN = re.compile(r"^BetterVoiceTyping-Setup-(\d+\.\d+\.\d+)\.exe$")
CHECKSUMS_ASSET = "SHA256SUMS.txt"
HTTP_TIMEOUT_S = 15


@dataclass
class Release:
    version: str            # bare, e.g. '1.2.0'
    installer_url: str
    installer_name: str
    checksums_url: Optional[str]
    notes: str


def parse_version(text: str) -> Tuple[int, ...]:
    """'v1.2.0' / '1.2.0' -> (1, 2, 0). Anything unparseable sorts lowest."""
    match = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        return (0,)
    return tuple(int(part) for part in match.groups())


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def _get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    request = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": f"better-voice-typing/{app_version()}",
    })
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
        return response.read()


def fetch_latest_release() -> Optional[Release]:
    """Latest release with a Windows installer asset, or None when the API
    is unreachable or the release carries no installer."""
    data = json.loads(_get(LATEST_API).decode("utf-8"))
    installer = next((a for a in data.get("assets", [])
                      if INSTALLER_PATTERN.match(a.get("name", ""))), None)
    if installer is None:
        return None
    checksums = next((a for a in data.get("assets", [])
                      if a.get("name") == CHECKSUMS_ASSET), None)
    return Release(
        version=parse_version_string(data.get("tag_name", "")),
        installer_url=installer["browser_download_url"],
        installer_name=installer["name"],
        checksums_url=checksums["browser_download_url"] if checksums else None,
        notes=data.get("body") or "",
    )


def parse_version_string(tag: str) -> str:
    return ".".join(str(n) for n in parse_version(tag)) if parse_version(tag) != (0,) else "0.0.0"


def expected_sha256(checksums_text: str, filename: str) -> Optional[str]:
    """Pull one file's digest out of a SHA256SUMS-style listing."""
    for line in checksums_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
            return parts[0].lower()
    return None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download_installer(release: Release) -> Path:
    """Download the installer to the update cache and verify its checksum.

    Raises RuntimeError on a checksum mismatch (the file is deleted) and on
    a release that publishes no checksums (refuse to run unverified code)."""
    if not release.checksums_url:
        raise RuntimeError("release has no SHA256SUMS.txt; refusing unverified installer")
    expected = expected_sha256(_get(release.checksums_url, "text/plain").decode("utf-8"),
                               release.installer_name)
    if not expected:
        raise RuntimeError(f"{release.installer_name} missing from SHA256SUMS.txt")

    UPDATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = UPDATE_CACHE_DIR / release.installer_name
    if target.exists() and sha256_of(target) == expected:
        return target  # already downloaded and verified
    tmp = target.with_suffix(".part")
    with open(tmp, "wb") as f:
        f.write(_get(release.installer_url, "application/octet-stream"))
    actual = sha256_of(tmp)
    if actual != expected:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"installer checksum mismatch (expected {expected[:12]}…, got {actual[:12]}…)")
    os.replace(tmp, target)
    # Older downloads are dead weight
    for old in UPDATE_CACHE_DIR.glob("BetterVoiceTyping-Setup-*.exe"):
        if old != target:
            try:
                old.unlink()
            except OSError:
                pass
    return target


def helper_script(pid: int, installer: Path, exe: Path) -> str:
    """Batch script: wait for ``pid`` to exit, install silently, relaunch."""
    return "\r\n".join([
        "@echo off",
        ":wait",
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL',
        "if not errorlevel 1 (",
        "    timeout /t 1 /nobreak >NUL",
        "    goto wait",
        ")",
        f'"{installer}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS',
        f'start "" "{exe}"',
        "",
    ])


def launch_installer_and_exit(installer: Path) -> None:
    """Start the detached update helper and terminate this process."""
    if not FROZEN:
        raise RuntimeError("self-update is only available in the installed app")
    script = UPDATE_CACHE_DIR / "apply-update.cmd"
    script.write_text(helper_script(os.getpid(), installer, INSTALL_DIR / EXE_NAME),
                      encoding="utf-8")
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
        getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(["cmd.exe", "/c", str(script)], creationflags=creation_flags,
                     close_fds=True, cwd=str(UPDATE_CACHE_DIR))
    logger.info(f"Update helper started for {installer.name}; exiting to let it install")
    logging.shutdown()
    os._exit(0)


def open_releases_page() -> None:
    import webbrowser
    webbrowser.open(RELEASES_PAGE)


def current_version() -> str:
    return app_version()


def running_as_installed() -> bool:
    return FROZEN and sys.platform == "win32"
