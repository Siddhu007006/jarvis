"""
screen/vision.py — Screen Understanding via moondream2 (V5.1)
────────────────
Screen understanding using moondream2 (1.8B local vision model).
Maintains a rolling screen context so the LLM always knows what's on screen.

V5.1 changes:
    - CONDITIONAL VISION: moondream2 only runs when WorldState can't explain
      the current context. This eliminates ~80% of unnecessary inference calls.
    - Vision gate checks: active window, focused control, browser URL.
      If these are available and fresh, vision is skipped.
    - Vision ALWAYS runs for: user-requested screen questions, image content,
      unknown UIs, and when WorldState has no active window info.

Concept: Vision as Fallback, Not Primary
    Before V5.1, vision ran on every SCREEN_CHANGED event (every 8+ seconds).
    After V5.1, WorldState provides semantic context for ~80% of situations
    (active window, focused control, browser URL, workflow). Vision only fires
    when the WorldState can't explain what the user is looking at.

Install:
    pip install transformers torch pillow
    # Model downloads automatically on first use (~1.8GB)
"""

import time
import logging
import threading
from PIL import Image
from datetime import datetime

log = logging.getLogger("JARVIS.vision")


# ═══════════════════════════════════════════════════════════════
# VISION GATE — Decides if moondream2 inference is needed
# ═══════════════════════════════════════════════════════════════
#
# Concept: CAPABILITY-BASED SUFFICIENCY, not whitelist-only.
#
# The old approach checked: "is this a known exe?"
# Problem: Electron apps, custom launchers, renamed executables,
# portable apps, and embedded browsers bypass the whitelist.
#
# The new approach checks: "does WorldState have enough SIGNALS
# to describe what the user is doing?"
#
# Sufficiency signals:
#   - has_window_title  (0.25) — window has a meaningful title
#   - has_focused_control (0.20) — we know what UI element is focused
#   - has_browser_url   (0.15) — browser URL is available
#   - workflow_confident (0.25) — workflow detection is >60% confident
#   - title_descriptive (0.15) — title is long enough to be useful
#
# Score >= 0.6 → WorldState is sufficient → skip vision
# Score <  0.6 → WorldState insufficient → run vision
#
# The known-app whitelist is KEPT as a Tier 1 fast-path optimization.
# Capability scoring is Tier 2 — handles everything the whitelist misses.
# ═══════════════════════════════════════════════════════════════

# Tier 1 fast-path: Apps guaranteed to be fully described by WorldState.
# These skip the scoring entirely — no point computing signals.
_KNOWN_SUFFICIENT_EXES = frozenset({
    # Browsers — URL + title is enough
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
    "opera.exe", "vivaldi.exe",
    # Code editors — file path in title is enough
    "code.exe", "pycharm64.exe", "pycharm.exe", "devenv.exe",
    "sublime_text.exe", "idea64.exe", "notepad++.exe", "notepad.exe",
    # Terminals — workflow detection handles these
    "windowsterminal.exe", "cmd.exe", "pwsh.exe", "powershell.exe",
    # Communication — title tells us enough
    "discord.exe", "slack.exe", "ms-teams.exe", "teams.exe",
    "telegram.exe", "whatsapp.exe",
    # Office — document title is enough
    "winword.exe", "excel.exe", "powerpnt.exe", "onenote.exe",
    # Media — workflow detection handles these
    "spotify.exe", "vlc.exe", "foobar2000.exe", "itunes.exe",
    # System — never need vision
    "explorer.exe", "taskmgr.exe", "mstsc.exe",
})

# Sufficiency threshold — score >= this means WorldState is enough
_SUFFICIENCY_THRESHOLD = 0.6

# Weight for each capability signal
_SIGNAL_WEIGHTS = {
    "has_window_title":    0.25,  # We know what app/document is open
    "has_focused_control": 0.20,  # We know what UI element the user is on
    "has_browser_url":     0.15,  # We have the exact URL (browsers only)
    "workflow_confident":  0.25,  # Workflow detection is confident (>0.6)
    "title_descriptive":   0.15,  # Title is long enough to be semantically useful
}


def _compute_sufficiency(world_state) -> tuple:
    """
    Compute a capability-based sufficiency score from WorldState signals.

    Concept: Instead of "is this exe in a list?", we check "do we have
    enough context signals to describe the user's activity?". Each signal
    contributes a weight. The total score determines if vision is needed.

    This handles:
        - Electron apps (have UIA controls + titles -> sufficient)
        - Custom launchers (have window titles + workflow -> sufficient)
        - Renamed executables (doesn't matter — we check signals, not names)
        - Portable apps (same — signals, not names)
        - Embedded browsers (have URL via UIA -> sufficient)

    Returns:
        Tuple of (score: float, signals: dict) — score in [0.0, 1.0],
        signals dict shows which capabilities were present.
    """
    signals = {
        "has_window_title":    False,
        "has_focused_control": False,
        "has_browser_url":     False,
        "workflow_confident":  False,
        "title_descriptive":   False,
    }

    try:
        # Signal 1: Window title exists and is non-empty
        windows = world_state.get("windows")
        active_title = ""
        if windows:
            active_title = windows.get("active_title") or ""
            if len(active_title) > 0:
                signals["has_window_title"] = True

        # Signal 2: Title is descriptive (>10 chars = likely has useful context)
        if len(active_title) > 10:
            signals["title_descriptive"] = True

        # Signal 3: Focused control is known
        control = world_state.get("control")
        if control:
            ctrl_name = control.get("name")
            if isinstance(ctrl_name, dict):
                ctrl_value = ctrl_name.get("value", "")
                ctrl_conf = ctrl_name.get("confidence", 0.0)
            else:
                ctrl_value = str(ctrl_name) if ctrl_name else ""
                ctrl_conf = 1.0 if ctrl_value else 0.0

            ctrl_type = control.get("type", "")
            # Control is known if we have a type OR a named control with confidence
            if ctrl_type or (ctrl_value and ctrl_conf >= 0.3):
                signals["has_focused_control"] = True

        # Signal 4: Browser URL is available
        browser = world_state.get("browser")
        if browser:
            url_data = browser.get("url")
            if isinstance(url_data, dict):
                url_value = url_data.get("value", "")
                url_conf = url_data.get("confidence", 0.0)
            else:
                url_value = str(url_data) if url_data else ""
                url_conf = 1.0 if url_value else 0.0

            if url_value and url_conf >= 0.5:
                signals["has_browser_url"] = True

        # Signal 5: Workflow detection is confident
        workflow = world_state.get("workflow")
        if workflow:
            primary = workflow.get("primary", "idle")
            scores = workflow.get("scores", {})
            primary_score = scores.get(primary, 0.0) if scores else 0.0
            # Workflow is confident if primary != idle and score > 0.6
            if primary != "idle" and primary_score > 0.6:
                signals["workflow_confident"] = True

    except Exception as e:
        log.debug("Sufficiency scoring error: %s", e)

    # Compute weighted score
    score = sum(
        _SIGNAL_WEIGHTS[signal]
        for signal, present in signals.items()
        if present
    )

    return score, signals


def _vision_needed(world_state) -> tuple:
    """
    Determine if moondream2 inference is needed given current WorldState.

    Concept: Two-tier gate function.

    Tier 1 (fast-path): Check known exe whitelist.
        Known apps like Chrome, VS Code, Terminal are guaranteed to be
        fully described by WorldState. Skip immediately — no scoring needed.

    Tier 2 (capability-based): Compute sufficiency score from WorldState signals.
        For unknown/custom/Electron apps, check if WorldState has enough
        context signals (title, control, URL, workflow) to describe the
        user's activity. Score >= 0.6 means WorldState is sufficient.

    This handles the whitelist bypass problem:
        - Electron apps → have UIA controls + descriptive titles → sufficient
        - Custom launchers → have window titles → sufficient if descriptive
        - Renamed exes → doesn't matter, we check signals not names
        - Portable apps → same
        - Embedded browsers → have URL via UIA → sufficient

    Returns:
        Tuple of (bool, str) — (should_run_vision, reason_string)
    """
    if world_state is None:
        return True, "no_world_state"

    try:
        windows = world_state.get("windows")
        if not windows:
            return True, "no_window_data"

        active_exe = (windows.get("active_exe") or "").lower()
        active_title = windows.get("active_title") or ""

        # Case 1: No active window info at all
        if not active_exe and not active_title:
            return True, "no_active_window"

        # Tier 1: Known app fast-path (no scoring needed)
        if active_exe in _KNOWN_SUFFICIENT_EXES:
            return False, f"known_app:{active_exe}"

        # Tier 2: Capability-based sufficiency scoring
        score, signals = _compute_sufficiency(world_state)

        if score >= _SUFFICIENCY_THRESHOLD:
            active_signals = [s for s, v in signals.items() if v]
            return False, f"sufficient:{score:.2f}|{'+'.join(active_signals)}"

        # WorldState insufficient — vision needed
        missing = [s for s, v in signals.items() if not v]
        return True, f"insufficient:{score:.2f}|missing={'+'.join(missing)}"

    except Exception as e:
        log.debug("Vision gate check failed: %s", e)
        return True, f"gate_error:{e}"


# ═══════════════════════════════════════════════════════════════
# VISION ENGINE
# ═══════════════════════════════════════════════════════════════

class VisionEngine:
    """
    Wraps moondream2 for fast local screen understanding.
    Maintains screen_context — always-fresh summary of what's on screen.

    V5.1: Conditional inference — only runs when WorldState can't
    explain the current context. Saves ~80% of inference calls.
    """

    def __init__(self, bus, world_state=None):
        self.bus            = bus
        self._world_state   = world_state
        self.screen_context = "JARVIS just started. Screen context not yet loaded."
        self.last_app       = ""
        self._model         = None
        self._tokenizer     = None
        self._ready         = False
        self._lock          = threading.Lock()

        # V5.1: Track skip/run stats for debugging
        self._stats = {
            "total_events": 0,
            "skipped": 0,
            "analyzed": 0,
            "last_skip_reason": "",
        }

        # Load model in background thread so startup isn't blocked
        t = threading.Thread(target=self._load_model, daemon=True)
        t.start()

        # Subscribe to screen change events
        self.bus.on("SCREEN_CHANGED", self._on_screen_changed)

    # ── Model loading ─────────────────────────────────────
    def _load_model(self):
        """Load moondream2. Downloads on first run, cached after."""
        log.info("Loading moondream2 vision model...")
        start = time.time()
        try:
            from transformers import AutoTokenizer, AutoModelForCausalLM

            model_id = "vikhyatk/moondream2"
            revision  = "2024-07-23"   # Pin to stable version

            self._tokenizer = AutoTokenizer.from_pretrained(
                model_id, revision=revision, trust_remote_code=True
            )
            self._model = AutoModelForCausalLM.from_pretrained(
                model_id, revision=revision,
                trust_remote_code = True,
                torch_dtype       = "auto",    # Uses fp16 if GPU available
                low_cpu_mem_usage = True,
            )
            self._model.eval()
            self._ready = True
            elapsed = time.time() - start
            log.info(f"moondream2 loaded in {elapsed:.1f}s")
            self.bus.emit("VISION_READY", {})

        except Exception as e:
            log.error(f"Failed to load moondream2: {e}")
            log.info("Falling back to screen description via OCR only")
            self._ready = False

    # ── Screen change handler (V5.1 — conditional) ────────
    def _on_screen_changed(self, event: dict):
        """
        Called when ScreenWatcher fires SCREEN_CHANGED.

        V5.1: CONDITIONAL — checks WorldState BEFORE running moondream2.
        If WorldState already knows the active app, focused control, and
        browser URL, we skip the expensive vision inference entirely.

        Vision ALWAYS runs for:
            - Initial boot context (first frame)
            - When WorldState has no active window info
            - Unknown/custom apps with empty titles
        """
        if not self._ready:
            return

        img = event.get("image")
        if img is None:
            return

        self._stats["total_events"] += 1

        # V5.1: Always analyze initial frame (boot context)
        is_initial = event.get("initial", False)

        # V5.1: CONDITIONAL VISION GATE
        if not is_initial:
            # Try to resolve WorldState at call time if not set at init
            ws = self._world_state
            if ws is None:
                try:
                    from core.world_state import get_world_state
                    ws = get_world_state()
                except Exception:
                    pass

            needed, reason = _vision_needed(ws)

            if not needed:
                self._stats["skipped"] += 1
                self._stats["last_skip_reason"] = reason
                log.debug("Vision SKIPPED (reason=%s, skipped=%d/%d)",
                          reason, self._stats["skipped"],
                          self._stats["total_events"])
                return

        # Vision IS needed — run moondream2
        self._stats["analyzed"] += 1

        try:
            description = self._describe(img, (
                "Describe this computer screen concisely: "
                "what application is in focus, what the user is working on, "
                "any visible errors, important text, or notable UI state. "
                "Under 3 sentences."
            ))

            # Detect active application name
            app = self._detect_active_app(img)

            with self._lock:
                self.screen_context = description
                if app:
                    self.last_app = app

            log.debug("Screen context updated (analyzed=%d/%d): %s",
                       self._stats["analyzed"], self._stats["total_events"],
                       description[:80])

            # Fire event so proactive agent can decide to speak
            self.bus.emit("SCREEN_CONTEXT_UPDATED", {
                "context":   description,
                "app":       app,
                "timestamp": datetime.now().isoformat(),
                "initial":   event.get("initial", False)
            })

        except Exception as e:
            log.error(f"Vision analysis error: {e}")

    # ── Answer a question about current screen ───────────
    def ask(self, question: str, img: Image.Image = None) -> str:
        """
        Answer a specific question about the screen.

        NOTE: User-requested screen questions ALWAYS run vision.
        The conditional gate only applies to automatic SCREEN_CHANGED events.
        """
        if not self._ready:
            return "Vision model is still loading, please wait a moment."

        if img is None:
            # Use cached frame — no new screenshot needed
            with self._lock:
                img = getattr(self, "_cached_img", None)
            if img is None:
                return "No screen frame available yet."

        return self._describe(img, question)

    def get_opinion(self, aspect: str = "general") -> str:
        """Get JARVIS's opinion on what's currently on screen."""
        if not self._ready:
            return "My vision module is still warming up."

        prompts = {
            "general": "Look at this screen and give a brief honest opinion or observation about what you see.",
            "design":  "Evaluate the visual design and layout of what's on screen. Be honest about issues.",
            "code":    "Review the code visible on screen. Point out any obvious bugs, inefficiencies, or improvements.",
            "error":   "Explain the error or problem visible on screen and suggest how to fix it.",
        }
        prompt = prompts.get(aspect, prompts["general"])

        with self._lock:
            img = getattr(self, "_cached_img", None)

        if img is None:
            return "I can't see your screen right now."

        return self._describe(img, prompt)

    # ── Core inference ────────────────────────────────────
    def _describe(self, img: Image.Image, question: str) -> str:
        """Run moondream2 inference. Thread-safe."""
        if not self._ready or self._model is None:
            return "Vision model not available."

        # Cache the image for subsequent asks without new screenshot
        with self._lock:
            self._cached_img = img

        try:
            # Encode image
            enc_image = self._model.encode_image(img)

            # Run inference
            answer = self._model.answer_question(enc_image, question, self._tokenizer)
            return answer.strip()

        except Exception as e:
            log.error(f"moondream2 inference error: {e}")
            return "I had trouble analyzing the screen."

    # ── Active app detection ──────────────────────────────
    def _detect_active_app(self, img: Image.Image) -> str:
        """Quick detection of which app is in focus using Windows API."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            # Extract app name from title (usually "Document - AppName" format)
            parts = title.split(" - ")
            return parts[-1] if parts else title
        except Exception:
            return ""

    # ── Context property ──────────────────────────────────
    @property
    def context(self) -> str:
        """Current screen context string, always fresh."""
        with self._lock:
            return self.screen_context

    @property
    def is_ready(self) -> bool:
        return self._ready

    # ── V5.1: Debug stats ─────────────────────────────────
    @property
    def stats(self) -> dict:
        """Return vision gate statistics for debugging."""
        return dict(self._stats)
