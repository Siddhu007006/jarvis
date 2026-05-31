"""
Screen Vision — Captures screen + active window context for Gemini.

v3.2: Returns JPEG bytes + foreground window context so Gemini knows
      WHICH app is currently visible, not just raw pixels.

      Fix: Was saying 'I see your desktop' even when TradingView was open.
      Root cause: Gemini received the image but NO context about which window
      was in the foreground. Now we explicitly report the active window title
      so Gemini can never again confuse an open app for 'the desktop'.
"""

import logging
import time
from io import BytesIO

log = logging.getLogger(__name__)


def get_active_window_context() -> str:
    """
    Returns a string describing the current foreground window and all open
    windows. This gives Gemini the context it needs to accurately describe
    what the user is looking at.

    Called by engine.py's screen_vision handler and injected into the
    result text sent back to Gemini alongside the image.
    """
    context_parts = []

    # Get the ACTIVE foreground window title via Win32 API
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if title:
                    context_parts.append(f"ACTIVE WINDOW: \"{title}\"")
    except Exception:
        pass

    # Get all visible window titles for full context
    try:
        from actions.window_manager import list_open_windows
        all_windows = list_open_windows()
        if all_windows:
            top = all_windows[:10]  # cap to avoid token waste
            context_parts.append("OPEN WINDOWS: " + ", ".join(f'"{w}"' for w in top))
    except Exception:
        pass

    return " | ".join(context_parts) if context_parts else ""


def capture_screen() -> bytes | None:
    """Capture the primary screen and return JPEG bytes."""
    try:
        from PIL import ImageGrab

        # Small pause so any recent window switch has time to paint
        time.sleep(0.1)

        img = ImageGrab.grab()

        # Resize to reduce token cost (max 1280px wide)
        max_w = 1280
        if img.width > max_w:
            ratio = max_w / img.width
            new_h = int(img.height * ratio)
            img = img.resize((max_w, new_h))

        # Convert to RGB (JPEG doesn't support alpha)
        img = img.convert("RGB")

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=60, optimize=True)
        jpeg_bytes = buf.getvalue()

        log.info("📸 Screenshot captured: %d bytes (%dx%d) [JPEG 60%%]",
                 len(jpeg_bytes), img.width, img.height)
        return jpeg_bytes

    except Exception as e:
        log.error("Screenshot failed: %s", e)
        return None
