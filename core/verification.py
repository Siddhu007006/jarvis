"""
core/verification.py — Legacy Validation Engine for Jarvis.

⚠️ SOFT DEPRECATED (V5.1): This module is superseded by core/validator.py
which provides WorldState-first verification, confidence scoring, failure
typing, scoped snapshots, and field-level diffing. New code should import
from core.validator import Validator instead.

This file is kept for backward compatibility only. It will NOT be deleted
until all callers have migrated to the new Validator class.

See: core/validator.py for the new implementation.
Blueprint Section 14: Verification Engine.
Blueprint Principle 4: "Every action must be verified."

Concept: After the executor runs an action, we don't trust it blindly.
We verify the action actually succeeded using multiple independent sources.
This prevents the #1 failure mode in AI desktop assistants: the AI thinks
it did something, but the action silently failed, leading to hallucinated
state and cascading errors.

Verification Sources (from blueprint, in priority order):
  1. Process state    — Is the expected process running? (psutil)
  2. Window focus     — Is the correct window in the foreground? (Win32)
  3. Accessibility    — Can we find the expected UI element? (pywinauto)
  4. OCR              — Is the expected text visible on screen? (optional)
  5. Screenshot diff  — Did the screen change? (pixel comparison)

Usage:
    from core.verification import ValidationEngine
    ve = ValidationEngine(bus=get_bus())
    result = ve.verify_action("open_app", {"app_name": "chrome"})
    # result = {"verified": True, "method": "process", "detail": "chrome.exe PID 1234"}
"""

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)


class ValidationEngine:
    """
    Verifies that actions actually succeeded using multiple independent sources.

    Blueprint Principle 4: Verification Loops Are Mandatory.

    Without verification:
      - Hallucinations occur (AI thinks Chrome is open, but it isn't)
      - State becomes inconsistent (FSM says SPEAKING but audio failed)
      - Tasks fail silently (step 3 depends on step 2 which silently failed)

    The engine uses a cascade of verification methods, from fastest/cheapest
    (process check) to slowest/most expensive (screenshot analysis).
    """

    def __init__(self, bus=None):
        self._bus = bus
        # Lazy-initialize process monitor to avoid circular imports
        self._pm = None

    def _get_pm(self):
        """Lazy-load ProcessMonitor."""
        if self._pm is None:
            from core.process_monitor import ProcessMonitor
            self._pm = ProcessMonitor(bus=self._bus)
        return self._pm

    def verify_action(self, action_type: str, params: dict) -> dict:
        """
        Route to the appropriate verification method based on action type.

        Args:
            action_type: The tool name (e.g. "open_app", "computer_control")
            params:      The parameters that were passed to the tool

        Returns:
            Dict with:
              - verified: bool — whether the action was confirmed successful
              - method:   str  — which verification method succeeded
              - detail:   str  — human-readable explanation
        """
        verifiers = {
            "open_app":         self._verify_open_app,
            "close_app":        self._verify_close_app,
            "computer_control": self._verify_computer_control,
            "run_command":      self._verify_run_command,
            "system_control":   self._verify_system_control,
        }

        verifier = verifiers.get(action_type)
        if not verifier:
            # No specific verifier — assume success (tools like web_search,
            # file_manager already return their own success/failure indicators)
            return {"verified": True, "method": "passthrough", "detail": "No specific verifier for this action"}

        try:
            result = verifier(params)
            # Emit event on bus
            if self._bus:
                from core.events import Event
                event = Event.VALIDATION_PASSED if result["verified"] else Event.VALIDATION_FAILED
                self._bus.emit_async(event, {
                    "action": action_type,
                    "params": params,
                    "result": result,
                })
            return result
        except Exception as e:
            log.warning("Verification error for %s: %s", action_type, e)
            return {"verified": False, "method": "error", "detail": str(e)}

    # ══════════════════════════════════════════════════════════
    #  ACTION-SPECIFIC VERIFIERS
    # ══════════════════════════════════════════════════════════

    def _verify_open_app(self, params: dict) -> dict:
        """
        Verify an app was opened successfully.

        Verification cascade:
          1. Process check (psutil) — is the exe running?
          2. Window check (Win32)  — is a window with the app name visible?
        """
        app_name = params.get("app_name", "")
        if not app_name:
            return {"verified": False, "method": "params", "detail": "No app_name provided"}

        pm = self._get_pm()

        # Layer 1: Process check (fastest, most reliable)
        if pm.is_running(app_name):
            pid = pm.get_pid(app_name)
            log.info("✅ Verified: '%s' is running (PID %s)", app_name, pid)
            return {"verified": True, "method": "process", "detail": f"{app_name} running (PID {pid})"}

        # Layer 2: Window title check (process might use different name)
        try:
            from actions.window_manager import find_window
            win = find_window(app_name)
            if win:
                log.info("✅ Verified: '%s' window found: '%s'", app_name, win.get("title", ""))
                return {"verified": True, "method": "window", "detail": f"Window: {win.get('title', '')}"}
        except Exception as e:
            log.debug("Window check failed: %s", e)

        # Layer 3: Wait briefly and retry process check
        # Some apps take a moment to register in the process table
        time.sleep(1.5)
        pm._cache_time = 0  # force refresh
        if pm.is_running(app_name):
            pid = pm.get_pid(app_name)
            log.info("✅ Verified (delayed): '%s' is running (PID %s)", app_name, pid)
            return {"verified": True, "method": "process_delayed", "detail": f"{app_name} running after delay (PID {pid})"}

        log.warning("❌ Verification failed: '%s' not detected", app_name)
        return {"verified": False, "method": "process", "detail": f"{app_name} not found in process table or window list"}

    def _verify_close_app(self, params: dict) -> dict:
        """Verify an app was closed — it should NOT be in the process table."""
        app_name = params.get("app_name", "")
        if not app_name:
            return {"verified": False, "method": "params", "detail": "No app_name"}

        pm = self._get_pm()

        # Wait a moment for process to terminate
        time.sleep(1.0)
        pm._cache_time = 0

        if not pm.is_running(app_name):
            log.info("✅ Verified: '%s' is closed", app_name)
            return {"verified": True, "method": "process", "detail": f"{app_name} no longer running"}
        else:
            log.warning("❌ Verification failed: '%s' still running", app_name)
            return {"verified": False, "method": "process", "detail": f"{app_name} still running"}

    def _verify_computer_control(self, params: dict) -> dict:
        """
        Verify a computer_control action.

        Strategy depends on the specific action:
          - focus_window: check if the correct window has focus
          - screen_click: check if the click target was found (result-based)
          - Others: passthrough (hard to verify programmatically)
        """
        action = params.get("action", "")

        if action == "focus_window":
            title = params.get("title", "")
            return self._verify_window_focused(title)

        # For screen_click, screen_find — verification is result-based
        # (the tool itself returns success/failure). We trust that.
        return {"verified": True, "method": "passthrough", "detail": f"Action '{action}' — result-based verification"}

    def _verify_window_focused(self, title: str) -> dict:
        """
        Verify that a window with the given title is in the foreground.

        Uses Win32 GetForegroundWindow API for ground-truth.
        """
        if not title:
            return {"verified": False, "method": "params", "detail": "No title"}

        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()

            if not hwnd:
                return {"verified": False, "method": "win32", "detail": "No foreground window"}

            # Get window title
            length = user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(length)
            user32.GetWindowTextW(hwnd, buf, length)
            fg_title = buf.value.lower()

            if title.lower() in fg_title:
                log.info("✅ Verified: '%s' is focused (title: '%s')", title, buf.value)
                return {"verified": True, "method": "win32_focus", "detail": f"Window focused: {buf.value}"}
            else:
                log.warning("❌ Focus check failed: wanted '%s', got '%s'", title, buf.value)
                return {"verified": False, "method": "win32_focus", "detail": f"Wrong window focused: {buf.value}"}

        except Exception as e:
            log.warning("Win32 focus check failed: %s", e)
            return {"verified": False, "method": "error", "detail": str(e)}

    def _verify_run_command(self, params: dict) -> dict:
        """
        Verify a command execution.

        Commands return their output directly, so verification is result-based.
        We can only check if the output looks like an error.
        """
        # Result-based — the executor already checks for failure indicators
        return {"verified": True, "method": "passthrough", "detail": "Command output is self-verifying"}

    def _verify_system_control(self, params: dict) -> dict:
        """
        Verify system control actions where possible.

        Some actions are verifiable (volume, brightness), others aren't
        (media_play — we can't easily check if music is playing).
        """
        action = params.get("action", "")

        # For shutdown/restart/sleep — if we're still running, it failed
        if action in ("shutdown", "restart", "sleep"):
            # Can't really verify these — if we're here, it either worked
            # or the user cancelled. Give it a moment.
            time.sleep(2.0)
            return {"verified": True, "method": "timeout", "detail": "System command issued"}

        # Volume verification
        if action in ("volume_set", "volume_up", "volume_down", "volume_mute"):
            try:
                from actions.system_control import _get_volume
                vol = _get_volume()
                if vol is not None:
                    return {"verified": True, "method": "api", "detail": f"Volume at {vol}%"}
            except Exception:
                pass

        return {"verified": True, "method": "passthrough", "detail": f"System action '{action}' — limited verification"}

    # ══════════════════════════════════════════════════════════
    #  GENERIC VERIFICATION HELPERS
    # ══════════════════════════════════════════════════════════

    def verify_app_opened(self, app_name: str) -> bool:
        """Quick check: is this app running? Used by executor directly."""
        return self._get_pm().is_running(app_name)

    def verify_window_focused(self, title: str) -> bool:
        """Quick check: is this window focused?"""
        result = self._verify_window_focused(title)
        return result["verified"]

    def verify_text_present(self, text: str) -> bool:
        """
        OCR the screen to verify expected text is visible.

        Layer 4 verification — expensive, used as last resort.
        Only called when faster methods fail.
        """
        try:
            import pyautogui
            from PIL import Image
            import io

            screenshot = pyautogui.screenshot()
            # Convert to bytes for OCR
            buf = io.BytesIO()
            screenshot.save(buf, format='PNG')
            buf.seek(0)

            # Try EasyOCR if available
            try:
                import easyocr
                reader = easyocr.Reader(['en'], gpu=False, verbose=False)
                results = reader.readtext(buf.getvalue())
                all_text = " ".join(r[1] for r in results).lower()
                return text.lower() in all_text
            except ImportError:
                pass

            # Fallback: pytesseract
            try:
                import pytesseract
                ocr_text = pytesseract.image_to_string(screenshot).lower()
                return text.lower() in ocr_text
            except ImportError:
                pass

            log.warning("No OCR engine available (install easyocr or pytesseract)")
            return False

        except Exception as e:
            log.warning("OCR verification failed: %s", e)
            return False
