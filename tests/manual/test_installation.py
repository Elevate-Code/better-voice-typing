from pathlib import Path
import shutil
import subprocess
import sys
import os
from typing import Optional

def create_clean_test_env(base_dir: str = "test_environment") -> Path:
    """Create a clean test environment and return its path."""
    test_dir = Path(base_dir).absolute()
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir()
    return test_dir

def copy_dist_files(test_dir: Path) -> None:
    """Copy only the files needed for distribution to test directory."""
    dist_files = [
        "setup.bat",
        "requirements.txt",
        "check_update.py",
        "version.txt",
        "voice_typing.pyw"
    ]

    for file in dist_files:
        src = Path(file)
        if src.exists():
            shutil.copy2(src, test_dir / file)

def simulate_user_data(test_dir: Path) -> None:
    """Create mock user data for testing updates."""
    # Create .env with dummy keys
    with open(test_dir / ".env", "w") as f:
        f.write("OPENAI_API_KEY=test-key\nANTHROPIC_API_KEY=test-key")

    # Create mock history
    history_dir = test_dir / "transcription_history"
    history_dir.mkdir()
    with open(history_dir / "history.json", "w") as f:
        f.write('{"transcriptions": ["test transcription"]}')

def run_setup_bat(test_dir: Path, simulate_input: Optional[str] = None) -> bool:
    """Run setup.bat and return True if successful."""
    try:
        # Change to test directory
        original_dir = os.getcwd()
        os.chdir(test_dir)

        if simulate_input:
            # Simulate user input for first-time setup
            process = subprocess.Popen(
                ["setup.bat"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = process.communicate(simulate_input)
        else:
            # For update testing, just run normally
            result = subprocess.run(["setup.bat"], capture_output=True, text=True)

        os.chdir(original_dir)
        return True
    except Exception as e:
        print(f"Error running setup.bat: {e}")
        os.chdir(original_dir)
        return False

def test_fresh_install() -> bool:
    """Test the fresh installation process."""
    print("\nTesting fresh installation...")
    test_dir = create_clean_test_env("test_fresh_install")
    copy_dist_files(test_dir)

    # Simulate user entering API keys
    user_input = "test-openai-key\ntest-anthropic-key\n"
    success = run_setup_bat(test_dir, user_input)

    # Verify installation - check for .venv directory
    venv_exists = (test_dir / ".venv").exists()
    env_exists = (test_dir / ".env").exists()

    # Check if uv created the proper venv structure
    venv_scripts_exists = (test_dir / ".venv" / "Scripts").exists()
    python_exists = (test_dir / ".venv" / "Scripts" / "python.exe").exists()

    print(f"Virtual environment created: {venv_exists}")
    print(f"Virtual environment Scripts folder exists: {venv_scripts_exists}")
    print(f"Python executable exists: {python_exists}")
    print(f"Environment file created: {env_exists}")

    return success and venv_exists and env_exists and venv_scripts_exists and python_exists

def test_update_process() -> bool:
    """Test the update process with existing user data."""
    print("\nTesting update process...")
    test_dir = create_clean_test_env("test_update")
    copy_dist_files(test_dir)

    # Create initial installation
    run_setup_bat(test_dir, "test-key\ntest-key\n")

    # Simulate existing user data
    simulate_user_data(test_dir)

    # Store original data for comparison
    original_env = (test_dir / ".env").read_text()
    original_history = (test_dir / "transcription_history" / "history.json").read_text()

    # Run update
    success = run_setup_bat(test_dir)

    # Verify user data preserved
    env_preserved = (test_dir / ".env").read_text() == original_env
    history_preserved = (test_dir / "transcription_history" / "history.json").read_text() == original_history

    print(f"Environment file preserved: {env_preserved}")
    print(f"History preserved: {history_preserved}")

    return success and env_preserved and history_preserved

if __name__ == "__main__":
    print("Starting installation tests...")

    fresh_install_success = test_fresh_install()
    update_success = test_update_process()

    print("\nTest Results:")
    print(f"Fresh Installation: {'✓' if fresh_install_success else '✗'}")
    print(f"Update Process: {'✓' if update_success else '✗'}")

    sys.exit(0 if fresh_install_success and update_success else 1)