"""
core/llm_router.py  [NEW]
─────────────────────────
Routes each request to the right model:
  qwen2.5-coder:3b  → fast (0.3s), simple commands, JSON, short answers
  qwen3:4b           → powerful (0.5s), analysis, opinions, complex reasoning
  groq       → fallback when Ollama is slow or down

Rule: use the smallest model that can handle the task well.
"""

import re
import logging

log = logging.getLogger("JARVIS.llm_router")

# ── Keyword patterns that need llama3 ────────────────────
# These tasks need reasoning depth that phi3:mini struggles with
NEEDS_LLAMA3 = [
    # Analysis / opinion
    r"\b(analyse|analyze|opinion|think|review|evaluate|compare|explain why)\b",
    # Long form
    r"\b(write|draft|compose|summarize|summarise|describe in detail)\b",
    # Code
    r"\b(debug|fix this|refactor|optimize|what('s| is) wrong with)\b",
    # Complex reasoning
    r"\b(should i|what would|pros and cons|difference between|help me decide)\b",
    # Screen understanding
    r"\b(what('s| is) on|look at|screen|opinion on this|what do you see)\b",
]

# ── Patterns that phi3:mini handles perfectly ─────────────
# Simple commands, factual, short responses needed
FINE_FOR_PHI3 = [
    r"\b(open|close|launch|start|stop|minimize|maximize)\b",
    r"\b(volume|brightness|mute|unmute)\b",
    r"\b(time|date|day|weather|battery|wifi)\b",
    r"\b(reminder|remind|note|todo)\b",
    r"\b(play|pause|next|previous|skip)\b",
    r"\b(search|find|look up|google)\b",
    r"\b(screenshot|copy|paste|select all)\b",
    r"\b(yes|no|confirm|cancel|ok|done)\b",
    r"\b(hello|hi|hey|thanks|thank you|bye|goodbye)\b",
    r"\b(message|text|email|send)\b",
]

# Rough token count threshold — short queries → phi3
MAX_TOKENS_FOR_PHI3 = 18  # ~18 words


class LLMRouter:
    """
    Decides which model handles each request.
    Called by your Brain/providers before sending to Ollama.
    """

    def __init__(self,
                 fast_model: str   = "qwen2.5-coder:3b",
                 smart_model: str  = "qwen2.5-coder:3b",
                 groq_threshold_ms: int = 2000):
        """
        groq_threshold_ms: if Ollama response takes longer than this,
                           next request routes to Groq instead.
        """
        self.fast_model  = fast_model
        self.smart_model = smart_model
        self._groq_ms    = groq_threshold_ms
        self._last_ollama_ms = 0   # Track actual latency

        self._needs_llama3 = [re.compile(p, re.IGNORECASE) for p in NEEDS_LLAMA3]
        self._fine_for_phi3 = [re.compile(p, re.IGNORECASE) for p in FINE_FOR_PHI3]

        log.info(f"LLM Router: fast={fast_model}, smart={smart_model}")

    def choose(self, user_input: str,
               has_screen_context: bool = False) -> dict:
        """
        Returns routing decision:
        {
            "model":    "phi3:mini" | "llama3",
            "provider": "ollama"    | "groq",
            "reason":   str         (for logging)
        }
        """
        text = user_input.lower().strip()

        # ── Rule 1: Ollama was too slow last time → Groq ──
        if self._last_ollama_ms > self._groq_ms:
            log.debug(f"Routing to Groq (last Ollama={self._last_ollama_ms}ms)")
            return {
                "model":    "llama3-8b-8192",
                "provider": "groq",
                "reason":   f"Ollama slow ({self._last_ollama_ms}ms)"
            }

        # ── Rule 2: Screen context = needs llama3 ─────────
        if has_screen_context and self._matches(text, self._needs_llama3):
            return {
                "model":    self.smart_model,
                "provider": "ollama",
                "reason":   "Screen analysis needs llama3"
            }

        # ── Rule 3: Explicit complex task → llama3 ────────
        if self._matches(text, self._needs_llama3):
            return {
                "model":    self.smart_model,
                "provider": "ollama",
                "reason":   "Complex task pattern matched"
            }

        # ── Rule 4: Simple command → phi3:mini ────────────
        if self._matches(text, self._fine_for_phi3):
            return {
                "model":    self.fast_model,
                "provider": "ollama",
                "reason":   "Simple command pattern matched"
            }

        # ── Rule 5: Short input → phi3:mini ───────────────
        word_count = len(text.split())
        if word_count <= MAX_TOKENS_FOR_PHI3:
            return {
                "model":    self.fast_model,
                "provider": "ollama",
                "reason":   f"Short input ({word_count} words)"
            }

        # ── Default: llama3 for anything ambiguous ─────────
        return {
            "model":    self.smart_model,
            "provider": "ollama",
            "reason":   "Default: ambiguous, using llama3"
        }

    def record_latency(self, ms: int):
        """Call this after each Ollama response with actual latency."""
        self._last_ollama_ms = ms
        if ms > self._groq_ms:
            log.warning(f"Ollama slow ({ms}ms) — next request may route to Groq")

    @staticmethod
    def _matches(text: str, patterns: list) -> bool:
        return any(p.search(text) for p in patterns)


# ── How to use in your providers.py ──────────────────────
#
# Add to your existing Brain/providers:
#
#   router = LLMRouter()
#
#   def get_response(user_input, screen_context=""):
#       start = time.time()
#       route = router.choose(user_input, has_screen_context=bool(screen_context))
#
#       if route["provider"] == "groq":
#           response = groq_client.chat(...)
#       else:
#           response = ollama_httpx_call(route["model"], user_input, ...)
#
#       router.record_latency(int((time.time() - start) * 1000))
#       return response
