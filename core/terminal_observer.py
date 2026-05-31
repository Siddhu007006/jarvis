"""
core/terminal_observer.py — Terminal Error Detection (FIX 1A)
────────────────────────────
Monitors terminal windows for error patterns and emits semantic events.

Concept: Terminal Output as Semantic Gold
    Terminal windows contain the most actionable context for coding workflows:
    stack traces, build failures, git conflicts, npm errors. Currently Jarvis
    cannot read any of this without screenshots. This module reads terminal
    text via Windows UIA (accessibility tree) and emits targeted events.

Architecture:
    - Runs on its own polling loop (every 3 seconds via WorldState medium loop)
    - Reads focused terminal text via UIA TextPattern or ValuePattern
    - Matches against known error patterns (Traceback, npm ERR!, Build failed, etc.)
    - Emits semantic events: TERMINAL_ERROR_DETECTED, BUILD_FAILED, RUNTIME_EXCEPTION
    - Stores parsed terminal state in WorldState under a new "terminal" domain
    - Does NOT run moondream2 — pure UIA + regex

Detection categories:
    1. Python errors    — Traceback, ModuleNotFoundError, SyntaxError, TypeError
    2. Node/npm errors  — npm ERR!, ENOENT, MODULE_NOT_FOUND
    3. Build errors     — Build failed, Compilation error, FAILED
    4. Git conflicts    — CONFLICT, merge conflict, rebase
    5. Runtime crashes  — Segmentation fault, SIGKILL, Access violation
    6. Generic errors   — Error:, FATAL, panic

Anti-spam:
    - Same error signature is only emitted ONCE until it changes
    - Error hash prevents duplicate events for the same stack trace
    - Cooldown of 10 seconds between events of the same category

Install:
    pip install uiautomation  (already installed for WorldState)
"""

import hashlib
import logging
import re
import time
import threading
from typing import Optional

log = logging.getLogger("JARVIS.terminal_observer")


# ═══════════════════════════════════════════════════════════════
# ERROR PATTERN DEFINITIONS
# ═══════════════════════════════════════════════════════════════
#
# Concept: Each category has a list of regex patterns that match
# common terminal error signatures. Patterns are pre-compiled for
# performance since they run every 3 seconds.
#
# Categories map to semantic events:
#   python_error    → TERMINAL_ERROR_DETECTED (type=python)
#   node_error      → TERMINAL_ERROR_DETECTED (type=node)
#   build_error     → BUILD_FAILED
#   git_conflict    → TERMINAL_ERROR_DETECTED (type=git_conflict)
#   runtime_crash   → RUNTIME_EXCEPTION
#   generic_error   → TERMINAL_ERROR_DETECTED (type=generic)

_ERROR_PATTERNS = {
    "python_error": [
        re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE),
        re.compile(r"ModuleNotFoundError:\s+No module named", re.IGNORECASE),
        re.compile(r"ImportError:\s+cannot import name", re.IGNORECASE),
        re.compile(r"SyntaxError:\s+", re.IGNORECASE),
        re.compile(r"TypeError:\s+", re.IGNORECASE),
        re.compile(r"ValueError:\s+", re.IGNORECASE),
        re.compile(r"KeyError:\s+", re.IGNORECASE),
        re.compile(r"AttributeError:\s+", re.IGNORECASE),
        re.compile(r"NameError:\s+name .+ is not defined", re.IGNORECASE),
        re.compile(r"FileNotFoundError:\s+", re.IGNORECASE),
        re.compile(r"PermissionError:\s+", re.IGNORECASE),
        re.compile(r"RuntimeError:\s+", re.IGNORECASE),
        re.compile(r"IndentationError:\s+", re.IGNORECASE),
        re.compile(r"RecursionError:\s+", re.IGNORECASE),
        re.compile(r"AssertionError", re.IGNORECASE),
    ],

    "node_error": [
        re.compile(r"npm ERR!", re.IGNORECASE),
        re.compile(r"npm warn", re.IGNORECASE),
        re.compile(r"Error: Cannot find module", re.IGNORECASE),
        re.compile(r"MODULE_NOT_FOUND", re.IGNORECASE),
        re.compile(r"ENOENT:\s+no such file or directory", re.IGNORECASE),
        re.compile(r"ERR_MODULE_NOT_FOUND", re.IGNORECASE),
        re.compile(r"SyntaxError: Unexpected token", re.IGNORECASE),
        re.compile(r"ReferenceError:\s+\w+ is not defined", re.IGNORECASE),
        re.compile(r"TypeError:\s+Cannot read propert", re.IGNORECASE),
        re.compile(r"EACCES:\s+permission denied", re.IGNORECASE),
        re.compile(r"EADDRINUSE:\s+address already in use", re.IGNORECASE),
    ],

    "build_error": [
        re.compile(r"Build failed", re.IGNORECASE),
        re.compile(r"BUILD FAILED", re.IGNORECASE),
        re.compile(r"Compilation failed", re.IGNORECASE),
        re.compile(r"error TS\d+:", re.IGNORECASE),  # TypeScript errors
        re.compile(r"error CS\d+:", re.IGNORECASE),   # C# errors
        re.compile(r"error C\d+:", re.IGNORECASE),     # C/C++ errors
        re.compile(r"FAILED \(errors=\d+", re.IGNORECASE),  # pytest
        re.compile(r"FAIL\s+\w+", re.IGNORECASE),     # Jest/Go test failures
        re.compile(r"error\[E\d+\]:", re.IGNORECASE),  # Rust compiler
        re.compile(r"CMake Error", re.IGNORECASE),
        re.compile(r"make\[\d+\]: \*\*\* .+ Error", re.IGNORECASE),
        re.compile(r"gradle.*FAILED", re.IGNORECASE),
        re.compile(r"mvn.*BUILD FAILURE", re.IGNORECASE),
    ],

    "git_conflict": [
        re.compile(r"CONFLICT \(content\):", re.IGNORECASE),
        re.compile(r"Merge conflict in", re.IGNORECASE),
        re.compile(r"fix conflicts and then commit", re.IGNORECASE),
        re.compile(r"Automatic merge failed", re.IGNORECASE),
        re.compile(r"rebase.*--continue", re.IGNORECASE),
        re.compile(r"You have unmerged paths", re.IGNORECASE),
        re.compile(r"<<<<<<< HEAD", re.IGNORECASE),
    ],

    "runtime_crash": [
        re.compile(r"Segmentation fault", re.IGNORECASE),
        re.compile(r"SIGKILL", re.IGNORECASE),
        re.compile(r"SIGSEGV", re.IGNORECASE),
        re.compile(r"SIGABRT", re.IGNORECASE),
        re.compile(r"Access violation", re.IGNORECASE),
        re.compile(r"core dumped", re.IGNORECASE),
        re.compile(r"fatal error", re.IGNORECASE),
        re.compile(r"panic:", re.IGNORECASE),  # Go panics
        re.compile(r"Killed\s*$", re.IGNORECASE),  # OOM killer
        re.compile(r"OutOfMemoryError", re.IGNORECASE),
        re.compile(r"StackOverflowError", re.IGNORECASE),
    ],

    "generic_error": [
        re.compile(r"^Error:\s+", re.MULTILINE | re.IGNORECASE),
        re.compile(r"FATAL\s*:", re.IGNORECASE),
        re.compile(r"CRITICAL\s*:", re.IGNORECASE),
        re.compile(r"Exception in thread", re.IGNORECASE),
        re.compile(r"Unhandled exception", re.IGNORECASE),
        re.compile(r"command not found", re.IGNORECASE),
        re.compile(r"Permission denied", re.IGNORECASE),
        re.compile(r"Connection refused", re.IGNORECASE),
        re.compile(r"Connection timed out", re.IGNORECASE),
    ],
}

# Map categories to semantic events
_CATEGORY_EVENT_MAP = {
    "python_error":   "TERMINAL_ERROR_DETECTED",
    "node_error":     "TERMINAL_ERROR_DETECTED",
    "build_error":    "BUILD_FAILED",
    "git_conflict":   "TERMINAL_ERROR_DETECTED",
    "runtime_crash":  "RUNTIME_EXCEPTION",
    "generic_error":  "TERMINAL_ERROR_DETECTED",
}

# Exes that are considered "terminal" windows
_TERMINAL_EXES = frozenset({
    "windowsterminal.exe", "cmd.exe", "pwsh.exe", "powershell.exe",
    "conhost.exe", "mintty.exe", "alacritty.exe", "wezterm-gui.exe",
    "hyper.exe", "terminus.exe", "cmder.exe", "mobaxterm.exe",
    "gitbash.exe", "git-bash.exe",
})

# Anti-spam cooldown (seconds) — same category won't fire again within this window
_COOLDOWN_SECONDS = 10.0


# ═══════════════════════════════════════════════════════════════
# TERMINAL TEXT READER
# ═══════════════════════════════════════════════════════════════

def _read_terminal_text() -> str:
    """
    Read visible text from the active terminal window via UIA.

    Concept: Windows terminal controls expose their text content through
    the UIA accessibility tree. We try multiple strategies:
        1. TextPattern — richest, gives full text buffer
        2. ValuePattern — for simpler controls
        3. Control.Name — fallback, usually the title

    We only read the LAST 50 lines to keep pattern matching fast
    and avoid processing massive scrollback buffers.

    Returns:
        Last ~50 lines of terminal text, or empty string if not readable.
    """
    try:
        import uiautomation as uia

        # Get the focused control (should be the terminal text area)
        focused = uia.GetFocusedControl()
        if focused is None:
            return ""

        text = ""

        # Strategy 1: TextPattern (richest — gives full text content)
        try:
            text_pattern = focused.GetTextPattern()
            if text_pattern:
                doc_range = text_pattern.DocumentRange
                if doc_range:
                    text = doc_range.GetText(maxLength=8000)
        except Exception:
            pass

        # Strategy 2: ValuePattern (for simpler controls)
        if not text:
            try:
                value_pattern = focused.GetValuePattern()
                if value_pattern:
                    text = value_pattern.Value or ""
            except Exception:
                pass

        # Strategy 3: Walk child controls looking for text
        if not text:
            try:
                # Get the window, then find text-containing children
                window = focused.GetTopLevelControl()
                if window:
                    # Try to find the main text area
                    for child in window.GetChildren():
                        try:
                            tp = child.GetTextPattern()
                            if tp:
                                dr = tp.DocumentRange
                                if dr:
                                    text = dr.GetText(maxLength=8000)
                                    if text:
                                        break
                        except Exception:
                            continue
            except Exception:
                pass

        if not text:
            return ""

        # Only keep the last 50 lines (most relevant for error detection)
        lines = text.splitlines()
        if len(lines) > 50:
            lines = lines[-50:]

        return "\n".join(lines)

    except Exception as e:
        log.debug("Terminal text read failed: %s", e)
        return ""


# ═══════════════════════════════════════════════════════════════
# TERMINAL OBSERVER CLASS
# ═══════════════════════════════════════════════════════════════

class TerminalObserver:
    """
    Monitors terminal windows for error patterns and emits semantic events.

    Concept: Polling-based observer that runs every ~3 seconds.
    Only reads terminal text when a terminal-type app is in the foreground.
    Uses pre-compiled regex patterns for fast matching.

    Anti-spam: Each error category has a cooldown. The same error signature
    (hash of matched text) is only emitted once until it changes.

    Integration:
        - Created by main.py after WorldState
        - Reads active_exe from WorldState to know if terminal is focused
        - Emits events on the bus: TERMINAL_ERROR_DETECTED, BUILD_FAILED,
          RUNTIME_EXCEPTION
        - Optionally writes to WorldState "terminal" domain (if added)
    """

    def __init__(self, bus=None, world_state=None):
        self._bus = bus
        self._world_state = world_state
        self._running = False
        self._lock = threading.Lock()

        # Anti-spam state
        self._last_error_hash: str = ""
        self._category_cooldowns: dict[str, float] = {}

        # Stats
        self._stats = {
            "total_checks": 0,
            "terminal_reads": 0,
            "errors_detected": 0,
            "events_emitted": 0,
            "events_suppressed": 0,
        }

        # Current terminal state (for WorldState integration)
        self._current_state = {
            "has_error": False,
            "error_category": "",
            "error_summary": "",
            "last_error_time": 0.0,
        }

        log.info("TerminalObserver initialized (%d error categories, %d patterns)",
                 len(_ERROR_PATTERNS),
                 sum(len(p) for p in _ERROR_PATTERNS.values()))

    # ─── Lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        """Start the terminal monitoring loop (every 3 seconds)."""
        if self._running:
            return
        self._running = True
        t = threading.Thread(
            target=self._loop, daemon=True, name="terminal-observer"
        )
        t.start()
        log.info("TerminalObserver started (3s polling)")

    def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False

    # ─── Main Loop ────────────────────────────────────────────

    def _loop(self) -> None:
        """Poll every 3 seconds, only read when a terminal is focused."""
        while self._running:
            try:
                self._check()
            except Exception as e:
                log.error("TerminalObserver loop error: %s", e, exc_info=True)
            time.sleep(3.0)

    def _check(self) -> None:
        """
        Single check cycle.

        Steps:
            1. Check if active window is a terminal
            2. If yes, read terminal text via UIA
            3. Match against error patterns
            4. If match found and not anti-spammed, emit event
        """
        self._stats["total_checks"] += 1

        # Step 1: Check if terminal is focused
        active_exe = self._get_active_exe()
        if not active_exe or active_exe.lower() not in _TERMINAL_EXES:
            # Not a terminal — clear error state
            with self._lock:
                if self._current_state["has_error"]:
                    self._current_state["has_error"] = False
                    self._current_state["error_category"] = ""
                    self._current_state["error_summary"] = ""
            return

        # Step 2: Read terminal text
        self._stats["terminal_reads"] += 1
        text = _read_terminal_text()
        if not text:
            return

        # Step 3: Match error patterns
        match = self._match_errors(text)
        if match is None:
            # No errors found — clear state
            with self._lock:
                self._current_state["has_error"] = False
                self._current_state["error_category"] = ""
                self._current_state["error_summary"] = ""
            return

        category, matched_line = match
        self._stats["errors_detected"] += 1

        # Step 4: Anti-spam check
        error_hash = hashlib.md5(matched_line.encode()).hexdigest()[:12]

        if error_hash == self._last_error_hash:
            self._stats["events_suppressed"] += 1
            return

        now = time.monotonic()
        last_time = self._category_cooldowns.get(category, 0)
        if now - last_time < _COOLDOWN_SECONDS:
            self._stats["events_suppressed"] += 1
            return

        # Emit event
        self._last_error_hash = error_hash
        self._category_cooldowns[category] = now

        # Update current state
        summary = self._extract_error_summary(text, category)
        with self._lock:
            self._current_state["has_error"] = True
            self._current_state["error_category"] = category
            self._current_state["error_summary"] = summary
            self._current_state["last_error_time"] = time.time()

        # Emit semantic event
        event_name = _CATEGORY_EVENT_MAP.get(category, "TERMINAL_ERROR_DETECTED")
        event_data = {
            "category": category,
            "matched_line": matched_line.strip()[:200],
            "summary": summary,
            "error_hash": error_hash,
            "timestamp": time.time(),
        }

        if self._bus:
            self._bus.emit_async(event_name, event_data)
            self._stats["events_emitted"] += 1
            log.info("Terminal error detected: %s — %s", category, matched_line.strip()[:80])

    # ─── Pattern Matching ─────────────────────────────────────

    def _match_errors(self, text: str) -> Optional[tuple]:
        """
        Match text against all error patterns.

        Concept: Priority ordering — more specific categories are checked
        first (python, node, build, git), generic last. First match wins.

        Returns:
            Tuple of (category, matched_line) or None.
        """
        # Check in priority order — specific before generic
        priority_order = [
            "python_error",
            "node_error",
            "build_error",
            "git_conflict",
            "runtime_crash",
            "generic_error",
        ]

        for category in priority_order:
            patterns = _ERROR_PATTERNS.get(category, [])
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    # Find the full line containing the match
                    start = text.rfind("\n", 0, match.start()) + 1
                    end = text.find("\n", match.end())
                    if end == -1:
                        end = len(text)
                    matched_line = text[start:end]
                    return category, matched_line

        return None

    def _extract_error_summary(self, text: str, category: str) -> str:
        """
        Extract a human-readable error summary from terminal text.

        Concept: For each error category, we know the typical structure
        of the error output. We extract the most informative line(s).

        For Python tracebacks: the last line (the actual error message).
        For npm errors: the line starting with 'npm ERR!'.
        For build errors: the line containing 'error' or 'failed'.
        For git conflicts: the filename after 'CONFLICT'.
        """
        lines = text.strip().splitlines()
        if not lines:
            return ""

        if category == "python_error":
            # Python tracebacks: last non-empty line is the error message
            for line in reversed(lines):
                stripped = line.strip()
                if stripped and not stripped.startswith(">>>"):
                    # Check if it looks like an error line
                    if any(err in stripped for err in [
                        "Error:", "Error(", "error:",
                        "Traceback", "File ", "ModuleNotFoundError",
                    ]):
                        return stripped[:200]
                    # If we hit a line that looks like the actual error
                    if re.match(r"^\w+Error:", stripped):
                        return stripped[:200]
            # Fallback: last line
            return lines[-1].strip()[:200]

        elif category == "node_error":
            for line in lines:
                if "npm ERR!" in line:
                    return line.strip()[:200]
            return lines[-1].strip()[:200]

        elif category == "build_error":
            for line in lines:
                lower = line.lower()
                if "error" in lower or "failed" in lower:
                    return line.strip()[:200]
            return lines[-1].strip()[:200]

        elif category == "git_conflict":
            for line in lines:
                if "CONFLICT" in line or "conflict" in line:
                    return line.strip()[:200]
            return lines[-1].strip()[:200]

        else:
            # Generic: return the matched line
            return lines[-1].strip()[:200]

    # ─── Helpers ──────────────────────────────────────────────

    def _get_active_exe(self) -> str:
        """Get the active exe from WorldState or fallback to win32gui."""
        # Try WorldState first (already polled at 250ms)
        if self._world_state:
            try:
                windows = self._world_state.get("windows")
                if windows:
                    return windows.get("active_exe", "")
            except Exception:
                pass

        # Fallback to direct win32gui call
        try:
            import win32gui
            import win32process
            import psutil

            hwnd = win32gui.GetForegroundWindow()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid > 0:
                return psutil.Process(pid).name()
        except Exception:
            pass

        return ""

    # ─── Public API ───────────────────────────────────────────

    @property
    def state(self) -> dict:
        """Current terminal error state (thread-safe copy)."""
        with self._lock:
            return dict(self._current_state)

    @property
    def stats(self) -> dict:
        """Debug statistics."""
        return dict(self._stats)

    @property
    def has_error(self) -> bool:
        """Quick check if terminal currently shows an error."""
        with self._lock:
            return self._current_state["has_error"]
