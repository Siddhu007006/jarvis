# Document 02 — TRD (Technical Requirements Document)

## Frontend (UI)
- **Framework**: `PyQt6` — native borderless, always-on-top floating Dynamic Island overlay.
- **Animations**: `QPropertyAnimation` — smooth pulsing, breathing, and sliding state transitions.
- **Design**: Apple Dynamic Island-inspired — draggable, translucent, hover-to-expand.

## Backend (Core Logic)
- **Language**: Python 3.10+ (asyncio for non-blocking audio, network, and UI operations).
- **Orchestration**: Custom Planner + Executor (`core/planner.py`, `core/executor.py`) — breaks user intent into JSON step plans.
- **Event System**: Thread-safe Pub/Sub Event Bus (`core/events.py`) — all modules communicate through events, not direct calls.

## UI Automation — 5-Tier Execution Hierarchy (V5.1)

All desktop UI interactions route through `tools/ui_controller.py` (orchestration layer):

| Tier | Method | Library | Speed | Use Case |
|------|--------|---------|-------|----------|
| 1 | **UI Automation** — accessibility tree + patterns | `uiautomation`, `pywinauto` | ~10ms | Buttons, textboxes, tabs, menus |
| 2 | **Keyboard Shortcuts** — 40+ built-in shortcuts | `pyautogui` | ~5ms | Save, undo, copy, switch tabs |
| 3 | **Win32 API** — direct window manipulation | `pywin32`, `pygetwindow` | ~5ms | Focus, restore, close windows |
| 4 | **OCR** — screen text matching | `pytesseract`, `mss` | ~1s | Custom-rendered UIs |
| 5 | **Vision Fallback** — screenshot + model | `moondream2`, Gemini | ~3s | Absolute last resort |

**Architecture:**
```
Planner / Engine → ui_controller.py (orchestration, retries, scoring)
                        ↓
                  win_automation.py (raw UIA/Win32 driver layer)
                        ↓
                  Windows UI Automation APIs
```

- `ui_controller.py` handles: element interaction, retry logic, confidence scoring, semantic element matching, and fallback strategy.
- `win_automation.py` handles: raw Win32/UIA operations. Stable backend, not a decision-making layer.
- New modules must NOT import `win_automation.py` directly — always route through `ui_controller.py`.

## Voice Pipeline
- **Wake Word**: `vosk` (grammar-optimized, ~50% CPU reduction vs full vocabulary).
- **STT**: `faster-whisper` (CTranslate2-optimized Whisper, local, GPU-accelerated).
- **LLM**: Groq API (Llama 3.3 70B) primary, Ollama (local) fallback. Streaming via httpx NDJSON/SSE.
- **TTS**: Kokoro-ONNX (local, 82M params, ~180ms) → `edge-tts` (cloud) → `pyttsx3` (SAPI5 offline).
- **Streaming**: Sentence-level overlap — LLM tokens buffer into sentences, TTS plays each while LLM continues.

## System Cognition & Awareness (V5.1)
- **World State Engine**: Continuously polls UI focus, window state, clipboard, and browser URLs. Emits granular domain events to prevent redundant processing.
- **Terminal Observer**: Uses Windows UIA TextPattern/ValuePattern to semantically extract console output. Matches regex patterns to instantly detect Exceptions, Build Failures, and Git Conflicts.
- **Accessibility Observer**: Uses `win32gui.EnumWindows` to detect blocking structural UI changes (modals, UAC prompts, file dialogs) before they disrupt automation.
- **Vision Fallback**: 
  - **Capture**: `mss` (lowered to 0.2fps).
  - **Gate**: 2-Tier Sufficiency Gate (Whitelist + Capability Scoring). Screenshots are ONLY sent to the vision model if deterministic World State is deemed insufficient.
  - **Vision Model**: `moondream2` (1.8B local) — identifies apps, errors, code, UI state.
- **Proactive Agent**: Tiered commentary (Critical → Useful → Opinion) driven by semantic events (e.g. `TERMINAL_ERROR_DETECTED`) or vision context, with rate limiting.

## Database
- No heavy database. Conversation history and user preferences stored in local JSON files (`memory/`).

## Auth
- No user authentication (local-only application).
- API keys for Gemini and Groq stored in `config/api_keys.json` and `config/settings.json`.

## Hosting / Environment
- Runs entirely as a local process on Windows 11.
- No remote server or cloud hosting (other than API dependencies for LLM/TTS fallback).

## Key Libraries

| Category | Libraries |
|----------|-----------|
| **UI Automation** | `uiautomation`, `pywinauto`, `pygetwindow`, `pywin32`, `comtypes` |
| **Audio** | `sounddevice`, `vosk`, `faster-whisper`, `kokoro-onnx`, `edge-tts`, `pyttsx3` |
| **Vision** | `torch`, `transformers` (moondream2), `mss`, `imagehash`, `pytesseract` |
| **LLM** | `httpx` (Groq/Ollama streaming), `google-generativeai` (Gemini fallback) |
| **UI** | `PyQt6` |
| **System** | `psutil`, `pyperclip`, `pyautogui`, `pycaw`, `subprocess` |
| **Search** | `duckduckgo_search` |

## Constraints
- Must interact with standard Windows desktop applications regardless of technology (Electron, WPF, native C++, UWP) using the accessibility tree as the primary method.
- Screenshots are a **last resort** (Tier 5), not the default interaction method.
- UI must remain performant and non-blocking while background automation occurs.
- The Dynamic Island must support transparent backgrounds and remain always-on-top.
- All new automation code must route through `tools/ui_controller.py`, not `tools/win_automation.py` directly.
