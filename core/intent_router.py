"""
core/intent_router.py — Pre-LLM Intent Router
═══════════════════════════════════════════════
Catches obvious voice commands in pure Python BEFORE the LLM,
giving instant (~10ms) responses for known patterns.

Concept — Why a Pre-LLM Router?
  The LLM round-trip costs ~5s even for trivial commands like
  "pause music" or "open Chrome". These commands have exactly ONE
  correct tool call. The intent router uses compiled regex patterns
  to match them instantly, bypassing the LLM entirely.

  Rule: if the mapping is unambiguous, don't waste LLM tokens.
  If there's any doubt, return None and let the LLM decide.

Architecture:
  voice/text → IntentRouter.route()
    → match?  → {tool, params}  → engine._execute_tool_local()
    → None    → engine._think() → LLM

Categories:
  1. MEDIA       → system_control (pause/play/next/prev/volume)
  2. APP LAUNCH  → open_app / close_app
  3. MEMORY      → save_memory
  4. SYSTEM      → system_control (screenshot/lock/shutdown/battery)
  5. JARVIS CTRL → sleep_jarvis / shutdown_jarvis
  6. COMPOUND    → agent_task (multi-step: "open X and do Y")
  7. CONVERSATIONAL → None (greetings, math, jokes, questions)
"""

import re
import logging
import time
from typing import Optional

log = logging.getLogger("JARVIS.intent_router")


# ─── Compiled Regex Patterns ────────────────────────────────────────
# All patterns use re.IGNORECASE and word boundaries to avoid partial matches.
# Organized by category for maintainability.

# ── 1. MEDIA CONTROL ────────────────────────────────────────────────
_MEDIA_PAUSE = re.compile(
    r"^(?:pause|pause\s+(?:the\s+)?music|stop\s+(?:the\s+)?music|pause\s+(?:the\s+)?song)$",
    re.IGNORECASE,
)
_MEDIA_PLAY = re.compile(
    r"^(?:play|play\s+(?:the\s+)?music|resume|resume\s+(?:the\s+)?music|unpause|continue\s+(?:the\s+)?music)$",
    re.IGNORECASE,
)
_MEDIA_NEXT = re.compile(
    r"^(?:next\s+(?:song|track)|skip|skip\s+(?:this\s+)?(?:song|track)|next)$",
    re.IGNORECASE,
)
_MEDIA_PREV = re.compile(
    r"^(?:prev(?:ious)?\s+(?:song|track)|go\s+back|previous|prev)$",
    re.IGNORECASE,
)
_MEDIA_MUTE = re.compile(r"^(?:mute|mute\s+(?:the\s+)?(?:volume|sound|audio))$", re.IGNORECASE)
_MEDIA_UNMUTE = re.compile(r"^(?:unmute|unmute\s+(?:the\s+)?(?:volume|sound|audio))$", re.IGNORECASE)

# Volume with explicit number: "volume 40", "set volume to 50", "volume at 80"
_VOLUME_SET = re.compile(
    r"^(?:set\s+)?volume\s+(?:to\s+|at\s+)?(\d{1,3})(?:\s*%)?$",
    re.IGNORECASE,
)
_VOLUME_UP = re.compile(
    r"^(?:volume\s+up|increase\s+(?:the\s+)?volume|louder|turn\s+(?:it\s+)?up)$",
    re.IGNORECASE,
)
_VOLUME_DOWN = re.compile(
    r"^(?:volume\s+down|decrease\s+(?:the\s+)?volume|quieter|softer|turn\s+(?:it\s+)?down)$",
    re.IGNORECASE,
)
_BRIGHTNESS_SET = re.compile(
    r"^(?:set\s+)?brightness\s+(?:to\s+|at\s+)?(\d{1,3})(?:\s*%)?$",
    re.IGNORECASE,
)

# ── 2. APP LAUNCH / CLOSE ──────────────────────────────────────────
# "open X", "launch X", "start X" — but NOT "open X and Y" (compound)
_APP_OPEN = re.compile(
    r"^(?:open|launch|start|run)\s+(.+)$",
    re.IGNORECASE,
)
_APP_CLOSE = re.compile(
    r"^(?:close|quit|exit|kill|stop)\s+(.+)$",
    re.IGNORECASE,
)
# Compound detector: "open X and search/play/type/do Y"
_COMPOUND = re.compile(
    r"^(?:open|launch|start)\s+.+?\s+and\s+(?:search|play|type|do|go|find|navigate|send|write|create|check)\b",
    re.IGNORECASE,
)

# ── 3. MEMORY ──────────────────────────────────────────────────────
_REMEMBER = re.compile(
    r"^(?:remember\s+(?:that\s+)?|note\s+(?:that\s+)?|save\s+(?:that\s+)?)(.+)$",
    re.IGNORECASE,
)

# ── 4. SYSTEM CONTROL ──────────────────────────────────────────────
_SCREENSHOT = re.compile(
    r"^(?:(?:take\s+)?(?:a\s+)?screenshot|screen\s*(?:shot|capture)|capture\s+(?:the\s+)?screen)$",
    re.IGNORECASE,
)
_LOCK = re.compile(
    r"^(?:lock|lock\s+(?:the\s+)?(?:screen|computer|pc|laptop))$",
    re.IGNORECASE,
)
_BATTERY = re.compile(
    r"^(?:battery|battery\s+(?:status|level|percentage|life)|how\s+much\s+battery|check\s+battery)$",
    re.IGNORECASE,
)
_SHUTDOWN = re.compile(
    r"^(?:shutdown|shut\s+down)(?:\s+(?:the\s+)?(?:computer|pc|laptop|system))?$",
    re.IGNORECASE,
)
_RESTART = re.compile(
    r"^(?:restart|reboot)(?:\s+(?:the\s+)?(?:computer|pc|laptop|system))?$",
    re.IGNORECASE,
)
_SLEEP_COMPUTER = re.compile(
    r"^(?:sleep|put\s+(?:the\s+)?(?:computer|pc|laptop|system)\s+to\s+sleep)$",
    re.IGNORECASE,
)
_SYSTEM_INFO = re.compile(
    r"^(?:system\s+info|system\s+information|system\s+status)$",
    re.IGNORECASE,
)
_WIFI_ON = re.compile(r"^(?:(?:turn\s+)?wifi\s+on|enable\s+wifi|connect\s+wifi)$", re.IGNORECASE)
_WIFI_OFF = re.compile(r"^(?:(?:turn\s+)?wifi\s+off|disable\s+wifi|disconnect\s+wifi)$", re.IGNORECASE)

# ── 5. JARVIS CONTROL ──────────────────────────────────────────────
_JARVIS_SLEEP = re.compile(
    r"^(?:jarvis\s+sleep|stop\s+listening|go\s+to\s+sleep|i(?:'?m)?\s+done)$",
    re.IGNORECASE,
)
_JARVIS_SHUTDOWN = re.compile(
    r"^(?:goodbye|bye(?:\s+jarvis)?|shut\s+down\s+jarvis|exit\s+jarvis|close\s+jarvis|quit\s+jarvis)$",
    re.IGNORECASE,
)

# ── 6. CONVERSATIONAL (return None → LLM) ──────────────────────────
# These patterns identify inputs that should NEVER be routed to a tool.
_CONVERSATIONAL = re.compile(
    r"(?:"
    # Greetings
    r"^(?:hello|hi|hey|good\s+(?:morning|afternoon|evening|night)|howdy|sup|what'?s\s+up)"
    r"|"
    # Math (contains operators)
    r"(?:what\s+is\s+)?\d+\s*[\+\-\*\/\%\^]\s*\d+"
    r"|"
    # Questions/explanations
    r"^(?:what\s+is|who\s+is|explain|why\s+is|how\s+(?:does|do|is|are)|tell\s+me\s+about|define|describe)\s+"
    r"|"
    # Jokes
    r"^(?:tell\s+me\s+a\s+joke|joke|make\s+me\s+laugh|something\s+funny)"
    r"|"
    # Opinions
    r"^(?:what\s+do\s+you\s+think|should\s+i|do\s+you\s+think|give\s+me\s+(?:your\s+)?opinion)"
    r"|"
    # Thanks
    r"^(?:thanks?|thank\s+you|great|awesome|perfect|nice|cool|ok|okay|alright|got\s+it)"
    r")",
    re.IGNORECASE,
)

# ── Close-command blocklist: these should NOT route to close_app ─────
# Words after "close" that are conversational, not app names
_CLOSE_BLOCKLIST = frozenset({
    "the door", "the deal", "my eyes", "it", "that", "this",
    "the gap", "the loop", "the book", "the case", "the window",
})

# ── App name blocklist: these look like "open X" but aren't apps ─────
_OPEN_BLOCKLIST = frozenset({
    "the door", "my eyes", "it", "that", "this", "up",
    "a file", "a folder", "a new file", "a new folder",
    "your eyes", "the box", "the book", "minded",
})


class IntentRouter:
    """
    Pre-LLM intent classifier for obvious commands.

    Concept:
      Uses compiled regex patterns to match user text against known
      command patterns. Returns a tool+params dict for unambiguous
      matches, or None for anything that needs LLM judgment.

    Design decisions:
      - Compiled regexes for speed (<0.1ms per route call)
      - Word boundaries to prevent "open" matching "opening"
      - Compound detection to catch "open X and Y" → agent_task
      - Explicit conversational blocklist to prevent tool calls on chat
      - No fuzzy matching — ambiguity always falls through to LLM
    """

    def route(self, text: str) -> Optional[dict]:
        """
        Route user text to a tool, or return None for LLM.

        Args:
            text: Raw user input (will be normalized internally)

        Returns:
            dict with 'tool' and 'params' keys, or None if the LLM
            should handle this input. For conversational inputs,
            returns None.

        Concept — Routing priority:
          1. Conversational check (return None early for chat)
          2. Compound check ("open X and Y" → agent_task)
          3. Media control (most common voice command)
          4. System control (screenshot, lock, battery)
          5. Jarvis control (sleep, shutdown)
          6. Memory (remember/note)
          7. App launch/close (last, because it's the broadest match)
        """
        start = time.perf_counter_ns()
        normalized = self._normalize(text)

        if not normalized:
            return None

        result = self._route_internal(normalized, text)

        elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
        if result:
            log.info(
                "⚡ ROUTED [%.2fms]: '%s' → %s(%s)",
                elapsed_ms, text[:60], result["tool"],
                {k: str(v)[:30] for k, v in result["params"].items()},
            )
        else:
            log.debug("→ LLM [%.2fms]: '%s' (no pattern match)", elapsed_ms, text[:60])

        return result

    def _route_internal(self, text: str, original: str) -> Optional[dict]:
        """Core routing logic on normalized text."""

        # ── 0. Conversational → None (skip tools entirely) ──────────
        if _CONVERSATIONAL.search(text):
            return None

        # ── 1. Compound → agent_task ────────────────────────────────
        if _COMPOUND.match(text):
            return {"tool": "agent_task", "params": {"goal": original.strip()}}

        # ── 2. Media control ────────────────────────────────────────
        if _MEDIA_PAUSE.match(text):
            return {"tool": "system_control", "params": {"action": "media_pause"}}
        if _MEDIA_PLAY.match(text):
            return {"tool": "system_control", "params": {"action": "media_play"}}
        if _MEDIA_NEXT.match(text):
            return {"tool": "system_control", "params": {"action": "media_next"}}
        if _MEDIA_PREV.match(text):
            return {"tool": "system_control", "params": {"action": "media_prev"}}
        if _MEDIA_MUTE.match(text):
            return {"tool": "system_control", "params": {"action": "volume_mute"}}
        if _MEDIA_UNMUTE.match(text):
            return {"tool": "system_control", "params": {"action": "volume_mute"}}

        m = _VOLUME_SET.match(text)
        if m:
            level = min(100, max(0, int(m.group(1))))
            return {"tool": "system_control", "params": {"action": "volume_set", "value": str(level)}}
        if _VOLUME_UP.match(text):
            return {"tool": "system_control", "params": {"action": "volume_up"}}
        if _VOLUME_DOWN.match(text):
            return {"tool": "system_control", "params": {"action": "volume_down"}}

        m = _BRIGHTNESS_SET.match(text)
        if m:
            level = min(100, max(0, int(m.group(1))))
            return {"tool": "system_control", "params": {"action": "brightness_set", "value": str(level)}}

        # ── 3. System control ───────────────────────────────────────
        if _SCREENSHOT.match(text):
            return {"tool": "system_control", "params": {"action": "screenshot"}}
        if _LOCK.match(text):
            return {"tool": "system_control", "params": {"action": "lock"}}
        if _BATTERY.match(text):
            return {"tool": "system_control", "params": {"action": "battery"}}
        if _SHUTDOWN.match(text):
            return {"tool": "system_control", "params": {"action": "shutdown"}}
        if _RESTART.match(text):
            return {"tool": "system_control", "params": {"action": "restart"}}
        if _SLEEP_COMPUTER.match(text):
            return {"tool": "system_control", "params": {"action": "sleep"}}
        if _SYSTEM_INFO.match(text):
            return {"tool": "system_control", "params": {"action": "system_info"}}
        if _WIFI_ON.match(text):
            return {"tool": "system_control", "params": {"action": "wifi_on"}}
        if _WIFI_OFF.match(text):
            return {"tool": "system_control", "params": {"action": "wifi_off"}}

        # ── 4. Jarvis control ───────────────────────────────────────
        if _JARVIS_SLEEP.match(text):
            return {"tool": "sleep_jarvis", "params": {}}
        if _JARVIS_SHUTDOWN.match(text):
            return {"tool": "shutdown_jarvis", "params": {}}

        # ── 5. Memory ──────────────────────────────────────────────
        m = _REMEMBER.match(text)
        if m:
            value = m.group(1).strip()
            if len(value) > 3:  # Ignore too-short notes
                # Generate a key from first few words
                words = value.split()[:3]
                key = "_".join(w.lower() for w in words if w.isalnum())[:30] or "note"
                return {
                    "tool": "save_memory",
                    "params": {"category": "notes", "key": key, "value": value},
                }

        # ── 6. App close ───────────────────────────────────────────
        m = _APP_CLOSE.match(text)
        if m:
            app = m.group(1).strip()
            if app.lower() not in _CLOSE_BLOCKLIST and len(app) < 30:
                return {"tool": "close_app", "params": {"app_name": app}}

        # ── 7. App open (broadest — must be last) ──────────────────
        m = _APP_OPEN.match(text)
        if m:
            app = m.group(1).strip()
            if app.lower() not in _OPEN_BLOCKLIST and len(app) < 30:
                return {"tool": "open_app", "params": {"app_name": app}}

        # ── No match → LLM ─────────────────────────────────────────
        return None

    @staticmethod
    def _normalize(text: str) -> str:
        """
        Normalize input for pattern matching.

        Steps:
          1. Strip leading/trailing whitespace
          2. Lowercase
          3. Collapse multiple spaces to single space
          4. Remove trailing punctuation (., !, ?)
        """
        text = text.strip().lower()
        text = re.sub(r"\s+", " ", text)
        text = text.rstrip(".!?")
        return text


# ─── Singleton ──────────────────────────────────────────────────────
_router_instance: Optional[IntentRouter] = None


def get_intent_router() -> IntentRouter:
    """Singleton accessor for the intent router."""
    global _router_instance
    if _router_instance is None:
        _router_instance = IntentRouter()
        log.info("IntentRouter initialized")
    return _router_instance


# ─── Self-Test ──────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    router = IntentRouter()

    # Test suite: (input, expected_tool_or_None)
    tests = [
        # ── Media (15) ──
        ("pause", "system_control"),
        ("pause music", "system_control"),
        ("pause the music", "system_control"),
        ("stop music", "system_control"),
        ("play", "system_control"),
        ("play music", "system_control"),
        ("resume", "system_control"),
        ("resume music", "system_control"),
        ("next song", "system_control"),
        ("skip", "system_control"),
        ("next track", "system_control"),
        ("previous song", "system_control"),
        ("mute", "system_control"),
        ("volume 50", "system_control"),
        ("set volume to 80", "system_control"),
        # ── App Launch (10) ──
        ("open chrome", "open_app"),
        ("launch spotify", "open_app"),
        ("start notepad", "open_app"),
        ("open vs code", "open_app"),
        ("open file explorer", "open_app"),
        ("close chrome", "close_app"),
        ("quit spotify", "close_app"),
        ("exit notepad", "close_app"),
        ("open calculator", "open_app"),
        ("open tradingview", "open_app"),
        # ── Memory (5) ──
        ("remember that my birthday is Jan 5", "save_memory"),
        ("note that the meeting is at 3pm", "save_memory"),
        ("remember I like dark mode", "save_memory"),
        ("save that API key is xyz", "save_memory"),
        ("remember my favorite color is blue", "save_memory"),
        # ── System (5) ──
        ("take a screenshot", "system_control"),
        ("lock screen", "system_control"),
        ("battery", "system_control"),
        ("screenshot", "system_control"),
        ("lock the screen", "system_control"),
        # ── Conversational → None (10) ──
        ("hello", None),
        ("what is 2+2", None),
        ("tell me a joke", None),
        ("who is Elon Musk", None),
        ("explain quantum computing", None),
        ("good morning", None),
        ("what do you think about AI", None),
        ("thanks", None),
        ("how does gravity work", None),
        ("should I learn Python", None),
        # ── Compound → agent_task (5) ──
        ("open chrome and search for python", "agent_task"),
        ("open spotify and play shape of you", "agent_task"),
        ("launch notepad and type hello world", "agent_task"),
        ("open tradingview and search XAUUSD", "agent_task"),
        ("open whatsapp and send a message to mom", "agent_task"),
    ]

    passed = 0
    failed = 0
    print(f"\n{'-' * 70}")
    print(f"  IntentRouter Self-Test - {len(tests)} phrases")
    print(f"{'-' * 70}")
    print(f"  {'Input':<45} {'Expected':<15} {'Got':<15} {'Status'}")
    print(f"{'-' * 70}")

    for text, expected in tests:
        result = router.route(text)
        got = result["tool"] if result else None
        ok = got == expected
        if ok:
            passed += 1
        else:
            failed += 1
        status = "PASS" if ok else "FAIL"
        print(f"  {text:<45} {str(expected):<15} {str(got):<15} {status}")

    print(f"{'-' * 70}")
    print(f"  Results: {passed}/{len(tests)} passed, {failed} failed")
    print(f"  Accuracy: {passed/len(tests)*100:.1f}%")
    print(f"{'-' * 70}")
