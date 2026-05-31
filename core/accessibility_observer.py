"""
core/accessibility_observer.py — Accessibility Tree Observer (FIX 1B)
──────────────────────────────────
Monitors the Windows accessibility tree for UI state changes that
WorldState's focused control tracking doesn't cover.

Concept: Dialog/Modal/Notification Detection
    WorldState already tracks the FOCUSED control (name, type, bounds).
    But it doesn't detect STRUCTURAL changes in the UI tree:
        - New dialog windows appearing (Save As, Open File, etc.)
        - System notifications popping up
        - Permission prompts (UAC, browser permission bars)
        - Error dialogs (app crash dialogs, Windows error reporting)
        - Confirmation modals (delete confirmations, close unsaved, etc.)
        - Browser popups (alert(), confirm(), permission bars)

    These are critical because they BLOCK automation. If Jarvis doesn't
    know a dialog is open, it will try to interact with controls behind
    the dialog and fail silently.

Architecture:
    - Runs on its own polling loop (every 2 seconds)
    - Uses UIA to enumerate top-level windows and classify them
    - Detects new dialog/modal windows by comparing with previous scan
    - Emits DIALOG_DETECTED, NOTIFICATION_DETECTED events
    - Stores parsed state for WorldState/LLM injection

Detection method:
    We classify windows by their UIA ControlTypeName and properties:
    - Dialog/Modal: ControlType == "WindowControl" with IsModal or small size
    - Notification: ControlType == "ToolTipControl" or toast patterns
    - Permission: Known UIA names ("User Account Control", "Allow access", etc.)
    - Error: Known crash dialog patterns ("has stopped working", "Not Responding")

Install:
    pip install uiautomation  (already installed for WorldState)
"""

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("JARVIS.accessibility_observer")


# ═══════════════════════════════════════════════════════════════
# DIALOG / NOTIFICATION CLASSIFICATION
# ═══════════════════════════════════════════════════════════════

# Known dialog title patterns and their categories
_DIALOG_PATTERNS = {
    # System / UAC
    "permission": [
        "user account control",
        "windows security",
        "allow this app",
        "do you want to allow",
        "administrator",
        "elevation required",
        "smartscreen",
    ],

    # Error dialogs
    "error_dialog": [
        "has stopped working",
        "not responding",
        "encountered an error",
        "application error",
        "runtime error",
        "critical error",
        "windows error reporting",
        "problem caused this program",
        "crash report",
        "unhandled exception",
    ],

    # Save / File dialogs
    "file_dialog": [
        "save as",
        "open file",
        "select file",
        "choose file",
        "browse for folder",
        "save file",
        "export",
        "import",
        "select a folder",
    ],

    # Confirmation dialogs
    "confirmation": [
        "are you sure",
        "do you want to save",
        "confirm",
        "delete",
        "discard changes",
        "unsaved changes",
        "close without saving",
        "overwrite",
        "replace",
    ],

    # Browser prompts
    "browser_prompt": [
        "says",  # JavaScript alert/confirm pattern: "site.com says"
        "wants to",  # "site.com wants to send notifications"
        "allow notifications",
        "allow location",
        "allow camera",
        "allow microphone",
        "block",
        "pop-up blocked",
        "download",
    ],

    # Update dialogs
    "update": [
        "update available",
        "restart to update",
        "new version",
        "install update",
        "restart now",
        "update and restart",
    ],
}

# UIA control types that indicate a dialog/modal
_MODAL_CONTROL_TYPES = frozenset({
    "WindowControl",
    "PaneControl",
    "DialogControl",
})

# Max window size (width * height) to consider it a dialog vs full app
# Dialogs are typically small — under 800x600
_DIALOG_MAX_AREA = 800 * 600


def _classify_window(title: str) -> Optional[str]:
    """
    Classify a window title into a dialog category.

    Concept: Simple keyword matching against known dialog patterns.
    Returns the category name or None if it doesn't match any pattern.
    """
    title_lower = title.lower()

    for category, patterns in _DIALOG_PATTERNS.items():
        for pattern in patterns:
            if pattern in title_lower:
                return category

    return None


# ═══════════════════════════════════════════════════════════════
# ACCESSIBILITY OBSERVER CLASS
# ═══════════════════════════════════════════════════════════════

class AccessibilityObserver:
    """
    Monitors the accessibility tree for dialog/modal/notification changes.

    Concept: Structural UI change detection.
    While WorldState tracks the focused control, this observer detects
    NEW windows appearing (dialogs, modals, notifications, prompts).
    These are critical because they block automation and require
    special handling.

    Integration:
        - Created by main.py after WorldState
        - Runs its own 2-second polling loop
        - Emits DIALOG_DETECTED, DIALOG_DISMISSED events on the bus
        - Exposes .state property for WorldState/LLM injection
    """

    def __init__(self, bus=None, world_state=None):
        self._bus = bus
        self._world_state = world_state
        self._running = False
        self._lock = threading.Lock()

        # Track known dialog windows to detect new ones
        self._known_dialogs: dict[int, dict] = {}  # hwnd -> dialog info

        # Current state
        self._current_state = {
            "has_dialog": False,
            "dialog_type": "",      # permission, error_dialog, file_dialog, etc.
            "dialog_title": "",
            "dialog_count": 0,
            "dialogs": [],          # List of active dialog infos
        }

        # Stats
        self._stats = {
            "total_checks": 0,
            "dialogs_detected": 0,
            "dialogs_dismissed": 0,
            "events_emitted": 0,
        }

        log.info("AccessibilityObserver initialized (%d dialog categories)",
                 len(_DIALOG_PATTERNS))

    # ─── Lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        """Start the accessibility monitoring loop (every 2 seconds)."""
        if self._running:
            return
        self._running = True
        t = threading.Thread(
            target=self._loop, daemon=True, name="accessibility-observer"
        )
        t.start()
        log.info("AccessibilityObserver started (2s polling)")

    def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False

    # ─── Main Loop ────────────────────────────────────────────

    def _loop(self) -> None:
        """Poll every 2 seconds for dialog/modal changes."""
        while self._running:
            try:
                self._check()
            except Exception as e:
                log.error("AccessibilityObserver loop error: %s", e, exc_info=True)
            time.sleep(2.0)

    def _check(self) -> None:
        """
        Single check cycle.

        Steps:
            1. Enumerate top-level windows via UIA
            2. Identify dialog/modal windows (by size, type, or title)
            3. Compare with previous scan to detect new/dismissed dialogs
            4. Emit events for new dialogs
            5. Update current state
        """
        self._stats["total_checks"] += 1

        # Step 1 + 2: Find dialog windows
        current_dialogs = self._find_dialogs()

        # Step 3: Detect new dialogs (appeared since last check)
        current_hwnds = set(current_dialogs.keys())
        known_hwnds = set(self._known_dialogs.keys())

        new_hwnds = current_hwnds - known_hwnds
        dismissed_hwnds = known_hwnds - current_hwnds

        # Step 4: Emit events for new dialogs
        for hwnd in new_hwnds:
            dialog_info = current_dialogs[hwnd]
            self._stats["dialogs_detected"] += 1

            if self._bus:
                self._bus.emit_async("DIALOG_DETECTED", dialog_info)
                self._stats["events_emitted"] += 1
                log.info("Dialog detected: %s — \"%s\"",
                         dialog_info["type"], dialog_info["title"][:60])

        # Emit events for dismissed dialogs
        for hwnd in dismissed_hwnds:
            dialog_info = self._known_dialogs[hwnd]
            self._stats["dialogs_dismissed"] += 1

            if self._bus:
                self._bus.emit_async("DIALOG_DISMISSED", dialog_info)
                log.info("Dialog dismissed: %s — \"%s\"",
                         dialog_info["type"], dialog_info["title"][:60])

        # Step 5: Update tracking state
        self._known_dialogs = current_dialogs

        with self._lock:
            if current_dialogs:
                # Use the most important dialog (by category priority)
                primary = self._get_primary_dialog(current_dialogs)
                self._current_state["has_dialog"] = True
                self._current_state["dialog_type"] = primary["type"]
                self._current_state["dialog_title"] = primary["title"]
                self._current_state["dialog_count"] = len(current_dialogs)
                self._current_state["dialogs"] = list(current_dialogs.values())
            else:
                self._current_state["has_dialog"] = False
                self._current_state["dialog_type"] = ""
                self._current_state["dialog_title"] = ""
                self._current_state["dialog_count"] = 0
                self._current_state["dialogs"] = []

    # ─── Dialog Finder ────────────────────────────────────────

    def _find_dialogs(self) -> dict:
        """
        Enumerate top-level windows and identify dialogs/modals.

        Concept: We use win32gui to enumerate all visible top-level windows,
        then check each one for dialog characteristics:
            - Title matches known dialog patterns
            - Window is small (< _DIALOG_MAX_AREA) and has a parent
            - UIA reports it as a modal window

        Returns:
            Dict of hwnd -> dialog_info for all identified dialog windows.
        """
        dialogs = {}

        try:
            import win32gui
            import win32con

            active_hwnd = win32gui.GetForegroundWindow()
            active_title = win32gui.GetWindowText(active_hwnd) or ""

            def enum_callback(hwnd, results):
                """Callback for EnumWindows — check each visible window."""
                try:
                    # Skip invisible or minimized windows
                    if not win32gui.IsWindowVisible(hwnd):
                        return True

                    title = win32gui.GetWindowText(hwnd) or ""
                    if not title:
                        return True

                    # Skip the main active window itself (it's not a dialog)
                    # But check if the active window IS a dialog
                    is_foreground = (hwnd == active_hwnd)

                    # Check 1: Title matches a dialog pattern
                    category = _classify_window(title)

                    # Check 2: Window is small enough to be a dialog
                    is_small = False
                    try:
                        rect = win32gui.GetWindowRect(hwnd)
                        w = rect[2] - rect[0]
                        h = rect[3] - rect[1]
                        area = w * h
                        is_small = (area > 0 and area < _DIALOG_MAX_AREA)
                    except Exception:
                        pass

                    # Check 3: Window has WS_EX_DLGMODALFRAME style (modal dialog)
                    is_modal = False
                    try:
                        style_ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                        is_modal = bool(style_ex & win32con.WS_EX_DLGMODALFRAME)
                    except Exception:
                        pass

                    # Check 4: Window has an owner (sub-window, likely a dialog)
                    has_owner = False
                    try:
                        owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
                        has_owner = (owner != 0)
                    except Exception:
                        pass

                    # Decision: Is this a dialog?
                    is_dialog = False

                    if category:
                        # Known dialog pattern in title — definitely a dialog
                        is_dialog = True
                    elif is_modal:
                        # Modal style flag — definitely a dialog
                        is_dialog = True
                        if not category:
                            category = "modal"
                    elif is_small and has_owner:
                        # Small sub-window — likely a dialog
                        is_dialog = True
                        if not category:
                            category = "unknown_dialog"

                    if is_dialog:
                        results[hwnd] = {
                            "hwnd": hwnd,
                            "title": title,
                            "type": category or "unknown_dialog",
                            "is_foreground": is_foreground,
                            "is_modal": is_modal,
                            "timestamp": time.time(),
                        }

                except Exception:
                    pass

                return True  # Continue enumeration

            win32gui.EnumWindows(enum_callback, dialogs)

        except ImportError:
            log.debug("win32gui not available for dialog detection")
        except Exception as e:
            log.debug("Dialog enumeration failed: %s", e)

        return dialogs

    def _get_primary_dialog(self, dialogs: dict) -> dict:
        """
        Get the most important dialog from the current set.

        Priority order: permission > error_dialog > confirmation >
        browser_prompt > file_dialog > update > modal > unknown_dialog
        """
        priority = [
            "permission", "error_dialog", "confirmation",
            "browser_prompt", "file_dialog", "update",
            "modal", "unknown_dialog",
        ]

        for cat in priority:
            for info in dialogs.values():
                if info["type"] == cat:
                    return info

        # Fallback: any dialog
        return next(iter(dialogs.values()))

    # ─── Public API ───────────────────────────────────────────

    @property
    def state(self) -> dict:
        """Current accessibility state (thread-safe copy)."""
        with self._lock:
            return dict(self._current_state)

    @property
    def stats(self) -> dict:
        """Debug statistics."""
        return dict(self._stats)

    @property
    def has_dialog(self) -> bool:
        """Quick check if any dialog is currently open."""
        with self._lock:
            return self._current_state["has_dialog"]

    @property
    def dialog_type(self) -> str:
        """Type of the primary dialog, or empty string."""
        with self._lock:
            return self._current_state["dialog_type"]
