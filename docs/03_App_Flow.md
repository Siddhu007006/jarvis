# Document 03 — App Flow (Navigation & User Journey Map)

## Pages / Screens List
- **Main Overlay**: A floating Apple-inspired **Dynamic Island** (PyQt6, borderless, always-on-top). Displays state-reactive glowing ring animations. This is the primary and only persistent interface.
- **Expanded State**: Hover or click to expand the Dynamic Island, revealing status info and quick controls.
- **Setup**: No explicit setup dialog — API keys are configured via `config/api_keys.json` and `config/settings.json` before first run.

## Navigation Type
- **Floating Always-On-Top Widget**: No standard windows or tabs. The Dynamic Island exists above all other desktop applications.
- **Mouse Interaction**: Hover to expand, click & drag to reposition.
- **Voice Interaction**: Wake-word activated ("Jarvis", "Hey Jarvis", "Okay Jarvis") via local `vosk` engine, or toggle with `Win+J` global hotkey.
- **Keyboard Shortcut**: `Win+J` toggles mute/listening.

## First Screen
- **Brand New User**: Dynamic Island spawns and enters idle state. User must configure API keys in `config/` before voice features activate.
- **Returning User**: Dynamic Island spawns immediately. Vosk starts listening for wake word. Screen watcher begins capturing at 1fps.

---

## Core User Journey 1: Voice Command → Autonomous Execution

1. User speaks wake word: **"Jarvis"**.
2. `vosk` detects the wake word → emits `WAKE_WORD_DETECTED` on the Event Bus.
3. Dynamic Island transitions to **Listening** state (green audio-reactive pulse).
4. User speaks command: *"Open Notepad and type hello world"*.
5. `faster-whisper` transcribes locally → emits `SPEECH_RECOGNIZED`.
6. `engine.py` receives the transcription and routes it:
   - **Conversational query** → Streaming LLM pipeline (sentence-by-sentence TTS).
   - **Action query** → Non-streaming LLM for tool-call JSON parsing.
7. Dynamic Island transitions to **Thinking** state (purple spinning arcs).
8. The Planner generates a JSON execution plan:
   ```json
   [
     {"tool": "open_app", "params": {"app_name": "Notepad"}},
     {"tool": "ui_control", "params": {"action": "type_into", "target": "Editor", "text": "hello world", "window": "Notepad"}}
   ]
   ```
9. The Engine executes each tool:
   - `open_app("Notepad")` → launches via `actions/app_launcher.py`
   - `ui_control(action="type_into", ...)` → routes to `UIController.type_into_field()`
10. **UIController 5-Tier Cascade**:
    - **Tier 1**: `uiautomation` finds the Edit control → `ValuePattern.SetValue("hello world")` ✅ (~10ms)
    - *(If Tier 1 fails)*: **Tier 2**: pywinauto `child_window()` → `set_edit_text()`
    - *(If Tier 2 fails)*: **Tier 3**: `pyautogui` clipboard paste into focused field
    - *(If Tier 3 fails)*: **Tier 4**: OCR text matching
    - *(If Tier 4 fails)*: **Tier 5**: Vision model screenshot analysis (last resort)
11. Dynamic Island transitions to **Speaking** state (blue pulsing ring).
12. Jarvis confirms: *"Done. I've typed 'hello world' into Notepad."*
13. Dynamic Island returns to **Idle** state (soft white breathing animation).

---

## Core User Journey 2: UI Button Click (5-Tier in Action)

1. User says: *"Click the Submit button in Chrome"*.
2. Engine calls: `UIController.click_button("Submit", window="Chrome")`
3. **Step 1**: Focus Chrome window via `pygetwindow` (restore if minimized).
4. **Step 2**: UIController begins 5-tier cascade:

| Attempt | Tier | What Happens | Result |
|---------|------|-------------|--------|
| 1 | Tier 1 | Walks Chrome's accessibility tree, finds ButtonControl named "Submit" | `InvokePattern.Invoke()` → ✅ **Done in 10ms** |
| — | — | *If not found:* retries once (300ms delay) | — |
| 2 | Tier 2 | pywinauto `child_window(title_re="Submit")` → `click_input()` | ✅ or ❌ |
| 3 | Tier 3 | Checks SHORTCUTS dict — no match for "Submit" | ❌ Skip |
| 4 | Tier 4 | OCR screenshot via pytesseract, finds "Submit" text at (450, 320) | `pyautogui.click(450, 320)` |
| 5 | Tier 5 | Screenshot → moondream2 vision model → coordinate estimation | Last resort ⚠️ |

---

## Core User Journey 3: Proactive AI Commentary

1. Jarvis is running in background. User is coding in VS Code terminal.
2. `TerminalObserver` continuously scans the terminal TextPattern via UIA (no screenshots).
3. It detects a Python traceback: *"ImportError: No module named 'requests'"*
4. `TERMINAL_ERROR_DETECTED` event is emitted with semantic error summary.
5. `ProactiveAgent` evaluates → **Critical tier** (error detected).
6. `PROACTIVE_TRIGGER` event → TTS speaks: *"I notice you have an import error. You might need to pip install requests."*
7. Rate limiting: max 1 proactive comment per 45 seconds (critical alerts bypass this).

---

## Core User Journey 4: Menu Navigation

1. User says: *"Go to File, then Save As in Notepad"*.
2. Engine calls: `UIController.select_menu_item("File > Save As", window="Notepad")`
3. **Tier 1**: pywinauto `menu_select("File->Save As")` → ✅ **Done**
4. *(If Tier 1 fails)*: **Tier 2**: UIA sequential click — find "File" MenuItemControl → click → find "Save As" → click.
5. *(If Tier 2 fails)*: **Tier 3**: Keyboard `Alt` → `F` → `A` (Alt key menu navigation).

---

## Status States (Dynamic Island Ring Colors)

| State | Color | Animation | Trigger |
|-------|-------|-----------|---------|
| ⚪ **IDLE** | White | Soft breathing | No active task |
| 🟢 **LISTENING** | Green | Audio-reactive pulse | Wake word detected |
| 🟣 **THINKING** | Purple | Spinning arcs | LLM processing |
| 🔵 **SPEAKING** | Blue | Pulsing ring | TTS playback active |
| 🔴 **ERROR** | Red | Flashing ring | Task failure or connection loss |

## Edge Cases & Error Handling

- **Element Not Found**: UIController retries 2× (300ms delay), then cascades through all 5 tiers. If all fail, responds verbally: *"I couldn't find that element."*
- **Blocking Dialogs / Modals**: `AccessibilityObserver` detects open dialogs (e.g., UAC, Save As, "Not Responding") and emits `DIALOG_DETECTED`. The Execution Engine knows to resolve these blocking structural changes before interacting with the underlying app.
- **Window Not Found**: Tries pygetwindow → win32gui EnumWindows → backend WinAutomation.raise_window(). Reports if no match.
- **Minimized Window**: `pygetwindow.restore()` or `win32gui.ShowWindow(SW_RESTORE)` before interaction — works even if the window is behind others.
- **Lost API Connection**: Fallback chain: Groq → Gemini → Ollama (local). TTS: Kokoro → edge-tts → pyttsx3. If all fail, Dynamic Island enters ERROR state.
- **Streaming Artifact**: Safety filter in `stream_tts.py` strips `<tool_call>` JSON from speech output — prevents raw JSON from being spoken aloud.
