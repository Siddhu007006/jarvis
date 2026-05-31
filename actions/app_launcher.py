"""
App Launcher — Opens and closes applications on Windows.

v3.3: Dynamic app discovery via Get-StartApps.
      Can open ANY installed app, not just hardcoded ones.
      Also handles URLs and website shortcuts.

      Bug fixes (adapted from Linux bug diagnosis):
      - Bug 1: If app is running but minimized, raises it instead of launching new
      - Bug 3: Uses psutil.process_iter() to check REAL process state, never cached
"""

import subprocess
import os
import webbrowser
import logging
import re
import threading
import psutil

log = logging.getLogger(__name__)


# ── Dynamic App Cache (built from Get-StartApps at startup) ───
# Format: { "app name lowercase": "AppId" }
_installed_apps: dict[str, str] = {}
_apps_loaded = threading.Event()


def _build_app_cache():
    """Scan all installed apps via PowerShell Get-StartApps. Runs once at import."""
    global _installed_apps
    try:
        # Use pipe-separated output (more reliable than JSON for large lists)
        ps_cmd = (
            "Get-StartApps | ForEach-Object { "
            "$_.Name + '|||' + $_.AppId "
            "}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if "|||" not in line:
                    continue
                parts = line.split("|||", 1)
                if len(parts) == 2:
                    name = parts[0].strip()
                    app_id = parts[1].strip()
                    if name and app_id:
                        _installed_apps[name.lower()] = app_id
            log.info("📦 Cached %d installed apps", len(_installed_apps))
    except Exception as e:
        log.warning("⚠️ Failed to build app cache: %s", e)
    finally:
        _apps_loaded.set()


# Build cache in background thread at import time
threading.Thread(target=_build_app_cache, daemon=True).start()


# ── Known exe shortcuts (fast path, no PowerShell needed) ─────
_EXE_SHORTCUTS: dict[str, str] = {
    "chrome":           "chrome.exe",
    "google chrome":    "chrome.exe",
    "edge":             "msedge.exe",
    "microsoft edge":   "msedge.exe",
    "firefox":          "firefox.exe",
    "brave":            "brave.exe",
    "brave browser":    "brave.exe",
    "opera":            "opera.exe",
    "notepad":          "notepad.exe",
    "calculator":       "calc.exe",
    "calc":             "calc.exe",
    "explorer":         "explorer.exe",
    "file explorer":    "explorer.exe",
    "cmd":              "cmd.exe",
    "command prompt":   "cmd.exe",
    "terminal":         "wt.exe",
    "windows terminal": "wt.exe",
    "powershell":       "powershell.exe",
    "vscode":           "code",
    "vs code":          "code",
    "visual studio code": "code",
    "paint":            "mspaint.exe",
    "task manager":     "taskmgr.exe",
    "snipping tool":    "snippingtool.exe",
    "settings":         "ms-settings:",
    "windows settings": "ms-settings:",
}

# ── Process name mapping (for psutil-based live state check) ──
# Maps friendly names → process names as seen in psutil
_PROCESS_NAMES: dict[str, str] = {
    "chrome":           "chrome.exe",
    "google chrome":    "chrome.exe",
    "edge":             "msedge.exe",
    "microsoft edge":   "msedge.exe",
    "firefox":          "firefox.exe",
    "brave":            "brave.exe",
    "brave browser":    "brave.exe",
    "opera":            "opera.exe",
    "notepad":          "notepad.exe",
    "calculator":       "CalculatorApp.exe",
    "calc":             "CalculatorApp.exe",
    "vscode":           "Code.exe",
    "vs code":          "Code.exe",
    "visual studio code": "Code.exe",
    "spotify":          "Spotify.exe",
    "discord":          "Discord.exe",
    "slack":            "slack.exe",
    "terminal":         "WindowsTerminal.exe",
    "windows terminal": "WindowsTerminal.exe",
    "paint":            "mspaint.exe",
    "task manager":     "Taskmgr.exe",
    "whatsapp":         "WhatsApp.exe",
    "telegram":         "Telegram.exe",
    "zoom":             "Zoom.exe",
    "vlc":              "vlc.exe",
}

# ── Window title keywords (for focusing existing windows) ─────
_WINDOW_TITLES: dict[str, str] = {
    "chrome":       "Chrome",
    "google chrome": "Chrome",
    "edge":         "Edge",
    "microsoft edge": "Edge",
    "firefox":      "Firefox",
    "brave":        "Brave",
    "notepad":      "Notepad",
    "vscode":       "Visual Studio Code",
    "vs code":      "Visual Studio Code",
    "visual studio code": "Visual Studio Code",
    "spotify":      "Spotify",
    "discord":      "Discord",
    "slack":        "Slack",
    "terminal":     "Terminal",
    "windows terminal": "Terminal",
    "paint":        "Paint",
    "vlc":          "VLC",
    "whatsapp":     "WhatsApp",
    "telegram":     "Telegram",
    "zoom":         "Zoom",
}


def is_app_running(app_name: str) -> tuple[bool, list]:
    """
    Check if an app is actually running RIGHT NOW using psutil.
    Reads the live process table from the OS kernel — impossible to be stale.
    
    This is the fix for Bug 3: never trust a cached variable.
    Always ask the OS what is actually running.
    
    Returns (is_running, list_of_matching_pids).
    """
    name_lower = app_name.lower().strip()
    search_name = _PROCESS_NAMES.get(name_lower, name_lower)
    if search_name.endswith(".exe"):
        search_name = search_name[:-4]
    
    matches = []
    for proc in psutil.process_iter(["pid", "name", "status"]):
        try:
            pname = (proc.info["name"] or "").lower()
            if not pname:
                continue
            if pname.endswith(".exe"):
                pname = pname[:-4]
                
            if pname == search_name:
                # Verify it's actually alive, not a zombie
                if proc.info["status"] != psutil.STATUS_ZOMBIE:
                    matches.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    
    is_running = len(matches) > 0
    if is_running:
        log.debug("📋 '%s' IS running (pids: %s)", app_name, matches[:3])
    else:
        log.debug("📋 '%s' NOT running", app_name)
    
    return is_running, matches


# ── URL Detection ─────────────────────────────────────────────
_URL_RE = re.compile(
    r"^(https?://)"
    r"|(\w+\.(com|org|net|io|dev|ai|co|in|edu|gov|me|app|xyz|tv|gg|ly))",
    re.IGNORECASE
)

_WEBSITE_SHORTCUTS = {
    "youtube":       "https://www.youtube.com",
    "google":        "https://www.google.com",
    "gmail":         "https://mail.google.com",
    "github":        "https://www.github.com",
    "twitter":       "https://www.twitter.com",
    "x":             "https://www.x.com",
    "reddit":        "https://www.reddit.com",
    "instagram":     "https://www.instagram.com",
    "facebook":      "https://www.facebook.com",
    "linkedin":      "https://www.linkedin.com",
    "chatgpt":       "https://chat.openai.com",
    "netflix":       "https://www.netflix.com",
    "amazon":        "https://www.amazon.in",
    "whatsapp web":  "https://web.whatsapp.com",
    "stackoverflow": "https://stackoverflow.com",
}


def _fuzzy_find_installed(name: str) -> str | None:
    """Fuzzy match against all installed apps. Returns AppId or None."""
    _apps_loaded.wait(timeout=5)  # wait for cache to build

    if not _installed_apps:
        return None

    # Exact match
    if name in _installed_apps:
        return _installed_apps[name]

    # Substring match (prefer shorter app name = more specific match)
    candidates = []
    for app_name, app_id in _installed_apps.items():
        if name in app_name or app_name in name:
            candidates.append((app_name, app_id))

    if candidates:
        # Sort by name length (shortest = most specific match)
        candidates.sort(key=lambda x: len(x[0]))
        return candidates[0][1]

    # Word overlap match: "neo browser" matches "Neo Browser App"
    name_words = set(name.split())
    best_match = None
    best_overlap = 0
    for app_name, app_id in _installed_apps.items():
        app_words = set(app_name.split())
        overlap = len(name_words & app_words)
        if overlap > best_overlap:
            best_overlap = overlap
            best_match = app_id

    if best_overlap > 0:
        return best_match

    return None


def open_app(app_name: str) -> str:
    """
    Open any application, URL, or website by name.
    
    Bug 1+3 fix: Before launching, checks if the app is ALREADY RUNNING
    using psutil (live OS process table). If running, brings the existing
    window to the foreground instead of spawning a duplicate.
    """
    name = app_name.lower().strip()
    log.info("🚀 open_app called with: '%s'", name)

    # ── Step 1: Website shortcuts ─────────────────────────────
    if name in _WEBSITE_SHORTCUTS:
        url = _WEBSITE_SHORTCUTS[name]
        webbrowser.open(url)
        log.info("🌐 Opened website shortcut: %s", url)
        return f"Opened {app_name} in your browser."

    # ── Step 2: URL detection ─────────────────────────────────
    if _URL_RE.search(name):
        url = name if name.startswith("http") else f"https://{name}"
        webbrowser.open(url)
        log.info("🌐 Opened URL: %s", url)
        return f"Opened {url} in your browser."

    # ── Step 3: CHECK IF ALREADY RUNNING (Bug 1+3 fix) ────────
    # Always ask the OS — never trust a cached variable.
    running, pids = is_app_running(name)
    if running:
        log.info("📋 '%s' already running (pid %s). Checking for window.", name, pids[0])
        # Try to bring the existing window to the foreground
        window_title = _WINDOW_TITLES.get(name, app_name)
        try:
            from actions.window_manager import find_window as fw
            win = fw(window_title)
            if win:
                # Window exists — focus it
                from actions.window_manager import focus_window
                focus_window(window_title)
                log.info("✅ Raised existing window: %s", window_title)
                return f"{app_name.title()} is already open — bringing it to the front."
            else:
                # GHOST PROCESS: process alive but NO window → kill and relaunch
                log.warning("👻 Ghost process detected: '%s' running (pid %s) but no window. Killing.", name, pids[0])
                proc_name = _PROCESS_NAMES.get(name, f"{name}.exe")
                if not proc_name.endswith(".exe"):
                    proc_name += ".exe"
                try:
                    subprocess.run(
                        ["taskkill", "/IM", proc_name, "/F"],
                        capture_output=True, text=True, timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    import time as _time
                    _time.sleep(1.5)  # Wait for process to fully die
                    log.info("✅ Ghost process killed. Launching fresh.")
                except Exception as ke:
                    log.warning("Ghost kill failed: %s", ke)
                # Fall through to launch fresh below
        except Exception as e:
            log.warning("⚠️ Couldn't check window: %s. Continuing with launch.", e)

    # ── Helper: wait for app window to be visible ─────────
    def _wait_visible(friendly_name: str, timeout: float = 8.0):
        """Wait until the app's window appears on screen so user sees it."""
        try:
            from actions.window_manager import wait_for_window, focus_window
            win = wait_for_window(friendly_name, timeout=timeout)
            if win:
                focus_window(friendly_name)
                log.info("🪟 App visible: '%s'", win['title'])
            else:
                log.warning("⏰ Window didn't appear for '%s' within %.0fs", friendly_name, timeout)
        except Exception as e:
            log.warning("_wait_visible error: %s", e)

    # ── Step 4: Known exe shortcuts (instant, no lookup) ──────
    if name in _EXE_SHORTCUTS:
        target = _EXE_SHORTCUTS[name]
        try:
            if ":" in target and not target.endswith(".exe"):
                os.startfile(target)
            else:
                subprocess.Popen(
                    f"start {target}", shell=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
            log.info("✅ Opened (shortcut): %s → %s", name, target)
            _wait_visible(_WINDOW_TITLES.get(name, app_name))
            return f"Opened {app_name}."
        except Exception as e:
            log.warning("⚠️ Shortcut launch failed: %s", e)

    # ── Step 5: Fuzzy match against ALL installed apps ────────
    app_id = _fuzzy_find_installed(name)
    if app_id:
        try:
            subprocess.Popen(
                f'explorer.exe shell:AppsFolder\\{app_id}', shell=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            log.info("✅ Opened (installed): %s → %s", name, app_id)
            _wait_visible(_WINDOW_TITLES.get(name, app_name))
            return f"Opened {app_name}."
        except Exception as e:
            log.warning("⚠️ AppId launch failed: %s", e)

    # ── Step 6: Direct Start-Process (last resort) ────────────
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             f'Start-Process "{app_name}"'],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        log.info("✅ Start-Process fallback: %s", name)
        _wait_visible(_WINDOW_TITLES.get(name, app_name))
        return f"Opened {app_name}."
    except Exception as e:
        log.error("❌ All launch methods failed for: %s", name)
        return f"Could not find or open {app_name}: {e}"


def close_app(app_name: str) -> str:
    """
    Close a running application by name.
    Uses psutil to verify the app is actually running before attempting to close,
    and verifies it actually stopped after taskkill.
    """
    name = app_name.lower().strip()

    # Check if actually running first (Bug 3 fix: ground truth)
    running, pids = is_app_running(name)
    if not running:
        return f"{app_name} is not running."

    # Resolve to exe name for taskkill
    proc_name = _PROCESS_NAMES.get(name, _EXE_SHORTCUTS.get(name, f"{name}.exe"))
    if not proc_name.endswith(".exe"):
        proc_name += ".exe"

    try:
        result = subprocess.run(
            ["taskkill", "/IM", proc_name, "/F"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0:
            # Verify it actually closed (wait up to 3s)
            import time
            for _ in range(6):
                time.sleep(0.5)
                still_running, _ = is_app_running(name)
                if not still_running:
                    log.info("✅ Closed and verified: %s", proc_name)
                    return f"Closed {app_name}."
            log.warning("⚠️ taskkill succeeded but process still alive: %s", proc_name)
            return f"Closed {app_name} (may take a moment to fully exit)."
        else:
            return f"Failed to close {app_name}: {result.stderr.strip()}"
    except Exception as e:
        return f"Failed to close {app_name}: {e}"

