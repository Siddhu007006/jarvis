"""
core/events.py — Centralized Event Bus for Jarvis.

Every component communicates through this bus. UI, voice, engine, and
automation layers all react to events independently — no direct coupling.

Architecture concept: Observer/Pub-Sub pattern.
  - Components call `emit(event, data)` to broadcast events
  - Other components call `on(event, callback)` to subscribe
  - Events are dispatched synchronously on the caller's thread
  - Thread-safe via a lock on the subscriber dict

Example events (from the blueprint):
  LISTENING_STARTED, PROCESSING_STARTED, SPEAKING_STARTED,
  OPENING_CHROME, ERROR_OCCURRED, STATE_CHANGED, etc.
"""

import logging
import threading
from typing import Callable, Any
from collections import defaultdict
from enum import Enum, auto

log = logging.getLogger(__name__)


# ── Event Types ───────────────────────────────────────────────

class Event(str, Enum):
    """All system events. Using str+Enum so values are readable strings."""

    # State transitions
    STATE_CHANGED       = "STATE_CHANGED"

    # Voice pipeline
    WAKE_WORD_DETECTED  = "WAKE_WORD_DETECTED"
    LISTENING_STARTED   = "LISTENING_STARTED"
    LISTENING_STOPPED   = "LISTENING_STOPPED"
    SPEECH_RECOGNIZED   = "SPEECH_RECOGNIZED"

    # AI processing
    PROCESSING_STARTED  = "PROCESSING_STARTED"
    PROCESSING_COMPLETE = "PROCESSING_COMPLETE"

    # Speaking
    SPEAKING_STARTED    = "SPEAKING_STARTED"
    SPEAKING_FINISHED   = "SPEAKING_FINISHED"
    SPEAK_TEXT_UPDATED  = "SPEAK_TEXT_UPDATED"

    # Automation
    ACTION_STARTED      = "ACTION_STARTED"
    ACTION_COMPLETE     = "ACTION_COMPLETE"
    ACTION_FAILED       = "ACTION_FAILED"

    # System
    ERROR_OCCURRED      = "ERROR_OCCURRED"
    NOTIFICATION        = "NOTIFICATION"
    SLEEP_TOGGLED       = "SLEEP_TOGGLED"
    DEACTIVATE_REQUESTED = "DEACTIVATE_REQUESTED"
    SHUTDOWN            = "SHUTDOWN"

    # Screen awareness (V4)
    SCREENSHOT_TAKEN    = "SCREENSHOT_TAKEN"
    SCREEN_ANALYZED     = "SCREEN_ANALYZED"

    # Screen awareness (V5 — watcher + vision + proactive pipeline)
    SCREEN_CHANGED          = "SCREEN_CHANGED"          # perceptual hash diff > 25%
    SCREEN_CONTEXT_UPDATED  = "SCREEN_CONTEXT_UPDATED"  # moondream2 described new screen
    VISION_READY            = "VISION_READY"            # vision model loaded
    PROACTIVE_TRIGGER       = "PROACTIVE_TRIGGER"       # proactive agent decides to speak

    # Streaming pipeline (V5)
    LLM_TOKEN_RECEIVED  = "LLM_TOKEN_RECEIVED"          # per-token event for streaming

    # UI Automation (V5)
    ELEMENT_FOUND       = "ELEMENT_FOUND"               # uiautomation located a UI element

    # Memory
    CONTEXT_UPDATED     = "CONTEXT_UPDATED"

    # UI Interaction (Phase 1 — Dynamic Island)
    HOVER_ENTERED       = "HOVER_ENTERED"
    HOVER_EXITED        = "HOVER_EXITED"
    DRAGGING_STARTED    = "DRAGGING_STARTED"
    DRAGGING_ENDED      = "DRAGGING_ENDED"
    QUICK_ACTION        = "QUICK_ACTION"

    # Validation (Phase 7 — Verification loops)
    VALIDATION_PASSED   = "VALIDATION_PASSED"
    VALIDATION_FAILED   = "VALIDATION_FAILED"

    # Process awareness (Phase 6 — Execution engine)
    PROCESS_DETECTED    = "PROCESS_DETECTED"
    PROCESS_CLOSED      = "PROCESS_CLOSED"

    # World State domain events (V5.1 — granular, not generic)
    # Concept: Each domain emits its own event so subscribers only
    # react to changes in their specific domain of interest.
    # This replaces a noisy WORLD_STATE_UPDATED catch-all.
    WINDOW_CHANGED         = "WINDOW_CHANGED"           # active_title or active_exe changed
    ACTIVE_CONTROL_CHANGED = "ACTIVE_CONTROL_CHANGED"   # focused control changed
    BROWSER_URL_CHANGED    = "BROWSER_URL_CHANGED"       # browser URL changed
    CLIPBOARD_CHANGED      = "CLIPBOARD_CHANGED"         # clipboard content changed
    WORKFLOW_CHANGED       = "WORKFLOW_CHANGED"           # primary workflow changed
    RUNNING_APPS_CHANGED   = "RUNNING_APPS_CHANGED"      # running apps list changed

    # Terminal awareness (FIX 1A — semantic terminal monitoring)
    TERMINAL_ERROR_DETECTED = "TERMINAL_ERROR_DETECTED"  # error pattern found in terminal
    BUILD_FAILED            = "BUILD_FAILED"             # build/compilation failure detected
    RUNTIME_EXCEPTION       = "RUNTIME_EXCEPTION"        # runtime crash/panic detected

    # Accessibility awareness (FIX 1B — dialog/modal detection)
    DIALOG_DETECTED         = "DIALOG_DETECTED"          # new dialog/modal appeared
    DIALOG_DISMISSED        = "DIALOG_DISMISSED"         # dialog/modal was closed

    # Task lifecycle
    TASK_STARTED        = "TASK_STARTED"
    TASK_COMPLETED      = "TASK_COMPLETED"


# ── Event Bus ─────────────────────────────────────────────────

class EventBus:
    """
    Thread-safe publish/subscribe event bus.

    Concept: Observer/Pub-Sub pattern — the backbone of Jarvis's
    architecture. Every component communicates through this bus,
    preventing direct coupling between modules.

    Usage:
        bus = EventBus()
        bus.on(Event.WAKE_WORD_DETECTED, lambda data: print("Wake!"))
        bus.emit(Event.WAKE_WORD_DETECTED, {"confidence": 0.95})
    """

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.Lock()

    def on(self, event: str, callback: Callable) -> None:
        """Subscribe to an event."""
        with self._lock:
            self._subscribers[event].append(callback)

    def off(self, event: str, callback: Callable) -> None:
        """Unsubscribe from an event."""
        with self._lock:
            try:
                self._subscribers[event].remove(callback)
            except ValueError:
                pass

    def emit(self, event: str, data: Any = None) -> None:
        """
        Broadcast an event to all subscribers synchronously.

        Exceptions in callbacks are caught and logged to prevent
        one broken subscriber from crashing the whole system.
        """
        with self._lock:
            callbacks = list(self._subscribers.get(event, []))

        for cb in callbacks:
            try:
                cb(data)
            except Exception as e:
                log.error("Event handler error [%s]: %s", event, e, exc_info=True)

    def emit_async(self, event: str, data: Any = None) -> None:
        """
        Broadcast an event on a background thread (non-blocking).

        Use this for events emitted from the UI paint thread or
        time-critical paths where subscriber callbacks should not
        block the caller.
        """
        t = threading.Thread(
            target=self.emit, args=(event, data),
            daemon=True, name=f"event-{event}"
        )
        t.start()

    def clear(self) -> None:
        """Remove all subscribers."""
        with self._lock:
            self._subscribers.clear()


# ── Singleton Bus ─────────────────────────────────────────────

_bus = EventBus()

def get_bus() -> EventBus:
    """Get the global event bus singleton."""
    return _bus
