"""
Keyboard Automation — Types text or presses hotkeys via pyautogui.
"""

import logging
import time

log = logging.getLogger(__name__)


def type_text(text: str = "", hotkey: str = None) -> str:
    """Type text or press a hotkey combination."""
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
    except ImportError:
        return "pyautogui not installed. Run: pip install pyautogui"

    try:
        # If both text and hotkey are provided, type text then press hotkey
        if text:
            log.info("⌨️ Typing: %s", text[:40])
            time.sleep(0.3) # Wait for focus
            pyautogui.write(text, interval=0.02)
            
        if hotkey:
            keys = [k.strip() for k in hotkey.split("+")]
            log.info("⌨️ Hotkey: %s", hotkey)
            pyautogui.hotkey(*keys)
            
        if text and hotkey:
            return f"Typed {len(text)} characters and pressed {hotkey}."
        elif text:
            return f"Typed {len(text)} characters."
        elif hotkey:
            return f"Pressed {hotkey}."
        else:
            return "No text or hotkey provided."
            
    except Exception as e:
        return f"Keyboard action failed: {e}"
