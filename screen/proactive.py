"""
screen/proactive.py — Proactive Commentary Agent
───────────────────
The proactive commentary agent.
JARVIS notices things and speaks up — without you asking.
Like a knowledgeable co-worker sitting next to you.

Behaviour:
  - Critical alerts (errors, crashes) → speak immediately
  - Useful observations → speak if user idle > 30s  
  - Opinions/suggestions → speak if no activity for 2 minutes
  - Max 1 comment per 45 seconds to avoid being annoying
"""

import time
import logging
import threading
from datetime import datetime, timedelta
from enum import Enum

log = logging.getLogger("JARVIS.proactive")


class Priority(Enum):
    CRITICAL = 1   # Speak immediately, bypass rate limit
    HIGH     = 2   # Speak within 10s if user idle
    MEDIUM   = 3   # Speak if idle > 30s
    LOW      = 4   # Speak if idle > 120s (2 min)


# Rate limit: minimum seconds between proactive comments
RATE_LIMIT_SEC = 45

# How long user must be idle before we speak (per priority)
IDLE_THRESHOLDS = {
    Priority.CRITICAL: 0,
    Priority.HIGH:     10,
    Priority.MEDIUM:   30,
    Priority.LOW:      120,
}

# Prompts that generate natural-sounding proactive comments
PROACTIVE_PROMPTS = {
    "error": """The user's screen shows an error. Comment naturally, like a colleague who noticed it. Offer to help if appropriate. Under 2 sentences.
Examples:
Screen: VS Code terminal shows TypeError: cannot read properties of undefined. Output: That TypeError looks like something is null or undefined before a property access, Siddhu. Want me to inspect the code path?
Screen: Browser shows ERR_CONNECTION_REFUSED. Output: The local server is refusing the connection. I can check whether the dev server is running.""",
    "code_review": """You can see code on screen. Mention only a concrete bug, slow pattern, or improvement. If the code looks fine, return exactly: NOTHING. Under 2 sentences.
Examples:
Screen: Python loop opens the same file repeatedly. Output: That loop is reopening the file every iteration; moving the open call outside would be cleaner and faster.
Screen: Well-structured React component with no obvious issue. Output: NOTHING""",
    "long_task": """A long-running operation just completed on screen. Acknowledge it naturally in one sentence.
Examples:
Screen: Tests passed, 42 passed in 18.4s. Output: Tests are green, Siddhu.
Screen: npm install completed successfully. Output: Install finished cleanly.""",
    "new_app": """The user just opened a new application. Offer brief context or help only if useful. Otherwise return exactly: NOTHING.
Examples:
Screen: TradingView opened to a blank chart. Output: TradingView is up. I can pull up a symbol or add an indicator if you want.
Screen: Notepad opened with an empty document. Output: NOTHING""",
    "generic": """You've been watching the screen. Share only a genuinely useful observation or opinion. If there is nothing useful to say, return exactly: NOTHING.
Examples:
Screen: User is idle on a settings confirmation dialog. Output: That dialog is waiting for confirmation, Siddhu.
Screen: User is reading a normal webpage. Output: NOTHING""",
}


class ProactiveAgent:
    """
    Decides when and what to say proactively.
    Subscribes to SCREEN_CONTEXT_UPDATED events and evaluates each one.
    """

    def __init__(self, bus, brain, tts, profile: dict):
        self.bus       = bus
        self.brain     = brain
        self.tts       = tts
        self.name      = profile.get("name", "sir")

        self._last_spoke_time   = 0      # Timestamp of last proactive comment
        self._last_user_action  = time.time()  # Timestamp of last user voice input
        self._comment_queue     = []     # Pending comments
        self._seen_contexts     = set()  # Avoid commenting twice on same thing
        self._lock              = threading.Lock()

        # Subscribe to events
        self.bus.on("SCREEN_CONTEXT_UPDATED", self._on_context_update)
        self.bus.on("COMMAND_RECEIVED",        self._on_user_activity)
        self.bus.on("WAKE_WORD_DETECTED",       self._on_user_activity)

        # Start the queue processor
        t = threading.Thread(target=self._process_queue, daemon=True)
        t.start()

        log.info("Proactive agent started")

    # ── User activity tracking ────────────────────────────
    def _on_user_activity(self, _event):
        """Reset idle timer when user interacts."""
        self._last_user_action = time.time()

    # ── Screen context handler ────────────────────────────
    def _on_context_update(self, event: dict):
        """
        Called when screen changes. Decide if we should say something.
        Runs analysis in background thread so it doesn't block anything.
        """
        ctx = event.get("context", "")
        app = event.get("app", "")

        if not ctx:
            return

        t = threading.Thread(
            target=self._analyse_and_queue,
            args=(ctx, app, event.get("initial", False)),
            daemon=True
        )
        t.start()

    def _analyse_and_queue(self, context: str, app: str, initial: bool):
        """
        Analyse screen context and decide whether to queue a comment.
        """
        # Skip initial boot context — just getting oriented
        if initial:
            return

        # Dedup — don't comment on the same thing twice
        ctx_key = context[:80]
        if ctx_key in self._seen_contexts:
            return
        self._seen_contexts.add(ctx_key)

        # Keep seen set bounded
        if len(self._seen_contexts) > 100:
            self._seen_contexts = set(list(self._seen_contexts)[-50:])

        # ── Classify the context ──────────────────────────
        priority, prompt_key = self._classify(context, app)

        if priority is None:
            return  # Nothing worth saying

        # ── Generate the comment via LLM ──────────────────
        prompt = f"""
{PROACTIVE_PROMPTS[prompt_key]}

Screen context: {context}
Active app: {app}
User name: {self.name}

Respond ONLY with the spoken comment, nothing else.
If there is nothing useful to say, respond with exactly: NOTHING
"""
        try:
            comment = self.brain.think(prompt, extra_context=context)
            comment = comment.strip()

            if not comment or comment.upper() == "NOTHING" or len(comment) < 8:
                log.debug(f"Proactive agent decided: nothing to say")
                return

            log.info(f"Proactive [{priority.name}]: '{comment[:60]}...'")
            self._enqueue(comment, priority)

        except Exception as e:
            log.error(f"Proactive comment generation failed: {e}")

    # ── Context classifier ────────────────────────────────
    def _classify(self, context: str, app: str) -> tuple:
        """
        Return (Priority, prompt_key) or (None, None) if not worth commenting.
        Uses keyword detection — fast, no LLM needed for classification.
        """
        ctx_lower = context.lower()

        # Critical: errors and crashes
        error_keywords = [
            "error", "exception", "traceback", "crashed", "failed",
            "access denied", "not found", "syntax error", "undefined",
            "blue screen", "fatal", "critical error"
        ]
        if any(kw in ctx_lower for kw in error_keywords):
            return Priority.CRITICAL, "error"

        # High: task completions
        completion_keywords = [
            "completed", "finished", "done", "100%", "successful",
            "build succeeded", "tests passed", "installed"
        ]
        if any(kw in ctx_lower for kw in completion_keywords):
            return Priority.HIGH, "long_task"

        # Medium: code visible — offer review
        code_keywords = [
            "def ", "function", "class ", "import ", "const ", "var ",
            "return", ".py", ".js", ".ts", ".cpp", "console.log"
        ]
        if any(kw in ctx_lower for kw in code_keywords):
            return Priority.MEDIUM, "code_review"

        # Low: general screen observation
        # Only comment occasionally, not on every minor change
        if len(ctx_lower) > 50:
            return Priority.LOW, "generic"

        return None, None

    # ── Comment queue ─────────────────────────────────────
    def _enqueue(self, comment: str, priority: Priority):
        with self._lock:
            self._comment_queue.append({
                "comment":  comment,
                "priority": priority,
                "queued_at": time.time()
            })
            # Sort by priority
            self._comment_queue.sort(key=lambda x: x["priority"].value)

    def _process_queue(self):
        """
        Background thread: checks queue and speaks when appropriate.
        Respects rate limit and idle thresholds.
        """
        while True:
            time.sleep(2)  # Check every 2 seconds

            with self._lock:
                if not self._comment_queue:
                    continue
                item = self._comment_queue[0]

            priority = item["priority"]
            now      = time.time()

            # Check rate limit (bypass for CRITICAL)
            if priority != Priority.CRITICAL:
                if now - self._last_spoke_time < RATE_LIMIT_SEC:
                    continue

            # Check idle threshold
            idle_secs = now - self._last_user_action
            required_idle = IDLE_THRESHOLDS[priority]

            if idle_secs < required_idle:
                continue

            # Check comment hasn't expired (don't say stale things)
            age = now - item["queued_at"]
            if age > 120 and priority.value >= Priority.MEDIUM.value:
                log.debug("Proactive comment expired, discarding")
                with self._lock:
                    self._comment_queue.pop(0)
                continue

            # All checks passed — speak
            with self._lock:
                self._comment_queue.pop(0)

            self._last_spoke_time = now
            log.info(f"Proactive speaking [{priority.name}]: '{item['comment'][:50]}...'")
            self.tts.speak(item["comment"])

    # ── Manual trigger ────────────────────────────────────
    def ask_for_opinion(self):
        """
        User explicitly asks 'what do you think of my screen'.
        Bypass rate limit and idle requirement.
        """
        # Clear queue to prioritise the direct ask
        with self._lock:
            self._comment_queue.clear()

        self._enqueue(
            self.brain.get_opinion("general"),
            Priority.CRITICAL
        )
