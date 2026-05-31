"""
core/world_state.py — World State Engine for Jarvis (V5.1)

THE SINGLE SOURCE OF TRUTH for all system context.

Architecture:
    All modules READ from WorldState.
    Only the update loops WRITE to WorldState.
    Engine sets automation.current_task at tool boundaries.

Design principles:
    1. Domain-based structure — not a flat dict
    2. Versioned — every update cycle increments version
    3. Confidence-scored — UIA-derived values carry confidence
    4. Semantic — vision stores structured context, not raw blobs
    5. Secret-filtered — clipboard never stores passwords/keys
    6. Scored workflows — probabilities, not binary labels
    7. Granular events — per-domain, not generic WORLD_STATE_UPDATED
    8. Three-speed loops — fast(250ms), medium(2s), slow(5-10s)
    9. State diffing — only emit events for changed domains
   10. Current truth only — NO history, NO logs, NO memory

Concept: Observer Pattern + Periodic Polling
    WorldState combines event-driven updates (from the bus) with
    periodic polling (from the 3 update loops). Polling covers things
    the OS doesn't emit events for (active window, clipboard, etc.).
    The bus events handle things Jarvis itself triggers (task start/end).
"""

import copy
import hashlib
import logging
import re
import threading
import time
from typing import Any, Optional

log = logging.getLogger("JARVIS.world_state")

# ═══════════════════════════════════════════════════════════════
# SENSITIVE DATA FILTER
# ═══════════════════════════════════════════════════════════════

# Pre-compiled patterns for performance — checked once per clipboard read
_SENSITIVE_PATTERNS = [
    # API keys (OpenAI, Google, GitHub, AWS, Slack, Stripe, Anthropic)
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"AIza[a-zA-Z0-9_-]{30,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{30,}"),
    re.compile(r"gho_[a-zA-Z0-9]{30,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"xoxb-[a-zA-Z0-9-]+"),
    re.compile(r"xoxp-[a-zA-Z0-9-]+"),
    re.compile(r"sk_live_[a-zA-Z0-9]{20,}"),
    re.compile(r"sk_test_[a-zA-Z0-9]{20,}"),
    re.compile(r"sk-ant-[a-zA-Z0-9-]{20,}"),
    # JWT tokens
    re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}"),
    # SSH / PGP keys
    re.compile(r"-----BEGIN\s+(RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"-----BEGIN PGP"),
    # Bearer tokens
    re.compile(r"Bearer\s+[a-zA-Z0-9_.-]{20,}"),
    # Long hex strings (>40 chars = likely a secret)
    re.compile(r"[a-fA-F0-9]{40,}"),
    # Common password field indicators
    re.compile(r"password\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"secret\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"token\s*[:=]\s*\S+", re.IGNORECASE),
]


def _looks_sensitive(text: str) -> bool:
    """
    Check if clipboard text contains sensitive data.

    Concept: Defense-in-depth for prompt injection and data leakage.
    Any content matching known secret patterns is NEVER stored in
    WorldState and NEVER injected into LLM prompts.

    Returns:
        True if the text matches any known secret pattern.
    """
    if not text or len(text) < 8:
        return False
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# SYSTEM PROCESS FILTER
# ═══════════════════════════════════════════════════════════════

# System services that should never appear in "running apps" list.
# These pollute the state and confuse the LLM.
_SYSTEM_PROCESSES = frozenset({
    "svchost.exe", "csrss.exe", "lsass.exe", "services.exe",
    "smss.exe", "wininit.exe", "winlogon.exe", "dwm.exe",
    "conhost.exe", "fontdrvhost.exe", "sihost.exe", "taskhostw.exe",
    "runtimebroker.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "textinputhost.exe", "ctfmon.exe",
    "dllhost.exe", "applicationframehost.exe", "widgetservice.exe",
    "systemsettingsbroker.exe", "securityhealthservice.exe",
    "sgrmbroker.exe", "registry.exe", "memory compression",
    "system", "idle", "system idle process", "audiodg.exe",
    "searchindexer.exe", "searchprotocolhost.exe", "spoolsv.exe",
    "wudfhost.exe", "dashost.exe", "wsappx.exe", "lockapp.exe",
    "smartscreen.exe", "msiexec.exe", "trustedinstaller.exe",
    "tiworker.exe", "wmiprvse.exe", "unsecapp.exe",
    "securityhealthsystray.exe", "phoneexperiencehost.exe",
    "gamebarpresencewriter.exe", "gamebarftserver.exe",
    "yourphone.exe", "crashpad_handler.exe", "nvidia web helper.exe",
    "nvidia share.exe", "nvcontainer.exe", "nvspcaps64.exe",
    "msedgewebview2.exe",
})


# ═══════════════════════════════════════════════════════════════
# WORKFLOW SCORING
# ═══════════════════════════════════════════════════════════════

# Concept: Rule-based probabilistic workflow detection.
# Each signal (exe running, window title keyword, active app)
# contributes a weight to a workflow score. Scores are normalized
# to 0.0–1.0. Humans multitask, so we produce probabilities,
# not binary labels.

WORKFLOW_SIGNALS = {
    "coding": {
        "exe_boost":   {"code.exe": 0.4, "pycharm64.exe": 0.4, "pycharm.exe": 0.4,
                        "devenv.exe": 0.4, "sublime_text.exe": 0.35, "idea64.exe": 0.4,
                        "notepad++.exe": 0.25},
        "title_keywords": {"terminal": 0.2, "git": 0.15, "debug": 0.2,
                           "python": 0.15, ".py": 0.15, ".js": 0.15,
                           ".ts": 0.15, "compile": 0.2, "build": 0.15,
                           "error": 0.1, "stack": 0.1},
        "running_boost":  {"windowsterminal.exe": 0.15, "cmd.exe": 0.1,
                           "pwsh.exe": 0.15, "git.exe": 0.1, "node.exe": 0.1,
                           "python.exe": 0.1, "python3.exe": 0.1},
    },
    "browsing": {
        "exe_boost":   {"chrome.exe": 0.35, "msedge.exe": 0.35,
                        "firefox.exe": 0.35, "brave.exe": 0.35,
                        "opera.exe": 0.3, "vivaldi.exe": 0.3},
        "title_keywords": {"google": 0.1, "youtube": 0.15, "reddit": 0.15,
                           "twitter": 0.1, "facebook": 0.1, "amazon": 0.1,
                           "stackoverflow": 0.15, "github.com": 0.1},
        "running_boost":  {},
    },
    "writing": {
        "exe_boost":   {"winword.exe": 0.5, "excel.exe": 0.3,
                        "powerpnt.exe": 0.4, "onenote.exe": 0.35,
                        "notepad.exe": 0.3, "wordpad.exe": 0.3},
        "title_keywords": {"document": 0.2, "word": 0.15, ".docx": 0.2,
                           ".xlsx": 0.2, ".pptx": 0.2, "google docs": 0.3},
        "running_boost":  {},
    },
    "media": {
        "exe_boost":   {"spotify.exe": 0.5, "vlc.exe": 0.4,
                        "foobar2000.exe": 0.4, "wmplayer.exe": 0.3,
                        "itunes.exe": 0.4},
        "title_keywords": {"youtube": 0.3, "netflix": 0.4, "twitch": 0.35,
                           "spotify": 0.3, "playing": 0.2, "music": 0.2,
                           "video": 0.15},
        "running_boost":  {},
    },
    "communication": {
        "exe_boost":   {"discord.exe": 0.45, "slack.exe": 0.45,
                        "ms-teams.exe": 0.45, "teams.exe": 0.45,
                        "telegram.exe": 0.4, "whatsapp.exe": 0.4,
                        "zoom.exe": 0.4, "outlook.exe": 0.35},
        "title_keywords": {"chat": 0.2, "message": 0.15, "call": 0.2,
                           "meeting": 0.25, "inbox": 0.15, "email": 0.15},
        "running_boost":  {},
    },
    "gaming": {
        "exe_boost":   {"steam.exe": 0.2, "epicgameslauncher.exe": 0.15},
        "title_keywords": {"game": 0.2, "playing": 0.15, "fps": 0.15,
                           "level": 0.1, "score": 0.1},
        "running_boost":  {"steamwebhelper.exe": 0.1},
    },
    "productivity": {
        "exe_boost":   {"explorer.exe": 0.1, "calculatorapp.exe": 0.2,
                        "calc.exe": 0.2, "mstsc.exe": 0.3},
        "title_keywords": {"file explorer": 0.3, "settings": 0.2,
                           "control panel": 0.2, "task manager": 0.2,
                           "calculator": 0.2},
        "running_boost":  {},
    },
    "idle": {
        "exe_boost":   {},
        "title_keywords": {},
        "running_boost":  {},
    },
}


def _compute_workflow_scores(
    active_exe: str,
    active_title: str,
    running_exes: list[str],
) -> dict[str, float]:
    """
    Compute workflow probability scores based on current system state.

    Concept: Weighted signal accumulation with normalization.
    Each running exe, active exe, and title keyword contributes a
    weight to the corresponding workflow. Scores are clamped to [0, 1]
    and the total is normalized so they sum to ~1.0.

    Returns:
        Dict mapping workflow names to scores (0.0 – 1.0).
    """
    scores = {wf: 0.0 for wf in WORKFLOW_SIGNALS}
    active_exe_lower = active_exe.lower()
    title_lower = active_title.lower()
    running_lower = {e.lower() for e in running_exes}

    for wf, signals in WORKFLOW_SIGNALS.items():
        # Active exe boost (strongest signal — this is what the user is looking at)
        if active_exe_lower in signals["exe_boost"]:
            scores[wf] += signals["exe_boost"][active_exe_lower]

        # Title keyword matching
        for kw, weight in signals["title_keywords"].items():
            if kw in title_lower:
                scores[wf] += weight

        # Running app boost (weaker — just because it's open)
        for exe, weight in signals.get("running_boost", {}).items():
            if exe in running_lower:
                scores[wf] += weight

        # Clamp individual score
        scores[wf] = min(scores[wf], 1.0)

    # Normalize so scores sum to 1.0 (or all zeros if nothing detected)
    total = sum(scores.values())
    if total > 0:
        scores = {wf: round(s / total, 3) for wf, s in scores.items()}
    else:
        # Nothing detected — mark as idle
        scores["idle"] = 1.0

    return scores


# ═══════════════════════════════════════════════════════════════
# WORLD STATE CLASS
# ═══════════════════════════════════════════════════════════════

def _default_state() -> dict:
    """Return a fresh default world state dict."""
    return {
        "version": 0,
        "timestamp": 0.0,

        "windows": {
            "active_title": "",
            "active_exe": "",
            "open_windows": [],
        },

        "control": {
            "name":    {"value": "", "confidence": 1.0},
            "type":    "",
            "bounds":  None,   # (x, y, w, h) tuple or None
            "enabled": True,
        },

        "browser": {
            "url":       {"value": "", "confidence": 0.0},
            "tab_title": "",
            "browser":   "",   # "chrome" | ""
        },

        "clipboard": {
            "text":     "",
            "hash":     "",
            "redacted": False,
        },

        "workflow": {
            "scores": {
                "coding":        0.0,
                "browsing":      0.0,
                "writing":       0.0,
                "media":         0.0,
                "communication": 0.0,
                "gaming":        0.0,
                "productivity":  0.0,
                "idle":          1.0,
            },
            "primary": "idle",
        },

        "automation": {
            "running":      False,
            "current_task": None,
        },

        "vision": {
            "app":       "",
            "page_type": "",
            "intent":    "",
            "entities":  [],
            "raw_age_s": 999.0,
        },

        "system": {
            "running_apps": [],
            "volume":       None,
            "cpu_percent":  0.0,
            "ram_percent":  0.0,
            "music_app":    None,
        },
    }


class WorldState:
    """
    Realtime current truth about the desktop environment.

    NOT a memory store. NOT a log. NOT a history tracker.

    Architecture:
        - All modules READ via get() / snapshot()
        - Only the 3 update loops WRITE to _state
        - Engine calls set_task() / clear_task() at tool boundaries
        - Bus events drive vision context updates

    Concept: This is the "ground truth cache" that eliminates:
        - Repeated screenshot analysis
        - LLM hallucination about app states
        - Redundant process enumeration
        - Stateless decision-making

    Thread safety:
        All reads and writes go through self._lock (RLock for reentrant access).
        Update loops acquire the lock for the shortest possible duration.
    """

    def __init__(self, bus=None, process_monitor=None, context_manager=None):
        self._lock = threading.RLock()
        self._state = _default_state()
        self._bus = bus
        self._process_monitor = process_monitor
        self._context_manager = context_manager

        # Track threads for graceful shutdown
        self._running = False
        self._threads: list[threading.Thread] = []

        # Subscribe to vision context updates from the existing ContextManager
        if bus:
            try:
                from core.events import Event
                bus.on(Event.SCREEN_CONTEXT_UPDATED, self._on_vision_update)
                bus.on(Event.ACTION_STARTED, self._on_action_started)
                bus.on(Event.ACTION_COMPLETE, self._on_action_complete)
                bus.on(Event.ACTION_FAILED, self._on_action_failed)
            except Exception as e:
                log.warning("Failed to subscribe to bus events: %s", e)

        log.info("WorldState initialized (domains: %d)",
                 len([k for k in self._state if k not in ("version", "timestamp")]))

    # ─── Public API: Read ──────────────────────────────────────

    def get(self, domain: str, key: str = None) -> Any:
        """
        Read a domain or a specific field within a domain.

        Examples:
            ws.get("windows")                → entire windows dict
            ws.get("browser", "url")         → {"value": "...", "confidence": 0.9}
            ws.get("workflow", "primary")     → "coding"
        """
        with self._lock:
            domain_data = self._state.get(domain)
            if domain_data is None:
                return None
            if key is None:
                return copy.deepcopy(domain_data)
            if isinstance(domain_data, dict):
                return copy.deepcopy(domain_data.get(key))
            return None

    def snapshot(self) -> dict:
        """
        Return a frozen deep copy of the entire world state.

        Used for:
            - State diffing (compare before/after update)
            - LLM context building
            - Debugging / logging
        """
        with self._lock:
            return copy.deepcopy(self._state)

    # ─── Public API: Write (Engine only) ──────────────────────

    def set_task(self, description: str) -> None:
        """Called by engine.py before each tool execution."""
        with self._lock:
            self._state["automation"]["current_task"] = description
            self._state["automation"]["running"] = True

    def clear_task(self) -> None:
        """Called by engine.py after each tool completes."""
        with self._lock:
            self._state["automation"]["current_task"] = None
            self._state["automation"]["running"] = False

    def set_automation_running(self, running: bool) -> None:
        """Called by engine.py at automation sequence boundaries."""
        with self._lock:
            self._state["automation"]["running"] = running

    # ─── State Diffing ─────────────────────────────────────────

    def diff(self, old_snapshot: dict) -> list[str]:
        """
        Compare current state to a previous snapshot.

        Concept: Structural diff at the domain level.
        Only checks top-level domain dicts for changes. This is
        O(domains) not O(fields), keeping it fast for the update loops.

        Returns:
            List of changed domain names: ["windows", "browser"]
        """
        changed = []
        with self._lock:
            current = self._state

        for domain in ("windows", "control", "browser", "clipboard",
                       "workflow", "automation", "vision", "system"):
            old_domain = old_snapshot.get(domain, {})
            new_domain = current.get(domain, {})
            if old_domain != new_domain:
                changed.append(domain)

        return changed

    # ─── LLM Context Injection ─────────────────────────────────

    def build_llm_injection(self) -> str:
        """
        Build a structured context string for LLM system prompt injection.

        Concept: Instead of raw screenshot descriptions, the LLM gets
        a compact, structured summary of the entire world state. This
        reduces token waste and improves contextual accuracy.

        Returns:
            Formatted string ready for system prompt prepending.
        """
        with self._lock:
            s = self._state

        parts = ["[WORLD STATE]"]

        # Windows
        w = s["windows"]
        if w["active_title"]:
            parts.append(f"Active: {w['active_title']} ({w['active_exe']})")

        # Workflow
        wf = s["workflow"]
        if wf["primary"] and wf["primary"] != "idle":
            score = wf["scores"].get(wf["primary"], 0)
            parts.append(f"Workflow: {wf['primary']} ({score:.2f})")

        # Running apps (top 10 to limit tokens)
        apps = s["system"]["running_apps"]
        if apps:
            parts.append(f"Apps: {', '.join(apps[:10])}")

        # Focused control
        ctrl = s["control"]
        if ctrl["name"]["value"]:
            enabled_str = "enabled" if ctrl["enabled"] else "disabled"
            conf = ctrl["name"]["confidence"]
            parts.append(f"Focused: {ctrl['type']} \"{ctrl['name']['value']}\" "
                         f"[{enabled_str}, conf={conf:.2f}]")

        # Browser
        br = s["browser"]
        if br["url"]["value"]:
            parts.append(f"Browser: {br['url']['value']} (conf={br['url']['confidence']:.2f})")

        # Clipboard
        clip = s["clipboard"]
        if clip["text"]:
            if clip["redacted"]:
                parts.append("Clipboard: [REDACTED — sensitive content]")
            else:
                # Truncate for token efficiency
                preview = clip["text"][:120]
                if len(clip["text"]) > 120:
                    preview += "..."
                parts.append(f"Clipboard: \"{preview}\"")

        # Automation
        auto = s["automation"]
        if auto["running"]:
            parts.append(f"Task: {auto['current_task'] or 'running'}")

        # Vision (semantic)
        vis = s["vision"]
        if vis["app"] and vis["raw_age_s"] < 30:
            vis_parts = [f"app={vis['app']}"]
            if vis["page_type"]:
                vis_parts.append(f"type={vis['page_type']}")
            if vis["intent"]:
                vis_parts.append(f"intent={vis['intent']}")
            if vis["entities"]:
                vis_parts.append(f"entities={vis['entities'][:5]}")
            parts.append(f"Vision: {', '.join(vis_parts)} ({vis['raw_age_s']:.0f}s ago)")

        parts.append("[END WORLD STATE]")

        return "\n".join(parts) if len(parts) > 2 else ""

    # ─── Lifecycle ─────────────────────────────────────────────

    def start(self) -> None:
        """
        Launch the 3 background update loops.

        Concept: Different state domains change at different speeds.
        Active window changes every time the user Alt+Tabs (~ms).
        Clipboard changes rarely (~minutes). Using one loop for
        everything wastes CPU on frequent clipboard reads or misses
        fast window switches with slow polling.

        Three loops, three speeds:
            Fast   (250ms): active window, focused control
            Medium (2s):    browser URL, workflow, running apps
            Slow   (5-10s): clipboard, system stats, music state
        """
        if self._running:
            log.warning("WorldState already running")
            return

        self._running = True

        loops = [
            ("ws-fast",   self._fast_loop,   0.25),
            ("ws-medium", self._medium_loop, 2.0),
            ("ws-slow",   self._slow_loop,   7.0),
        ]

        for name, target, interval in loops:
            t = threading.Thread(
                target=self._loop_wrapper,
                args=(target, interval, name),
                daemon=True,
                name=name,
            )
            t.start()
            self._threads.append(t)

        log.info("WorldState started (3 update loops)")

    def stop(self) -> None:
        """Gracefully stop all update threads."""
        self._running = False
        log.info("WorldState stopping...")

    def _loop_wrapper(self, update_fn, interval: float, name: str) -> None:
        """
        Generic loop runner with error handling and diffing.

        For each cycle:
            1. Snapshot the current state
            2. Run the domain-specific update function
            3. Diff against the snapshot
            4. Emit granular events for changed domains
            5. Increment version + timestamp
        """
        log.info("Update loop '%s' started (interval=%.2fs)", name, interval)

        while self._running:
            try:
                # 1. Pre-update snapshot
                before = self.snapshot()

                # 2. Run update
                update_fn()

                # 3. Diff
                changed = self.diff(before)

                # 4. Emit granular domain events
                if changed:
                    self._emit_domain_events(changed)

                    # 5. Increment version
                    with self._lock:
                        self._state["version"] += 1
                        self._state["timestamp"] = time.monotonic()

            except Exception as e:
                log.error("Update loop '%s' error: %s", name, e, exc_info=True)

            time.sleep(interval)

        log.info("Update loop '%s' stopped", name)

    # ─── Domain Event Emitter ──────────────────────────────────

    def _emit_domain_events(self, changed_domains: list[str]) -> None:
        """
        Emit granular events for each changed domain.

        Concept: Instead of one noisy WORLD_STATE_UPDATED event,
        we emit specific events so subscribers only react to
        their domain. This reduces CPU, noise, and coupling.
        """
        if not self._bus:
            return

        try:
            from core.events import Event

            domain_event_map = {
                "windows":    Event.WINDOW_CHANGED,
                "control":    Event.ACTIVE_CONTROL_CHANGED,
                "browser":    Event.BROWSER_URL_CHANGED,
                "clipboard":  Event.CLIPBOARD_CHANGED,
                "workflow":   Event.WORKFLOW_CHANGED,
                "system":     Event.RUNNING_APPS_CHANGED,
            }

            for domain in changed_domains:
                event = domain_event_map.get(domain)
                if event:
                    with self._lock:
                        data = copy.deepcopy(self._state.get(domain, {}))
                    self._bus.emit_async(event, data)

        except Exception as e:
            log.warning("Domain event emission failed: %s", e)

    # ═══════════════════════════════════════════════════════════
    # UPDATE FUNCTIONS (called by loop_wrapper)
    # ═══════════════════════════════════════════════════════════

    # ─── Fast Loop (250ms): active window, focused control ────

    def _fast_loop(self) -> None:
        """Update fast-changing state: active window, focused control, vision age."""
        self._update_active_window()
        self._update_focused_control()
        self._tick_vision_age()

    def _update_active_window(self) -> None:
        """
        Read the foreground window title and exe name.

        Concept: win32gui.GetForegroundWindow() returns the hwnd of
        the window the user is currently interacting with. We then
        get its title and owning process name. This is instant (< 1ms)
        and deterministic — no vision model needed.
        """
        try:
            import win32gui
            import win32process
            import psutil

            hwnd = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd) or ""

            # Get the exe name from the window's process
            exe = ""
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid > 0:
                    proc = psutil.Process(pid)
                    exe = proc.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass

            with self._lock:
                self._state["windows"]["active_title"] = title
                self._state["windows"]["active_exe"] = exe

        except Exception as e:
            log.debug("Active window update failed: %s", e)

    def _update_focused_control(self) -> None:
        """
        Read the currently focused UI control from the accessibility tree.

        Concept: uiautomation.GetFocusedControl() returns the exact
        control the user's cursor/focus is on (e.g., the search box
        in Chrome, a code line in VS Code). This gives Jarvis precise
        awareness of WHERE the user is working, not just WHICH app.

        We extract: name, type, bounding rect, and enabled state.
        The name carries confidence because UIA can return empty strings
        or generic names for custom-rendered controls.
        """
        try:
            import uiautomation as uia

            ctrl = uia.GetFocusedControl()
            if ctrl is None:
                return

            name = ctrl.Name or ""
            ctrl_type = ctrl.ControlTypeName or ""
            enabled = True
            bounds = None

            # Get bounding rectangle
            try:
                rect = ctrl.BoundingRectangle
                if rect and rect.width() > 0:
                    bounds = (rect.left, rect.top, rect.width(), rect.height())
            except Exception:
                pass

            # Get enabled state
            try:
                enabled = ctrl.IsEnabled
            except Exception:
                pass

            # Confidence: high if we got a name + type, lower otherwise
            confidence = 1.0
            if not name:
                confidence = 0.3
            elif len(name) < 3:
                confidence = 0.6

            with self._lock:
                self._state["control"]["name"] = {
                    "value": name,
                    "confidence": confidence,
                }
                self._state["control"]["type"] = ctrl_type
                self._state["control"]["bounds"] = bounds
                self._state["control"]["enabled"] = enabled

        except Exception as e:
            log.debug("Focused control update failed: %s", e)

    # ─── Medium Loop (2s): browser, workflow, running apps ────

    def _medium_loop(self) -> None:
        """Update medium-speed state: browser URL, workflow scores, running apps."""
        self._update_running_apps()
        self._update_browser_url()
        self._update_workflow()

    def _update_running_apps(self) -> None:
        """
        Get the list of user-facing running applications.

        Concept: Delegates to ProcessMonitor if available,
        otherwise falls back to direct psutil enumeration.
        Filters out system services (svchost, csrss, etc.)
        so the LLM only sees meaningful app names.
        """
        try:
            if self._process_monitor:
                all_exes = self._process_monitor.get_running_apps()
            else:
                import psutil
                all_exes = []
                for proc in psutil.process_iter(['name']):
                    try:
                        name = proc.info['name']
                        if name:
                            all_exes.append(name.lower())
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                all_exes = sorted(set(all_exes))

            # Filter system processes
            user_apps = [
                e for e in all_exes
                if e.lower() not in _SYSTEM_PROCESSES
            ]

            # Detect music app
            music_app = None
            music_exes = {"spotify.exe", "vlc.exe", "foobar2000.exe",
                          "wmplayer.exe", "itunes.exe"}
            for app in user_apps:
                if app.lower() in music_exes:
                    music_app = app.replace(".exe", "")
                    break

            with self._lock:
                self._state["system"]["running_apps"] = user_apps[:50]  # cap at 50
                self._state["system"]["music_app"] = music_app

        except Exception as e:
            log.debug("Running apps update failed: %s", e)

    def _update_browser_url(self) -> None:
        """
        Extract the current URL from Chrome's address bar via UIA.

        Concept: Chrome's accessibility tree exposes the address bar as
        an EditControl within a ToolBarControl. We navigate the tree
        to find it and read its Value. This gives us the exact URL
        without any browser extension or debugging protocol.

        Chrome only for now — Edge and Firefox have different tree
        structures and will be added via browser adapter abstraction later.

        Confidence is based on how we found the URL:
            1.0 = found via UIA EditControl (reliable)
            0.5 = extracted from window title (less reliable)
            0.0 = not found
        """
        with self._lock:
            active_exe = self._state["windows"]["active_exe"].lower()
            active_title = self._state["windows"]["active_title"]

        # Only update when a browser is active
        if active_exe != "chrome.exe":
            # Clear browser state if no browser is active
            with self._lock:
                if self._state["browser"]["browser"]:
                    self._state["browser"] = {
                        "url": {"value": "", "confidence": 0.0},
                        "tab_title": "",
                        "browser": "",
                    }
            return

        url = ""
        confidence = 0.0
        tab_title = active_title

        # Tier 1: UIA — navigate Chrome's accessibility tree
        try:
            import uiautomation as uia

            # Find Chrome window
            chrome_win = uia.WindowControl(
                searchDepth=1,
                ClassName="Chrome_WidgetWin_1",
            )
            if chrome_win.Exists(maxSearchSeconds=0.3):
                # Chrome address bar is typically:
                # Window > Pane > ToolBar > Edit (named "Address and search bar")
                address_bar = chrome_win.EditControl(
                    searchDepth=8,
                    Name="Address and search bar",
                )
                if address_bar.Exists(maxSearchSeconds=0.3):
                    try:
                        val = address_bar.GetValuePattern().Value
                        if val:
                            url = val
                            confidence = 1.0
                    except Exception:
                        # Try legacy ValuePattern access
                        try:
                            url = address_bar.CurrentValue() or ""
                            confidence = 0.9 if url else 0.0
                        except Exception:
                            pass
        except Exception as e:
            log.debug("Chrome UIA URL extraction failed: %s", e)

        # Tier 2: Fall back to window title parsing
        if not url and " - " in active_title:
            # Chrome titles are typically "Page Title - Google Chrome"
            tab_title = active_title.rsplit(" - ", 1)[0].strip()

        with self._lock:
            self._state["browser"]["url"] = {
                "value": url,
                "confidence": confidence,
            }
            self._state["browser"]["tab_title"] = tab_title
            self._state["browser"]["browser"] = "chrome"

    def _update_workflow(self) -> None:
        """
        Compute workflow probability scores from current state.

        Concept: See _compute_workflow_scores() for the scoring algorithm.
        Uses active_exe, active_title, and running_apps as signals.
        """
        with self._lock:
            active_exe = self._state["windows"]["active_exe"]
            active_title = self._state["windows"]["active_title"]
            running = self._state["system"]["running_apps"]

        scores = _compute_workflow_scores(active_exe, active_title, running)

        # Determine primary workflow
        primary = max(scores, key=scores.get) if scores else "idle"

        with self._lock:
            self._state["workflow"]["scores"] = scores
            self._state["workflow"]["primary"] = primary

    # ─── Slow Loop (7s): clipboard, system stats ──────────────

    def _slow_loop(self) -> None:
        """Update slow-changing state: clipboard, system stats."""
        self._update_clipboard()
        self._update_system_stats()

    def _update_clipboard(self) -> None:
        """
        Read current clipboard text content.

        Concept: Windows clipboard is read via win32clipboard.
        We hash the content to detect changes without string comparison.
        Sensitive content (API keys, tokens, passwords) is NEVER stored —
        replaced with "[REDACTED]".

        Only reads CF_UNICODETEXT — ignores images, files, etc.
        """
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(
                    win32clipboard.CF_UNICODETEXT
                ):
                    raw_text = win32clipboard.GetClipboardData(
                        win32clipboard.CF_UNICODETEXT
                    )
                else:
                    raw_text = ""
            finally:
                win32clipboard.CloseClipboard()

            if not raw_text:
                return

            # Hash for change detection
            text_hash = hashlib.md5(raw_text.encode("utf-8", errors="ignore")).hexdigest()

            with self._lock:
                old_hash = self._state["clipboard"]["hash"]

            if text_hash == old_hash:
                return  # No change

            # Check for sensitive content
            redacted = _looks_sensitive(raw_text)
            stored_text = "[REDACTED]" if redacted else raw_text[:500]  # Cap at 500 chars

            with self._lock:
                self._state["clipboard"]["text"] = stored_text
                self._state["clipboard"]["hash"] = text_hash
                self._state["clipboard"]["redacted"] = redacted

            if redacted:
                log.info("Clipboard content redacted (sensitive data detected)")

        except Exception as e:
            log.debug("Clipboard update failed: %s", e)

    def _update_system_stats(self) -> None:
        """
        Read CPU and RAM usage.

        Delegates to ProcessMonitor.get_system_stats() if available,
        otherwise reads psutil directly.
        """
        try:
            if self._process_monitor:
                stats = self._process_monitor.get_system_stats()
            else:
                import psutil
                stats = {
                    "cpu_percent": psutil.cpu_percent(interval=0),
                    "ram_percent": psutil.virtual_memory().percent,
                }

            with self._lock:
                self._state["system"]["cpu_percent"] = stats.get("cpu_percent", 0.0)
                self._state["system"]["ram_percent"] = stats.get("ram_percent", 0.0)

        except Exception as e:
            log.debug("System stats update failed: %s", e)

    # ─── Vision Context (Bus event-driven, not polled) ─────────

    def _on_vision_update(self, event_data: dict) -> None:
        """
        Handle SCREEN_CONTEXT_UPDATED events from vision.py / context_manager.

        Concept: Instead of storing the raw moondream2 description blob,
        we parse it into semantic fields (app, page_type, intent, entities).
        This is lightweight parsing, not a full NLP pipeline — just keyword
        extraction from the vision model's output.
        """
        if not isinstance(event_data, dict):
            return

        raw_context = event_data.get("context", "")
        app = event_data.get("app", "")

        # Semantic parsing of vision output
        page_type = self._infer_page_type(raw_context)
        intent = self._infer_intent(raw_context)
        entities = self._extract_entities(raw_context)

        with self._lock:
            self._state["vision"]["app"] = app
            self._state["vision"]["page_type"] = page_type
            self._state["vision"]["intent"] = intent
            self._state["vision"]["entities"] = entities
            self._state["vision"]["raw_age_s"] = 0.0

    def _infer_page_type(self, context: str) -> str:
        """Infer page type from vision context keywords."""
        ctx = context.lower()
        type_map = {
            "search_results": ["search results", "google search", "bing search"],
            "video":          ["video", "youtube", "playing", "player", "streaming"],
            "code_editor":    ["code editor", "ide", "source code", "programming", "syntax"],
            "terminal":       ["terminal", "command line", "console", "shell", "powershell"],
            "document":       ["document", "word", "writing", "text editor"],
            "spreadsheet":    ["spreadsheet", "excel", "cells", "rows", "columns"],
            "email":          ["email", "inbox", "mail", "compose"],
            "chat":           ["chat", "message", "discord", "slack", "conversation"],
            "settings":       ["settings", "preferences", "configuration", "options"],
            "file_browser":   ["file explorer", "files", "folders", "directory"],
        }
        for ptype, keywords in type_map.items():
            for kw in keywords:
                if kw in ctx:
                    return ptype
        return ""

    def _infer_intent(self, context: str) -> str:
        """Infer user intent from vision context keywords."""
        ctx = context.lower()
        intent_map = {
            "debugging":       ["error", "traceback", "exception", "bug", "failed"],
            "watching_video":  ["watching", "playing video", "youtube", "streaming"],
            "reading_docs":    ["documentation", "docs", "readme", "reading"],
            "coding":          ["writing code", "editing", "programming", "typing code"],
            "browsing":        ["browsing", "scrolling", "web page"],
            "searching":       ["searching", "search results", "looking for"],
            "communicating":   ["chatting", "messaging", "email", "typing message"],
        }
        for intent, keywords in intent_map.items():
            for kw in keywords:
                if kw in ctx:
                    return intent
        return ""

    def _extract_entities(self, context: str) -> list[str]:
        """
        Extract notable entities from vision context.

        Simple keyword extraction — looks for capitalized words,
        technology names, and common entities.
        """
        if not context:
            return []

        # Known tech/tool entities to look for
        known_entities = [
            "Python", "JavaScript", "TypeScript", "React", "Node",
            "Docker", "Git", "GitHub", "Chrome", "VS Code", "VSCode",
            "Spotify", "Discord", "Slack", "YouTube", "Google",
            "OpenAI", "ChatGPT", "Gemini", "Claude", "Copilot",
            "Windows", "Linux", "Terminal", "PowerShell",
            "Stack Overflow", "Reddit", "Twitter", "Netflix",
        ]

        found = []
        for entity in known_entities:
            if entity.lower() in context.lower():
                found.append(entity)

        return found[:10]  # Cap at 10 entities

    # ─── Automation Bus Event Handlers ─────────────────────────

    def _on_action_started(self, data: dict) -> None:
        """Track when an automation action starts."""
        if isinstance(data, dict):
            desc = data.get("action", data.get("tool", "automation"))
            self.set_task(str(desc))

    def _on_action_complete(self, data: dict) -> None:
        """Track when an automation action completes."""
        self.clear_task()

    def _on_action_failed(self, data: dict) -> None:
        """Track when an automation action fails."""
        self.clear_task()

    # ─── Vision age ticker (called by fast loop) ───────────────

    def _tick_vision_age(self) -> None:
        """Increment the vision context age counter."""
        with self._lock:
            if self._state["vision"]["raw_age_s"] < 999:
                self._state["vision"]["raw_age_s"] += 0.25  # fast loop interval


# ═══════════════════════════════════════════════════════════════
# SINGLETON ACCESSOR
# ═══════════════════════════════════════════════════════════════
#
# Same pattern as events.py (get_bus) and providers.py (set_context_manager).
# main.py calls set_world_state() during startup.
# Other modules call get_world_state() to read the singleton.
#

_world_state_instance: Optional[WorldState] = None


def set_world_state(ws: WorldState) -> None:
    """Called by main.py to register the WorldState singleton."""
    global _world_state_instance
    _world_state_instance = ws


def get_world_state() -> Optional[WorldState]:
    """Get the global WorldState singleton (or None if not initialized)."""
    return _world_state_instance


def get_world_context() -> str:
    """
    Get the current world state as an LLM injection string.

    Replacement for providers.get_screen_context().
    Returns empty string if WorldState is not initialized.
    """
    if _world_state_instance is not None:
        return _world_state_instance.build_llm_injection()
    return ""

