"""
core/automation.py — Desktop Automation Engine for Jarvis.

Executes structured action plans returned by Gemini. Gemini NEVER
directly controls the computer — it returns JSON action plans, and
this engine interprets and executes them safely.

Supported actions (from the blueprint):
  - open_app: Launch applications
  - focus_window: Bring a window to front
  - browser_open: Open URL in browser
  - type_text: Keyboard typing
  - key_press: Hotkeys (ctrl+c, alt+tab, etc.)
  - mouse_click: Click at coordinates
  - mouse_move: Move mouse
  - scroll: Scroll up/down
  - screenshot: Capture screen
  - file_create: Create a file
  - window_manage: Minimize/maximize/close windows

Concept: Command Pattern — each action is a self-contained command
with validation, execution, and error handling.
"""

import logging
import subprocess
import time
import os
from typing import Any

from core.events import Event, get_bus

log = logging.getLogger(__name__)


class AutomationEngine:
    """
    Executes desktop automation actions from structured plans.

    Usage:
        engine = AutomationEngine(bus=get_bus())
        engine.execute({"action": "open_app", "target": "notepad"})
        engine.execute_plan([
            {"action": "browser_open", "url": "https://google.com"},
            {"action": "type_text", "text": "hello world"},
        ])
    """

    def __init__(self, bus=None):
        self._bus = bus or get_bus()

        # Action dispatch table
        self._actions = {
            "open_app":       self._open_app,
            "focus_window":   self._focus_window,
            "browser_open":   self._browser_open,
            "type_text":      self._type_text,
            "key_press":      self._key_press,
            "mouse_click":    self._mouse_click,
            "mouse_move":     self._mouse_move,
            "scroll":         self._scroll,
            "screenshot":     self._screenshot,
            "file_create":    self._file_create,
            "window_manage":  self._window_manage,
        }

    def execute_plan(self, actions: list[dict]) -> list[dict]:
        """
        Execute a list of actions sequentially.

        Returns a list of results: [{"action": ..., "success": bool, "result": ...}]
        """
        results = []
        for action in actions:
            result = self.execute(action)
            results.append(result)
            if not result.get("success"):
                break  # Stop on first failure
            time.sleep(0.3)  # Brief pause between actions
        return results

    def execute(self, action: dict) -> dict:
        """
        Execute a single action.

        Args:
            action: {"action": "open_app", "target": "notepad", ...}

        Returns:
            {"action": str, "success": bool, "result": str}
        """
        action_type = action.get("action", "")
        handler = self._actions.get(action_type)

        if not handler:
            msg = f"Unknown action: {action_type}"
            log.error(msg)
            self._bus.emit(Event.ACTION_FAILED, {"action": action_type, "error": msg})
            return {"action": action_type, "success": False, "result": msg}

        self._bus.emit(Event.ACTION_STARTED, {"action": action_type, "details": action})

        try:
            result = handler(action)
            self._bus.emit(Event.ACTION_COMPLETE, {"action": action_type, "result": result})
            return {"action": action_type, "success": True, "result": result}
        except Exception as e:
            msg = f"{action_type} failed: {e}"
            log.error(msg, exc_info=True)
            self._bus.emit(Event.ACTION_FAILED, {"action": action_type, "error": msg})
            return {"action": action_type, "success": False, "result": msg}

    # ── Action Handlers ───────────────────────────────────────

    def _open_app(self, action: dict) -> str:
        """Launch an application by name or path."""
        target = action.get("target", "")
        if not target:
            raise ValueError("No target specified")

        # Common app aliases
        aliases = {
            "notepad": "notepad.exe",
            "calculator": "calc.exe",
            "explorer": "explorer.exe",
            "cmd": "cmd.exe",
            "terminal": "wt.exe",
            "powershell": "powershell.exe",
            "chrome": "chrome",
            "edge": "msedge",
            "spotify": "spotify",
            "vscode": "code",
            "settings": "ms-settings:",
        }

        cmd = aliases.get(target.lower(), target)

        if cmd.startswith("ms-"):
            os.startfile(cmd)
        else:
            subprocess.Popen(cmd, shell=True)

        return f"Opened {target}"

    def _focus_window(self, action: dict) -> str:
        """Bring a window to the foreground by title."""
        import pyautogui
        title = action.get("title", "")
        if not title:
            raise ValueError("No window title specified")

        import ctypes
        import ctypes.wintypes

        def find_window(title_part):
            """Find window by partial title match."""
            result = []
            def callback(hwnd, _):
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if length:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                    if title_part.lower() in buf.value.lower():
                        result.append(hwnd)
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.c_long)
            ctypes.windll.user32.EnumWindows(WNDENUMPROC(callback), 0)
            return result[0] if result else None

        hwnd = find_window(title)
        if hwnd:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return f"Focused window: {title}"
        raise ValueError(f"Window not found: {title}")

    def _browser_open(self, action: dict) -> str:
        """Open a URL in the default browser."""
        import webbrowser
        url = action.get("url", "")
        if not url:
            raise ValueError("No URL specified")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        webbrowser.open(url)
        return f"Opened {url}"

    def _type_text(self, action: dict) -> str:
        """Type text using keyboard simulation."""
        import pyautogui
        text = action.get("text", "")
        if not text:
            raise ValueError("No text specified")
        interval = action.get("interval", 0.02)
        pyautogui.typewrite(text, interval=interval) if text.isascii() else pyautogui.write(text)
        return f"Typed {len(text)} chars"

    def _key_press(self, action: dict) -> str:
        """Press a key or hotkey combination."""
        import pyautogui
        keys = action.get("keys", [])
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split("+")]
        if not keys:
            raise ValueError("No keys specified")
        pyautogui.hotkey(*keys)
        return f"Pressed {'+'.join(keys)}"

    def _mouse_click(self, action: dict) -> str:
        """Click at coordinates."""
        import pyautogui
        x = action.get("x", None)
        y = action.get("y", None)
        button = action.get("button", "left")
        clicks = action.get("clicks", 1)
        if x is not None and y is not None:
            pyautogui.click(x, y, clicks=clicks, button=button)
            return f"Clicked ({x}, {y})"
        else:
            pyautogui.click(clicks=clicks, button=button)
            return "Clicked current position"

    def _mouse_move(self, action: dict) -> str:
        """Move mouse to coordinates."""
        import pyautogui
        x = action.get("x", 0)
        y = action.get("y", 0)
        duration = action.get("duration", 0.3)
        pyautogui.moveTo(x, y, duration=duration)
        return f"Moved to ({x}, {y})"

    def _scroll(self, action: dict) -> str:
        """Scroll up or down."""
        import pyautogui
        amount = action.get("amount", 3)
        direction = action.get("direction", "down")
        clicks = -amount if direction == "down" else amount
        pyautogui.scroll(clicks)
        return f"Scrolled {direction} {amount}"

    def _screenshot(self, action: dict) -> str:
        """Capture a screenshot and save it."""
        import pyautogui
        from PIL import Image
        path = action.get("path", os.path.join(os.path.expanduser("~"), "Desktop", "jarvis_screenshot.png"))
        img = pyautogui.screenshot()
        img.save(path)
        return path

    def _file_create(self, action: dict) -> str:
        """Create a file with content."""
        path = action.get("path", "")
        content = action.get("content", "")
        if not path:
            raise ValueError("No file path specified")
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Created {path}"

    def _window_manage(self, action: dict) -> str:
        """Minimize, maximize, or close a window."""
        import ctypes
        operation = action.get("operation", "")
        title = action.get("title", "")

        if not title:
            # Use foreground window
            hwnd = ctypes.windll.user32.GetForegroundWindow()
        else:
            # Find by title (reuse focus logic)
            result = self._focus_window({"title": title})
            hwnd = ctypes.windll.user32.GetForegroundWindow()

        ops = {
            "minimize": lambda: ctypes.windll.user32.ShowWindow(hwnd, 6),
            "maximize": lambda: ctypes.windll.user32.ShowWindow(hwnd, 3),
            "restore":  lambda: ctypes.windll.user32.ShowWindow(hwnd, 9),
            "close":    lambda: ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0),
        }

        handler = ops.get(operation)
        if not handler:
            raise ValueError(f"Unknown operation: {operation}")
        handler()
        return f"{operation} window"
