"""
Computer Control — Universal UI automation for ANY application.
Ported from Jarvis-MK37 and adapted for V3 architecture.

Provides: click, type, scroll, drag, hotkey, screenshot,
          and AI-powered screen_find / screen_click.

Bug 2 fix: Added OCR text-matching (pytesseract) as Tier 2 between
UI Automation (instant) and Gemini Vision (slow/expensive).
Flow: UI Automation → OCR text match → Gemini Vision
"""

import io
import json
import logging
import re
import time
from pathlib import Path

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

def _get_api_key() -> str:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("gemini_api_key", "")
    except Exception:
        return ""

def _require():
    if not _PYAUTOGUI:
        raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")


# ── Primitive Actions ─────────────────────────────────────────

def _click(x=None, y=None, button="left", clicks=1) -> str:
    _require()
    if x is not None and y is not None:
        pyautogui.click(int(x), int(y), button=button, clicks=clicks)
        return f"Clicked ({x}, {y}) [{button}]"
    pyautogui.click(button=button, clicks=clicks)
    return f"Clicked at current position [{button}]"


def _type_text(text: str, interval=0.03) -> str:
    _require()
    time.sleep(0.3)
    pyautogui.typewrite(text, interval=interval)
    return f"Typed: {text[:60]}"


def _smart_type(text: str, clear_first=True) -> str:
    """Clear field + paste via clipboard for speed and Unicode support."""
    _require()
    if clear_first:
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.press("delete")
        time.sleep(0.1)

    if _PYPERCLIP and len(text) > 5:
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        return f"Smart-typed (clipboard): {text[:60]}"

    pyautogui.typewrite(text, interval=0.04)
    return f"Smart-typed: {text[:60]}"


def _hotkey(keys_str: str) -> str:
    _require()
    keys = [k.strip() for k in keys_str.split("+")]
    pyautogui.hotkey(*keys)
    return f"Hotkey: {keys_str}"


def _press(key: str) -> str:
    _require()
    pyautogui.press(key)
    return f"Pressed: {key}"


def _scroll(direction="down", amount=3) -> str:
    _require()
    clicks = amount if direction in ("up", "right") else -amount
    if direction in ("up", "down"):
        pyautogui.scroll(clicks)
    else:
        pyautogui.hscroll(clicks)
    return f"Scrolled {direction} ×{amount}"


def _move(x: int, y: int) -> str:
    _require()
    pyautogui.moveTo(int(x), int(y), duration=0.3)
    return f"Mouse → ({x}, {y})"


def _drag(x1, y1, x2, y2) -> str:
    _require()
    pyautogui.moveTo(int(x1), int(y1), duration=0.2)
    pyautogui.dragTo(int(x2), int(y2), duration=0.5, button="left")
    return f"Dragged ({x1},{y1}) → ({x2},{y2})"


def _screenshot(save_path=None) -> str:
    _require()
    path = Path(save_path) if save_path else Path.home() / "Desktop" / "jarvis_screenshot.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    img = pyautogui.screenshot()
    img.save(str(path))
    return f"Screenshot saved: {path}"


def _clear_field() -> str:
    _require()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Field cleared"


def _focus_window(title: str) -> str:
    """Bring a window to the foreground — uses win32gui enumeration."""
    try:
        from actions.window_manager import focus_window as _wm_focus
        return _wm_focus(title)
    except ImportError:
        pass
    # Fallback: WScript.Shell
    import subprocess
    try:
        script = f'(New-Object -ComObject WScript.Shell).AppActivate("{title}")'
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        time.sleep(0.3)
        return f"Focused window: {title}"
    except Exception as e:
        return f"focus_window failed: {e}"


def _wait_for_app(app_name: str, timeout: float = 10.0) -> str:
    """Wait until a window matching app_name appears on screen."""
    try:
        from actions.window_manager import wait_for_window
        win = wait_for_window(app_name, timeout=timeout)
        if win:
            return f"Window ready: {win['title']}"
        return f"Timed out waiting for: {app_name}"
    except Exception as e:
        return f"wait_for_app failed: {e}"


def _wait(seconds=1.0) -> str:
    seconds = min(float(seconds), 30.0)
    time.sleep(seconds)
    return f"Waited {seconds}s"


# ── AI-Powered Vision Actions ────────────────────────────────

def _screen_find(description: str) -> tuple | None:
    """
    Take a screenshot and use a vision model to find a UI element.

    Uses core.providers.vision_find which routes to:
      - Ollama + moondream (local, free, no rate limits)
      - Gemini Vision (fallback if configured)

    Returns (x, y) pixel coordinates or None if not found.
    """
    _require()

    try:
        img = pyautogui.screenshot()

        # Compress to JPEG for speed
        buf = io.BytesIO()
        img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=85)
        image_bytes = buf.getvalue()

        from core.providers import vision_find
        result = vision_find(image_bytes, description)

        if result:
            log.info("🔍 screen_find('%s') → (%d, %d)", description, result[0], result[1])
        else:
            log.info("🔍 screen_find('%s') → NOT_FOUND", description)

        return result

    except Exception as e:
        log.error("screen_find failed: %s", e)
        return None


# ── Tier 2: OCR Text Matching (Bug 2 fix) ────────────────────

def _ocr_find_text(text: str) -> tuple | None:
    """
    Find text on screen using OCR and return its center coordinates.
    
    This is the Bug 2 fix: when UI Automation can't find a button
    (because the app doesn't expose accessibility labels), OCR scans
    the actual pixels on screen for the text before we fall back
    to the expensive Gemini Vision API.
    
    Uses pytesseract — works on any visible text in any app.
    Install: pip install pytesseract
             Download Tesseract-OCR from https://github.com/UB-Mannheim/tesseract/wiki
    
    Returns (x, y) center coordinates of the text, or None.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        log.debug("pytesseract not installed — skipping OCR tier")
        return None

    try:
        _require()
        screenshot = pyautogui.screenshot()
        
        data = pytesseract.image_to_data(
            screenshot,
            output_type=pytesseract.Output.DICT
        )
        
        text_lower = text.lower().strip()
        best_match = None
        
        for i, word in enumerate(data["text"]):
            if not word:
                continue
            word_str = str(word).strip()
            if not word_str:
                continue
                
            # Check if our target text appears in this word (or vice versa)
            if text_lower in word_str.lower() or word_str.lower() in text_lower:
                x = data["left"][i]
                y = data["top"][i]
                w = data["width"][i]
                h = data["height"][i]
                conf = int(data["conf"][i]) if data["conf"][i] != -1 else 0
                
                # Only accept matches with reasonable confidence
                if conf >= 40 and w > 5 and h > 5:
                    center = (x + w // 2, y + h // 2)
                    # Prefer higher confidence matches
                    if best_match is None or conf > best_match[2]:
                        best_match = (center[0], center[1], conf)
        
        if best_match:
            log.info("🔤 OCR found '%s' at (%d, %d) [conf=%d%%]", 
                     text, best_match[0], best_match[1], best_match[2])
            return (best_match[0], best_match[1])
        
        # Also try multi-word matching by checking adjacent words
        if " " in text_lower:
            words = text_lower.split()
            full_text = " ".join(str(w) for w in data["text"] if w).lower()
            if text_lower in full_text:
                # Found the phrase — now find its position
                # Use the position of the first word of the phrase
                for i, word in enumerate(data["text"]):
                    if str(word).lower().strip() == words[0]:
                        x = data["left"][i]
                        y = data["top"][i]
                        w = data["width"][i]
                        h = data["height"][i]
                        center = (x + w // 2, y + h // 2)
                        log.info("🔤 OCR phrase match '%s' at (%d, %d)", text, center[0], center[1])
                        return center
        
        log.debug("🔤 OCR: '%s' not found on screen", text)
        return None
        
    except Exception as e:
        log.debug("OCR search failed: %s", e)
        return None


def _screen_click(description: str) -> str:
    """
    Click a UI element by description.
    
    Tiered approach (Bug 2 fix — deterministic methods first, AI last):
      Tier 1 (instant):  UI Automation accessibility tree
      Tier 2 (~1s):      OCR text matching via pytesseract
      Tier 3 (2-5s):     Screenshot → Gemini Vision (AI, last resort)
    """
    # ── Tier 1: UI Automation (instant, no API call) ──
    try:
        from actions.ui_reader import click_element_by_name
        fast_result = click_element_by_name(description)
        if fast_result is not None:
            return fast_result  # Either success or "disabled"
        log.info("⚡→🔤 UI Automation couldn't find '%s', trying OCR", description)
    except Exception as e:
        log.debug("UI Automation unavailable: %s", e)

    # ── Tier 2: OCR text matching (Bug 2 fix) ──
    ocr_coords = _ocr_find_text(description)
    if ocr_coords:
        time.sleep(0.2)
        _click(x=ocr_coords[0], y=ocr_coords[1])
        return f"Clicked '{description}' at ({ocr_coords[0]}, {ocr_coords[1]}) [OCR]"
    log.info("🔤→🔍 OCR couldn't find '%s', falling back to vision", description)

    # ── Tier 3: Screenshot → Gemini Vision (last resort) ──
    coords = _screen_find(description)
    if coords:
        time.sleep(0.2)
        _click(x=coords[0], y=coords[1])
        return f"Clicked '{description}' at ({coords[0]}, {coords[1]})"
    return f"Element not found on screen: '{description}'"


# ── Main Dispatcher ───────────────────────────────────────────

def computer_control(action: str, **kwargs) -> str:
    """
    Universal computer control dispatcher.

    Actions:
      click, double_click, right_click — mouse clicks
      type, smart_type — keyboard input
      hotkey, press — key combinations / single keys
      scroll — scroll wheel
      move, drag — mouse movement
      screenshot — capture screen
      wait — pause execution
      clear_field — select all + delete
      focus_window — bring window to foreground
      screen_find — AI: find UI element coordinates
      screen_click — AI: find + click UI element
    """
    action = action.lower().strip()
    log.info("🖥️ computer_control: %s %s", action, kwargs)

    try:
        if action in ("click", "left_click"):
            return _click(kwargs.get("x"), kwargs.get("y"), "left", 1)

        elif action == "double_click":
            return _click(kwargs.get("x"), kwargs.get("y"), "left", 2)

        elif action == "right_click":
            return _click(kwargs.get("x"), kwargs.get("y"), "right", 1)

        elif action == "type":
            return _type_text(kwargs.get("text", ""))

        elif action == "smart_type":
            return _smart_type(
                kwargs.get("text", ""),
                clear_first=kwargs.get("clear_first", True),
            )

        elif action == "hotkey":
            return _hotkey(kwargs.get("keys", ""))

        elif action == "press":
            return _press(kwargs.get("key", "enter"))

        elif action == "scroll":
            return _scroll(
                direction=kwargs.get("direction", "down"),
                amount=int(kwargs.get("amount", 3)),
            )

        elif action == "move":
            return _move(int(kwargs.get("x", 0)), int(kwargs.get("y", 0)))

        elif action == "drag":
            return _drag(
                int(kwargs.get("x1", 0)), int(kwargs.get("y1", 0)),
                int(kwargs.get("x2", 0)), int(kwargs.get("y2", 0)),
            )

        elif action == "screenshot":
            return _screenshot(kwargs.get("path"))

        elif action == "wait":
            return _wait(kwargs.get("seconds", 1.0))

        elif action == "clear_field":
            return _clear_field()

        elif action == "focus_window":
            return _focus_window(kwargs.get("title", ""))

        elif action == "wait_for_app":
            return _wait_for_app(
                kwargs.get("app_name", kwargs.get("title", "")),
                float(kwargs.get("timeout", 10.0)),
            )

        elif action == "screen_find":
            coords = _screen_find(kwargs.get("description", ""))
            return f"{coords[0]},{coords[1]}" if coords else "NOT_FOUND"

        elif action == "screen_click":
            return _screen_click(kwargs.get("description", ""))

        else:
            return f"Unknown computer_control action: '{action}'"

    except Exception as e:
        log.error("computer_control '%s' failed: %s", action, e)
        return f"computer_control '{action}' failed: {e}"
