# Jarvis MK37 (v5.0) — Setup & Installation Guide

This guide will walk you through the system requirements, installation steps, local model setup, API configurations, and launching instructions for Jarvis.

---

## 🖥️ System Requirements & Prerequisites

Jarvis v5.0 runs locally on your Windows machine and uses both cloud APIs (Gemini/Groq) and local neural models (faster-whisper, moondream2, Kokoro-TTS).

### 1. Windows 11
Jarvis is custom-built for Windows 11 UI Automation, process monitoring, and desktop window interaction. The V5.1 update adds a **5-tier UI automation hierarchy** (`tools/ui_controller.py`) that uses the Windows Accessibility Tree (`uiautomation`, `pywinauto`) as the primary interaction method, with keyboard shortcuts, Win32 API, OCR, and vision as fallbacks. All automation dependencies (`pywinauto`, `uiautomation`, `pygetwindow`, `pywin32`) are installed automatically via `requirements.txt`.

### 2. Python 3.10+
Ensure Python is installed and added to your system environment variables. Python 3.10 through 3.14 are supported.

### 3. espeak-ng (System Phonemizer for Kokoro-TTS)
Kokoro-TTS requires a phoneme generator (`espeak-ng`) to convert raw text into phonemes (sound units) before generating neural speech.
- **Why it matters:** Without it, local speech synthesis will fail and fall back to the network-based `edge-tts`.
- **How to Install:**
  1. Download the latest `.msi` installer for Windows from the official [espeak-ng GitHub Releases](https://github.com/espeak-ng/espeak-ng/releases) (e.g., `espeak-ng-X.XX-x64.msi`).
  2. Run the installer and finish the wizard.
  3. **Crucial Step:** Add the `espeak-ng` bin path to your system's `PATH` environment variable:
     - Default installation path is usually `C:\Program Files\eSpeak NG` or `C:\Program Files (x86)\eSpeak NG`.
     - Make sure the directory containing `espeak-ng.exe` is in your environment variables.
     - You can verify it by opening a new PowerShell window and typing `espeak-ng --version`.

### 4. GPU Support via CUDA (Optional but Highly Recommended)
Running local STT (`faster-whisper`) and local screen vision (`moondream2`) on your CPU can be slow (adding several seconds of latency). If you have an NVIDIA GPU (e.g., RTX 3050 or higher):
- Install the [NVIDIA CUDA Toolkit](https://developer.nvidia.com/cuda-downloads) (version 11.8 or 12.x matches current PyTorch support).
- Install PyTorch with CUDA support in your virtual environment:
  ```powershell
  pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
  ```
  *(Replace `cu118` with your corresponding CUDA version, e.g. `cu121`)*
- **Impact:** Drop STT latency from ~400ms (CPU) to ~80ms (GPU), and vision processing from 4s to <1s.

---

## 📦 Installation Steps

### Step 1: Clone or Open the Directory
Open your terminal (PowerShell) in the `jarvis` project folder.

### Step 2: Create and Activate Virtual Environment
We recommend using a local virtual environment to isolate python dependencies:
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### Step 3: Run the Dependency Installer
Use `install.py` to automatically install all dependencies listed in `requirements.txt`:
```powershell
python install.py
```

---

## 🔽 Downloading Local AI Models

Large AI models are downloaded locally to preserve privacy and allow offline functionality. 

Run the following command to pre-download the models:
```powershell
python install.py --download-models
```
This command performs two downloads:
1. **moondream2 (~1.8 GB)**: Local vision model downloaded from Hugging Face via the `transformers` library. Used to parse screen screenshots.
2. **Kokoro-TTS & Voices (~500 MB)**: Swaps the standard `kokoro` library with an ONNX runtime version to prevent Cython compilation issues on newer Python installations. Downloads `kokoro-v1.0.onnx` and `voices-v1.0.bin` directly into the `resources/` folder.

To verify your environment is ready, run:
```powershell
python install.py --check
```
You should see:
```text
✅ All dependencies are satisfied!
✅ Model files found!
```

---

## 🔑 API Keys & Settings Configuration

Jarvis stores keys and settings in two JSON files located in the `config/` directory.

### 1. `config/api_keys.json`
Stores the Google Gemini API Key used for Gemini Live Audio and Gemini Vision API fallbacks:
```json
{
    "gemini_api_key": "YOUR_GEMINI_API_KEY"
}
```

### 2. `config/settings.json`
Stores the active system preferences, fallback logic, LLM configurations, and third-party keys (like Groq):
```json
{
    "llm_provider": "groq",
    "llm_model": "llama-3.3-70b-versatile",
    "ollama_url": "http://localhost:11434",
    "planner_provider": "groq",
    "planner_model": "llama-3.3-70b-versatile",
    "vision_provider": "ollama",
    "vision_model": "moondream",
    "tts_provider": "kokoro",
    "tts_voice": "en-US-JennyNeural",
    "kokoro_voice": "af_heart",
    "stt_provider": "faster-whisper",
    "stt_model": "base",
    "groq_api_key": "YOUR_GROQ_API_KEY",
    "screen_watcher_enabled": true,
    "proactive_agent_enabled": true,
    "fallback_to_gemini": true,
    "fallback_to_ollama": true,
    "ollama_model": "qwen2.5-coder:7b",
    "fast_ollama_model": "phi3:mini"
}
```

---

## 🚀 Running Jarvis

Jarvis runs in the background. To start the interface (PyQt6 Dynamic Island overlay):
```powershell
python main.py
```

### Command Line Flags:
Jarvis supports a few system hooks:
*   `python main.py --install` — Installs Jarvis into the Windows startup registry so it automatically opens when you log in.
*   `python main.py --uninstall` — Removes Jarvis from the Windows startup registry.
*   `python main.py --startup-status` — Prints whether auto-start is currently registered.

---

## 🛠️ Troubleshooting & Common Pitfalls

### 1. Error: "No module named 'imagehash'" or "No module named 'kokoro_onnx'"
*   **Cause:** Your python shell is not running within the virtual environment, or the packages failed to install.
*   **Fix:** Ensure you run `.venv\Scripts\Activate.ps1` before running `main.py`. Re-run `python install.py --check` to diagnose missing modules.

### 2. Quiet mode: "Screen awareness unavailable" in startup log
*   **Cause:** PyTorch (`torch`), `transformers`, or `imagehash` is missing from the environment.
*   **Fix:** Run `python install.py` to restore pip packages. If running PyTorch on an older system, make sure the library matches your CPU/GPU hardware capability.

### 3. Local speech sounds robotic or is silent
*   **Cause:** `espeak-ng` is not installed on the system, causing Jarvis to fall back to SAPI5 (`pyttsx3`) if offline, or fail silent.
*   **Fix:** Install `espeak-ng` and add it to your Windows System environment variables. Restart your console/terminal to apply PATH changes.

### 4. Vosk / Audio Input Issues
*   **Cause:** No default audio input device is selected or Windows is blocking microphone permissions.
*   **Fix:** Go to **Windows Settings > Privacy & Security > Microphone** and ensure "Let desktop apps access your microphone" is turned on. Ensure your default audio recording device in Windows is set to your active microphone.
