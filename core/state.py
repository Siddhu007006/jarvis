"""
core/state.py — Centralized State Manager for Jarvis.

Single source of truth for the system's current state. All state
transitions go through here, which:
  1. Validates the transition
  2. Updates internal state
  3. Emits STATE_CHANGED event on the bus
  4. Updates the UI

This prevents state desync between engine, UI, and automation.
"""

import logging
import time
from typing import Optional

from core.events import Event, get_bus

log = logging.getLogger(__name__)

# Valid states (from the blueprint Section 8)
VALID_STATES = {
    "IDLE", "HOVER", "LISTENING", "PROCESSING",
    "SPEAKING", "NOTIFICATION", "DRAGGING",
    "ERROR", "SLEEP",
}

# Legal state transitions (from blueprint Section 8 + 9)
# Each key maps to the set of states it can transition TO.
# Concept: An FSM transition table prevents impossible state jumps
# (e.g. SLEEP → SPEAKING) which would cause UI desync and hallucinations.
TRANSITIONS = {
    "IDLE":         {"LISTENING", "HOVER", "DRAGGING", "PROCESSING", "NOTIFICATION", "ERROR", "SLEEP"},
    "HOVER":        {"IDLE", "LISTENING", "DRAGGING"},
    "LISTENING":    {"IDLE", "PROCESSING", "SPEAKING", "ERROR", "SLEEP"},
    "PROCESSING":   {"SPEAKING", "IDLE", "ERROR", "NOTIFICATION"},
    "SPEAKING":     {"IDLE", "LISTENING", "ERROR"},
    "NOTIFICATION": {"IDLE"},
    "DRAGGING":     {"IDLE", "HOVER"},
    "ERROR":        {"IDLE", "LISTENING"},
    "SLEEP":        {"IDLE", "LISTENING"},
}


class StateManager:
    """
    Manages Jarvis state transitions.

    Concept: Finite State Machine (FSM)
      - Only defined states are allowed
      - Every transition is logged and broadcast
      - UI is updated atomically with state change
    """

    def __init__(self, ui=None):
        self._state = "IDLE"
        self._text = ""
        self._speak_text = ""
        self._sleeping = False
        self._ui = ui
        self._bus = get_bus()
        self._last_change = time.monotonic()

        # Wire bus events to state changes
        self._bus.on(Event.WAKE_WORD_DETECTED, self._on_wake_word)
        self._bus.on(Event.LISTENING_STARTED, lambda _: self.set_state("LISTENING", "Listening…"))
        self._bus.on(Event.PROCESSING_STARTED, lambda d: self.set_state("PROCESSING", d.get("text", "Processing…") if isinstance(d, dict) else "Processing…"))
        self._bus.on(Event.SPEAKING_STARTED, lambda _: self.set_state("SPEAKING"))
        self._bus.on(Event.SPEAKING_FINISHED, lambda _: self.set_state("IDLE"))
        self._bus.on(Event.ERROR_OCCURRED, lambda d: self.set_state("ERROR", d.get("message", "Error") if isinstance(d, dict) else str(d)))
        self._bus.on(Event.NOTIFICATION, lambda d: self.set_state("NOTIFICATION", d.get("message", "") if isinstance(d, dict) else str(d)))
        self._bus.on(Event.SLEEP_TOGGLED, lambda d: self._toggle_sleep())
        self._bus.on(Event.SPEAK_TEXT_UPDATED, lambda d: self.set_speak_text(d.get("text", "") if isinstance(d, dict) else str(d)))
        self._bus.on(Event.DEACTIVATE_REQUESTED, self._on_deactivate)

        # Phase 1 — UI Interaction events
        self._bus.on(Event.HOVER_ENTERED, lambda _: self.set_state("HOVER"))
        self._bus.on(Event.HOVER_EXITED, lambda _: self.set_state("IDLE"))
        self._bus.on(Event.DRAGGING_STARTED, lambda _: self.set_state("DRAGGING"))
        self._bus.on(Event.DRAGGING_ENDED, lambda _: self.set_state("IDLE"))

    @property
    def state(self) -> str:
        return self._state

    @property
    def sleeping(self) -> bool:
        return self._sleeping

    def set_ui(self, ui) -> None:
        """Attach/replace the UI reference."""
        self._ui = ui

    def set_state(self, state: str, text: str = "") -> None:
        """
        Transition to a new state.

        Validation pipeline:
          1. Check state name is valid
          2. Check transition is legal (warn if not, but allow)
          3. Skip if already in the same state (dedup)
          4. Update internal state
          5. Notify UI
          6. Emit STATE_CHANGED event

        Concept: The FSM validates transitions but never blocks them.
        Blocking would risk deadlocks in the realtime audio pipeline.
        Instead, illegal transitions are logged as warnings so we can
        fix the root cause without crashing.
        """
        if state not in VALID_STATES:
            log.warning("Invalid state rejected: %s", state)
            return

        old = self._state

        # Skip duplicate transitions (prevents UI flicker)
        if old == state and not text:
            return

        # Validate transition legality
        allowed = TRANSITIONS.get(old, set())
        if state != old and state not in allowed:
            log.warning(
                "Illegal state transition: %s → %s (allowed: %s). "
                "Proceeding anyway for robustness.",
                old, state, allowed
            )

        self._state = state
        self._text = text
        self._last_change = time.monotonic()

        # Update UI
        if self._ui:
            try:
                self._ui.set_state(state, text)
            except Exception as e:
                log.error("UI update failed: %s", e)

        # Broadcast
        self._bus.emit(Event.STATE_CHANGED, {
            "old": old,
            "new": state,
            "text": text,
        })

        log.info("State: %s → %s %s", old, state, f'({text})' if text else '')

    def set_speak_text(self, text: str) -> None:
        """Update the speaking subtitle text."""
        self._speak_text = text
        if self._ui:
            try:
                self._ui.set_speak_text(text)
            except Exception as e:
                log.error("UI speak text update failed: %s", e)

    def _toggle_sleep(self) -> None:
        """Toggle between sleep and wake."""
        self._sleeping = not self._sleeping
        if self._sleeping:
            self.set_state("SLEEP")
        else:
            self.set_state("IDLE", "Waking up…")
        if self._ui:
            try:
                self._ui.set_sleeping(self._sleeping)
            except Exception:
                pass

    def _on_wake_word(self, data) -> None:
        """
        Handle wake word detection ("Jarvis").

        Works in ALL states including SLEEP:
          - If sleeping → wake up and start listening
          - If already active → transition to LISTENING
        """
        if self._sleeping:
            # Wake from dormant mode
            self._sleeping = False
            log.info("Jarvis waking up from sleep!")
            if self._ui:
                try:
                    self._ui.set_sleeping(False)
                except Exception:
                    pass
        self.set_state("LISTENING", "Listening…")

    def _on_deactivate(self, data) -> None:
        """
        Handle voice deactivation ("deactivate", "go to sleep", etc.).

        Puts Jarvis into dormant SLEEP mode:
          - UI pill stays visible but dimmed
          - Wake word detector keeps running
          - All other processing stops
        """
        phrase = data.get("phrase", "deactivate") if isinstance(data, dict) else "deactivate"
        log.info("Deactivation requested: '%s'", phrase)
        self._sleeping = True
        self.set_state("SLEEP", "")
        if self._ui:
            try:
                self._ui.set_sleeping(True)
            except Exception:
                pass
