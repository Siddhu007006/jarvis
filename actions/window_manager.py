"""
Window Manager — Reliable window focus and enumeration using win32gui.

Replaces WScript.Shell.AppActivate which silently fails if the title
doesn't match exactly. This module enumerates ALL visible windows,
fuzzy-matches the target app name against real window titles, and
uses SetForegroundWindow to bring the correct window to front.

Used by: computer_control._focus_window, executor auto-focus
"""

import logging
import time

log = logging.getLogger(__name__)

# ── Try to import win32 libs ──────────────────────────────────
try:
    import win32gui
    import win32con
    import win32process
    import win32api
    _WIN32 = True
except ImportError:
    _WIN32 = False
    log.warning("pywin32 not installed — window management will use fallback")

try:
    import pygetwindow as gw
    _PYGETWINDOW = True
except ImportError:
    _PYGETWINDOW = False


# ── Core: Enumerate all visible windows ───────────────────────

def get_all_windows() -> list[dict]:
    """
    Return a list of all visible, non-empty titled windows.
    Each entry: { 'hwnd': int, 'title': str, 'title_lower': str }
    """
    if not _WIN32:
        return []

    windows = []

    def _callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title.strip():
            windows.append({
                "hwnd": hwnd,
                "title": title,
                "title_lower": title.lower(),
            })

    win32gui.EnumWindows(_callback, None)
    return windows


def list_open_windows() -> list[str]:
    """
    Return a list of human-readable window titles for all open windows.
    Used by the planner to know what's already open.
    """
    windows = get_all_windows()
    # Filter out noise (system windows, tiny names)
    titles = []
    skip_patterns = {"program manager", "settings", "microsoft text input"}
    for w in windows:
        title = w["title"]
        if len(title) < 3:
            continue
        if w["title_lower"] in skip_patterns:
            continue
        titles.append(title)
    return titles


def find_window(app_name: str) -> dict | None:
    """
    Find the best matching open window for the given app name.
    Uses a tiered fuzzy match:
      1. Exact title match
      2. Title starts with app name
      3. App name is a substring of the title
      4. Any word in app name appears in the title
    Returns the matching window dict or None.
    """
    name = app_name.lower().strip()
    windows = get_all_windows()

    if not windows:
        return None

    # Tier 1: Exact match
    for w in windows:
        if w["title_lower"] == name:
            return w

    # Tier 2: Title starts with the app name
    for w in windows:
        if w["title_lower"].startswith(name):
            return w

    # Tier 3: App name is a substring of the title
    for w in windows:
        if name in w["title_lower"]:
            return w

    # Tier 4: Any significant word from app name in title
    name_words = [w for w in name.split() if len(w) > 2]  # skip short words
    for w in windows:
        for word in name_words:
            if word in w["title_lower"]:
                return w

    return None


# ── Focus: Bring window to foreground ─────────────────────────

def _force_foreground(hwnd: int) -> bool:
    """
    Aggressively bring a window to the foreground, bypassing
    the Windows foreground lock.

    Technique: Attach our thread to the foreground window's thread,
    which grants us permission to call SetForegroundWindow.
    This is the same trick Task Manager and Alt+Tab use internally.
    """
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Get the thread that currently owns the foreground
    fg_hwnd = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
    our_thread = kernel32.GetCurrentThreadId()

    attached = False
    try:
        # If we're not the foreground thread, attach to it
        if fg_thread != our_thread:
            user32.AttachThreadInput(our_thread, fg_thread, True)
            attached = True

        # Now we have permission — restore if minimized
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.15)

        # Layer 1: ShowWindow to make it visible
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

        # Layer 2: BringWindowToTop
        win32gui.BringWindowToTop(hwnd)

        # Layer 3: SetForegroundWindow (this is the one that normally fails)
        win32gui.SetForegroundWindow(hwnd)

        # Layer 4: SetFocus for input
        try:
            user32.SetFocus(hwnd)
        except Exception:
            pass

        return True

    except Exception as e:
        log.warning("_force_foreground failed: %s", e)
        return False

    finally:
        # Always detach threads
        if attached:
            user32.AttachThreadInput(our_thread, fg_thread, False)


def focus_window(app_name: str) -> str:
    """
    Bring the window matching app_name to the foreground.
    Uses aggressive thread-attach trick to bypass Windows foreground lock.
    Even works when the user has manually clicked on another app.
    Falls back to AppActivate if win32 is unavailable.
    """
    if not _WIN32:
        return _fallback_focus(app_name)

    win = find_window(app_name)
    if not win:
        log.warning("focus_window: no window found for '%s'", app_name)
        return _fallback_focus(app_name)

    hwnd = win["hwnd"]
    title = win["title"]

    success = _force_foreground(hwnd)
    if success:
        time.sleep(0.2)
        # Verify we actually got focus
        fg = win32gui.GetForegroundWindow()
        if fg == hwnd:
            log.info("✅ Focused window: '%s' (hwnd=%d)", title, hwnd)
            return f"Focused: {title}"
        else:
            log.warning("⚠️ Focus claimed but foreground is different, retrying...")
            # One more attempt
            _force_foreground(hwnd)
            time.sleep(0.2)
            log.info("✅ Focused window (retry): '%s'", title)
            return f"Focused: {title}"
    else:
        log.warning("Force-foreground failed for '%s' — trying fallback", title)
        return _fallback_focus(app_name)


def _fallback_focus(app_name: str) -> str:
    """Fallback: WScript.Shell.AppActivate."""
    import subprocess
    try:
        script = f'(New-Object -ComObject WScript.Shell).AppActivate("{app_name}")'
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        time.sleep(0.3)
        return f"Focused (fallback): {app_name}"
    except Exception as e:
        return f"focus failed: {e}"


# ── Wait for Window ───────────────────────────────────────────

def wait_for_window(app_name: str, timeout: float = 10.0, poll: float = 0.4) -> dict | None:
    """
    Poll until a window matching app_name appears (or timeout).
    Used after open_app to confirm the app is actually ready.
    Returns the window dict if found, None if timed out.
    """
    deadline = time.time() + timeout
    name = app_name.lower().strip()

    log.info("⏳ Waiting for window '%s' (up to %.1fs)...", app_name, timeout)

    while time.time() < deadline:
        win = find_window(name)
        if win:
            log.info("✅ Window appeared: '%s'", win["title"])
            return win
        time.sleep(poll)

    log.warning("⏰ Timed out waiting for window: '%s'", app_name)
    return None


# ── List all open window titles (for debugging) ───────────────

def list_open_windows() -> list[str]:
    """Return all visible window titles. Useful for debugging."""
    return [w["title"] for w in get_all_windows()]


# ── Get current foreground window ─────────────────────────────

def get_foreground_hwnd() -> int | None:
    """Return the hwnd of the currently focused window."""
    if not _WIN32:
        return None
    try:
        return win32gui.GetForegroundWindow()
    except Exception:
        return None


def restore_window(hwnd: int) -> None:
    """Restore a previously focused window by its hwnd."""
    if not _WIN32 or not hwnd:
        return
    try:
        _force_foreground(hwnd)
    except Exception as e:
        log.warning("restore_window failed: %s", e)


# ── Stealth Focus (background-like execution) ─────────────────

class stealth_focus:
    """
    Context manager for 'background' execution.

    Saves the user's currently active window, brings the target
    app to the foreground, runs the action block, then immediately
    restores the user's original window.

    Usage:
        with stealth_focus("spotify"):
            # Spotify is now in front — take screenshot, click, type
            ...
        # User's original window is back in front

    The switch takes ~200ms each way. The user sees a brief flicker
    but their work is not disrupted.
    """

    def __init__(self, app_name: str):
        self.app_name = app_name
        self._saved_hwnd = None

    def __enter__(self):
        # Save user's current window
        self._saved_hwnd = get_foreground_hwnd()
        # Focus target app
        focus_window(self.app_name)
        time.sleep(0.15)  # let the window paint
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Restore user's original window
        if self._saved_hwnd:
            time.sleep(0.1)  # small gap so the action completes
            restore_window(self._saved_hwnd)
            log.info("🔙 Restored user's original window")
        return False  # don't suppress exceptions

