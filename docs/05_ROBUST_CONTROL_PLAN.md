# Robust Laptop Control Plan

This is the practical next plan for improving Jarvis navigation accuracy. The
main insight is that reliability should come from deterministic control,
observation, verification, and benchmarks. Fine-tuning can help later, but it
should not be the primary control layer.

## Current Assessment

Jarvis already has strong foundations:

- `core/world_state.py` tracks active window, focused control, browser context,
  clipboard, workflow, running apps, vision context, and system state.
- `core/execution_graph.py` already runs plans through snapshot, execute,
  stabilize, verify, retry, fallback, and replan.
- `core/validator.py` has typed verification and failure categories.
- `tools/ui_controller.py` already prefers UI Automation and keyboard before
  OCR or vision.

The highest-risk gaps are:

- No deterministic pre-LLM router for obvious commands.
- No app-profile workflow layer for common apps.
- The local model still over-calls tools for normal conversation.
- Benchmarks exist for model routing, but not for live desktop navigation.
- Failure logs are not yet structured into reusable training examples.

## Target Architecture

```text
voice/text input
  -> normalize + intent router
  -> deterministic app profile workflow, if available
  -> LLM tool routing only for ambiguous requests
  -> planner for multi-step tasks
  -> execution graph
  -> validator + world state
  -> failure logger + benchmark metrics
```

The rule is simple: use deterministic code for known actions and the LLM only
where judgment is actually needed.

## Phase 1: Intent Router

Build `core/intent_router.py`.

Responsibilities:

- Route obvious media commands directly:
  `pause`, `resume`, `next song`, `previous song`, `mute`, `volume 40`.
- Route obvious app launches:
  `open chrome`, `launch spotify`, `close notepad`.
- Route obvious memory:
  `remember that...`, `note that...`.
- Route obvious conversational non-actions away from tools:
  greetings, math, jokes, definitions, explanations.
- Route obvious compound actions to `agent_task`:
  `open chrome and search...`, `open spotify and play...`.

Acceptance gates:

- 50-router phrase test suite.
- `pause music` should bypass LLM.
- `what is 2 + 2` should never call a tool.
- Ambiguous inputs must return `None` and fall through to the LLM.

## Phase 2: App Profiles

Build `core/app_profiles.py`.

Start with deterministic profiles for:

- Chrome
- Spotify
- VS Code
- File Explorer
- WhatsApp
- TradingView

Each profile should define:

- executable/window aliases
- common shortcuts
- stable UIA targets
- high-level workflows such as browser search, Spotify search/play, VS Code
  quick open, and TradingView symbol search

Acceptance gates:

- Profile workflow generation does not call the LLM.
- Generated plans pass `validate_plan()`.
- Known app actions prefer `ui_control`, keyboard shortcuts, and media keys.

## Phase 3: Live Navigation Benchmark

Add `benchmarks/navigation_benchmark.py`.

Categories:

- app launch
- browser navigation
- media control
- file operations in a scratch folder
- VS Code navigation
- multi-step workflows

Each task needs:

- setup
- command
- expected tool or plan
- verifier
- cleanup
- latency measurement

Acceptance gates:

- No destructive actions outside a scratch directory.
- Per-task pass/fail JSONL output.
- Summary includes success rate and p50/p95 latency.

## Phase 4: Failure Logging

Build `core/failure_logger.py`.

Log every failed or recovered automation step as JSONL:

- user input
- selected route
- plan
- step
- tool parameters
- world state before
- world state after
- validation result
- recovery attempted
- recovery success

Acceptance gates:

- Logs are redacted for clipboard/secrets.
- Logs can be converted into training examples later.
- Recovered failures are logged too, because those are the best examples.

## Phase 5: Fine-Tune From Real Data

Only fine-tune after collecting real failures and successful recoveries.

Use synthetic examples for coverage, but prioritize:

- real routing failures
- real wrong-tool calls
- real app-control recoveries
- successful deterministic plans from app profiles

Acceptance gates:

- Compare stock model, `jarvis` Modelfile model, and fine-tuned model with the
  same benchmark suite.
- Do not replace the active model unless it beats the fallback on both tool
  accuracy and live navigation success.

## Recommended Order

1. Intent router.
2. App profiles for Chrome, Spotify, VS Code, and File Explorer.
3. Live navigation benchmark.
4. Failure logger.
5. Fine-tuning with real logs.

This order improves reliability before changing model weights, which is the
robust path for laptop control.
