"""
core/failure_logger.py — Structured Failure & Routing Logger
═══════════════════════════════════════════════════════════════
Logs every failed, recovered, and successfully routed automation
step as structured JSONL for future fine-tuning.

Concept — Why Log Failures?
  Every failed action is a free training example. Every successful
  recovery is an even BETTER training example (it shows the model
  what went wrong AND how to fix it). By logging structured data
  we build a dataset that can later produce:
  - Fine-tuning examples for tool selection accuracy
  - Benchmark regressions (did we break something?)
  - Root cause analysis (which tools fail most often?)

Output files:
  training_data/failures.jsonl  — failed/recovered tool executions
  training_data/routing.jsonl   — intent routing decisions
  training_data/successes.jsonl — successful executions (positive examples)

Security:
  All entries are redacted before writing — clipboard text, passwords,
  tokens, and API keys are replaced with '[REDACTED]'.
"""

import copy
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("JARVIS.failure_logger")

BASE_DIR = Path(__file__).parent.parent
DEFAULT_LOG_DIR = BASE_DIR / "training_data"

# Fields that contain sensitive data and must be redacted
_SENSITIVE_KEYS = frozenset({
    "clipboard_text", "clipboard", "password", "token", "secret",
    "api_key", "apikey", "api_secret", "access_token", "refresh_token",
    "authorization", "auth", "cookie", "session_id", "private_key",
    "content",  # file content can contain secrets
})

# Regex to detect potential secrets inline (Bearer tokens, API keys, etc.)
_SECRET_PATTERN = re.compile(
    r"(?:"
    r"(?:Bearer|Basic)\s+[A-Za-z0-9+/=_\-]{20,}"
    r"|"
    r"(?:sk|pk|api|key|token|secret|password)[_\-]?[A-Za-z0-9]{16,}"
    r"|"
    r"[A-Za-z0-9]{32,}"  # Long hex/base64 strings (likely tokens)
    r")",
    re.IGNORECASE,
)

# Max file size before rotation (50 MB)
_MAX_FILE_SIZE = 50 * 1024 * 1024


def _redact(data: Any, depth: int = 0) -> Any:
    """
    Deep-copy and redact sensitive fields from a data structure.

    Concept:
      Recursively walks dicts and lists, replacing values of sensitive
      keys with '[REDACTED]'. Also scrubs inline secrets from string
      values (Bearer tokens, long API keys, etc.)

    Args:
        data: Any JSON-serializable structure
        depth: Recursion depth limiter (max 10)

    Returns:
        Redacted deep copy of the data
    """
    if depth > 10:
        return "[DEPTH_LIMIT]"

    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if k.lower() in _SENSITIVE_KEYS:
                result[k] = "[REDACTED]"
            else:
                result[k] = _redact(v, depth + 1)
        return result
    elif isinstance(data, (list, tuple)):
        return [_redact(item, depth + 1) for item in data]
    elif isinstance(data, str):
        if len(data) > 200:
            # Truncate long strings (probably file content)
            return data[:100] + f"... [TRUNCATED {len(data)} chars]"
        if _SECRET_PATTERN.search(data):
            return "[REDACTED]"
        return data
    else:
        return data


def _safe_world_state(ws: Any) -> dict:
    """
    Extract safe fields from a WorldState object for logging.

    Concept:
      WorldState contains everything about the desktop — including
      clipboard text and raw vision context. We only log the fields
      that are useful for training without privacy risk.

    Included: active_window, focused_control, browser_url, running_apps, workflow
    Excluded: clipboard_text, raw vision context, system metrics
    """
    if ws is None:
        return {}

    safe = {}

    # Try various accessor patterns (WorldState uses properties)
    for field in ("active_window", "focused_control", "browser_url", "workflow"):
        try:
            val = getattr(ws, field, None)
            if val is not None:
                safe[field] = str(val)[:200]
        except Exception:
            pass

    # Running apps — just names
    try:
        apps = getattr(ws, "running_apps", None)
        if apps and isinstance(apps, (list, set)):
            safe["running_apps"] = sorted(str(a) for a in list(apps)[:20])
    except Exception:
        pass

    return safe


class FailureLogger:
    """
    Thread-safe JSONL logger for automation failures, recoveries,
    and routing decisions.

    Concept — JSONL Format:
      Each line is a standalone JSON object. This format is:
      - Append-friendly (no need to parse the whole file)
      - Streamable (process line-by-line)
      - Compatible with training data loaders
      - Easy to grep/filter

    Thread safety:
      All writes are serialized through a threading.Lock per file.
      Each write is flushed immediately to prevent data loss on crash.

    File rotation:
      When a log file exceeds 50MB, it's renamed with a timestamp
      suffix and a new file is created.
    """

    def __init__(self, log_dir: Optional[str] = None):
        """
        Initialize the failure logger.

        Args:
            log_dir: Directory for log files. Defaults to training_data/
                     in the Jarvis project root.
        """
        self._log_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
        self._log_dir.mkdir(parents=True, exist_ok=True)

        self._session_id = str(uuid.uuid4())[:8]
        self._lock_failures = threading.Lock()
        self._lock_routing = threading.Lock()
        self._lock_successes = threading.Lock()

        self._failures_path = self._log_dir / "failures.jsonl"
        self._routing_path = self._log_dir / "routing.jsonl"
        self._successes_path = self._log_dir / "successes.jsonl"

        log.info(
            "FailureLogger initialized: session=%s, dir=%s",
            self._session_id, self._log_dir,
        )

    @property
    def session_id(self) -> str:
        return self._session_id

    def log_failure(self, entry: dict) -> None:
        """
        Log a failed or recovered automation step.

        Entry schema:
          timestamp, session_id, user_input, route, plan, step_index,
          tool, params, tool_result, validation, recovery_attempted,
          recovery_success, world_state_before, world_state_after, latency_ms
        """
        entry = _redact(copy.deepcopy(entry))
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        entry.setdefault("session_id", self._session_id)
        self._write(self._failures_path, entry, self._lock_failures)

    def log_routing(self, entry: dict) -> None:
        """
        Log an intent routing decision for analysis.

        Entry schema:
          timestamp, user_input, route, tool, params, reason, latency_ms
        """
        entry = _redact(copy.deepcopy(entry))
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        entry.setdefault("session_id", self._session_id)
        self._write(self._routing_path, entry, self._lock_routing)

    def log_success(self, entry: dict) -> None:
        """
        Log a successful tool execution (positive training example).

        Entry schema:
          timestamp, session_id, tool, params, result, validation,
          latency_ms, route
        """
        entry = _redact(copy.deepcopy(entry))
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        entry.setdefault("session_id", self._session_id)
        self._write(self._successes_path, entry, self._lock_successes)

    def _write(self, path: Path, entry: dict, lock: threading.Lock) -> None:
        """
        Append a JSON entry to a file with rotation and flushing.

        Thread-safe: acquires lock before write.
        Crash-safe: flushes after every write.
        """
        with lock:
            try:
                # Check rotation
                self._rotate_if_needed(path)

                with open(path, "a", encoding="utf-8") as f:
                    json.dump(entry, f, ensure_ascii=False, default=str)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())

            except Exception as e:
                log.error("Failed to write log entry to %s: %s", path, e)

    def _rotate_if_needed(self, path: Path) -> None:
        """
        Rotate log file if it exceeds the max size.

        Concept:
          Rename current file with timestamp suffix, so the main
          filename is always the active log. Old logs are preserved
          for analysis.
        """
        if path.exists() and path.stat().st_size > _MAX_FILE_SIZE:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            rotated = path.with_suffix(f".{ts}.jsonl")
            try:
                path.rename(rotated)
                log.info("Rotated log: %s → %s", path.name, rotated.name)
            except OSError as e:
                log.warning("Log rotation failed: %s", e)


# ─── Singleton ──────────────────────────────────────────────────────
_logger_instance: Optional[FailureLogger] = None
_singleton_lock = threading.Lock()


def get_failure_logger() -> FailureLogger:
    """Thread-safe singleton accessor."""
    global _logger_instance
    if _logger_instance is None:
        with _singleton_lock:
            if _logger_instance is None:
                _logger_instance = FailureLogger()
    return _logger_instance


# ─── Convenience Functions (for engine.py integration) ──────────────

def log_tool_failure(
    tool_name: str,
    params: dict,
    result: str,
    validation: dict,
    user_input: str = "",
    recovery_info: Optional[dict] = None,
    world_state_before: Any = None,
    world_state_after: Any = None,
    latency_ms: float = 0,
) -> None:
    """
    Shorthand for logging a tool failure from engine._execute_tool_local().

    Args:
        tool_name: Name of the tool that failed
        params: Parameters passed to the tool
        result: String result from the tool
        validation: Validation result dict (success, confidence, failure_type)
        user_input: Original user text that triggered this
        recovery_info: Dict with recovery_attempted and recovery_success
        world_state_before: WorldState snapshot before action
        world_state_after: WorldState snapshot after action
        latency_ms: Time taken for the tool execution
    """
    entry = {
        "tool": tool_name,
        "params": params,
        "tool_result": str(result)[:500],
        "validation": validation,
        "user_input": user_input,
        "latency_ms": round(latency_ms, 1),
        "world_state_before": _safe_world_state(world_state_before),
        "world_state_after": _safe_world_state(world_state_after),
    }
    if recovery_info:
        entry["recovery_attempted"] = recovery_info.get("attempted", "")
        entry["recovery_success"] = recovery_info.get("success", False)
    get_failure_logger().log_failure(entry)


def log_tool_success(
    tool_name: str,
    params: dict,
    result: str,
    validation: dict,
    latency_ms: float = 0,
    route: str = "llm",
) -> None:
    """
    Log a successful tool execution (positive training example).

    Args:
        tool_name: Name of the tool
        params: Parameters passed
        result: String result
        validation: Validation dict
        latency_ms: Execution time
        route: How this was routed (intent_router, app_profile, llm)
    """
    entry = {
        "tool": tool_name,
        "params": params,
        "tool_result": str(result)[:300],
        "validation": validation,
        "latency_ms": round(latency_ms, 1),
        "route": route,
    }
    get_failure_logger().log_success(entry)


def log_route_decision(
    user_input: str,
    route: str,
    tool: str,
    params: dict,
    reason: str,
    latency_ms: float = 0,
) -> None:
    """
    Log an intent routing decision.

    Args:
        user_input: Original user text
        route: Router that handled it (intent_router, app_profile, llm)
        tool: Tool name selected
        params: Tool parameters
        reason: Why this route was chosen
        latency_ms: Routing decision time
    """
    entry = {
        "user_input": user_input,
        "route": route,
        "tool": tool,
        "params": params,
        "reason": reason,
        "latency_ms": round(latency_ms, 3),
    }
    get_failure_logger().log_routing(entry)


# ─── Self-Test ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile

    logging.basicConfig(level=logging.DEBUG, format="%(message)s")

    # Use temp dir for test
    test_dir = Path(tempfile.mkdtemp(prefix="jarvis_test_"))
    print(f"\nTest log dir: {test_dir}\n")

    logger = FailureLogger(log_dir=str(test_dir))

    # ── Test failures ──
    print("Logging 3 failures...")
    logger.log_failure({
        "user_input": "open chrome and search for ML",
        "route": "agent_task",
        "step_index": 2,
        "tool": "computer_control",
        "params": {"action": "smart_type", "text": "ML"},
        "tool_result": "Typed text: ML",
        "validation": {"success": False, "confidence": 0.3, "failure_type": "focus_lost"},
        "world_state_before": {"active_window": "Chrome", "focused_control": "BookmarksBar"},
        "world_state_after": {"active_window": "Chrome", "focused_control": "OmniboxEditCtrl"},
        "recovery_attempted": "refocus_window",
        "recovery_success": True,
        "latency_ms": 1200,
    })

    logger.log_failure({
        "user_input": "open spotify",
        "tool": "open_app",
        "params": {"app_name": "spotify"},
        "validation": {"success": False, "failure_type": "timeout"},
        "latency_ms": 15000,
    })

    # Test with sensitive data (should be redacted)
    logger.log_failure({
        "user_input": "paste clipboard",
        "tool": "clipboard",
        "params": {"action": "read"},
        "tool_result": "my_password_123_secret",
        "clipboard_text": "Bearer eyJhbGciOiJIUzI1NiJ9.test",
        "password": "hunter2",
        "api_key": "sk-1234567890abcdef",
        "validation": {"success": False},
    })

    # ── Test routing ──
    print("Logging 5 routing decisions...")
    for text, tool, route in [
        ("pause music", "system_control", "intent_router"),
        ("open chrome", "open_app", "intent_router"),
        ("what is 2+2", "none", "llm"),
        ("volume 50", "system_control", "intent_router"),
        ("open chrome and search python", "agent_task", "intent_router"),
    ]:
        logger.log_routing({
            "user_input": text,
            "route": route,
            "tool": tool,
            "params": {},
            "reason": f"pattern_match: {text.split()[0]}",
            "latency_ms": 0.1,
        })

    # ── Test successes ──
    print("Logging 2 successes...")
    logger.log_success({
        "tool": "system_control",
        "params": {"action": "media_pause"},
        "tool_result": "Media paused",
        "validation": {"success": True, "confidence": 1.0},
        "route": "intent_router",
        "latency_ms": 5,
    })

    logger.log_success({
        "tool": "open_app",
        "params": {"app_name": "chrome"},
        "tool_result": "Opened Chrome",
        "validation": {"success": True, "confidence": 0.95},
        "route": "intent_router",
        "latency_ms": 800,
    })

    # ── Verify files ──
    print(f"\n{'─' * 60}")
    for name in ["failures.jsonl", "routing.jsonl", "successes.jsonl"]:
        fpath = test_dir / name
        if fpath.exists():
            lines = fpath.read_text().strip().split("\n")
            print(f"\n📄 {name} ({len(lines)} entries):")
            for line in lines:
                entry = json.loads(line)
                # Check redaction
                flat = json.dumps(entry)
                if "hunter2" in flat or "eyJhbGci" in flat or "sk-1234" in flat:
                    print("  ❌ REDACTION FAILED — sensitive data leaked!")
                else:
                    tool = entry.get("tool", entry.get("route", "?"))
                    print(f"  ✅ {tool}: {entry.get('user_input', '')[:40]}")

    print(f"\n{'─' * 60}")
    print(f"All tests passed. Logs at: {test_dir}")
