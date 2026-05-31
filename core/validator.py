"""
core/validator.py — Validation Engine for Jarvis (V5.1)

Transforms Jarvis from "tries automation" into "verifies automation."

Architecture:
    Validator is PURE VERIFICATION. It does NOT retry, it does NOT
    orchestrate, it does NOT reason. It answers ONE question:

        "Did the expected state transition occur?"

    The caller (engine.py or executor.py) decides what to do with the result.

Concept: State Transition Engineering
    Instead of "did the tool return success?", we check "did the system
    state actually change in the way we expected?". This catches:
      - Silent failures (tool returned OK but nothing happened)
      - Wrong targets (clicked the wrong button)
      - Partial success (app opened but didn't focus)
      - Environmental interference (another window stole focus)

Verification Hierarchy (fastest → slowest):
    1. WorldState   (~0ms) — read from in-memory state cache
    2. UIA          (~50ms) — query accessibility tree
    3. Process      (~100ms) — psutil process checks
    4. Result Parse (~0ms) — parse structured tool output
    5. OCR          (~1s) — screen text extraction (NEVER auto-escalated)
    6. Vision       (~3s) — screenshot model analysis (NEVER auto-escalated)

Confidence Calibration (fixed rules, not learned):
    WorldState match  → 0.95
    UIA match         → 0.85
    Process match     → 0.80
    Result parse      → 0.70
    OCR match         → 0.65
    Vision match      → 0.45

Design Rules:
    1. Validator NEVER retries — only reports
    2. Validator NEVER escalates to OCR/Vision automatically
    3. Validator is DETERMINISTIC — no AI reasoning
    4. Snapshots are SCOPED — only relevant domains per tool
    5. Diffs are FIELD-LEVEL — not just domain-level
"""

import copy
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("JARVIS.validator")


# ═══════════════════════════════════════════════════════════════
# FAILURE TYPE ENUM
# ═══════════════════════════════════════════════════════════════

class FailureType(str, enum.Enum):
    """
    Typed failure classification for intelligent recovery.

    Concept: Different failure types require different recovery strategies.
    A 'not_found' needs OCR fallback. A 'focus_lost' needs window refocus.
    A 'timeout' needs a longer wait. Without this, all failures look the
    same and recovery logic becomes primitive.

    Used by: executor.py retry logic, future execution graph engine.
    """
    NONE              = "none"                # No failure — action succeeded
    NOT_FOUND         = "not_found"           # Target element/app not found → try OCR/alt target
    STATE_UNCHANGED   = "state_unchanged"     # Action ran but nothing changed → retry or escalate
    PERMISSION_DENIED = "permission_denied"   # OS denied the action → run as admin or abort
    FOCUS_LOST        = "focus_lost"          # Wrong window has focus → refocus and retry
    TIMEOUT           = "timeout"             # Action took too long → increase wait or retry
    AMBIGUOUS_MATCH   = "ambiguous_match"     # Multiple elements matched → need disambiguation
    WRONG_STATE       = "wrong_state"         # State changed but to wrong value → different approach
    VERIFICATION_FAILED = "verification_failed"  # Generic — could not determine outcome


# ═══════════════════════════════════════════════════════════════
# VALIDATION RESULT
# ═══════════════════════════════════════════════════════════════

@dataclass
class ValidationResult:
    """
    Structured result from validation.

    Every action returns one of these. The LLM and executor can use
    confidence, failure_type, and changed_fields to make informed
    decisions about retries and fallbacks.

    Concept: This is the contract between the validator and its callers.
    It provides enough information for the caller to make intelligent
    recovery decisions without the validator doing any reasoning itself.
    """
    success: bool                      # Did the action achieve its goal?
    confidence: float                  # 0.0 – 1.0, calibrated per method
    verification_method: str           # "world_state" | "uia" | "process" | "result_parse" | "ocr" | "vision" | "passthrough"
    failure_type: FailureType          # Typed failure for recovery routing
    detail: str                        # Human-readable explanation
    retry_count: int = 0              # How many retries the CALLER has done (set by caller)
    changed_domains: list = field(default_factory=list)   # ["windows", "browser"]
    changed_fields: list = field(default_factory=list)    # ["windows.active_title", "browser.url.value"]
    before_snapshot: dict = field(default_factory=dict, repr=False)  # Scoped pre-action snapshot

    def to_dict(self) -> dict:
        """Serialize for LLM injection or logging."""
        return {
            "success": self.success,
            "confidence": round(self.confidence, 2),
            "verification_method": self.verification_method,
            "failure_type": self.failure_type.value,
            "detail": self.detail,
            "retry_count": self.retry_count,
            "changed_domains": self.changed_domains,
            "changed_fields": self.changed_fields,
        }

    def to_result_string(self) -> str:
        """Format for appending to tool result string."""
        if self.success:
            return f" [verified: {self.verification_method}, conf={self.confidence:.2f}]"
        else:
            return (f" [UNVERIFIED: {self.failure_type.value}, "
                    f"method={self.verification_method}, conf={self.confidence:.2f}]")


# ═══════════════════════════════════════════════════════════════
# CONFIDENCE CALIBRATION
# ═══════════════════════════════════════════════════════════════

# Fixed confidence ranges per verification method.
# These are NOT learned — they reflect inherent reliability of each source.

CONFIDENCE = {
    "world_state":  0.95,   # In-memory cache, updated every 250ms
    "uia":          0.85,   # Accessibility tree — reliable but can be stale
    "process":      0.80,   # psutil — reliable but process names can be ambiguous
    "result_parse": 0.70,   # String parsing — works but brittle
    "ocr":          0.65,   # OCR — can misread, depends on resolution
    "vision":       0.45,   # Vision model — hallucination risk
    "passthrough":  0.60,   # No specific verification — moderate confidence
}


# ═══════════════════════════════════════════════════════════════
# SCOPED SNAPSHOT MAPPING
# ═══════════════════════════════════════════════════════════════

# Which WorldState domains are relevant for each tool type.
# Only these domains are snapshotted before execution.
# This keeps snapshots lightweight as WorldState grows.

TOOL_RELEVANT_DOMAINS = {
    "open_app":         ["windows", "system"],
    "close_app":        ["windows", "system"],
    "ui_control":       ["windows", "control", "browser"],
    "click_element":    ["windows", "control"],
    "type_into":        ["control"],
    "type_text":        ["control"],
    "computer_control": ["windows", "control", "browser"],
    "file_manager":     [],          # Result-based, no state to snapshot
    "web_search":       [],          # Result-based
    "clipboard":        ["clipboard"],
    "system_control":   ["system"],
    "run_command":      [],          # Result-based
    "set_volume_precise": ["system"],
}


# ═══════════════════════════════════════════════════════════════
# VALIDATOR CLASS
# ═══════════════════════════════════════════════════════════════

class Validator:
    """
    Deterministic action verification engine.

    Architecture:
        - DOES: Check if state transitions occurred
        - DOES: Classify failure types
        - DOES: Report confidence-scored results
        - DOES NOT: Retry actions
        - DOES NOT: Choose fallback strategies
        - DOES NOT: Use AI reasoning

    Usage:
        validator = Validator()
        before = validator.snapshot_before("open_app")
        # ... execute the tool ...
        result = validator.verify("open_app", params, tool_result, before)
    """

    def __init__(self):
        self._ws = None  # Lazy-loaded WorldState

    def _get_ws(self):
        """Lazy-load WorldState singleton."""
        if self._ws is None:
            try:
                from core.world_state import get_world_state
                self._ws = get_world_state()
            except Exception:
                pass
        return self._ws

    # ─── Pre-Action Snapshot ──────────────────────────────────

    def snapshot_before(self, tool_name: str) -> dict:
        """
        Take a scoped WorldState snapshot BEFORE action execution.

        Concept: Only snapshot the domains relevant to this tool type.
        An open_app action doesn't need to snapshot clipboard or browser.
        This keeps snapshots lightweight as WorldState grows over time.

        Args:
            tool_name: The tool about to be executed.

        Returns:
            Dict of {domain_name: domain_data} for relevant domains only.
        """
        ws = self._get_ws()
        if ws is None:
            return {}

        domains = TOOL_RELEVANT_DOMAINS.get(tool_name, [])
        snapshot = {}
        for domain in domains:
            data = ws.get(domain)
            if data is not None:
                snapshot[domain] = data

        return snapshot

    # ─── Post-Action Verification ─────────────────────────────

    def verify(
        self,
        tool_name: str,
        params: dict,
        tool_result: str,
        before: dict,
    ) -> ValidationResult:
        """
        Verify that an action succeeded.

        This is the SINGLE entry point for all verification.
        Routes to tool-specific verifiers.

        Args:
            tool_name:   The tool that was executed
            params:      The parameters that were passed to it
            tool_result: The string result returned by the tool
            before:      The scoped snapshot from snapshot_before()

        Returns:
            ValidationResult with success, confidence, failure_type, etc.
        """
        # Route to tool-specific verifier
        verifiers = {
            "open_app":           self._verify_open_app,
            "close_app":          self._verify_close_app,
            "ui_control":         self._verify_ui_control,
            "click_element":      self._verify_click_element,
            "type_into":          self._verify_type_into,
            "type_text":          self._verify_type_text,
            "computer_control":   self._verify_computer_control,
            "file_manager":       self._verify_result_based,
            "web_search":         self._verify_result_based,
            "run_command":        self._verify_result_based,
            "clipboard":          self._verify_clipboard,
            "system_control":     self._verify_system_control,
            "set_volume_precise": self._verify_system_control,
        }

        verifier = verifiers.get(tool_name, self._verify_passthrough)

        try:
            result = verifier(params, tool_result, before)
            result.before_snapshot = before
            return result
        except Exception as e:
            log.warning("Verification error for %s: %s", tool_name, e)
            return ValidationResult(
                success=False,
                confidence=0.0,
                verification_method="error",
                failure_type=FailureType.VERIFICATION_FAILED,
                detail=f"Verification crashed: {e}",
                before_snapshot=before,
            )

    # ─── Field-Level Diffing ──────────────────────────────────

    def _diff_fields(self, before: dict, tool_name: str) -> tuple[list, list]:
        """
        Compute field-level changes between before snapshot and current state.

        Concept: Domain-level diffing tells you "windows changed".
        Field-level diffing tells you "windows.active_title changed from
        X to Y". This precision is critical for:
          - Recovery planning (what exactly went wrong?)
          - Intelligent retries (what specifically needs to change?)
          - Proactive intelligence (what did the user just do?)

        Returns:
            Tuple of (changed_domains, changed_fields)
        """
        ws = self._get_ws()
        if ws is None or not before:
            return [], []

        changed_domains = []
        changed_fields = []

        for domain, old_data in before.items():
            current = ws.get(domain)
            if current is None:
                continue

            if old_data != current:
                changed_domains.append(domain)
                # Field-level diff
                if isinstance(old_data, dict) and isinstance(current, dict):
                    for key in set(list(old_data.keys()) + list(current.keys())):
                        old_val = old_data.get(key)
                        new_val = current.get(key)
                        if old_val != new_val:
                            changed_fields.append(f"{domain}.{key}")

        return changed_domains, changed_fields

    # ═══════════════════════════════════════════════════════════
    # TOOL-SPECIFIC VERIFIERS
    # ═══════════════════════════════════════════════════════════

    def _verify_open_app(self, params: dict, result: str, before: dict) -> ValidationResult:
        """
        Verify open_app: is the app running and is its window visible?

        Verification cascade:
            1. WorldState: check system.running_apps for the exe
            2. WorldState: check windows.active_exe matches
            3. Process: direct psutil check (fallback)
        """
        app_name = params.get("app_name", "").lower()
        if not app_name:
            return ValidationResult(
                success=False, confidence=0.0,
                verification_method="params",
                failure_type=FailureType.NOT_FOUND,
                detail="No app_name provided",
            )

        # Check for failure in result string first
        if _result_indicates_failure(result):
            return ValidationResult(
                success=False, confidence=CONFIDENCE["result_parse"],
                verification_method="result_parse",
                failure_type=FailureType.NOT_FOUND,
                detail=f"Tool reported failure: {result[:100]}",
            )

        changed_domains, changed_fields = self._diff_fields(before, "open_app")

        # Phase 2: reduced from 0.5s. The execution_graph already
        # waits (stabilization_ms) before calling verify(). This sleep
        # only needs to cover one WorldState poll cycle (~250ms).
        time.sleep(0.15)

        ws = self._get_ws()

        # Tier 1: WorldState — check running_apps
        if ws:
            running = ws.get("system", "running_apps") or []
            running_lower = [a.lower() for a in running]

            # Match by name — "chrome" matches "chrome.exe"
            app_found = any(
                app_name in exe or exe.replace(".exe", "") == app_name
                for exe in running_lower
            )

            if app_found:
                # Also check if it has focus (stronger signal)
                active_exe = (ws.get("windows", "active_exe") or "").lower()
                if app_name in active_exe or active_exe.replace(".exe", "") == app_name:
                    return ValidationResult(
                        success=True, confidence=CONFIDENCE["world_state"],
                        verification_method="world_state",
                        failure_type=FailureType.NONE,
                        detail=f"{app_name} running and focused",
                        changed_domains=changed_domains,
                        changed_fields=changed_fields,
                    )
                else:
                    # Running but not focused — partial success
                    return ValidationResult(
                        success=True, confidence=CONFIDENCE["world_state"] - 0.05,
                        verification_method="world_state",
                        failure_type=FailureType.NONE,
                        detail=f"{app_name} running (not focused — active: {active_exe})",
                        changed_domains=changed_domains,
                        changed_fields=changed_fields,
                    )

        # Tier 2: Process check — direct psutil fallback
        try:
            import psutil
            for proc in psutil.process_iter(['name']):
                try:
                    pname = (proc.info['name'] or "").lower()
                    if app_name in pname or pname.replace(".exe", "") == app_name:
                        return ValidationResult(
                            success=True, confidence=CONFIDENCE["process"],
                            verification_method="process",
                            failure_type=FailureType.NONE,
                            detail=f"{app_name} found in process list (PID {proc.pid})",
                            changed_domains=changed_domains,
                            changed_fields=changed_fields,
                        )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            log.debug("Process check failed: %s", e)

        # Tier 3: Delayed retry — reduced from 1.5s (Phase 2)
        # The graph's stabilization window already covers the bulk of
        # the wait; this is a last-resort check for very slow apps.
        time.sleep(0.5)
        if ws:
            running = ws.get("system", "running_apps") or []
            running_lower = [a.lower() for a in running]
            app_found = any(
                app_name in exe or exe.replace(".exe", "") == app_name
                for exe in running_lower
            )
            if app_found:
                return ValidationResult(
                    success=True, confidence=CONFIDENCE["world_state"] - 0.10,
                    verification_method="world_state",
                    failure_type=FailureType.NONE,
                    detail=f"{app_name} running (detected after delay)",
                    changed_domains=changed_domains,
                    changed_fields=changed_fields,
                )

        return ValidationResult(
            success=False, confidence=CONFIDENCE["process"],
            verification_method="process",
            failure_type=FailureType.NOT_FOUND,
            detail=f"{app_name} not found in process table or WorldState",
            changed_domains=changed_domains,
            changed_fields=changed_fields,
        )

    def _verify_close_app(self, params: dict, result: str, before: dict) -> ValidationResult:
        """
        Verify close_app: the app should NOT be running.
        """
        app_name = params.get("app_name", "").lower()
        if not app_name:
            return ValidationResult(
                success=False, confidence=0.0,
                verification_method="params",
                failure_type=FailureType.NOT_FOUND,
                detail="No app_name provided",
            )

        changed_domains, changed_fields = self._diff_fields(before, "close_app")

        # Phase 2: reduced from 1.0s (graph stabilization covers the rest)
        time.sleep(0.3)

        ws = self._get_ws()
        if ws:
            running = ws.get("system", "running_apps") or []
            running_lower = [a.lower() for a in running]
            still_running = any(
                app_name in exe or exe.replace(".exe", "") == app_name
                for exe in running_lower
            )
            if not still_running:
                return ValidationResult(
                    success=True, confidence=CONFIDENCE["world_state"],
                    verification_method="world_state",
                    failure_type=FailureType.NONE,
                    detail=f"{app_name} no longer running",
                    changed_domains=changed_domains,
                    changed_fields=changed_fields,
                )
            else:
                return ValidationResult(
                    success=False, confidence=CONFIDENCE["world_state"],
                    verification_method="world_state",
                    failure_type=FailureType.STATE_UNCHANGED,
                    detail=f"{app_name} still running after close attempt",
                    changed_domains=changed_domains,
                    changed_fields=changed_fields,
                )

        # Fallback: process check
        try:
            import psutil
            time.sleep(0.15)  # Phase 2: reduced from 0.5s
            for proc in psutil.process_iter(['name']):
                try:
                    pname = (proc.info['name'] or "").lower()
                    if app_name in pname:
                        return ValidationResult(
                            success=False, confidence=CONFIDENCE["process"],
                            verification_method="process",
                            failure_type=FailureType.STATE_UNCHANGED,
                            detail=f"{app_name} still in process table",
                        )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

        return ValidationResult(
            success=True, confidence=CONFIDENCE["process"],
            verification_method="process",
            failure_type=FailureType.NONE,
            detail=f"{app_name} not found — likely closed",
            changed_domains=changed_domains,
            changed_fields=changed_fields,
        )

    def _verify_ui_control(self, params: dict, result: str, before: dict) -> ValidationResult:
        """
        Verify ui_control actions (click, type, select_tab, etc.).

        Strategy:
            - Check if result string indicates failure
            - Check if WorldState changed (window, control, browser)
            - For type actions: try to read control value via UIA
        """
        action = params.get("action", "")
        target = params.get("target", "")

        # Check result string first
        if _result_indicates_failure(result):
            failure_type = _classify_failure(result)
            return ValidationResult(
                success=False, confidence=CONFIDENCE["result_parse"],
                verification_method="result_parse",
                failure_type=failure_type,
                detail=f"ui_control({action}) failed: {result[:100]}",
            )

        changed_domains, changed_fields = self._diff_fields(before, "ui_control")

        # For click actions: state should have changed
        if action in ("click", "click_button", "select_tab"):
            if changed_domains:
                return ValidationResult(
                    success=True, confidence=CONFIDENCE["world_state"],
                    verification_method="world_state",
                    failure_type=FailureType.NONE,
                    detail=f"State changed after {action}: {changed_fields}",
                    changed_domains=changed_domains,
                    changed_fields=changed_fields,
                )

            # No state change but tool said success — uncertain
            # Give moderate confidence (tool reported success but we can't confirm)
            return ValidationResult(
                success=True, confidence=CONFIDENCE["passthrough"],
                verification_method="passthrough",
                failure_type=FailureType.NONE,
                detail=f"{action}('{target}') — tool reported success, no state change detected",
                changed_domains=changed_domains,
                changed_fields=changed_fields,
            )

        # For type actions: check if control text changed
        if action in ("type", "type_into_field", "set_text"):
            text = params.get("text", "")
            if text:
                # Try UIA to verify typed text
                typed_ok = self._verify_typed_text_uia(text)
                if typed_ok:
                    return ValidationResult(
                        success=True, confidence=CONFIDENCE["uia"],
                        verification_method="uia",
                        failure_type=FailureType.NONE,
                        detail=f"Typed text confirmed in control via UIA",
                        changed_domains=changed_domains,
                        changed_fields=changed_fields,
                    )

            # Fall through to passthrough
            return ValidationResult(
                success=True, confidence=CONFIDENCE["passthrough"],
                verification_method="passthrough",
                failure_type=FailureType.NONE,
                detail=f"type action — could not confirm text in control",
                changed_domains=changed_domains,
                changed_fields=changed_fields,
            )

        # For other actions: trust tool result
        return ValidationResult(
            success=True, confidence=CONFIDENCE["result_parse"],
            verification_method="result_parse",
            failure_type=FailureType.NONE,
            detail=f"ui_control({action}) — result-based verification",
            changed_domains=changed_domains,
            changed_fields=changed_fields,
        )

    def _verify_click_element(self, params: dict, result: str, before: dict) -> ValidationResult:
        """Verify click_element — delegates to ui_control click logic."""
        # Repackage as ui_control click
        repackaged = {"action": "click", "target": params.get("element_name", "")}
        return self._verify_ui_control(repackaged, result, before)

    def _verify_type_into(self, params: dict, result: str, before: dict) -> ValidationResult:
        """Verify type_into — check if target field contains the typed text."""
        if _result_indicates_failure(result):
            failure_type = _classify_failure(result)
            return ValidationResult(
                success=False, confidence=CONFIDENCE["result_parse"],
                verification_method="result_parse",
                failure_type=failure_type,
                detail=f"type_into failed: {result[:100]}",
            )

        text = params.get("text", "")
        changed_domains, changed_fields = self._diff_fields(before, "type_into")

        # Try UIA verification
        if text:
            typed_ok = self._verify_typed_text_uia(text)
            if typed_ok:
                return ValidationResult(
                    success=True, confidence=CONFIDENCE["uia"],
                    verification_method="uia",
                    failure_type=FailureType.NONE,
                    detail=f"Typed text '{text[:30]}' confirmed in control",
                    changed_domains=changed_domains,
                    changed_fields=changed_fields,
                )

        return ValidationResult(
            success=True, confidence=CONFIDENCE["passthrough"],
            verification_method="passthrough",
            failure_type=FailureType.NONE,
            detail=f"type_into — tool reported success",
            changed_domains=changed_domains,
            changed_fields=changed_fields,
        )

    def _verify_type_text(self, params: dict, result: str, before: dict) -> ValidationResult:
        """Verify type_text — check if focused control changed."""
        changed_domains, changed_fields = self._diff_fields(before, "type_text")

        if _result_indicates_failure(result):
            return ValidationResult(
                success=False, confidence=CONFIDENCE["result_parse"],
                verification_method="result_parse",
                failure_type=_classify_failure(result),
                detail=f"type_text failed: {result[:100]}",
            )

        # If control domain changed, typing likely worked
        if "control" in changed_domains:
            return ValidationResult(
                success=True, confidence=CONFIDENCE["world_state"],
                verification_method="world_state",
                failure_type=FailureType.NONE,
                detail="Focused control changed after typing",
                changed_domains=changed_domains,
                changed_fields=changed_fields,
            )

        # Hotkey path: check if anything changed
        hotkey = params.get("hotkey")
        if hotkey:
            if changed_domains:
                return ValidationResult(
                    success=True, confidence=CONFIDENCE["world_state"],
                    verification_method="world_state",
                    failure_type=FailureType.NONE,
                    detail=f"State changed after hotkey '{hotkey}'",
                    changed_domains=changed_domains,
                    changed_fields=changed_fields,
                )

        # No detectable change — trust tool result with lower confidence
        return ValidationResult(
            success=True, confidence=CONFIDENCE["passthrough"],
            verification_method="passthrough",
            failure_type=FailureType.NONE,
            detail="type_text — no state change detected",
            changed_domains=changed_domains,
            changed_fields=changed_fields,
        )

    def _verify_computer_control(self, params: dict, result: str, before: dict) -> ValidationResult:
        """
        Verify computer_control actions.

        Strategy depends on action type:
            - focus_window: check active_title matches
            - screen_click: check state changed
            - type/smart_type: check control changed
        """
        action = params.get("action", "")

        if _result_indicates_failure(result):
            return ValidationResult(
                success=False, confidence=CONFIDENCE["result_parse"],
                verification_method="result_parse",
                failure_type=_classify_failure(result),
                detail=f"computer_control({action}) failed: {result[:100]}",
            )

        changed_domains, changed_fields = self._diff_fields(before, "computer_control")

        # Focus verification
        if action == "focus_window":
            title = params.get("title", "")
            ws = self._get_ws()
            if ws and title:
                active = (ws.get("windows", "active_title") or "").lower()
                if title.lower() in active:
                    return ValidationResult(
                        success=True, confidence=CONFIDENCE["world_state"],
                        verification_method="world_state",
                        failure_type=FailureType.NONE,
                        detail=f"Window '{title}' is focused",
                        changed_domains=changed_domains,
                        changed_fields=changed_fields,
                    )
                else:
                    return ValidationResult(
                        success=False, confidence=CONFIDENCE["world_state"],
                        verification_method="world_state",
                        failure_type=FailureType.FOCUS_LOST,
                        detail=f"Wanted '{title}' focused, got '{active}'",
                        changed_domains=changed_domains,
                        changed_fields=changed_fields,
                    )

        # Click/type: check state changed
        if action in ("screen_click", "screen_find", "type", "smart_type"):
            if changed_domains:
                return ValidationResult(
                    success=True, confidence=CONFIDENCE["world_state"],
                    verification_method="world_state",
                    failure_type=FailureType.NONE,
                    detail=f"State changed after {action}: {changed_fields}",
                    changed_domains=changed_domains,
                    changed_fields=changed_fields,
                )

        # Generic: trust result
        return ValidationResult(
            success=True, confidence=CONFIDENCE["result_parse"],
            verification_method="result_parse",
            failure_type=FailureType.NONE,
            detail=f"computer_control({action}) — result-based",
            changed_domains=changed_domains,
            changed_fields=changed_fields,
        )

    def _verify_clipboard(self, params: dict, result: str, before: dict) -> ValidationResult:
        """Verify clipboard actions."""
        action = params.get("action", "read")
        changed_domains, changed_fields = self._diff_fields(before, "clipboard")

        if action == "write" or action == "copy":
            # Check if clipboard changed
            if "clipboard" in changed_domains:
                return ValidationResult(
                    success=True, confidence=CONFIDENCE["world_state"],
                    verification_method="world_state",
                    failure_type=FailureType.NONE,
                    detail="Clipboard content updated",
                    changed_domains=changed_domains,
                    changed_fields=changed_fields,
                )
            else:
                return ValidationResult(
                    success=True, confidence=CONFIDENCE["passthrough"],
                    verification_method="passthrough",
                    failure_type=FailureType.NONE,
                    detail="Clipboard write — no change detected in WorldState (may be delayed)",
                    changed_domains=changed_domains,
                    changed_fields=changed_fields,
                )

        # Read: result-based
        return self._verify_result_based(params, result, before)

    def _verify_system_control(self, params: dict, result: str, before: dict) -> ValidationResult:
        """Verify system control actions where possible."""
        action = params.get("action", "")
        changed_domains, changed_fields = self._diff_fields(before, "system_control")

        if _result_indicates_failure(result):
            return ValidationResult(
                success=False, confidence=CONFIDENCE["result_parse"],
                verification_method="result_parse",
                failure_type=_classify_failure(result),
                detail=f"system_control({action}) failed: {result[:100]}",
            )

        # Volume: try API verification
        if action in ("volume_set", "volume_up", "volume_down", "volume_mute"):
            try:
                from actions.system_control import _get_volume
                vol = _get_volume()
                if vol is not None:
                    return ValidationResult(
                        success=True, confidence=CONFIDENCE["uia"],
                        verification_method="uia",
                        failure_type=FailureType.NONE,
                        detail=f"Volume confirmed at {vol}%",
                        changed_domains=changed_domains,
                        changed_fields=changed_fields,
                    )
            except Exception:
                pass

        return ValidationResult(
            success=True, confidence=CONFIDENCE["result_parse"],
            verification_method="result_parse",
            failure_type=FailureType.NONE,
            detail=f"system_control({action}) — result-based",
            changed_domains=changed_domains,
            changed_fields=changed_fields,
        )

    def _verify_result_based(self, params: dict, result: str, before: dict) -> ValidationResult:
        """
        Verify tools that return their own success/failure indicators.

        Concept: Some tools (file_manager, web_search, run_command) produce
        self-describing results. We parse the result string for known
        failure patterns. This is inherently brittle (Issue #8) but is
        the pragmatic approach until tools return structured responses.
        """
        if _result_indicates_failure(result):
            return ValidationResult(
                success=False, confidence=CONFIDENCE["result_parse"],
                verification_method="result_parse",
                failure_type=_classify_failure(result),
                detail=f"Result indicates failure: {result[:100]}",
            )

        return ValidationResult(
            success=True, confidence=CONFIDENCE["result_parse"],
            verification_method="result_parse",
            failure_type=FailureType.NONE,
            detail=f"Result indicates success: {result[:80]}",
        )

    def _verify_passthrough(self, params: dict, result: str, before: dict) -> ValidationResult:
        """
        Passthrough for tools with no specific verifier.

        Used for: sleep_jarvis, shutdown_jarvis, agent_task, etc.
        """
        return ValidationResult(
            success=True, confidence=CONFIDENCE["passthrough"],
            verification_method="passthrough",
            failure_type=FailureType.NONE,
            detail="No specific verifier — passthrough",
        )

    # ═══════════════════════════════════════════════════════════
    # UIA HELPERS (Tier 2)
    # ═══════════════════════════════════════════════════════════

    def _verify_typed_text_uia(self, expected_text: str) -> bool:
        """
        Check if the currently focused control contains the expected text.

        Uses UIA ValuePattern to read the control's current value.
        This works for: text inputs, address bars, search fields.
        Does NOT work for: code editors, terminals, rich text editors.
        """
        try:
            import uiautomation as uia
            ctrl = uia.GetFocusedControl()
            if ctrl is None:
                return False

            # Try ValuePattern
            try:
                value = ctrl.GetValuePattern().Value or ""
                if expected_text.lower() in value.lower():
                    return True
            except Exception:
                pass

            # Try control Name (some controls put text in Name)
            name = ctrl.Name or ""
            if expected_text.lower() in name.lower():
                return True

            return False
        except Exception:
            return False

    # ═══════════════════════════════════════════════════════════
    # BACKWARD COMPATIBILITY (drop-in for verification.py)
    # ═══════════════════════════════════════════════════════════

    def verify_action(self, action_type: str, params: dict) -> dict:
        """
        Legacy API compatible with core/verification.py:ValidationEngine.

        Used by executor.py which expects:
            {"verified": bool, "method": str, "detail": str}

        Wraps the new verify() method.
        """
        before = self.snapshot_before(action_type)
        # Phase 2: reduced from 0.3s — one WorldState poll cycle
        time.sleep(0.1)
        result = self.verify(action_type, params, "", before)
        return {
            "verified": result.success,
            "method": result.verification_method,
            "detail": result.detail,
            "confidence": result.confidence,
            "failure_type": result.failure_type.value,
            "changed_fields": result.changed_fields,
        }


# ═══════════════════════════════════════════════════════════════
# RESULT STRING PARSING
# ═══════════════════════════════════════════════════════════════

# Concept: Tools currently return unstructured strings. These patterns
# detect failures from the result text. This is inherently brittle
# (Issue #8) and should be replaced with structured tool responses
# in a future refactor. For now, it's the pragmatic approach.

_FAILURE_INDICATORS = [
    "not found",
    "element not found",
    "could not find",
    "unable to find",
    "no element",
    "failed to",
    "error:",
    "timed out",
    "cannot find",
    "access denied",
    "permission denied",
    "not available",
    "unavailable",
    "does not exist",
    "no such",
    "could not connect",
    "connection refused",
]


def _result_indicates_failure(result: str) -> bool:
    """Check if a tool result string indicates failure."""
    if not result:
        return True
    result_lower = result.lower()
    return any(indicator in result_lower for indicator in _FAILURE_INDICATORS)


def _classify_failure(result: str) -> FailureType:
    """
    Classify a failure result string into a FailureType.

    Concept: Different failure messages map to different recovery strategies.
    This classification is deterministic — no AI reasoning.
    """
    if not result:
        return FailureType.VERIFICATION_FAILED

    result_lower = result.lower()

    if any(kw in result_lower for kw in ["not found", "no element", "cannot find",
                                          "could not find", "unable to find",
                                          "does not exist", "no such"]):
        return FailureType.NOT_FOUND

    if any(kw in result_lower for kw in ["access denied", "permission denied",
                                          "not authorized", "forbidden"]):
        return FailureType.PERMISSION_DENIED

    if any(kw in result_lower for kw in ["timed out", "timeout", "deadline"]):
        return FailureType.TIMEOUT

    if any(kw in result_lower for kw in ["ambiguous", "multiple", "too many matches"]):
        return FailureType.AMBIGUOUS_MATCH

    if any(kw in result_lower for kw in ["focus", "foreground", "wrong window"]):
        return FailureType.FOCUS_LOST

    return FailureType.VERIFICATION_FAILED
