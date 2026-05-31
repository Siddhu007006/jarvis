"""
core/process_monitor.py — Process Awareness System for Jarvis.

Blueprint Section 15: Process Awareness System.

Concept: Instead of guessing whether an app is running (which causes
hallucinated state), we use psutil to get ground-truth process data.
This module centralizes ALL process detection, replacing ad-hoc
psutil.process_iter() calls scattered across the codebase.

The ProcessMonitor:
  - Detects running applications by name
  - Waits for processes to start (with timeout)
  - Tracks process lifecycle (PROCESS_DETECTED / PROCESS_CLOSED events)
  - Reports system stats (CPU, RAM) for performance monitoring
  - Maps friendly app names to executable names

Usage:
    from core.process_monitor import ProcessMonitor
    pm = ProcessMonitor(bus=get_bus())
    pm.is_running("chrome")     # → True/False
    pm.wait_for_process("chrome", timeout=10)  # → True when found
    pm.get_system_stats()       # → {"cpu": 5.2, "ram_gb": 3.1}
"""

import logging
import time
import threading
from typing import Optional

log = logging.getLogger(__name__)

# Friendly name → possible executable names mapping
# Concept: Users say "Chrome" but the process is "chrome.exe".
# This map bridges the gap between natural language and process table.
APP_PROCESS_MAP = {
    # Browsers
    "chrome":       ["chrome.exe"],
    "google chrome": ["chrome.exe"],
    "firefox":      ["firefox.exe"],
    "edge":         ["msedge.exe"],
    "microsoft edge": ["msedge.exe"],
    "brave":        ["brave.exe"],
    "opera":        ["opera.exe"],
    "vivaldi":      ["vivaldi.exe"],

    # Communication
    "discord":      ["discord.exe", "update.exe"],
    "slack":        ["slack.exe"],
    "teams":        ["ms-teams.exe", "teams.exe"],
    "microsoft teams": ["ms-teams.exe", "teams.exe"],
    "whatsapp":     ["whatsapp.exe"],
    "telegram":     ["telegram.exe"],
    "zoom":         ["zoom.exe"],

    # Media
    "spotify":      ["spotify.exe"],
    "vlc":          ["vlc.exe"],
    "foobar":       ["foobar2000.exe"],

    # Development
    "code":         ["code.exe"],
    "vs code":      ["code.exe"],
    "visual studio code": ["code.exe"],
    "visual studio": ["devenv.exe"],
    "pycharm":      ["pycharm64.exe", "pycharm.exe"],
    "intellij":     ["idea64.exe", "idea.exe"],
    "sublime":      ["sublime_text.exe"],
    "notepad":      ["notepad.exe"],
    "notepad++":    ["notepad++.exe"],
    "terminal":     ["windowsterminal.exe", "wt.exe"],
    "windows terminal": ["windowsterminal.exe", "wt.exe"],
    "powershell":   ["powershell.exe", "pwsh.exe"],
    "cmd":          ["cmd.exe"],

    # Productivity
    "word":         ["winword.exe"],
    "excel":        ["excel.exe"],
    "powerpoint":   ["powerpnt.exe"],
    "outlook":      ["outlook.exe"],
    "onenote":      ["onenote.exe"],

    # File management
    "explorer":     ["explorer.exe"],
    "file explorer": ["explorer.exe"],

    # System
    "task manager":  ["taskmgr.exe"],
    "settings":      ["systemsettings.exe"],
    "control panel": ["control.exe"],
    "calculator":    ["calculatorapp.exe", "calc.exe"],

    # Trading
    "tradingview":   ["tradingview.exe"],

    # Games
    "steam":        ["steam.exe"],
    "epic games":   ["epicgameslauncher.exe"],
}


def _get_exe_names(app_name: str) -> list[str]:
    """
    Convert a friendly app name to possible executable names.

    Strategy:
      1. Check the APP_PROCESS_MAP first (exact matches)
      2. If not found, generate common patterns: name.exe, name64.exe
    """
    key = app_name.lower().strip()
    if key in APP_PROCESS_MAP:
        return APP_PROCESS_MAP[key]

    # Generate reasonable guesses
    base = key.replace(" ", "")
    return [f"{base}.exe", f"{base}64.exe"]


class ProcessMonitor:
    """
    Centralized process awareness system.

    Blueprint Section 15: Use psutil for deterministic app detection
    instead of guessing or screenshot-based approaches.

    Thread-safe — all methods can be called from any thread.
    """

    def __init__(self, bus=None):
        self._bus = bus
        self._cache_ttl = 2.0  # seconds
        self._cache_time = 0.0
        self._cache: dict[str, int] = {}  # exe_name → pid
        self._lock = threading.Lock()

    def _refresh_cache(self) -> None:
        """Refresh the process name cache if stale."""
        now = time.monotonic()
        if now - self._cache_time < self._cache_ttl:
            return

        try:
            import psutil
            new_cache = {}
            for proc in psutil.process_iter(['name', 'pid']):
                try:
                    name = proc.info['name']
                    if name:
                        new_cache[name.lower()] = proc.info['pid']
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            with self._lock:
                self._cache = new_cache
                self._cache_time = now
        except Exception as e:
            log.warning("Process cache refresh failed: %s", e)

    def is_running(self, app_name: str) -> bool:
        """
        Check if an application is currently running.

        Args:
            app_name: Friendly name (e.g. "chrome", "Spotify")

        Returns:
            True if any matching executable is found in the process table.

        Concept: This is the ground-truth check. Instead of asking AI
        "is Chrome open?" (which can hallucinate), we check the actual
        process table. This is deterministic and instant.
        """
        self._refresh_cache()
        exe_names = _get_exe_names(app_name)

        with self._lock:
            for exe in exe_names:
                if exe.lower() in self._cache:
                    return True
        return False

    def get_pid(self, app_name: str) -> Optional[int]:
        """Get the PID of a running application, or None."""
        self._refresh_cache()
        exe_names = _get_exe_names(app_name)

        with self._lock:
            for exe in exe_names:
                pid = self._cache.get(exe.lower())
                if pid:
                    return pid
        return None

    def get_running_apps(self) -> list[str]:
        """
        Get a list of all running application executable names.

        Returns:
            Sorted list of unique exe names currently in the process table.
        """
        self._refresh_cache()
        with self._lock:
            return sorted(set(self._cache.keys()))

    def wait_for_process(self, app_name: str, timeout: float = 10.0) -> bool:
        """
        Block until the application appears in the process table.

        Args:
            app_name: Friendly name (e.g. "chrome")
            timeout:  Maximum seconds to wait

        Returns:
            True if the process was found within the timeout.

        Concept: After launching an app, we can't just assume it started.
        Blueprint Principle 4 (Verification Loops) requires us to verify.
        This method polls the process table until the app appears or times out.
        """
        deadline = time.monotonic() + timeout
        exe_names = _get_exe_names(app_name)

        while time.monotonic() < deadline:
            # Force cache refresh
            self._cache_time = 0.0
            self._refresh_cache()

            with self._lock:
                for exe in exe_names:
                    if exe.lower() in self._cache:
                        log.info("Process '%s' detected (exe: %s)", app_name, exe)
                        if self._bus:
                            try:
                                from core.events import Event
                                self._bus.emit(Event.PROCESS_DETECTED, {
                                    "app": app_name, "exe": exe,
                                    "pid": self._cache.get(exe.lower())
                                })
                            except Exception:
                                pass
                        return True

            time.sleep(0.5)

        log.warning("Process '%s' not found after %.1fs", app_name, timeout)
        return False

    def get_system_stats(self) -> dict:
        """
        Get current system performance metrics.

        Returns:
            Dict with cpu_percent, ram_used_gb, ram_total_gb, ram_percent.

        Used for Blueprint Section 21 (Performance Targets):
          - Idle: < 3% CPU, < 1 GB RAM
          - Active: 2-4 second latency
        """
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0.5)
            mem = psutil.virtual_memory()
            return {
                "cpu_percent": cpu,
                "ram_used_gb": round(mem.used / (1024 ** 3), 1),
                "ram_total_gb": round(mem.total / (1024 ** 3), 1),
                "ram_percent": mem.percent,
            }
        except Exception as e:
            log.warning("System stats failed: %s", e)
            return {"cpu_percent": 0, "ram_used_gb": 0, "ram_total_gb": 0, "ram_percent": 0}
