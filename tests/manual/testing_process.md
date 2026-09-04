Here's a systematic approach to test your installation and update process:

## Installation & Update Testing

**What Automated Tests Currently Cover**

`cd .\tests\setup_test`
`.\test_setup_simple.ps1`

- Script logic and flow verification
- Basic file operations
- Configuration management

**What Will Require Manual Testing**

- Real environment setup with actual Python/uv
- Functional application testing (actual app launch after fresh install)
- Real update mechanism with GitHub releases
- Customize some settings; Run an update
- Verify everything still works and no settings were lost

### Manual Testing of Fresh Install

Testing a fresh install is as simple as following your README.md quick start instructions in a clean directory:

1. Create a new empty folder
2. Copy your project files there
3. Run `setup.bat` and follow the prompts
4. Verify the app launches correctly

### Testing Update Mechanism (Quick Approach)

Here's a quick way to test your update mechanism without committing to GitHub:

1. **Create "Old Version" (5 min)**
   ```powershell
   # Create a test directory
   mkdir update_test
   cd update_test

   # Copy your current project
   xcopy /s /e /i "C:\path\to\your\project\*" "old_version\"

   # Set an "old" version
   echo "1.0.0" > old_version\version.txt

   # Add test user data
   echo "OPENAI_API_KEY=test_key_to_preserve" > old_version\.env
   echo "{\"setting\":\"to_preserve\"}" > old_version\user_settings.json
   ```

2. **Create "New Version" (5 min)**
   ```powershell
   # Make a copy as new version
   xcopy /s /e /i "old_version\*" "new_version\"

   # Make visible changes to verify update worked
   echo "1.1.0" > new_version\version.txt
   echo "# Updated file" > new_version\updated_file.txt
   ```

3. **Test Update (5 min)**
   ```powershell
   # Create a simplified check_update.py in old_version
   @"
   # Test version of check_update.py
   import shutil
   import os
   import sys
   from pathlib import Path

   def update_app():
       print("Simulating update from version 1.0.0 to 1.1.0")

       # Items to preserve
       preserve = ['.env', '.venv', 'user_settings.json', 'transcription_history']

       # Backup preserved items
       backup_dir = Path("backup_test")
       backup_dir.mkdir(exist_ok=True)

       for item in preserve:
           src = Path(item)
           if src.exists():
               if src.is_dir():
                   shutil.copytree(src, backup_dir / item, dirs_exist_ok=True)
               else:
                   shutil.copy2(src, backup_dir / item)

       # Copy new version files (excluding preserved items)
       new_version_dir = Path("../new_version")
       for item in new_version_dir.iterdir():
           if item.name not in preserve:
               dest = Path(item.name)
               if dest.exists():
                   if dest.is_dir():
                       shutil.rmtree(dest)
                   else:
                       dest.unlink()
               if item.is_dir():
                   shutil.copytree(item, dest)
               else:
                   shutil.copy2(item, dest)

       # Restore preserved items
       for item in preserve:
           backup_path = backup_dir / item
           if backup_path.exists():
               dest = Path(item)
               if backup_path.is_dir():
                   if dest.exists():
                       shutil.rmtree(dest)
                   shutil.copytree(backup_path, dest)
               else:
                   shutil.copy2(backup_path, dest)

       print("Update complete!")
       return True

   if __name__ == "__main__":
       success = update_app()
       sys.exit(0 if success else 1)
   "@ > old_version\check_update.py

   # Run update test
   cd old_version
   setup.bat
   # Choose "Y" when asked about updates
   ```

4. **Verify Results**
   - Check that version.txt now shows 1.1.0
   - Verify updated_file.txt exists (confirming new files were added)
   - Confirm .env still contains your test API key
   - Verify user_settings.json still has the preserved settings
   - Launch the app to ensure it still works

**High Level Testing Process Plan**

- Create a new virtual machine or clean Windows installation?
- Install Python 3.8+ from python.org
- Install uv using `curl -sSf https://astral.sh/uv/install.ps1 | powershell`
- Copy your entire project to the VM
- Run the test script to verify both fresh installation and updates work
- The script will:
  1. Test fresh installation by creating a clean environment
  2. Simulate user input for API keys
  3. Verify uv creates the virtual environment (`.venv` directory) and .env file correctly
  4. Test update process by creating mock user data
  5. Run the update process
  6. Verify user data is preserved