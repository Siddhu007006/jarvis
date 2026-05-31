"""
install.py — Model & dependency installer for Jarvis V5.

Usage:
    python install.py                    → Install pip dependencies
    python install.py --download-models  → Pre-download AI models (moondream2, Kokoro)
    python install.py --check            → Verify all components are available

Concept: Large AI models (moondream2 ~1.8GB, Kokoro ~500MB) download on
first use by default. This script lets you pre-download them explicitly
so the first startup isn't unexpectedly slow.
"""

import argparse
import sys
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).parent


def install_deps():
    """Install all pip dependencies from requirements.txt."""
    req = BASE_DIR / "requirements.txt"
    if not req.exists():
        print("❌ requirements.txt not found")
        return False

    print("📦 Installing pip dependencies...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req)],
        capture_output=False
    )
    return result.returncode == 0


def download_models():
    """Pre-download AI models so first startup is fast."""

    print("\n🔽 Downloading moondream2 vision model (~1.8GB)...")
    print("   This may take several minutes on the first run.\n")
    try:
        from transformers import AutoTokenizer, AutoModelForCausalLM
        tokenizer = AutoTokenizer.from_pretrained(
            "vikhyatk/moondream2", revision="2024-07-23"
        )
        model = AutoModelForCausalLM.from_pretrained(
            "vikhyatk/moondream2", revision="2024-07-23",
            trust_remote_code=True
        )
        print("✅ moondream2 downloaded successfully")
        del tokenizer, model  # Free memory
    except Exception as e:
        print(f"⚠️ moondream2 download failed: {e}")
        print("   You can try again later. Vision features will be unavailable.")

    print("\n🔽 Downloading Kokoro ONNX model and voices...")
    resources_dir = BASE_DIR / "resources"
    resources_dir.mkdir(exist_ok=True)
    
    onnx_path = resources_dir / "kokoro-v1.0.onnx"
    voices_path = resources_dir / "voices-v1.0.bin"
    
    import urllib.request
    
    def download_url(url: str, dest: Path):
        if dest.exists():
            print(f"  ✅ {dest.name} already exists.")
            return True
        print(f"  Downloading {url} to {dest}...")
        try:
            with urllib.request.urlopen(url) as response, open(dest, "wb") as out_file:
                meta = response.info()
                file_size = int(meta.get("Content-Length", 0))
                downloaded = 0
                block_size = 8192
                last_pct = -1
                
                while True:
                    buffer = response.read(block_size)
                    if not buffer:
                        break
                    downloaded += len(buffer)
                    out_file.write(buffer)
                    if file_size > 0:
                        pct = int(downloaded * 100 / file_size)
                        if pct % 10 == 0 and pct != last_pct:
                            print(f"    Progress: {pct}%")
                            last_pct = pct
            print(f"  ✅ Saved {dest.name}")
            return True
        except Exception as e:
            print(f"  ❌ Failed to download {url}: {e}")
            return False

    onnx_url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
    voices_url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
    
    onnx_ok = download_url(onnx_url, onnx_path)
    voices_ok = download_url(voices_url, voices_path)
    
    if onnx_ok and voices_ok:
        try:
            from kokoro_onnx import Kokoro
            kokoro = Kokoro(str(onnx_path), str(voices_path))
            print("✅ Kokoro ONNX model loaded and verified successfully")
            del kokoro
        except Exception as e:
            print(f"⚠️ Verification failed: {e}")
    else:
        print("❌ Kokoro ONNX model files could not be fully downloaded.")


def check_components():
    """Verify all Jarvis components are available."""
    checks = [
        ("faster-whisper (STT)", "faster_whisper"),
        ("sounddevice (audio)", "sounddevice"),
        ("PyQt6 (UI)", "PyQt6"),
        ("httpx (HTTP)", "httpx"),
        ("vosk (wake word)", "vosk"),
        ("pydub (audio)", "pydub"),
        ("psutil (system)", "psutil"),
        ("edge-tts (TTS fallback)", "edge_tts"),
        ("kokoro-onnx (TTS primary)", "kokoro_onnx"),
        ("soundfile (audio I/O)", "soundfile"),
        ("mss (screen capture)", "mss"),
        ("imagehash (change detect)", "imagehash"),
        ("transformers (vision)", "transformers"),
        ("uiautomation (UI control)", "uiautomation"),
    ]

    print("\n🔍 Component check:")
    all_ok = True
    for name, module in checks:
        try:
            __import__(module)
            print(f"  ✅ {name}")
        except ImportError:
            print(f"  ❌ {name} — not installed")
            all_ok = False

    # Check ONNX files
    resources_dir = BASE_DIR / "resources"
    onnx_path = resources_dir / "kokoro-v1.0.onnx"
    voices_path = resources_dir / "voices-v1.0.bin"
    if onnx_path.exists() and voices_path.exists():
        print("  ✅ Kokoro ONNX model files exist in resources/")
    else:
        print("  ❌ Kokoro ONNX model files missing in resources/ (Run: python install.py --download-models)")
        all_ok = False

    # Check espeak-ng (system dependency for Kokoro)
    import shutil
    if shutil.which("espeak-ng"):
        print("  ✅ espeak-ng (system)")
    else:
        print("  ⚠️ espeak-ng — not found (needed for Kokoro TTS)")
        all_ok = False

    if all_ok:
        print("\n✅ All components available!")
    else:
        print("\n⚠️ Some components are missing. Install them with:")
        print("   pip install -r requirements.txt")

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Jarvis V5 Installer")
    parser.add_argument("--download-models", action="store_true",
                        help="Pre-download AI models (moondream2, Kokoro)")
    parser.add_argument("--check", action="store_true",
                        help="Verify all components are available")
    parser.add_argument("--deps-only", action="store_true",
                        help="Only install pip dependencies")
    args = parser.parse_args()

    if args.check:
        check_components()
    elif args.download_models:
        download_models()
    elif args.deps_only:
        install_deps()
    else:
        install_deps()
        print("\n💡 To pre-download AI models, run:")
        print("   python install.py --download-models")
        print("\n💡 To verify everything is ready, run:")
        print("   python install.py --check")


if __name__ == "__main__":
    main()
