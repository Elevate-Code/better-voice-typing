@echo off
setlocal EnableDelayedExpansion

REM Voice Typing Assistant Setup/Update Tool
REM This script performs first-time setup or updates an existing installation
REM It checks Python requirements, manages dependencies, and configures API keys
echo Voice Typing Assistant Setup/Update Tool
echo ==========================================

REM Check if Python is installed (try both python and py commands)
python --version > nul 2>&1
if not errorlevel 1 (
    set PYTHON_CMD=python
    goto :PYTHON_FOUND
)

py --version > nul 2>&1
if not errorlevel 1 (
    set PYTHON_CMD=py
    goto :PYTHON_FOUND
)

echo Python is not installed or not in PATH! Please install Python 3.8 or newer from python.org
echo.
echo If Python is already installed, make sure it's added to your PATH environment variable.
pause
goto :KEEP_OPEN

:PYTHON_FOUND
REM Check for a suitable Python version (3.10-3.12)
for /f "tokens=1,2 delims=." %%A in ('%PYTHON_CMD% -c "import sys; print(sys.version.split()[0])"') do (
    set PYMAJOR=%%A
    set PYMINOR=%%B
)

if %PYMAJOR% LSS 3 (
    echo Error: Python 3.10-3.12 is required. Found Python %PYMAJOR%.%PYMINOR%
    goto :PYTHON_VERSION_ERROR
)

if %PYMAJOR% == 3 if %PYMINOR% LSS 10 (
    echo Error: Python 3.10-3.12 is required. Found Python %PYMAJOR%.%PYMINOR%
    goto :PYTHON_VERSION_ERROR
)

if %PYMAJOR% == 3 if %PYMINOR% GTR 12 (
    echo Error: Python 3.10-3.12 is required. Found Python %PYMAJOR%.%PYMINOR%
    echo Note: Pillow library does not yet support Python 3.13+
    goto :PYTHON_VERSION_ERROR
)

if %PYMAJOR% GTR 3 (
    echo Error: Python 3.10-3.12 is required. Found Python %PYMAJOR%.%PYMINOR%
    goto :PYTHON_VERSION_ERROR
)

REM Check if uv Python package manager is installed
uv --version > nul 2>&1
if errorlevel 1 (
    echo Error: uv is not installed
    echo Please install uv from https://docs.astral.sh/uv/getting-started/#installation
    echo You can run: curl -sSf https://astral.sh/uv/install.ps1 ^| powershell
    pause
    goto :KEEP_OPEN
)

REM Check if this is an update or first install
if exist .venv (
    echo Existing installation detected
    choice /C YN /M "Would you like to check for updates (Y/N)"
    if errorlevel 2 goto :SKIP_UPDATE

    echo Checking for updates...
    call .venv\Scripts\activate.bat
    python check_update.py
    if errorlevel 1 (
        echo Update failed. Please try again later.
    ) else (
        REM Update dependencies using uv package manager
        echo Updating dependencies...
        uv pip install -r requirements.txt
    )
    goto :END
)

:SKIP_UPDATE
REM First time setup continues here...
echo Creating virtual environment with uv...
uv venv --python ">=3.10,<3.13"
if errorlevel 1 (
    echo Error: Failed to create virtual environment.
    pause
    goto :KEEP_OPEN
)

REM Activate virtual environment and install requirements
echo Installing required packages with uv...
call .venv\Scripts\activate
call uv pip install -r requirements.txt
echo Package installation complete.
echo.
timeout /t 2 /nobreak > nul

REM Create .env file if it doesn't exist
if not exist .env (
    echo Creating configuration file...
    if exist .env.example (
        copy .env.example .env
        echo .env file created from template. Please edit it to add your API keys.
    ) else (
        echo WARNING: .env.example not found. Creating minimal .env file.
        echo ELEVENLABS_API_KEY=> .env
        echo OPENAI_API_KEY=>> .env
        echo ANTHROPIC_API_KEY=>> .env
    )
)

:END
echo.
echo Setup/Update complete! You can now run voice_typing.pyw to start the app.
echo Next: Setup your `.env` file and change your Taskbar settings to always show the icon in your system tray.
echo.
choice /C YN /M "Would you like to launch the application now (Y/N)"
if errorlevel 2 goto :EXIT
REM Launch the application if user chooses yes
echo Launching Voice Typing Assistant...
start pythonw voice_typing.pyw
goto :EXIT

:ERROR_EXIT
echo.
echo Setup encountered errors. Please check the messages above.
pause
goto :KEEP_OPEN

:EXIT
echo Setup complete! You can now close this window.
pause
goto :KEEP_OPEN

:PYTHON_VERSION_ERROR
echo.
echo If you have another Python installation (3.10-3.12) that isn't in your PATH:
echo 1. Ensure the Python 3.10-3.12 is added to your PATH environment variable, or
echo 2. Specify the full path to Python when running this script
pause
goto :KEEP_OPEN

REM -----------------------------------------------------------------------
REM Final label that prevents this window from ever closing on its own.
REM Press Ctrl+C or click the X button to exit manually.
REM -----------------------------------------------------------------------
:KEEP_OPEN
echo.
echo Script has reached the end. You can close the window to exit.
echo.
REM A simple infinite loop with 10-second waits is used here:
:loop
timeout /t 10 >nul
goto :loop