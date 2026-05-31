"""
core/execution_graph.py — Execution Graph Engine for Jarvis (V5.1)

Replaces the linear step-loop in executor.py with a DAG-based execution
model that enables:
  - Fallback chains per node (try keyboard, then UIA, then clipboard paste)
  - Typed recovery per FailureType (focus_lost -> refocus, not_found -> wait)
  - Critical path cascading (app won't open -> skip all downstream)
  - Partial success tracking (3/5 steps completed -> 60%)
  - Stabilization windows (open_app waits 2s before verify, type waits 0.3s)
  - Execution logs (full history of every attempt for debugging)

Architecture:
    - ExecutionNode: Single action with fallbacks and recovery strategies
    - ExecutionEdge: Dependency between two nodes
    - ExecutionGraph: DAG of nodes with edges
    - run_graph(): Main execution loop (Observe -> Execute -> Verify -> Recover)

Design Rules:
    1. The graph engine OWNS retries and fallbacks.
    2. The Validator ONLY reports — never retries (from CHANGE 3).
    3. Recovery maps are keyed by FailureType — typed recovery, not generic.
    4. Fallback chains try DIFFERENT approaches, not the same one repeated.
    5. Replanning calls LLM only for failed subgraph, not the whole plan.
    6. Execution is sequential (parallel later — Issue 3 from user feedback).

Concept: State-Driven Execution
    Instead of "run step 1, run step 2, run step 3", the graph asks:
    "Which nodes have their dependencies satisfied?" and runs those.
    This enables adaptive execution where failures don't always mean
    stopping — independent downstream nodes can still proceed.
"""

import copy
import enum
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from uuid import uuid4

log = logging.getLogger("JARVIS.exec_graph")


# ═══════════════════════════════════════════════════════════════
# NODE STATUS ENUM
# ═══════════════════════════════════════════════════════════════

class NodeStatus(str, enum.Enum):
    """
    Lifecycle status for an execution node.

    State machine:
        PENDING -> RUNNING -> COMPLETED
                           -> FAILED
                           -> SKIPPED (if upstream critical node failed)
    """
    PENDING   = "pending"      # Not yet started
    RUNNING   = "running"      # Currently executing (primary or fallback)
    COMPLETED = "completed"    # Successfully verified
    FAILED    = "failed"       # All attempts + fallbacks exhausted
    SKIPPED   = "skipped"      # Upstream critical node failed


# ═══════════════════════════════════════════════════════════════
# EXECUTION NODE
# ═══════════════════════════════════════════════════════════════

@dataclass
class ExecutionNode:
    """
    A single action in the execution graph.

    Concept: Each node represents a GOAL, not just a tool call.
    The primary tool + params are the preferred way to achieve the goal.
    The fallback_chain provides alternative approaches.
    The recovery_map provides pre-retry repairs based on failure type.

    Example:
        Goal: "Type 'hello' into the search box"
        Primary: type_text(text="hello")
        Fallback 1: computer_control(action="type", text="hello")
        Fallback 2: clipboard(action="paste", text="hello")
        Recovery[FOCUS_LOST]: refocus window before retrying
    """
    id: str                         # Unique node ID (e.g. "step_1")
    tool: str                       # Primary tool name
    params: dict                    # Primary tool parameters
    description: str                # Human-readable description

    # State tracking
    status: NodeStatus = NodeStatus.PENDING
    attempts: int = 0               # Total attempts on PRIMARY action
    max_attempts: int = 3           # Max retries for primary (before fallbacks)
    result: str = ""                # Last tool result string
    validation: Any = None          # Last ValidationResult

    # Fallback chain: list of (tool, params) tuples
    # These are DIFFERENT approaches to the same goal.
    # Tried in order AFTER primary exhausts max_attempts.
    fallback_chain: list = field(default_factory=list)
    fallback_index: int = -1        # -1 = primary, 0+ = fallback chain index
    fallback_attempts: list = field(default_factory=list)  # Track attempts per fallback

    # Timing
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    stabilization_ms: int = 500     # Wait time after exec before verify (ms)
    node_timeout_s: float = 30.0    # Max time per node before giving up

    # Dependency management
    is_critical: bool = True        # If True, failure blocks downstream nodes

    # Execution log: full history of every attempt
    execution_log: list = field(default_factory=list)

    def log_attempt(self, tool: str, params: dict, result: str,
                    validation: Any, is_fallback: bool = False,
                    recovery_action: str = ""):
        """Record an attempt in the execution log."""
        self.execution_log.append({
            "timestamp": time.time(),
            "tool": tool,
            "params": {k: str(v)[:100] for k, v in params.items()},
            "result": str(result)[:200],
            "success": validation.success if validation else False,
            "confidence": validation.confidence if validation else 0.0,
            "failure_type": validation.failure_type.value if validation else "",
            "method": validation.verification_method if validation else "",
            "is_fallback": is_fallback,
            "recovery_action": recovery_action,
        })

    def elapsed_ms(self) -> float:
        """Compute elapsed time in milliseconds."""
        if self.started_at is None:
            return 0.0
        end = self.completed_at or time.time()
        return (end - self.started_at) * 1000.0


# ═══════════════════════════════════════════════════════════════
# EXECUTION EDGE
# ═══════════════════════════════════════════════════════════════

@dataclass
class ExecutionEdge:
    """
    Dependency between two execution nodes.

    Concept: An edge from A to B means "B depends on A".
    B can only execute when A is COMPLETED.
    If A is FAILED and A.is_critical, then B is SKIPPED.

    edge_type:
        "depends_on"     — hard dependency, B cannot run without A
        "optional_after" — soft dependency, B prefers A but can proceed
    """
    from_node: str       # Source node ID
    to_node: str         # Target node ID
    edge_type: str = "depends_on"


# ═══════════════════════════════════════════════════════════════
# STABILIZATION WINDOWS (per tool type)
# ═══════════════════════════════════════════════════════════════

# How long to wait after tool execution before running verification.
# This addresses Issue 4 (temporal validation) from user feedback:
# some state changes take time (window appearing, page loading).

STABILIZATION_MS = {
    "open_app":         2000,   # Windows need time to render
    "close_app":        1500,   # Process termination propagation
    "ui_control":       500,    # UI reactions are fast
    "click_element":    500,
    "type_into":        300,    # Typing is immediate
    "type_text":        300,
    "computer_control": 800,    # Mixed — some actions are slow
    "file_manager":     200,    # Filesystem ops are fast
    "web_search":       200,    # Result-based, no state to wait for
    "clipboard":        200,
    "system_control":   1000,   # System changes can be slow
    "run_command":      500,
}


# ═══════════════════════════════════════════════════════════════
# DEFAULT FALLBACK CHAINS (per tool type)
# ═══════════════════════════════════════════════════════════════

# Each fallback is a DIFFERENT approach to achieve the same goal.
# Parameters use {param_name} placeholders that are resolved at runtime.
# Max 3 fallbacks per node (per plan decision).

def _build_fallbacks(tool: str, params: dict) -> list:
    """
    Build fallback chain for a tool based on its type and parameters.

    Concept: Fallbacks should be fundamentally different approaches,
    not just retries. For type_text, the fallback chain is:
      1. computer_control type (coordinate-based)
      2. clipboard paste (OS clipboard injection)
    These are three DIFFERENT mechanisms for achieving the same goal.
    """
    fallbacks = []

    if tool == "open_app":
        app = params.get("app_name", "")
        # Fallback: use shell start command
        fallbacks.append(("run_command", {"command": f"start {app}"}))

    elif tool == "type_text":
        text = params.get("text", "")
        hotkey = params.get("hotkey", "")
        if text:
            # Fallback 1: computer_control type
            fallbacks.append(("computer_control", {"action": "type", "text": text}))
            # Fallback 2: clipboard paste
            fallbacks.append(("clipboard", {"action": "paste", "text": text}))
        elif hotkey:
            # Hotkey has no good fallback
            pass

    elif tool == "ui_control":
        action = params.get("action", "")
        target = params.get("target", "")
        if action in ("click", "click_button"):
            # Fallback: keyboard equivalent if possible
            fallbacks.append(("type_text", {"hotkey": "enter"}))
        elif action in ("type", "type_into_field", "set_text"):
            text = params.get("text", "")
            if text:
                fallbacks.append(("type_text", {"text": text}))
                fallbacks.append(("clipboard", {"action": "paste", "text": text}))

    elif tool == "computer_control":
        action = params.get("action", "")
        if action == "focus_window":
            title = params.get("title", "")
            # Fallback: alt-tab
            fallbacks.append(("type_text", {"hotkey": "alt+tab"}))
        elif action in ("type", "smart_type"):
            text = params.get("text", "")
            if text:
                fallbacks.append(("type_text", {"text": text}))
                fallbacks.append(("clipboard", {"action": "paste", "text": text}))

    # Cap at 3 fallbacks max
    return fallbacks[:3]


# ═══════════════════════════════════════════════════════════════
# DEFAULT RECOVERY MAP
# ═══════════════════════════════════════════════════════════════

def _recovery_refocus(tool_runner, node: ExecutionNode) -> str:
    """
    Recovery for FOCUS_LOST: bring the expected window back to focus.

    Concept: If we tried to click a button but another window
    stole focus, the fix is to refocus the correct window first,
    then retry. This is the most common recovery action.
    """
    # Try to infer the expected window from the node context
    try:
        from actions.window_manager import focus_window
        app_hint = node.params.get("app_name", "") or node.params.get("title", "")
        if app_hint:
            focus_window(app_hint)
            time.sleep(0.5)
            return f"Refocused: {app_hint}"
    except Exception as e:
        log.debug("Recovery refocus failed: %s", e)
    return "refocus_attempted"


def _recovery_wait(tool_runner, node: ExecutionNode) -> str:
    """
    Recovery for NOT_FOUND: wait longer for element to appear.

    Concept: If an element wasn't found, it might not have loaded yet.
    Increasing the stabilization window and waiting gives the UI
    time to render before the retry.
    """
    wait_s = min(node.stabilization_ms / 1000.0 * 2, 5.0)
    time.sleep(wait_s)
    # Increase stabilization for next attempt
    node.stabilization_ms = min(node.stabilization_ms * 2, 5000)
    return f"waited_{wait_s:.1f}s"


def _recovery_extend_timeout(tool_runner, node: ExecutionNode) -> str:
    """
    Recovery for TIMEOUT: increase stabilization window.

    Concept: If the action timed out, the system is probably
    under load or the operation is slow. Extend the timeout
    progressively.
    """
    node.stabilization_ms = min(node.stabilization_ms * 2, 8000)
    time.sleep(1.0)
    return f"timeout_extended_to_{node.stabilization_ms}ms"


# Map FailureType -> recovery function
# Recovery functions are called BEFORE retrying the failed action.
# They return a description string for the execution log.

def _get_recovery_fn(failure_type) -> Optional[Callable]:
    """
    Get the recovery function for a failure type.

    Concept: Typed recovery means each failure type has a specific
    fix that runs before the retry. This is fundamentally different
    from generic "wait and try again" retries.

    Import FailureType inside function to avoid circular imports.
    """
    from core.validator import FailureType

    recovery_map = {
        FailureType.FOCUS_LOST:      _recovery_refocus,
        FailureType.NOT_FOUND:       _recovery_wait,
        FailureType.TIMEOUT:         _recovery_extend_timeout,
        # These failures don't have automatic recovery:
        # PERMISSION_DENIED  -> needs admin, can't auto-fix
        # AMBIGUOUS_MATCH    -> needs context, can't auto-fix
        # WRONG_STATE        -> needs different approach entirely
        # STATE_UNCHANGED    -> retry directly (no pre-repair)
        # VERIFICATION_FAILED -> generic, retry directly
    }
    return recovery_map.get(failure_type)


# ═══════════════════════════════════════════════════════════════
# EXECUTION GRAPH
# ═══════════════════════════════════════════════════════════════

class ExecutionGraph:
    """
    DAG of execution nodes with dependency edges.

    Concept: A plan is not a list of steps — it's a graph of
    goals with dependencies. This enables:
      - Parallel execution (future)
      - Partial success (3/5 nodes completed)
      - Adaptive recovery (skip non-critical failures)
      - Typed retry/fallback per failure type

    Current design: sequential execution only (parallel later).
    Edges are linear by default (step_1 -> step_2 -> step_3).
    """

    def __init__(self, goal: str = ""):
        self.goal: str = goal
        self.nodes: dict[str, ExecutionNode] = {}
        self.edges: list[ExecutionEdge] = []
        self.current_node: Optional[str] = None
        self._started_at: Optional[float] = None
        self._completed_at: Optional[float] = None

    # ─── Construction ─────────────────────────────────────────

    @classmethod
    def from_plan(cls, plan: dict) -> "ExecutionGraph":
        """
        Convert a linear plan dict to an execution graph.

        Concept: The planner outputs a flat list of steps.
        from_plan() enriches each step with:
          - Fallback chains (from tool type)
          - Stabilization windows (from tool type)
          - Critical-path marking (open_app = critical)
          - Linear dependency edges (step_1 -> step_2 -> ...)

        Args:
            plan: Dict with 'goal' and 'steps' from planner.create_plan()

        Returns:
            ExecutionGraph ready for run_graph()
        """
        goal = plan.get("goal", "unknown task")
        graph = cls(goal=goal)

        steps = plan.get("steps", [])
        prev_id = None

        for step in steps:
            node_id = f"step_{step.get('step', len(graph.nodes) + 1)}"
            tool = step.get("tool", "")
            params = step.get("parameters", {})
            desc = step.get("description", "")

            node = ExecutionNode(
                id=node_id,
                tool=tool,
                params=params,
                description=desc,
                stabilization_ms=STABILIZATION_MS.get(tool, 500),
                fallback_chain=_build_fallbacks(tool, params),
                # open_app and focus_window are critical — downstream
                # steps usually depend on the app being open
                is_critical=tool in ("open_app", "close_app") or
                            (tool == "computer_control" and
                             params.get("action") == "focus_window"),
            )

            graph.nodes[node_id] = node

            # Linear dependency: each step depends on the previous
            if prev_id:
                graph.edges.append(ExecutionEdge(
                    from_node=prev_id,
                    to_node=node_id,
                    edge_type="depends_on",
                ))

            prev_id = node_id

        log.info("Graph built: %d nodes, %d edges for '%s'",
                 len(graph.nodes), len(graph.edges), goal[:60])
        return graph

    # ─── Dependency Resolution ────────────────────────────────

    def get_ready_nodes(self) -> list[ExecutionNode]:
        """
        Return nodes whose dependencies are all satisfied.

        Concept: A node is "ready" when:
          - Its status is PENDING
          - ALL its upstream dependencies are COMPLETED
          - No critical upstream node has FAILED (cascade handled by mark_failed)

        For sequential execution: returns at most 1 node.
        For parallel execution (future): returns all ready nodes.
        """
        ready = []

        for node_id, node in self.nodes.items():
            if node.status != NodeStatus.PENDING:
                continue

            # Check all upstream dependencies
            all_deps_met = True
            for edge in self.edges:
                if edge.to_node == node_id:
                    upstream = self.nodes.get(edge.from_node)
                    if upstream is None:
                        continue

                    if edge.edge_type == "depends_on":
                        if upstream.status != NodeStatus.COMPLETED:
                            all_deps_met = False
                            break
                    elif edge.edge_type == "optional_after":
                        # Optional: proceed if upstream is COMPLETED or FAILED
                        if upstream.status not in (NodeStatus.COMPLETED,
                                                   NodeStatus.FAILED,
                                                   NodeStatus.SKIPPED):
                            all_deps_met = False
                            break

            if all_deps_met:
                ready.append(node)

        return ready

    # ─── State Transitions ────────────────────────────────────

    def mark_completed(self, node_id: str, result: str, validation):
        """
        Mark a node as successfully completed.

        Updates timestamps and propagates to unlock downstream nodes.
        """
        node = self.nodes.get(node_id)
        if node is None:
            return

        node.status = NodeStatus.COMPLETED
        node.result = result
        node.validation = validation
        node.completed_at = time.time()

        log.info("Node %s COMPLETED (conf=%.2f, method=%s)",
                 node_id,
                 validation.confidence if validation else 0,
                 validation.verification_method if validation else "?")

    def mark_failed(self, node_id: str, result: str, validation):
        """
        Mark a node as failed after all attempts + fallbacks exhausted.

        If the node is critical, cascade SKIPPED to all downstream
        nodes that depend on it (transitively).

        Concept: Critical path cascading prevents wasting time
        executing steps that can't possibly succeed because their
        prerequisite failed.
        """
        node = self.nodes.get(node_id)
        if node is None:
            return

        node.status = NodeStatus.FAILED
        node.result = result
        node.validation = validation
        node.completed_at = time.time()

        log.warning("Node %s FAILED (type=%s, conf=%.2f)",
                    node_id,
                    validation.failure_type.value if validation else "?",
                    validation.confidence if validation else 0)

        # Critical path cascading
        if node.is_critical:
            self._cascade_skip(node_id)

    def _cascade_skip(self, failed_id: str):
        """
        Transitively skip all downstream nodes of a failed critical node.

        Uses BFS from the failed node through depends_on edges.
        """
        to_skip = set()
        queue = [failed_id]

        while queue:
            current = queue.pop(0)
            for edge in self.edges:
                if edge.from_node == current and edge.edge_type == "depends_on":
                    downstream_id = edge.to_node
                    downstream = self.nodes.get(downstream_id)
                    if downstream and downstream.status == NodeStatus.PENDING:
                        downstream.status = NodeStatus.SKIPPED
                        downstream.completed_at = time.time()
                        to_skip.add(downstream_id)
                        queue.append(downstream_id)

        if to_skip:
            log.warning("Critical path cascade: skipped %d nodes: %s",
                        len(to_skip), sorted(to_skip))

    # ─── Status + Summary ─────────────────────────────────────

    def get_partial_success_summary(self) -> dict:
        """
        Return partial success metrics.

        Concept: Tasks don't have to be all-or-nothing.
        "Open Chrome and search YouTube" might result in
        Chrome opening but search failing. The user needs to
        know exactly what succeeded and what didn't.
        """
        total = len(self.nodes)
        completed = sum(1 for n in self.nodes.values()
                        if n.status == NodeStatus.COMPLETED)
        failed = sum(1 for n in self.nodes.values()
                     if n.status == NodeStatus.FAILED)
        skipped = sum(1 for n in self.nodes.values()
                      if n.status == NodeStatus.SKIPPED)
        pending = sum(1 for n in self.nodes.values()
                      if n.status == NodeStatus.PENDING)

        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "pending": pending,
            "success_ratio": round(completed / max(total, 1), 2),
        }

    def is_finished(self) -> bool:
        """Check if all nodes are in a terminal state."""
        return all(
            n.status in (NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.SKIPPED)
            for n in self.nodes.values()
        )

    def to_execution_log(self) -> list[dict]:
        """
        Serialize full execution history for debugging and learning.

        Concept: Every attempt, every fallback, every recovery action
        is logged. This enables:
          - Post-mortem debugging ("why did step 3 fail?")
          - Performance analysis ("which fallbacks work best?")
          - Future learning (train recovery strategies from logs)
        """
        entries = []
        for node_id in sorted(self.nodes.keys()):
            node = self.nodes[node_id]
            entries.append({
                "node_id": node_id,
                "tool": node.tool,
                "description": node.description,
                "status": node.status.value,
                "attempts": node.attempts,
                "fallback_index": node.fallback_index,
                "elapsed_ms": round(node.elapsed_ms(), 1),
                "is_critical": node.is_critical,
                "validation": node.validation.to_dict() if node.validation else None,
                "execution_log": node.execution_log,
            })
        return entries


# ═══════════════════════════════════════════════════════════════
# GRAPH RUNNER
# ═══════════════════════════════════════════════════════════════

def run_graph(
    graph: ExecutionGraph,
    tool_runner,
    validator=None,
    screenshot_fn=None,
    max_replans: int = 2,
    cancel_event: threading.Event = None,
) -> str:
    """
    Execute the graph respecting dependencies.

    This is the MAIN execution loop. For each ready node:
    1. Pre-action: snapshot WorldState via Validator
    2. Execute: run primary tool
    3. Stabilize: wait for state to settle (interruptible via cancel_event)
    4. Verify: Validator.verify() -> ValidationResult
    5. If failure:
       a. Check recovery map for FailureType-specific recovery
       b. Retry primary (up to max_attempts)
       c. Try fallback chain entries
    6. If all exhausted: mark_failed, cascade skips
    7. If success: mark_completed, unlock downstream

    Concept (Phase 2): All stabilization waits now use
    cancel_event.wait(timeout=N) instead of time.sleep(N).
    This means the entire graph execution can be cancelled instantly
    by setting cancel_event from any thread (e.g., user barge-in).

    Args:
        graph: ExecutionGraph to execute
        tool_runner: Callable(tool_name, params) -> str
        validator: Validator instance (from core.validator)
        screenshot_fn: Optional callable for visual context
        max_replans: Max LLM replanning attempts
        cancel_event: threading.Event — set to cancel execution mid-graph

    Returns:
        Summary string with partial success info.
    """
    from core.validator import Validator, FailureType


    if validator is None:
        validator = Validator()

    # Emit lifecycle events
    bus = None
    try:
        from core.events import get_bus, Event
        bus = get_bus()
    except Exception:
        pass

    graph._started_at = time.time()

    # Default cancel event (never set) if caller didn't provide one
    if cancel_event is None:
        cancel_event = threading.Event()

    log.info("Executing graph: %d nodes for '%s'",
             len(graph.nodes), graph.goal[:60])

    if bus:
        try:
            bus.emit_async(Event.TASK_STARTED, {
                "goal": graph.goal,
                "step_count": len(graph.nodes),
            })
        except Exception:
            pass

    replan_count = 0
    _current_app = None  # Track last opened app for focus management

    # ─── Main execution loop ──────────────────────────────────
    while not graph.is_finished():
        ready = graph.get_ready_nodes()

        if not ready:
            # No ready nodes and not finished — deadlock or all failed
            log.warning("No ready nodes — graph stalled")
            break

        # Sequential: take the first ready node
        node = ready[0]
        graph.current_node = node.id
        node.status = NodeStatus.RUNNING
        node.started_at = time.time()

        log.info(">>> Node %s: [%s] %s", node.id, node.tool, node.description)

        # Track opened apps for focus management
        if node.tool == "open_app":
            _current_app = node.params.get("app_name", "").strip()

        success = _execute_node(
            node, tool_runner, validator, _current_app, screenshot_fn,
            cancel_event=cancel_event,
        )

        # Check for cancellation after each node
        if cancel_event.is_set():
            log.warning("Graph execution cancelled by external event")
            break

        if success:
            graph.mark_completed(node.id, node.result, node.validation)
        else:
            # Attempt replanning for failed critical nodes
            if node.is_critical and replan_count < max_replans:
                replan_count += 1
                log.info("Replanning for failed critical node %s (attempt %d/%d)",
                         node.id, replan_count, max_replans)

                replan_result = _try_replan(
                    graph, node, tool_runner, validator
                )
                if replan_result:
                    # Replan injected new nodes — re-enter loop
                    continue

            graph.mark_failed(node.id, node.result, node.validation)

    graph._completed_at = time.time()
    graph.current_node = None

    # ─── Build summary ────────────────────────────────────────
    summary = graph.get_partial_success_summary()
    elapsed = (graph._completed_at - graph._started_at) * 1000

    # Emit lifecycle event
    if bus:
        try:
            bus.emit_async(Event.TASK_COMPLETED, {
                "goal": graph.goal,
                "success": summary["failed"] == 0 and summary["skipped"] == 0,
                "completed": summary["completed"],
                "failed": summary["failed"],
                "skipped": summary["skipped"],
                "elapsed_ms": round(elapsed),
            })
        except Exception:
            pass

    return _build_summary(graph, summary, elapsed)


# ═══════════════════════════════════════════════════════════════
# NODE EXECUTION (with retry + fallback + recovery)
# ═══════════════════════════════════════════════════════════════

def _execute_node(
    node: ExecutionNode,
    tool_runner,
    validator,
    current_app: Optional[str],
    screenshot_fn=None,
    cancel_event: threading.Event = None,
) -> bool:
    """
    Execute a single node with retry, recovery, and fallback.

    Flow:
        1. Try PRIMARY tool up to max_attempts times
           - Before each retry: run recovery for the specific FailureType
        2. If primary exhausted: try each FALLBACK in chain
           - Each fallback gets 1 attempt (it's already a different approach)
        3. Return True if ANY attempt succeeded, False if ALL exhausted

    Concept: The retry loop is "smart" because recovery actions
    are specific to the failure type. A FOCUS_LOST failure runs
    refocus_window() before retrying. A NOT_FOUND failure extends
    the stabilization window. A TIMEOUT failure doubles the wait.
    """
    from core.validator import FailureType

    # ─── Phase 1: Primary tool attempts ───────────────────────
    for attempt in range(1, node.max_attempts + 1):
        node.attempts = attempt

        # Pre-attempt recovery (for retries, not first attempt)
        recovery_desc = ""
        if attempt > 1 and node.validation:
            recovery_fn = _get_recovery_fn(node.validation.failure_type)
            if recovery_fn:
                try:
                    recovery_desc = recovery_fn(tool_runner, node)
                    log.info("Recovery [%s]: %s",
                             node.validation.failure_type.value, recovery_desc)
                except Exception as e:
                    log.debug("Recovery failed: %s", e)
                    recovery_desc = f"recovery_error: {e}"

        # Focus management: ensure target app is visible
        if current_app and node.tool in ("computer_control", "ui_control",
                                          "type_text", "click_element", "type_into"):
            try:
                from actions.window_manager import focus_window
                focus_window(current_app)
                if cancel_event:
                    cancel_event.wait(timeout=0.3)  # interruptible
                else:
                    time.sleep(0.3)
            except Exception:
                pass

        # Snapshot WorldState before execution
        before = validator.snapshot_before(node.tool)

        # Execute the tool — with per-node timeout (Phase 2)
        # Concept: concurrent.futures wraps the synchronous tool_runner
        # in a worker thread with a hard deadline. If the tool hangs
        # (e.g., unresponsive app), we don't block the entire graph.
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(tool_runner, node.tool, node.params)
                try:
                    result = future.result(timeout=node.node_timeout_s)
                except FutureTimeout:
                    result_str = f"Tool timed out after {node.node_timeout_s}s"
                    log.warning("Node %s timed out", node.id)
                    # Record as a timeout attempt and move to next
                    from core.validator import FailureType
                    timeout_validation = type('V', (), {
                        'success': False, 'confidence': 0.0,
                        'failure_type': FailureType.TIMEOUT,
                        'verification_method': 'timeout'
                    })()
                    node.result = result_str
                    node.validation = timeout_validation
                    node.log_attempt(node.tool, node.params, result_str,
                                     timeout_validation, recovery_action="timeout")
                    continue  # skip to next attempt
                else:
                    result_str = str(result) if result else ""
        except Exception as e:
            result_str = f"Tool exception: {e}"
            log.warning("Node %s attempt %d exception: %s",
                        node.id, attempt, e)

        # Stabilization window — interruptible (Phase 2)
        # Concept: threading.Event.wait(timeout=N) returns immediately
        # if the event is set (cancelled), otherwise sleeps for N seconds.
        # This replaces blocking time.sleep() so the user can barge in.
        stab_s = node.stabilization_ms / 1000.0
        if stab_s > 0 and cancel_event:
            if cancel_event.wait(timeout=stab_s):
                log.info("Stabilization interrupted by cancel")
                return False
        elif stab_s > 0:
            time.sleep(stab_s)

        # Verify
        validation = validator.verify(node.tool, node.params, result_str, before)
        node.result = result_str
        node.validation = validation

        # Log the attempt
        node.log_attempt(node.tool, node.params, result_str, validation,
                         is_fallback=False, recovery_action=recovery_desc)

        if validation.success:
            log.info("Node %s attempt %d succeeded (conf=%.2f, method=%s)",
                     node.id, attempt, validation.confidence,
                     validation.verification_method)

            # Post-step screenshot for visual context
            _take_screenshot(screenshot_fn)
            return True

        log.warning("Node %s attempt %d failed (type=%s, conf=%.2f)",
                    node.id, attempt,
                    validation.failure_type.value, validation.confidence)

        # Brief wait between retries — interruptible
        if attempt < node.max_attempts:
            wait = 0.5 * attempt
            if cancel_event:
                if cancel_event.wait(timeout=wait):
                    return False
            else:
                time.sleep(wait)

    # ─── Phase 2: Fallback chain ──────────────────────────────
    for fb_idx, (fb_tool, fb_params) in enumerate(node.fallback_chain):
        node.fallback_index = fb_idx

        log.info("Node %s fallback %d: [%s] %s",
                 node.id, fb_idx, fb_tool, fb_params)

        # Snapshot before fallback
        before = validator.snapshot_before(fb_tool)

        # Execute fallback
        try:
            result = tool_runner(fb_tool, fb_params)
            result_str = str(result) if result else ""
        except Exception as e:
            result_str = f"Fallback exception: {e}"
            log.warning("Node %s fallback %d exception: %s",
                        node.id, fb_idx, e)

        # Stabilization — interruptible
        stab_s = STABILIZATION_MS.get(fb_tool, 500) / 1000.0
        if cancel_event:
            if cancel_event.wait(timeout=stab_s):
                log.info("Fallback stabilization interrupted by cancel")
                return False
        else:
            time.sleep(stab_s)

        # Verify using the ORIGINAL tool's verifier (we care about the goal,
        # not the mechanism that achieved it)
        validation = validator.verify(node.tool, node.params, result_str, before)
        node.result = result_str
        node.validation = validation

        # Log the fallback attempt
        node.log_attempt(fb_tool, fb_params, result_str, validation,
                         is_fallback=True)

        if validation.success:
            log.info("Node %s fallback %d succeeded (conf=%.2f)",
                     node.id, fb_idx, validation.confidence)
            _take_screenshot(screenshot_fn)
            return True

        log.warning("Node %s fallback %d failed (type=%s)",
                    node.id, fb_idx, validation.failure_type.value)

    # All exhausted
    log.error("Node %s: all %d primary + %d fallback attempts failed",
              node.id, node.max_attempts, len(node.fallback_chain))
    return False


# ═══════════════════════════════════════════════════════════════
# REPLANNING
# ═══════════════════════════════════════════════════════════════

def _try_replan(graph: ExecutionGraph, failed_node: ExecutionNode,
                tool_runner, validator) -> bool:
    """
    Attempt to replan from a failed node.

    Concept: Instead of replanning the ENTIRE remaining plan,
    we only replan from the failed node forward. This is cheaper
    (fewer LLM tokens) and preserves already-completed work.

    Returns True if new nodes were injected into the graph.
    """
    try:
        from core.planner import replan

        # Build completed steps summary
        completed_steps = [
            {"step": n.id, "tool": n.tool, "description": n.description}
            for n in graph.nodes.values()
            if n.status == NodeStatus.COMPLETED
        ]

        failed_step = {
            "tool": failed_node.tool,
            "description": failed_node.description,
        }

        error = failed_node.result[:200]
        if failed_node.validation:
            error += f" [failure_type={failed_node.validation.failure_type.value}]"

        new_plan = replan(graph.goal, completed_steps, failed_step, error)

        if not new_plan or not new_plan.get("steps"):
            log.warning("Replan returned no steps")
            return False

        # Validate the new plan
        from core.planner import validate_plan
        is_valid, _ = validate_plan(new_plan)
        if not is_valid:
            log.warning("Replanned plan failed validation")
            return False

        # Inject new nodes into graph (replace failed + downstream pending)
        _inject_replan(graph, failed_node.id, new_plan)
        return True

    except Exception as e:
        log.error("Replan failed: %s", e)
        return False


def _inject_replan(graph: ExecutionGraph, failed_id: str, new_plan: dict):
    """
    Replace failed node and its pending downstream with new plan nodes.

    Steps:
    1. Remove the failed node from the graph
    2. Remove any PENDING/SKIPPED downstream nodes
    3. Add new nodes from the replanned steps
    4. Wire edges from last completed node to first new node
    """
    # Find last completed node (for edge wiring)
    last_completed = None
    for nid in sorted(graph.nodes.keys()):
        if graph.nodes[nid].status == NodeStatus.COMPLETED:
            last_completed = nid

    # Remove failed + downstream pending/skipped nodes
    to_remove = set()
    for nid, node in graph.nodes.items():
        if node.status in (NodeStatus.FAILED, NodeStatus.PENDING, NodeStatus.SKIPPED):
            to_remove.add(nid)

    for nid in to_remove:
        del graph.nodes[nid]

    # Remove edges involving removed nodes
    graph.edges = [
        e for e in graph.edges
        if e.from_node not in to_remove and e.to_node not in to_remove
    ]

    # Add new nodes from replan
    new_steps = new_plan.get("steps", [])
    prev_id = last_completed

    for i, step in enumerate(new_steps):
        node_id = f"replan_{uuid4().hex[:8]}"
        tool = step.get("tool", "")
        params = step.get("parameters", {})
        desc = step.get("description", "")

        node = ExecutionNode(
            id=node_id,
            tool=tool,
            params=params,
            description=desc,
            stabilization_ms=STABILIZATION_MS.get(tool, 500),
            fallback_chain=_build_fallbacks(tool, params),
            is_critical=tool in ("open_app", "close_app"),
        )

        graph.nodes[node_id] = node

        if prev_id:
            graph.edges.append(ExecutionEdge(
                from_node=prev_id,
                to_node=node_id,
                edge_type="depends_on",
            ))

        prev_id = node_id

    log.info("Replan injected %d new nodes", len(new_steps))


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _take_screenshot(screenshot_fn):
    """Take a screenshot if the function is available."""
    if screenshot_fn:
        try:
            time.sleep(0.1)  # reduced from 0.3s (Phase 2)
            screenshot_fn()
        except Exception:
            pass


def _build_summary(graph: ExecutionGraph, summary: dict, elapsed_ms: float) -> str:
    """
    Build a concise, informative summary string.

    Concept: The summary needs to tell both the user and the LLM
    what happened. It includes partial success info so the LLM
    can inform the user accurately.
    """
    parts = []

    if summary["failed"] == 0 and summary["skipped"] == 0:
        # Full success
        parts.append(f"Completed all {summary['total']} steps for: {graph.goal[:80]}")
    else:
        # Partial success
        parts.append(
            f"Partial completion: {summary['completed']}/{summary['total']} steps "
            f"for: {graph.goal[:80]}"
        )

    # List completed steps
    completed_descs = [
        n.description for n in graph.nodes.values()
        if n.status == NodeStatus.COMPLETED and n.description
    ]
    if completed_descs:
        parts.append("Done: " + " -> ".join(d[:40] for d in completed_descs))

    # List failed steps
    failed_descs = [
        f"{n.description} ({n.validation.failure_type.value if n.validation else '?'})"
        for n in graph.nodes.values()
        if n.status == NodeStatus.FAILED and n.description
    ]
    if failed_descs:
        parts.append("Failed: " + "; ".join(d[:50] for d in failed_descs))

    # List skipped steps
    skipped_count = summary["skipped"]
    if skipped_count:
        parts.append(f"Skipped: {skipped_count} steps (blocked by upstream failure)")

    # Timing
    parts.append(f"[{elapsed_ms:.0f}ms]")

    return ". ".join(parts)
