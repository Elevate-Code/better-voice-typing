"""
Output Provider system for text insertion with plugin support.

Providers handle how transcribed text is inserted at the cursor position.
Custom providers can be added in ~/Documents/VoiceTyping/plugins/
"""

import importlib.util
import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pyautogui
import pyperclip

logger = logging.getLogger('voice_typing')

PLUGINS_DIR = Path.home() / "Documents" / "VoiceTyping" / "plugins"

# Global registry of loaded providers
_providers: Dict[str, 'OutputProvider'] = {}
_plugin_errors: List[str] = []


class OutputProvider(ABC):
    """
    Abstract base class for output providers.

    Custom providers should inherit from this class and implement:
    - name: str - unique identifier (e.g., "chunked_terminal")
    - display_name: str - shown in UI (e.g., "Chunked Terminal")
    - insert_text() - the actual insertion logic
    """

    name: str = "base"
    display_name: str = "Base Provider"

    @abstractmethod
    def insert_text(self, text: str, pyautogui_lock: threading.Lock,
                    root_after: Callable) -> None:
        """
        Insert text at the current cursor position.

        Args:
            text: The text to insert
            pyautogui_lock: Thread lock for pyautogui operations
            root_after: Tkinter root.after() for scheduling delayed operations
        """
        pass


class StandardOutputProvider(OutputProvider):
    """Standard output provider - pastes via clipboard + Ctrl+V"""

    name = "standard"
    display_name = "Standard (Ctrl+V)"

    def insert_text(self, text: str, pyautogui_lock: threading.Lock,
                    root_after: Callable) -> None:
        """Insert text using standard clipboard paste"""
        try:
            with pyautogui_lock:
                # Save original clipboard content
                original_clipboard = pyperclip.paste()

                # Copy new text and paste it
                pyperclip.copy(text)
                pyautogui.hotkey('ctrl', 'v')

                # Restore original clipboard content after a small delay
                root_after(100, lambda: pyperclip.copy(original_clipboard))
        except Exception as e:
            logger.error(f"StandardOutputProvider: Error during text insertion: {e}")


def _ensure_plugins_dir() -> None:
    """Create the plugins directory if it doesn't exist"""
    try:
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(f"Could not create plugins directory: {e}")


def _load_plugins() -> None:
    """Load OutputProvider subclasses from plugins directory"""
    global _plugin_errors
    _plugin_errors = []

    if not PLUGINS_DIR.exists():
        return

    for py_file in PLUGINS_DIR.glob("*.py"):
        if py_file.name.startswith("_"):
            continue  # Skip __init__.py, __pycache__, etc.

        try:
            spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
            if spec is None or spec.loader is None:
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Find OutputProvider subclasses
            for obj_name in dir(module):
                obj = getattr(module, obj_name)
                if (isinstance(obj, type)
                    and issubclass(obj, OutputProvider)
                    and obj is not OutputProvider
                    and hasattr(obj, 'name')):
                    try:
                        instance = obj()
                        _providers[instance.name] = instance
                        logger.info(f"Loaded output provider plugin: {instance.name} from {py_file.name}")
                    except Exception as e:
                        error_msg = f"Plugin '{py_file.name}': Failed to instantiate {obj_name}: {str(e)[:100]}"
                        _plugin_errors.append(error_msg)
                        logger.error(error_msg, exc_info=True)

        except Exception as e:
            error_msg = f"Plugin '{py_file.name}' failed: {str(e)[:100]}"
            _plugin_errors.append(error_msg)
            logger.error(f"Failed to load plugin {py_file}: {e}", exc_info=True)


def initialize_providers() -> List[str]:
    """
    Initialize the provider system. Call this once at app startup.

    Returns:
        List of error messages from failed plugin loads (empty if all succeeded)
    """
    global _providers, _plugin_errors

    # Start fresh
    _providers = {}
    _plugin_errors = []

    # Ensure plugins directory exists
    _ensure_plugins_dir()

    # Register built-in provider
    standard = StandardOutputProvider()
    _providers[standard.name] = standard

    # Load plugins
    _load_plugins()

    logger.info(f"Initialized {len(_providers)} output provider(s): {list(_providers.keys())}")

    return _plugin_errors


def get_output_provider(name: str) -> OutputProvider:
    """
    Get an output provider by name.

    Args:
        name: Provider name (e.g., "standard", "chunked_terminal")

    Returns:
        The requested provider, or StandardOutputProvider if not found
    """
    if not _providers:
        initialize_providers()

    if name in _providers:
        return _providers[name]

    logger.warning(f"Output provider '{name}' not found, falling back to 'standard'")
    return _providers.get("standard", StandardOutputProvider())


def get_available_providers() -> List[Dict[str, str]]:
    """
    Get list of available output providers for UI display.

    Returns:
        List of dicts with 'name' and 'display_name' keys
    """
    if not _providers:
        initialize_providers()

    return [
        {"name": p.name, "display_name": p.display_name}
        for p in _providers.values()
    ]
