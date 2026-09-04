@echo off
REM Better Voice Typing - source setup (developers / run-from-source users).
REM End users: use the installer from the GitHub releases page instead.
REM
REM Creates .venv with the locked dependencies via uv, which also fetches a
REM suitable Python (3.10-3.12) if none is installed.
title Better Voice Typing setup
cd /d "%~dp0"

uv --version >nul 2>&1
if errorlevel 1 (
    echo uv is not installed. Install it from https://docs.astral.sh/uv/getting-started/
    echo   PowerShell:  irm https://astral.sh/uv/install.ps1 ^| iex
    pause
    exit /b 1
)

echo Installing dependencies with uv sync...
uv sync
if errorlevel 1 (
    echo.
    echo Setup failed. See the messages above.
    pause
    exit /b 1
)

echo.
echo Done. Launch with run_voice_typing.bat, then add your API keys via the tray icon
echo (Open API Keys) - they live in Documents\VoiceTyping\.env.
echo.
choice /C YN /M "Launch Better Voice Typing now"
if errorlevel 2 exit /b 0
start "" ".\.venv\Scripts\pythonw.exe" ".\voice_typing.pyw"
exit /b 0
