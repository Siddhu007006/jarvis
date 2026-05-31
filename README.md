# Jarvis MK37 (v5.0)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Creator](https://img.shields.io/badge/Creator-Siddharth_Reddy-orange.svg)](https://github.com/Siddhu007006)

> **Created by [Siddharth Reddy](https://github.com/Siddhu007006)** — Original author and maintainer.

Jarvis is an autonomous, vision-based, human-like AI assistant designed specifically for Windows 11. Unlike standard web-based AI chatbots, Jarvis can "see" your desktop screen, interpret what is happening, plan multi-step workflows, and control your mouse, keyboard, volume, and applications autonomously just like a human would.

Jarvis features an elegant, Apple-inspired **Dynamic Island UI** that floats on top of your screen, responding dynamically with glowing animations depending on whether Jarvis is listening, thinking, speaking, or executing tasks.

---

## 📖 Onboarding Guides

If you are new to this repository or are setting it up for the first time, please read the following sub-documents:

*   **[Setup & Installation Guide (SETUP.md)](file:///c:/Users/Siddharth%20Reddy/projects/jarvis/SETUP.md):** Step-by-step setup for prerequisites (like `espeak-ng`), python virtual environments, local model downloading (`moondream2`, `kokoro-onnx`), and API key configuration.
*   **[Technical Architecture Guide (ARCHITECTURE.md)](file:///c:/Users/Siddharth%20Reddy/projects/jarvis/ARCHITECTURE.md):** Deep-dive into the event-driven system architecture, the Event Bus, streaming sentence pipelines, local vision loop execution, and Mermaid diagrams.
*   **[Local Model Tuning Guide (docs/04_FINE_TUNING.md)](file:///c:/Users/Siddharth%20Reddy/projects/jarvis/docs/04_FINE_TUNING.md):** Ollama Modelfile setup, seed training data, and benchmark workflow for the Jarvis local model.

---

## ✨ Key Features

1.  **Continuous Voice Pipeline:** Uses local speech detection (`vosk`) to listen for wake words. Jarvis automatically transcribes your requests using local `faster-whisper` STT.
2.  **Perceptual Screen Watching:** Captures screen updates at 1fps via `mss`. A perceptual hash engine (`imagehash`) determines if the screen changed >25% to trigger a local vision model description.
3.  **Local Neural Vision (`moondream2`):** Processes screenshots on-device. Jarvis identifies applications, file contents, code issues, compile errors, or idle states.
4.  **Proactive AI Commentary:** Spontaneously comments on screen changes (e.g. notifying you of a finished compile job or offering a coding suggestion) when the system is idle.
5.  **5-Tier UI Automation (V5.1):** All desktop interactions route through `tools/ui_controller.py` using a strict hierarchy: UI Automation (10ms) → Keyboard shortcuts (5ms) → Win32 API (5ms) → OCR (1s) → Vision fallback (3s). Screenshots are a last resort, not the default. Buttons, textboxes, tabs, menus, and windows are controlled via the OS accessibility tree using `uiautomation`, `pywinauto`, and `win32gui`.
6.  **Sub-Second Speech Streaming:** A sentence-level streaming engine overlaps LLM response token generation with `kokoro-onnx` local neural speech playback. You hear Jarvis start speaking in **~500ms** instead of waiting for the full response (~2.8s).
7.  **Robust Fallback Chain:** If cloud services (Groq Llama 3.3, Gemini) fail, Jarvis automatically falls back to local models (Ollama Qwen2.5-Coder / Phi-3 Mini) and offline TTS (Kokoro/SAPI5).

---

## 🗂️ Project Directory Layout

Here is a quick overview of the repository's files and folders:

```text
jarvis/
├── main.py                   # Main entry point. Initializes the PyQt6 UI and starts all core engines.
├── install.py                # Setup utility. Installs pip dependencies and downloads AI model files.
├── verify_local.py           # Verification script to test speech and vision pipelines locally.
├── requirements.txt          # Python packages list (PyQt6, sounddevice, torch, transformers, etc.).
│
├── core/                     # Central brains and logic pipeline.
│   ├── events.py             # Global thread-safe Pub/Sub Event Bus.
│   ├── engine.py             # Event coordinator, State Machine manager, and query router.
│   ├── stream_tts.py         # Buffers streaming LLM tokens and feeds sentences to the TTS player.
│   ├── tts.py                # TTS manager (Kokoro-ONNX -> edge-tts -> pyttsx3 fallback).
│   ├── stt.py                # Offline speech transcriber using faster-whisper.
│   ├── wake_word.py          # Vosk voice activity mic handler.
│   ├── planner.py            # Converts human intentions into JSON execution plans.
│   ├── executor.py           # Executes plan nodes by running local commands or desktop automation.
│   ├── providers.py          # API interfaces for Ollama, Groq, and Gemini.
│   └── memory.py             # Conversation memory manager (stores context in memory).
│
├── screen/                   # Screen vision and proactive thinking.
│   ├── watcher.py            # MSS screenshot taker and perceptual hash comparer.
│   ├── vision.py             # moondream2 vision model inference worker.
│   └── proactive.py          # Idle state evaluator and spontaneous comment trigger.
│
├── tools/                    # Hardware and OS automation hooks.
│   ├── ui_controller.py     # PRIMARY: 5-tier UI orchestration (UIA → KB → Win32 → OCR → Vision).
│   └── win_automation.py     # Backend: raw Windows UI Automation and Win32 API driver.
│
├── actions/                  # Executable commands mapped to LLM tools.
│   ├── app_launcher.py       # Starts applications by name or system paths.
│   ├── computer_control.py   # Mouse/keyboard clicking and coordinates.
│   ├── file_manager.py       # Reads, deletes, moves, or creates files and folders.
│   ├── system_control.py     # Edits volume levels, takes manual screenshots, and controls power states.
│   └── web_search.py         # Free DuckDuckGo queries for online information retrieval.
│
├── ui/                       # Presentation layer.
│   ├── dynamic_island.py     # Custom PyQt6 always-on-top frameless floating island overlay widget.
│   └── animations.py         # Dynamic pulsing, breathing, and sliding animations.
│
├── config/                   # Persistent user configurations.
│   ├── api_keys.json         # Gemini API keys.
│   └── settings.json         # Active model configurations, provider parameters, and settings.
│
├── memory/                   # Storage folder for conversation history.
└── resources/                # Stores downloaded neural voices and local ONNX model weights.
```

---

## 🎙️ Basic Usage

1.  **Launch Jarvis:**
    ```powershell
    python main.py
    ```
2.  **Wake Up:** Say **"Jarvis"** or press **`Win + J`** to toggle mute/listening.
3.  **Command Examples:**
    *   *System tasks:* "Jarvis, open Notepad and write a test file."
    *   *App tasks:* "Open Spotify and mute the volume."
    *   *Vision tasks:* "Look at my screen and tell me what error this code is showing."
    *   *File operations:* "Delete temporary files in the file explorer."
    *   *Conversational queries:* "Jarvis, explain the difference between processes and threads."
4.  **Proactive Mode:** Keep Jarvis running while you code or browse. If you make a mistake in your terminal or leave your screen idle, Jarvis will notice and speak up to guide you!

---

## 📄 License & Attribution

Copyright 2025 **Siddharth Reddy** ([Siddhu007006](https://github.com/Siddhu007006))

Licensed under the [Apache License 2.0](LICENSE). You are free to use, modify, and distribute this software, provided you:

1. **Give credit** — retain the original copyright and NOTICE file
2. **State changes** — clearly mark any modifications you make
3. **Don't misrepresent** — do not claim you are the original creator of Jarvis

See [NOTICE](NOTICE) for full attribution requirements.
