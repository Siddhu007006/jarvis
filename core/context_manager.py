"""
core/context_manager.py  [NEW]
──────────────────────────────
Manages the screen context that gets injected into every LLM call.
This is the glue between vision.py (which sees the screen) and
your brain/providers (which generate responses).

Without this, moondream2 runs but its output never reaches the LLM.
With this, JARVIS always knows what's on screen before answering.
"""

import threading
import logging
from datetime import datetime

log = logging.getLogger("JARVIS.context_manager")


class ContextManager:
    """
    Central store for all context that enriches LLM calls:
      - screen_context: what moondream2 sees right now
      - active_app: which app is in focus
      - recent_alerts: proactive things JARVIS noticed
      - user_activity: what the user seems to be doing
    
    Thread-safe. Updated by vision.py, read by providers.py.
    """

    def __init__(self, bus):
        self._lock = threading.RLock()

        # ── Context fields ────────────────────────────────
        self._screen_context = ""
        self._active_app     = ""
        self._last_updated   = None
        self._session_start  = datetime.now()

        # Subscribe to screen updates from vision.py
        bus.on("SCREEN_CONTEXT_UPDATED", self._on_screen_update)
        log.info("ContextManager ready")

    # ── Event handler ─────────────────────────────────────
    def _on_screen_update(self, event: dict):
        with self._lock:
            self._screen_context = event.get("context", "")
            self._active_app     = event.get("app", "")
            self._last_updated   = datetime.now()
        log.debug(f"Context updated: '{self._screen_context[:60]}...'")

    # ── Build system prompt injection ────────────────────
    def build_screen_injection(self) -> str:
        """
        Returns a formatted string to prepend to every LLM system prompt.
        Called by your brain/providers.py before each API call.
        """
        with self._lock:
            ctx = self._screen_context
            app = self._active_app
            updated = self._last_updated

        if not ctx:
            return ""

        # How stale is the context?
        if updated:
            age_secs = (datetime.now() - updated).total_seconds()
            age_str  = f"{int(age_secs)}s ago"
        else:
            age_str = "unknown"

        parts = ["[SCREEN CONTEXT]"]
        if app:
            parts.append(f"Active app: {app}")
        parts.append(f"What's on screen: {ctx}")
        parts.append(f"Context age: {age_str}")
        parts.append("[END SCREEN CONTEXT]")

        return "\n".join(parts)

    # ── Properties ────────────────────────────────────────
    @property
    def screen_context(self) -> str:
        with self._lock:
            return self._screen_context

    @property
    def active_app(self) -> str:
        with self._lock:
            return self._active_app

    @property
    def has_context(self) -> bool:
        with self._lock:
            return bool(self._screen_context)

    @property
    def context_age_seconds(self) -> float:
        with self._lock:
            if self._last_updated is None:
                return 999
            return (datetime.now() - self._last_updated).total_seconds()


# ═══════════════════════════════════════════════════════════
# HOW TO WIRE INTO YOUR EXISTING providers.py
# ═══════════════════════════════════════════════════════════
#
# In your providers.py (or wherever you build LLM messages):
#
#   # At startup (pass context_manager in from main.py):
#   def __init__(self, ..., context_manager: ContextManager):
#       self.ctx = context_manager
#
#   # In your message builder, ADD this before system prompt:
#   def _build_messages(self, user_input: str, system_prompt: str):
#       messages = []
#
#       # ── Inject screen context ──────────────────────────
#       screen_injection = self.ctx.build_screen_injection()
#       if screen_injection:
#           full_system = system_prompt + "\n\n" + screen_injection
#       else:
#           full_system = system_prompt
#
#       messages.append({"role": "system", "content": full_system})
#       messages.append({"role": "user",   "content": user_input})
#       return messages
#
# That's it. Now every LLM call automatically includes
# what moondream2 sees on screen.
# ═══════════════════════════════════════════════════════════
