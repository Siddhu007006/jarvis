"""
core/memory.py — Lightweight Context Memory for Jarvis.

Maintains:
  - Recent conversations (last N turns)
  - Recent actions executed
  - Current application context

The blueprint specifies memory should be lightweight to reduce
latency. This uses an in-memory ring buffer with optional JSON
persistence to survive restarts.

Concept: Sliding Window Memory
  - Fixed-size deque for conversations (default 20 turns)
  - Fixed-size deque for actions (default 50)
  - Current context dict (active app, last screenshot, etc.)
  - Auto-saves to disk every N updates
"""

import json
import logging
import os
import time
from collections import deque
from typing import Any, Optional

from core.events import Event, get_bus

log = logging.getLogger(__name__)

DEFAULT_MEMORY_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MAX_CONVERSATIONS = 20
MAX_ACTIONS = 50


class ContextMemory:
    """
    Lightweight context memory with sliding window.

    Usage:
        mem = ContextMemory()
        mem.add_conversation("user", "Open Spotify")
        mem.add_conversation("assistant", "Opening Spotify for you...")
        mem.add_action("open_app", {"target": "spotify"}, "success")
        context = mem.get_context()  # returns dict for Gemini prompt
    """

    def __init__(self, memory_dir: str = DEFAULT_MEMORY_DIR, bus=None):
        self._memory_dir = memory_dir
        self._bus = bus or get_bus()
        self._save_path = os.path.join(memory_dir, "memory.json")

        # Ring buffers
        self._conversations: deque = deque(maxlen=MAX_CONVERSATIONS)
        self._actions: deque = deque(maxlen=MAX_ACTIONS)

        # Current context
        self._context = {
            "active_app": None,
            "last_screenshot": None,
            "session_start": time.time(),
            "interaction_count": 0,
        }

        self._update_count = 0
        self._save_interval = 5  # save every 5 updates

        # Load persisted memory
        self._load()

        # Wire events
        self._bus.on(Event.SPEECH_RECOGNIZED, self._on_speech)
        self._bus.on(Event.SPEAKING_STARTED, self._on_speaking)
        self._bus.on(Event.ACTION_COMPLETE, self._on_action_complete)
        self._bus.on(Event.ACTION_FAILED, self._on_action_failed)
        self._bus.on(Event.SCREENSHOT_TAKEN, self._on_screenshot)

        log.info("Context memory initialized (%d conversations, %d actions)",
                 len(self._conversations), len(self._actions))

    def add_conversation(self, role: str, content: str) -> None:
        """Add a conversation turn."""
        self._conversations.append({
            "role": role,
            "content": content,
            "timestamp": time.time(),
        })
        self._context["interaction_count"] += 1
        self._auto_save()

        self._bus.emit(Event.CONTEXT_UPDATED, {"type": "conversation"})

    def add_action(self, action: str, details: dict, result: str) -> None:
        """Record an executed action."""
        self._actions.append({
            "action": action,
            "details": details,
            "result": result,
            "timestamp": time.time(),
        })
        self._auto_save()

    def set_active_app(self, app_name: str) -> None:
        """Update the currently active application."""
        self._context["active_app"] = app_name

    def get_context(self) -> dict:
        """
        Get the full context for Gemini prompts.

        Returns a dict with recent conversations, actions, and
        current system context — ready to inject into the AI prompt.

        Blueprint Section 17: Keep memory lightweight.
        If total tokens exceed the budget, we trim oldest entries.
        """
        # Token budget guard: if we're over budget, trim
        token_est = self.get_token_estimate()
        if token_est > 4000:
            log.warning("Memory token estimate (%d) exceeds 4000, trimming", token_est)
            # Remove oldest conversations until under budget
            while self.get_token_estimate() > 3000 and len(self._conversations) > 5:
                self._conversations.popleft()

        return {
            "recent_conversations": list(self._conversations),
            "recent_actions": list(self._actions)[-10:],  # last 10 actions
            "system_context": {
                "active_app": self._context.get("active_app"),
                "session_duration_min": round((time.time() - self._context["session_start"]) / 60, 1),
                "interaction_count": self._context["interaction_count"],
            },
        }

    def get_conversation_history(self) -> list[dict]:
        """Get recent conversations as a list for Gemini."""
        return [
            {"role": c["role"], "parts": [{"text": c["content"]}]}
            for c in self._conversations
        ]

    def get_token_estimate(self) -> int:
        """
        Estimate the token count of all stored memory.

        Blueprint Section 17: Too much memory increases latency,
        causes hallucinations, and increases token cost.

        Uses the rough heuristic: 1 token ≈ 4 characters.
        This is approximate but sufficient for budget checks.

        Returns:
            Estimated token count across conversations + actions.
        """
        total_chars = 0

        for c in self._conversations:
            total_chars += len(c.get("content", ""))
            total_chars += len(c.get("role", ""))

        for a in self._actions:
            total_chars += len(str(a.get("action", "")))
            total_chars += len(str(a.get("details", "")))
            total_chars += len(str(a.get("result", "")))

        return total_chars // 4  # rough: 1 token ≈ 4 chars

    def get_memory_stats(self) -> dict:
        """
        Get memory usage statistics for monitoring.

        Used for Blueprint Section 21 (Performance Targets):
        RAM target is < 1 GB, so memory system must stay lightweight.

        Returns:
            Dict with conversation count, action count, estimated tokens,
            and estimated bytes.
        """
        import sys
        conversations_bytes = sys.getsizeof(self._conversations)
        actions_bytes = sys.getsizeof(self._actions)

        for c in self._conversations:
            conversations_bytes += sys.getsizeof(c) + sum(sys.getsizeof(v) for v in c.values())
        for a in self._actions:
            actions_bytes += sys.getsizeof(a) + sum(sys.getsizeof(v) for v in a.values())

        total_bytes = conversations_bytes + actions_bytes
        token_est = self.get_token_estimate()

        stats = {
            "conversations": len(self._conversations),
            "conversations_max": MAX_CONVERSATIONS,
            "actions": len(self._actions),
            "actions_max": MAX_ACTIONS,
            "token_estimate": token_est,
            "memory_bytes": total_bytes,
            "memory_kb": round(total_bytes / 1024, 1),
        }

        if token_est > 3000:
            log.warning("Memory token estimate high: %d tokens (~%d KB)",
                       token_est, stats["memory_kb"])

        return stats

    def clear(self) -> None:
        """Clear all memory."""
        self._conversations.clear()
        self._actions.clear()
        self._context["interaction_count"] = 0
        self._save()
        log.info("Memory cleared")

    # ── Event handlers ────────────────────────────────────────

    def _on_speech(self, data) -> None:
        text = data.get("text", "") if isinstance(data, dict) else str(data)
        if text:
            self.add_conversation("user", text)

    def _on_speaking(self, data) -> None:
        text = data.get("text", "") if isinstance(data, dict) else ""
        if text:
            self.add_conversation("assistant", text)

    def _on_action_complete(self, data) -> None:
        if isinstance(data, dict):
            self.add_action(
                data.get("action", "unknown"),
                data.get("details", {}),
                data.get("result", ""),
            )

    def _on_action_failed(self, data) -> None:
        if isinstance(data, dict):
            self.add_action(
                data.get("action", "unknown"),
                {},
                f"FAILED: {data.get('error', 'unknown')}",
            )

    def _on_screenshot(self, data) -> None:
        if isinstance(data, dict):
            self._context["last_screenshot"] = data.get("path")

    # ── Persistence ───────────────────────────────────────────

    def _auto_save(self) -> None:
        self._update_count += 1
        if self._update_count >= self._save_interval:
            self._save()
            self._update_count = 0

    def _save(self) -> None:
        """Save memory to disk."""
        try:
            os.makedirs(self._memory_dir, exist_ok=True)
            data = {
                "conversations": list(self._conversations),
                "actions": list(self._actions),
                "context": {
                    "interaction_count": self._context["interaction_count"],
                },
            }
            with open(self._save_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error("Memory save failed: %s", e)

    def _load(self) -> None:
        """Load memory from disk."""
        if not os.path.exists(self._save_path):
            return
        try:
            with open(self._save_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for c in data.get("conversations", []):
                self._conversations.append(c)
            for a in data.get("actions", []):
                self._actions.append(a)
            ctx = data.get("context", {})
            self._context["interaction_count"] = ctx.get("interaction_count", 0)
            log.info("Memory loaded from disk")
        except Exception as e:
            log.warning("Memory load failed: %s", e)
