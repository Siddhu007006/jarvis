"""
core/tts.py — Text-to-Speech Engine (Kokoro-first, local-first).

Fallback chain (each level tries the next if it fails):
  1. Kokoro-TTS  — local neural model, ~180ms, no internet needed
  2. edge-tts    — free Microsoft Azure voices, ~800ms, needs internet
  3. pyttsx3     — Windows SAPI5 offline voices, ~200ms, lowest quality

Concept: Kokoro-TTS uses a lightweight 82M parameter neural model that runs
100% locally. It generates speech ~4x faster than edge-tts because there's
no network round-trip. The trade-off is a one-time ~500MB model download
and the espeak-ng system dependency for phonemization.

If espeak-ng isn't installed, Kokoro silently falls back to edge-tts.
If the network is down, edge-tts falls back to pyttsx3 (SAPI5).

Pipeline: Text → Kokoro/edge-tts → WAV/MP3 bytes → PCM → play via sounddevice

Dependencies:
  pip install kokoro soundfile    # Kokoro (primary)
  pip install edge-tts pydub      # edge-tts (fallback)
  pip install pyttsx3             # pyttsx3 (last resort)
  # System: espeak-ng MSI installer (for Kokoro phonemization)
"""

import asyncio
import io
import json
import logging
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent

# ── Kokoro Pipeline (lazy-loaded singleton) ─────────────────────
_kokoro_pipeline = None
_kokoro_lock = threading.Lock()
_kokoro_available = None  # None = not checked yet, True/False after check

# Phase 2: Cached settings — avoids disk read on every TTS call.
# Concept: Settings rarely change at runtime. We cache the parsed
# dict and only re-read when the file's mtime changes.
_settings_cache = None
_settings_mtime = 0.0
_settings_lock = threading.Lock()


def _get_settings() -> dict:
    """Load TTS settings from config/settings.json (cached by mtime)."""
    global _settings_cache, _settings_mtime
    settings_path = BASE_DIR / "config" / "settings.json"
    try:
        current_mtime = settings_path.stat().st_mtime
        with _settings_lock:
            if _settings_cache is not None and current_mtime == _settings_mtime:
                return _settings_cache
        # Read outside lock (I/O), then update under lock
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        with _settings_lock:
            _settings_cache = data
            _settings_mtime = current_mtime
        return data
    except Exception:
        return {}


def _get_voice() -> str:
    """Get configured TTS voice name."""
    settings = _get_settings()
    provider = settings.get("tts_provider", "kokoro")
    if provider == "kokoro":
        return settings.get("kokoro_voice", "af_heart")
    return settings.get("tts_voice", "en-US-JennyNeural")


def _get_kokoro_pipeline():
    """
    Lazy-load the Kokoro TTS pipeline (singleton).

    Concept: Kokoro class loads the ONNX model weights on first call,
    then caches them in memory. Subsequent calls reuse the loaded model.

    Returns the pipeline object, or None if Kokoro is unavailable.
    """
    global _kokoro_pipeline, _kokoro_available

    with _kokoro_lock:
        if _kokoro_available is False:
            return None
        if _kokoro_pipeline is not None:
            return _kokoro_pipeline

        try:
            from kokoro_onnx import Kokoro
            resources_dir = BASE_DIR / "resources"
            onnx_path = resources_dir / "kokoro-v1.0.onnx"
            voices_path = resources_dir / "voices-v1.0.bin"

            if not onnx_path.exists() or not voices_path.exists():
                log.warning("⚠️ Kokoro ONNX model files missing in resources/")
                _kokoro_available = False
                return None

            _kokoro_pipeline = Kokoro(str(onnx_path), str(voices_path))
            _kokoro_available = True
            log.info("🔊 Kokoro-ONNX pipeline loaded (local neural TTS)")
            return _kokoro_pipeline
        except ImportError:
            log.info("ℹ️ kokoro-onnx not installed (pip install kokoro-onnx). Using edge-tts.")
            _kokoro_available = False
            return None
        except Exception as e:
            log.warning("⚠️ Kokoro ONNX init failed: %s. Falling back to edge-tts.", e)
            _kokoro_available = False
            return None


# ── Kokoro-TTS (Primary — Local ONNX Neural) ───────────────────

def _kokoro_generate(text: str, output_path: str = None) -> str | None:
    """
    Generate speech using Kokoro-TTS (local, ~180ms latency via ONNX).

    Args:
        text: Text to speak
        output_path: Where to save the WAV file (auto-generated if None)

    Returns:
        Path to the generated WAV file, or None on failure
    """
    pipeline = _get_kokoro_pipeline()
    if pipeline is None:
        return None

    if not text or not text.strip():
        return None

    voice = _get_settings().get("kokoro_voice", "af_heart")

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        output_path = tmp.name
        tmp.close()

    try:
        import soundfile as sf

        # Generate audio (returns samples numpy array and sample rate)
        samples, sample_rate = pipeline.create(
            text,
            voice=voice,
            speed=1.0,
            lang="en-us"
        )

        if samples is None or len(samples) == 0:
            log.warning("Kokoro-ONNX generated no audio for: '%s'", text[:50])
            return None

        # Save as WAV
        sf.write(output_path, samples, sample_rate)

        log.info("🔊 Kokoro-ONNX TTS: generated %s (%d bytes)",
                 output_path, Path(output_path).stat().st_size)
        return output_path

    except Exception as e:
        log.error("Kokoro-ONNX generation failed: %s", e)
        return None


# ── Edge-TTS (Fallback #1 — Network) ───────────────────────────

# Phase 2: Persistent event loop for edge-tts async calls.
# Concept: Instead of creating a new event loop per call (asyncio.run),
# we keep one loop alive in a daemon thread. Coroutines are scheduled
# onto it via run_coroutine_threadsafe, which is thread-safe and reuses
# the existing loop. The daemon thread dies when the main process exits.
_edge_tts_loop = None
_edge_tts_loop_lock = threading.Lock()


def _get_edge_tts_loop() -> asyncio.AbstractEventLoop:
    """Get or create the persistent event loop for edge-tts."""
    global _edge_tts_loop
    with _edge_tts_loop_lock:
        if _edge_tts_loop is not None and _edge_tts_loop.is_running():
            return _edge_tts_loop

        loop = asyncio.new_event_loop()

        def _run_loop():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run_loop, daemon=True, name="edge-tts-loop")
        t.start()
        _edge_tts_loop = loop
        return loop


def _edge_tts_generate(text: str, output_path: str = None) -> str | None:
    """
    Generate speech using edge-tts (free Microsoft Azure voices).

    Concept: edge-tts uses Microsoft Edge's online TTS service which is
    free, requires no API key, and supports 300+ neural voices.
    The downside: requires internet (~800ms network latency per call).

    Returns path to MP3 file, or None on failure.
    """
    if not text or not text.strip():
        return None

    try:
        import edge_tts
    except ImportError:
        log.warning("edge-tts not installed. pip install edge-tts")
        return None

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        output_path = tmp.name
        tmp.close()

    voice = _get_settings().get("tts_voice", "en-US-JennyNeural")

    try:
        async def _generate():
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(output_path)

        # Phase 2: Persistent event loop in a daemon thread.
        # Concept: asyncio.run() creates + destroys an event loop each call
        # (~50ms overhead). Instead, we keep one loop alive in a background
        # thread and schedule coroutines onto it via run_coroutine_threadsafe.
        loop = _get_edge_tts_loop()
        future = asyncio.run_coroutine_threadsafe(_generate(), loop)
        future.result(timeout=15)  # block until done or timeout

        log.info("🔊 edge-tts: generated %s (%d bytes)",
                 output_path, Path(output_path).stat().st_size)
        return output_path

    except Exception as e:
        log.error("edge-tts failed: %s", e)
        return None


# ── pyttsx3 Fallback (Fallback #2 — Fully Offline) ─────────────

# Phase 2: Singleton pyttsx3 engine.
# Concept: pyttsx3.init() initialises COM, loads SAPI5 voices, and
# builds the audio pipeline — ~200ms each time. By caching the engine
# at module level we pay that cost once per process.
_pyttsx3_engine = None
_pyttsx3_lock = threading.Lock()


def _get_pyttsx3():
    """Get or create the singleton pyttsx3 engine."""
    global _pyttsx3_engine
    with _pyttsx3_lock:
        if _pyttsx3_engine is not None:
            return _pyttsx3_engine
        try:
            import pyttsx3
            _pyttsx3_engine = pyttsx3.init()
            _pyttsx3_engine.setProperty("rate", 180)
            return _pyttsx3_engine
        except Exception as e:
            log.error("pyttsx3 init failed: %s", e)
            return None


def _pyttsx3_fallback_file(text: str, output_path: str = None) -> str | None:
    """Generate speech using pyttsx3 (offline, lower quality)."""
    engine = _get_pyttsx3()
    if engine is None:
        return None
    try:
        if output_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            output_path = tmp.name
            tmp.close()

        engine.save_to_file(text, output_path)
        engine.runAndWait()

        log.info("TTS (pyttsx3 fallback): %s", output_path)
        return output_path

    except Exception as e:
        log.error("pyttsx3 fallback failed: %s", e)
        return None


def _pyttsx3_speak(text: str):
    """Speak text using pyttsx3 (blocking, offline)."""
    engine = _get_pyttsx3()
    if engine is None:
        return
    try:
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        log.error("pyttsx3 speak failed: %s", e)


# ── Public API ──────────────────────────────────────────────────

def speak_to_file(text: str, output_path: str = None) -> str | None:
    """
    Generate speech audio file from text.

    Fallback chain: Kokoro (local) → edge-tts (network) → pyttsx3 (SAPI5)

    Args:
        text: Text to speak
        output_path: Where to save the audio file (auto-generated if None)

    Returns:
        Path to the generated audio file, or None on failure
    """
    if not text or not text.strip():
        return None

    # Try Kokoro first (local, fastest)
    path = _kokoro_generate(text, output_path)
    if path:
        return path

    # Fallback to edge-tts (network)
    path = _edge_tts_generate(text, output_path)
    if path:
        return path

    # Last resort: pyttsx3 (offline, low quality)
    return _pyttsx3_fallback_file(text, output_path)


def speak_now(text: str):
    """
    Speak text out loud immediately (blocking).

    This is the primary entry point used by the streaming pipeline.
    Generates audio → plays immediately → returns when playback finishes.

    Concept: Used by StreamingPipeline._tts_worker() to speak each
    sentence chunk as it arrives from the LLM token stream. Must be
    blocking so the worker knows when one chunk finishes before starting
    the next.
    """
    if not text or not text.strip():
        return

    audio_path = speak_to_file(text)
    if audio_path:
        try:
            _play_audio_file(audio_path)
            return
        except Exception as e:
            log.error("speak_now playback failed: %s", e)

    # If file-based playback failed, try pyttsx3 direct
    _pyttsx3_speak(text)


def speak_bytes(text: str) -> bytes | None:
    """
    Generate speech as raw PCM bytes (int16, 24kHz, mono).

    Args:
        text: Text to speak

    Returns:
        Raw PCM int16 bytes at 24kHz, or None on failure
    """
    if not text or not text.strip():
        return None

    audio_path = speak_to_file(text)
    if not audio_path:
        return None

    try:
        return _audio_to_pcm(audio_path)
    finally:
        try:
            Path(audio_path).unlink(missing_ok=True)
        except Exception:
            pass


def play_text(text: str):
    """
    Speak text out loud immediately (blocking).

    Convenience wrapper — same as speak_now().
    Used for one-off responses outside the engine loop.
    """
    speak_now(text)


# ── Audio Helpers ───────────────────────────────────────────────

def _audio_to_pcm(file_path: str, target_rate: int = 24000) -> bytes | None:
    """
    Convert audio file (WAV or MP3) to raw PCM int16 bytes.

    Tries soundfile first (for WAV), then pydub (for MP3).
    """
    # Try soundfile (handles WAV natively, very fast)
    try:
        import soundfile as sf
        import numpy as np
        data, sr = sf.read(file_path, dtype="float32")
        # Phase 2: Proper polyphase resampling (replaces nearest-neighbor)
        # Concept: Nearest-neighbor resampling aliases high-frequency
        # content into the audible band, causing crackling/buzzing
        # artifacts. Polyphase filtering applies a proper anti-alias
        # FIR filter before decimation — clean audio, ~2ms cost.
        if sr != target_rate:
            try:
                from scipy.signal import resample_poly
                from math import gcd
                up = target_rate // gcd(sr, target_rate)
                down = sr // gcd(sr, target_rate)
                data = resample_poly(data, up, down).astype(np.float32)
            except ImportError:
                # scipy not available — fall back to nearest-neighbor
                ratio = target_rate / sr
                indices = (np.arange(int(len(data) * ratio)) / ratio).astype(int)
                indices = np.clip(indices, 0, len(data) - 1)
                data = data[indices]
        # Convert to int16
        pcm = (data * 32767).astype(np.int16)
        return pcm.tobytes()
    except Exception:
        pass

    # Try pydub (handles MP3)
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_file(file_path)
        audio = audio.set_channels(1).set_frame_rate(target_rate).set_sample_width(2)
        return audio.raw_data
    except Exception:
        pass

    # Fallback: wave module (WAV only)
    try:
        import wave
        with wave.open(file_path, "rb") as wf:
            return wf.readframes(wf.getnframes())
    except Exception:
        pass

    log.warning("Cannot decode audio file: %s", file_path)
    return None


def _play_audio_file(file_path: str):
    """Play an audio file through sounddevice. Blocking. Cleans up temp file after."""
    try:
        # Primary: soundfile → sounddevice (direct, fast)
        try:
            import soundfile as sf
            import sounddevice as sd
            data, sr = sf.read(file_path, dtype="float32")
            sd.play(data, samplerate=sr, blocksize=1024)
            sd.wait()
            return
        except Exception:
            pass

        # Fallback: pydub → numpy → sounddevice
        try:
            from pydub import AudioSegment
            import numpy as np
            import sounddevice as sd

            audio = AudioSegment.from_file(file_path)
            audio = audio.set_channels(1).set_frame_rate(24000).set_sample_width(2)
            samples = np.frombuffer(audio.raw_data, dtype=np.int16)
            sd.play(samples, samplerate=24000, blocksize=1024)
            sd.wait()
            return
        except Exception:
            pass

        log.error("Cannot play audio file: %s", file_path)
    finally:
        try:
            Path(file_path).unlink(missing_ok=True)
        except Exception:
            pass
