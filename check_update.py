from typing import Tuple, Optional, List
import requests
import os
import sys
import shutil
import zipfile
import tempfile
from pathlib import Path
import subprocess

def get_latest_release() -> Tuple[Optional[str], Optional[str]]:
    """Get the latest release version and download URL from GitHub."""
    try:
        response = requests.get(
            "https://api.github.com/repos/Elevate-Code/better-voice-typing/releases/latest"
        )
        response.raise_for_status()
        data = response.json()
        return data["tag_name"], data["zipball_url"]
    except Exception as e:
        print(f"Error checking for updates: {e}")
        return None, None

def get_current_version() -> str:
    """Read current version from version.txt."""
    try:
        with open("version.txt", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "0.0.0"

def backup_user_files(backup_dir: Path, preserve_items: List[str]) -> bool:
    """Backup important user files and return success status."""
    try:
        backup_dir.mkdir(exist_ok=True)
        for item in preserve_items:
            src = Path.cwd() / item
            if src.exists():
                dst = backup_dir / item
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
        return True
    except Exception as e:
        print(f"Error backing up files: {e}")
        return False

def restore_user_files(backup_dir: Path, preserve_items: List[str]) -> None:
    """Restore user files from backup."""
    for item in preserve_items:
        backup_path = backup_dir / item
        if backup_path.exists():
            dest = Path.cwd() / item
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            if backup_path.is_dir():
                shutil.copytree(backup_path, dest)
            else:
                shutil.copy2(backup_path, dest)

def download_and_extract(url: str, extract_dir: Path) -> bool:
    """Download and extract the latest release."""
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()

        zip_path = extract_dir / "update.zip"
        with open(zip_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

        return True
    except Exception as e:
        print(f"Error downloading or extracting: {e}")
        return False

def update_files(extract_dir: Path, preserve_items: List[str]) -> bool:
    """Update application files while preserving user data."""
    try:
        # Get the extracted folder name (usually includes the repo name and commit hash)
        extracted_folder = next(p for p in extract_dir.iterdir() if p.is_dir())

        # Update files while preserving user data
        for item in extracted_folder.iterdir():
            if item.name != '.venv' and item.name not in preserve_items:
                dest = Path.cwd() / item.name
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
        return True
    except Exception as e:
        print(f"Error updating files: {e}")
        return False

def update_dependencies() -> bool:
    """Attempt to update dependencies using uv."""
    try:
        # Check if uv is available
        subprocess.run(["uv", "--version"], check=True, capture_output=True)

        # Run dependency update
        print("Updating dependencies...")
        result = subprocess.run(
            ["uv", "pip", "install", "-r", "requirements.txt"],
            check=False,
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            print("Dependencies updated successfully!")
            return True
        else:
            print(f"Failed to update dependencies automatically: {result.stderr}")
            return False
    except FileNotFoundError:
        print("UV tool not found in PATH. Skipping automatic dependency update.")
        return False
    except subprocess.CalledProcessError:
        print("Error checking UV version. Skipping automatic dependency update.")
        return False
    except Exception as e:
        print(f"Unexpected error updating dependencies: {e}")
        return False

def update_app() -> bool:
    """Check for and apply updates if available."""
    current = get_current_version()
    latest, download_url = get_latest_release()

    if not latest or not download_url:
        return False

    # Release tags carry a 'v' prefix (e.g. 'v0.8.0') while version.txt
    # stores the bare number — compare them normalized, or "up to date"
    # would never trigger and every check would reinstall the app
    if latest.lstrip('vV') == current.lstrip('vV'):
        print("Already up to date!")
        return True

    print(f"Updating from version {current} to {latest}")

    # List of files/folders to preserve
    preserve_items = [
        '.env',                    # API keys and user settings
        'settings.json',           # Any additional user settings
        'modules/settings.json'    # Legacy settings location (pre-0.7); newer versions store settings in Documents\VoiceTyping
    ]

    with tempfile.TemporaryDirectory() as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        backup_dir = temp_dir / "backup"
        extract_dir = temp_dir / "extracted"
        extract_dir.mkdir()

        # Backup important files
        if not backup_user_files(backup_dir, preserve_items):
            return False

        # Download and extract new version
        if not download_and_extract(download_url, extract_dir):
            return False

        # Update files
        if not update_files(extract_dir, preserve_items):
            # Restore from backup on failure
            restore_user_files(backup_dir, preserve_items)
            return False

        # Restore user files from backup
        restore_user_files(backup_dir, preserve_items)

        print("\n✅ Files updated successfully!")

        # Try to update dependencies automatically
        print("\n📦 Checking & updating dependencies...")
        deps_updated = update_dependencies()

        # Post-update information
        print("\n📋 Post-update checklist:")
        print("----------------------------------------")
        print("1. Check your .env file against .env.example for anything new/updated")
        print("2. If .env.example has new variables, add/update them in your .env file")

        if not deps_updated:
            print("3. Update dependencies manually by running:")
            print("  uv pip install -r requirements.txt")

        print("\n🎉 Update completed successfully!")
        return True

if __name__ == "__main__":
    success = update_app()
    sys.exit(0 if success else 1)