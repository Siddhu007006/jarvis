"""
core/executor.py — Execution Orchestrator for Jarvis.

V5.1: Thin wrapper around ExecutionGraph.

This module exists for backward compatibility. The actual execution
logic lives in core/execution_graph.py which provides:
  - DAG-based execution (not linear step loops)
  - Fallback chains per node (different approaches to same goal)
  - Typed recovery per FailureType (focus_lost -> refocus, etc.)
  - Critical path cascading (failed app open -> skip downstream)
  - Partial success tracking (3/5 steps = 60%)
  - Stabilization windows (wait before verify)
  - Full execution logs (every attempt, every fallback)

Pipeline per node:
    snapshot_before() -> execute -> stabilize -> verify -> recover/retry/fallback

The executor also emits TASK_STARTED / TASK_COMPLETED events on the bus
for the state manager and UI to track progress.
"""

import logging

log = logging.getLogger(__name__)


def execute_plan(plan: dict, tool_runner, screenshot_fn=None) -> str:
    """
    Execute a plan's steps using the Execution Graph Engine.

    Concept: This function is the bridge between the old API
    (planner outputs a plan dict) and the new execution model
    (plan dict -> ExecutionGraph -> run_graph).

    The function signature is UNCHANGED from V5.0 for backward
    compatibility. All callers (engine.py) continue to work.

    Args:
        plan: Dict with 'goal' and 'steps' from planner.create_plan()
        tool_runner: Callable(tool_name, params) -> str that runs a tool
        screenshot_fn: Optional callable() -> bytes for visual context

    Returns:
        Summary string of what was accomplished.
    """
    from core.planner import validate_plan
    from core.execution_graph import ExecutionGraph, run_graph
    from core.validator import Validator

    # ── Step 0: Pre-validate the plan ─────────────────────────
    # Blueprint Principle 1: Every plan must be validated before
    # it reaches the executor. This catches malformed plans early.
    is_valid, warnings = validate_plan(plan)
    if not is_valid:
        error_msg = f"Plan rejected: {'; '.join(warnings)}"
        log.error("Plan rejected: %s", error_msg)
        return error_msg

    # ── Step 1: Convert plan to execution graph ───────────────
    # The plan is a flat list of steps. from_plan() enriches it
    # with fallback chains, stabilization windows, dependency
    # edges, and critical-path marking.
    graph = ExecutionGraph.from_plan(plan)

    if not graph.nodes:
        return "No steps in plan."

    # ── Step 2: Initialize validator ──────────────────────────
    validator = Validator()

    # ── Step 3: Run the graph ─────────────────────────────────
    # This is the main execution loop. It handles retries,
    # fallbacks, typed recovery, and partial success tracking.
    log.info("Executor: delegating %d nodes to graph runner for '%s'",
             len(graph.nodes), graph.goal[:60])

    result = run_graph(
        graph=graph,
        tool_runner=tool_runner,
        validator=validator,
        screenshot_fn=screenshot_fn,
        max_replans=2,
    )

    # ── Step 4: Log execution history ─────────────────────────
    # The full execution log is available for debugging.
    exec_log = graph.to_execution_log()
    for entry in exec_log:
        status_icon = {"completed": "done", "failed": "FAIL",
                       "skipped": "SKIP"}.get(entry["status"], "?")
        log.info("  [%s] %s: %s (%s, %d attempts)",
                 status_icon, entry["node_id"],
                 entry["description"][:40], entry["status"],
                 entry["attempts"])

    return result

