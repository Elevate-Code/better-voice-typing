import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # This is a workaround to avoid circular imports
    from modules.settings import Settings

def get_log_dir() -> Path:
    """Logs directory in the user's documents folder.
    Ex. "C:\\Users\\{name}\\Documents\\VoiceTyping\\logs" """
    return Path.home() / "Documents" / "VoiceTyping" / "logs"


def setup_logging(settings: "Settings") -> logging.Logger:
    """Configure application logging"""
    log_dir = get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    # Clean up old log files
    cleanup_logs(log_dir, settings.get('log_retention_days'))

    # Create log file with timestamp
    log_file = log_dir / f"voice_typing_{datetime.now().strftime('%Y%m%d')}.log"

    # Configure logging
    logger = logging.getLogger('voice_typing')
    logger.setLevel(logging.DEBUG)

    # Prevent duplicate handlers
    if logger.hasHandlers():
        logger.handlers.clear()

    # File handler with explicit UTF-8 encoding
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)

    # Custom stream handler for UTF-8 console output on Windows
    class Utf8ConsoleHandler(logging.StreamHandler):
        def emit(self, record):
            try:
                msg = self.format(record)
                # On Windows, write directly to buffer with UTF-8 encoding
                if hasattr(self.stream, 'buffer'):
                    self.stream.buffer.write((msg + self.terminator).encode('utf-8'))
                    self.stream.buffer.flush()
                else:
                    # Fallback for non-buffer streams
                    self.stream.write(msg + self.terminator)
                    self.flush()
            except Exception:
                self.handleError(record)

    # Use the custom handler for console output
    console_handler = Utf8ConsoleHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_formatter = logging.Formatter('%(levelname)s: %(message)s')
    console_handler.setFormatter(console_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Log system info at startup
    logger.info(f"Python version: {sys.version}")
    logger.info(f"Platform: {sys.platform}")

    return logger

def cleanup_logs(log_dir: Path, retention_days: int):
    """Deletes log files older than the specified retention period."""
    if retention_days is None:
        return
    try:
        retention_cutoff = datetime.now() - timedelta(days=retention_days)
        for log_file in log_dir.glob("voice_typing_*.log"):
            try:
                file_date_str = log_file.stem.split("_")[-1]
                file_date = datetime.strptime(file_date_str, "%Y%m%d")
                if file_date < retention_cutoff:
                    log_file.unlink()
            except (ValueError, IndexError):
                # Ignore files with unexpected naming conventions
                continue
    except Exception as e:
        # Use print() here because the logger may not be fully configured yet.
        # This error is critical for debugging startup issues from a console.
        print(f"Error during log cleanup: {e}")