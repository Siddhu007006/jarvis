# Jarvis v5.0 - Technology Stack & Architecture

This document outlines the tools, libraries, and architecture used to build Jarvis v5.0. This version introduces **streaming LLM→TTS**, **local Kokoro-TTS**, **screen vision (moondream2)**, **proactive AI commentary**, and **Windows UI Automation** — all wired through the existing event bus.

## Core Language & Environment
*   **Python 3.10+**: The primary programming language.
*   **Asyncio**: Used extensively for non-blocking operations (audio, network, UI).

## Voice Pipeline (STT, LLM, TTS)

### 1. Wake Word Detection (Listening for "Jarvis")
*   **Engine**: `vosk` with `SetGrammar` optimization — restricts recognition to the wake word vocabulary only, cutting CPU usage ~50% and eliminating false triggers from background speech.
*   **Dedup**: Partial result tracking with `rec.Reset()` prevents the same wake word from firing multiple times.
*   **Fallback**: Energy-based detection via `sounddevice` + `numpy` (if Vosk fails to load).
*   **Audio**: `sounddevice` captures mic input at 16kHz in 4000-sample blocks.

### 2. Speech-to-Text (STT)
*   **Engine**: `faster-whisper` (CTranslate2-optimized Whisper). Runs 100% locally with near-instant transcription.

### 3. "The Brain" (Large Language Model)
*   **Primary**: `Groq` API (Llama 3.3 70B at 800 tokens/sec on LPUs).
*   **Fallback**: `Ollama` (local models: llama3, qwen2.5-coder:7b).
*   **Streaming**: `llm_stream()` yields tokens one at a time using httpx streaming (NDJSON for Ollama, SSE for Groq). This feeds the streaming TTS pipeline.
*   **Client**: `httpx` for all LLM HTTP communication.

### 4. Text-to-Speech (TTS) — Three-Level Fallback Chain
*   **Primary**: `Kokoro-TTS` — local 82M parameter neural model (~180ms latency, no internet needed). Requires `espeak-ng` system dependency for phonemization.
*   **Fallback #1**: `edge-tts` — free Microsoft Azure neural voices (~800ms, needs internet).
*   **Fallback #2**: `pyttsx3` — Windows SAPI5 voices (offline, lower quality).
*   **Audio**: `soundfile` for WAV I/O, `pydub` for MP3 conversion, `sounddevice` for playback.

### 5. Streaming Pipeline (V5 — the biggest speed win)
*   **Concept**: Instead of waiting for the complete LLM response before speaking, tokens are streamed and buffered into sentences. Each sentence is sent to TTS as it completes, while the LLM continues generating.
*   **Result**: First words heard at ~500ms instead of ~2.8s (perceived 3× speedup).
*   **Heuristic**: Conversational queries use streaming. Action queries (open, close, search) use non-streaming so tool call JSON can be parsed from the full response.

## Screen Vision & Proactive AI (V5)

### 6. Screen Watcher
*   **Capture**: `mss` — fastest Windows screenshot library (<1% CPU at 1fps).
*   **Change Detection**: `imagehash` — perceptual hashing (0.1ms per comparison). Only runs vision model when screen changes >25%.
*   **Event**: Emits `SCREEN_CHANGED` on the event bus when significant change detected.

### 7. Vision Engine
*   **Model**: `moondream2` (1.8B parameter local vision model via `transformers` + `torch`).
*   **Capability**: Understands screenshots — identifies apps, error messages, code, documents, UI state.
*   **Event**: Subscribes to `SCREEN_CHANGED`, emits `SCREEN_CONTEXT_UPDATED` with description.

### 8. Proactive Commentary Agent
*   **Purpose**: JARVIS speaks up without being asked — notices errors, completed builds, stalled debugging.
*   **Trigger Tiers**: Critical (speak immediately), Useful (speak if idle >30s), Opinion (speak if idle >2min).
*   **Rate Limiting**: Max 1 proactive comment per 45 seconds (critical alerts bypass this).
*   **Event**: Subscribes to `SCREEN_CONTEXT_UPDATED`, emits `PROACTIVE_TRIGGER`.

## User Interface (UI)
*   **Framework**: `PyQt6`
*   **Design**: Apple Dynamic Island — borderless, always-on-top, hover-to-expand, draggable, translucent.
*   **Animations**: `QPropertyAnimation` for smooth state transitions (pulsing green=listening, red=error, etc.).

## System Control & Automation

### UI Automation — 5-Tier Execution Hierarchy (V5.1)

All desktop UI interactions go through `tools/ui_controller.py`, which tries 5 methods in order:

| Tier | Method | Speed | Library | Reliability |
|------|--------|-------|---------|-------------|
| 1 | **UI Automation** — accessibility tree + patterns (InvokePattern, ValuePattern, SelectionItemPattern) | ~10ms | `uiautomation`, `pywinauto` | 98% for standard apps |
| 2 | **Keyboard Shortcuts** — built-in shortcut database (40+ shortcuts) | ~5ms | `pyautogui` | 100% for known shortcuts |
| 3 | **Win32 API** — direct window manipulation (hwnd, EnumWindows, ShowWindow) | ~5ms | `pywin32`, `ctypes`, `pygetwindow` | 99% for window ops |
| 4 | **OCR** — screen text matching via Tesseract | ~1s | `pytesseract`, `mss` | 85% (depends on text clarity) |
| 5 | **Vision Fallback** — screenshot + vision model (absolute last resort) | ~3s | `moondream2` or Gemini | 70% (hallucinates coordinates) |

**Architecture:**
```
Planner / Engine → ui_controller.py (orchestration, retries, scoring)
                        ↓
                  win_automation.py (raw UIA/Win32 driver layer)
                        ↓
                  Windows UI Automation APIs
```

*   **Primary Layer**: `tools/ui_controller.py` — handles all element interaction (buttons, textboxes, tabs, menus, windows), retry logic, confidence scoring, semantic element matching, and fallback strategy.
*   **Backend Layer**: `tools/win_automation.py` — raw Win32/UIA operations. Stable, low-level. Not a decision-making layer.
*   **Volume Control**: `pycaw` + `comtypes` — exact volume level via Windows Core Audio API.

### Other Tools
*   **App Launching**: Windows Shell + `subprocess`.
*   **System Metrics**: `psutil` (CPU, RAM, battery).
*   **Clipboard**: `win32clipboard` (via `pywin32`) — with mandatory sensitive data filtering.
*   **Web Search**: `duckduckgo_search` (no API key needed).

## World State Engine (V5.1)

`core/world_state.py` — **The single source of truth** for all system context. Replaces stateless guessing with realtime ground-truth awareness.

### Domain Structure

| Domain | Fields | Update Speed |
|--------|--------|-------------|
| `windows` | `active_title`, `active_exe`, `open_windows` | 250ms (fast) |
| `control` | `name` (+ confidence), `type`, `bounds`, `enabled` | 250ms (fast) |
| `browser` | `url` (+ confidence), `tab_title`, `browser` | 2s (medium) |
| `clipboard` | `text`, `hash`, `redacted` | 7s (slow) |
| `workflow` | `scores` (8 workflows, probability-scored), `primary` | 2s (medium) |
| `automation` | `running`, `current_task` | Event-driven |
| `vision` | `app`, `page_type`, `intent`, `entities`, `raw_age_s` | Event-driven |
| `system` | `running_apps`, `volume`, `cpu_percent`, `ram_percent`, `music_app` | 2–7s |

### Key Features
*   **Versioned** — monotonic `version` counter + `timestamp` on every update cycle.
*   **Confidence-scored** — UIA-derived values carry confidence (0.0–1.0). E.g. browser URL via UIA = 1.0, via title parsing = 0.5.
*   **Secret-filtered** — clipboard NEVER stores API keys, JWT tokens, SSH keys, passwords. Patterns matched via 17 pre-compiled regexes.
*   **Workflow scoring** — probabilities (not binary labels). Uses weighted signal accumulation from active exe, window title keywords, and running apps.
*   **State diffing** — each update loop snapshots before/after and only emits events for changed domains.
*   **Semantic vision** — stores `page_type`, `intent`, `entities` instead of raw moondream2 description blobs.
*   **Current truth only** — NO history, NO logs. Historical knowledge stays in `core/memory.py`.

### Three-Speed Update Loops
*   **Fast (250ms)**: Active window, focused control, vision age — things that change on every Alt+Tab.
*   **Medium (2s)**: Browser URL, workflow scores, running apps — moderate change rate.
*   **Slow (7s)**: Clipboard, CPU/RAM, music detection — infrequent changes.

### Granular Domain Events
Instead of one noisy `WORLD_STATE_UPDATED`, each domain emits its own event:
`WINDOW_CHANGED`, `ACTIVE_CONTROL_CHANGED`, `BROWSER_URL_CHANGED`, `CLIPBOARD_CHANGED`, `WORKFLOW_CHANGED`, `RUNNING_APPS_CHANGED`.

## Validation Engine (V5.1)

`core/validator.py` — Transforms Jarvis from **"tries automation"** into **"verifies automation"**. Purely deterministic — no AI reasoning, no retries, no orchestration.

### Core Flow
```
Observe (snapshot WorldState before)
   ↓
Execute (run the tool)
   ↓
Verify (compare before/after, check expectations)
   ↓
Report (ValidationResult with confidence + failure_type)
   ↓
Caller decides retry/fallback
```

### Verification Hierarchy (fastest → slowest)

| Tier | Method | Latency | Confidence | Auto-escalate? |
|------|--------|---------|------------|----------------|
| 1 | WorldState | ~0ms | 0.95 | Always |
| 2 | UIA | ~50ms | 0.85 | For type/click |
| 3 | Process | ~100ms | 0.80 | For open/close |
| 4 | Result Parse | ~0ms | 0.70 | Always |
| 5 | OCR | ~1s | 0.65 | **NEVER** |
| 6 | Vision | ~3s | 0.45 | **NEVER** |

### Failure Types
`not_found`, `state_unchanged`, `permission_denied`, `focus_lost`, `timeout`, `ambiguous_match`, `wrong_state`, `verification_failed` — each maps to a different recovery strategy.

### Key Features
*   **Scoped snapshots** — only snapshot relevant WorldState domains per tool type (e.g. open_app -> windows + system only).
*   **Field-level diffing** — reports exact changed fields (`windows.active_title`, `browser.url.value`), not just changed domains.
*   **Calibrated confidence** — fixed ranges per method, not learned. WorldState=0.95, UIA=0.85, process=0.80, OCR=0.65, vision=0.45.
*   **Backward compatible** — `verify_action()` method wraps new API for legacy `executor.py` compatibility.

## Execution Graph Engine (V5.1)

`core/execution_graph.py` — Replaces linear step-loop execution with a **DAG-based execution model**.

### Core Flow (per node)
```
Snapshot WorldState (before)
   |
Execute primary tool
   |
Stabilize (tool-specific wait: open_app=2s, type_text=300ms)
   |
Verify (Validator.verify() -> ValidationResult)
   |
If failure: typed recovery -> retry primary -> try fallback chain
   |
Mark COMPLETED or FAILED (with critical path cascade)
```

### Node Lifecycle
`PENDING -> RUNNING -> COMPLETED / FAILED / SKIPPED`

### Key Features

| Feature | Description |
|---------|-------------|
| **Fallback chains** | Each node has up to 3 alternative approaches (e.g. type_text -> computer_control type -> clipboard paste) |
| **Typed recovery** | FailureType-specific pre-retry repairs: `focus_lost` -> refocus window, `not_found` -> extend wait, `timeout` -> double stabilization |
| **Critical path cascade** | If `open_app` (critical) fails, all downstream nodes are SKIPPED instead of wastefully attempted |
| **Partial success** | Reports `{completed: 3, failed: 1, skipped: 1, success_ratio: 0.60}` |
| **Stabilization windows** | Tool-specific post-execution wait before verification (open_app=2s, type_text=300ms) |
| **Execution logs** | Every attempt, fallback, and recovery action is logged with timestamp + confidence |
| **Subgraph replanning** | On critical failure, replans only the failed node + downstream (not the whole plan) |

### Backward Compatibility
`executor.py` is now a thin wrapper: `execute_plan(plan, tool_runner) -> ExecutionGraph.from_plan() -> run_graph()`. Same function signature, no caller changes needed.

## Architecture Pattern
*   **Event-Driven (Pub/Sub)**: Global `EventBus` (`core/events.py`) decouples all components. V5.1 adds 6 world state domain events.
*   **World State**: `core/world_state.py` — single source of truth, consumed by all modules, written only by update loops and engine task tracking.
*   **Validation**: `core/validator.py` — deterministic state transition verification at both execution paths (engine + executor).
*   **Execution Graph**: `core/execution_graph.py` — DAG-based multi-step execution with fallbacks, typed recovery, and partial success.
*   **Single Instance Guard**: Windows Named Mutex ensures only one Jarvis runs at a time.
*   **Module Organization**: `core/` (engine, world_state, validator, execution_graph, providers, TTS, events), `screen/` (watcher, vision, proactive), `tools/` (ui_controller -> win_automation), `actions/` (app launcher, file ops), `ui/` (dynamic island).
