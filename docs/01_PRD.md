# Document 01 — PRD (Product Requirements Document)

## App Name
Jarvis

## Tagline
An autonomous, vision-based, human-like AI assistant for Windows that can see your screen and control your computer — using the OS accessibility tree.

## Problem
Standard AI assistants are locked inside browser tabs or chat windows. They cannot execute complex, multi-step actions across different desktop applications autonomously. Users need a hands-free, intelligent agent that can navigate the OS just like a human would.

Previous versions relied on a fragile **screenshot → Vision model → click coordinates** pipeline, causing hallucinated clicks, minimized-app failures, and 2–5 second latency per action. V5.1 solves this with deterministic UI Automation.

## Target User
Power users, developers, and desktop workers who want to automate workflows, control their system hands-free, and interact with a persistent, context-aware AI companion.

## Core Features (Must Have)

### Voice Pipeline
- Wake-word detection via `vosk` (grammar-optimized, low CPU).
- Local speech-to-text via `faster-whisper` (CTranslate2, GPU-accelerated).
- Streaming LLM response with sentence-level TTS overlap (~500ms first-word latency).
- 3-tier TTS fallback: Kokoro-ONNX (local, 180ms) → edge-tts (cloud) → pyttsx3 (SAPI5).

### UI Automation (V5.1 — 5-Tier Hierarchy)
- **Primary**: Windows Accessibility Tree via `uiautomation` + `pywinauto` — InvokePattern, ValuePattern, SelectionItemPattern for buttons, textboxes, tabs, and menus (~10ms, 98% accuracy).
- **Secondary**: Keyboard shortcuts database (40+ built-in combos like Ctrl+S, Alt+Tab).
- **Tertiary**: Win32 API direct window manipulation via `win32gui` / `pygetwindow`.
- **Fallback**: OCR text matching via `pytesseract` for custom-rendered UIs.
- **Last Resort**: Vision model screenshot analysis (moondream2/Gemini) — only used when all deterministic methods fail.
- All automation routes through `tools/ui_controller.py` (orchestration layer) → `tools/win_automation.py` (backend driver).
- Auto-Focus: Brings target applications to the foreground before interacting, including restoring minimized windows.

### Screen & System Awareness (Semantic-First Cognition)
- **World State Engine**: Maintains a real-time semantic model of the system (active window, focused control, URL, clipboard).
- **Terminal Observer**: Semantically reads terminal text via UIA to instantly detect build failures, exceptions, and git conflicts.
- **Accessibility Observer**: Detects blocking dialogs, modals, and permission prompts that interrupt automation.
- **Vision Fallback**: Screen watching runs at a low 0.2fps, strictly gated by a 2-tier sufficiency check (whitelist + capability scoring). The local vision model (`moondream2`, 1.8B) only runs when the deterministic WorldState is insufficient.
- **Proactive Agent**: Speaks without being prompted when it detects errors, completed builds, or idle debugging.

### System Control
- Floating Dynamic Island UI (PyQt6, always-on-top, borderless, draggable, state-reactive animations).
- Global system controls (volume, media, sleep, lock, screenshot).
- Task Planner + Executor: breaks complex requests into step-by-step JSON execution plans.
- Persistent conversation memory across sessions.

## Nice to Have
- Local Offline Mode (Full fallback to local LLMs and TTS/STT like Ollama, Vosk, and Kokoro if cloud APIs are unavailable).
- Advanced multi-agent orchestration for parallel tasks.
- LLM router (phi3:mini for simple commands, llama3 for complex ones).

## Out of Scope
- Cross-platform support (Mac/Linux). This version is strictly optimized for Windows 11.
- Mobile application counterpart.

## User Stories
- As a user, I want to say "play the songs library in Spotify", so that Jarvis autonomously opens Spotify, finds the library, and starts playing music without me touching the mouse.
- As a user, I want Jarvis to click buttons and type into fields **without taking screenshots** — using the accessibility tree directly for speed and accuracy.
- As a user, I want Jarvis to work even when target applications are minimized, behind other windows, or on different virtual desktops.
- As a user, I want Jarvis to remember my preferences so I don't have to repeat myself in future sessions.
- As a user, I want Jarvis to proactively notice errors on my screen and alert me.

## Success Metrics
- 95%+ success rate for autonomous UI interaction via accessibility tree (Tier 1 hit rate).
- Sub-500ms first-word latency for voice responses via streaming pipeline.
- <50ms per UI action (click, type, tab switch) when using Tier 1/2/3.
- Zero crashes during soft-failure recovery (e.g., retrying when a button is not found, cascading through 5 tiers).
