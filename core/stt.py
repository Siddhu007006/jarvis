"""
core/stt.py — Local Speech-to-Text using faster-whisper.

Concept: faster-whisper uses CTranslate2-optimized Whisper models that run
~4x faster than OpenAI's original Whisper on CPU. A 5-second audio clip
transcribes in <0.5 seconds on modern hardware.

This replaces Gemini Live Audio's built-in STT which was:
1. Slow (2-5s network round-trip)
2. Inaccurate (e.g., "ओ पन Spo tif y" instead of "Open Spotify")
3. Rate-limited (20 RPM on free tier)

Pipeline: Mic audio → WAV file or PCM bytes → faster-whisper → text
Fallback: If faster-whisper unavailable → Vosk (already installed)
"""

import io
import logging
import threading
import time
import wave
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Lazy-loaded model (load once, reuse) ────────────────────────

_whisper_model = None
_whisper_lock = threading.Lock()  # Phase 2: module-level — no lazy-init race


def _get_model():
    """Lazy-load the faster-whisper model on first use."""
    global _whisper_model

    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model

        try:
            from faster_whisper import WhisperModel

            # Load settings for model size
            import json
            settings_path = Path(__file__).parent.parent / "config" / "settings.json"
            model_size = "base"
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                model_size = settings.get("stt_model", "base")
            except Exception:
                pass

            log.info("🎙️ Loading faster-whisper model '%s'...", model_size)
            start = time.time()

            # Auto-detect CUDA GPU for massive speedup (400ms → 80ms on RTX 3050)
            # Concept: faster-whisper uses CTranslate2 which supports CUDA natively.
            # float16 on GPU is both faster AND more accurate than int8 on CPU.
            # Falls back to CPU int8 if no CUDA device is available.
            try:
                import torch
                if torch.cuda.is_available():
                    device = "cuda"
                    compute_type = "float16"
                    log.info("🎮 CUDA detected (%s) — using GPU acceleration",
                             torch.cuda.get_device_name(0))
                else:
                    device = "cpu"
                    compute_type = "int8"
            except ImportError:
                device = "cpu"
                compute_type = "int8"

            _whisper_model = WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type,
            )

            elapsed = time.time() - start
            log.info("🎙️ faster-whisper '%s' loaded in %.1fs", model_size, elapsed)
            return _whisper_model

        except ImportError:
            log.warning("faster-whisper not installed. pip install faster-whisper")
            return None
        except Exception as e:
            log.error("Failed to load faster-whisper: %s", e)
            return None


# ── Public API ──────────────────────────────────────────────────

# ── Intent Complexity Estimator ─────────────────────────────────

def _estimate_beam_size(
    pcm_bytes: bytes,
    sample_rate: int = 16000,
    context_hint: Optional[str] = None,
) -> int:
    """
    Estimate optimal beam_size from audio complexity — NOT just duration.

    Concept: Audio duration alone is a poor proxy for transcription
    difficulty. "Reconfigure the Kubernetes ingress with mTLS" is 2s
    but needs beam=5 for technical vocabulary. "Uhhh open Chrome" is
    8s but beam=1 nails it. We use 4 cheap audio-level signals that
    correlate with lexical complexity:

    1. Duration   — longer audio = more words = more search needed
    2. Energy var — high variance = varied intonation = complex speech
    3. ZCR        — high zero-crossing rate = fricatives/plosives =
                    technical vocabulary ("kubectl", "TLS", "HTTPS")
    4. SNR        — low signal-to-noise = noisy environment = wider
                    beam needed to separate speech from noise

    Each signal produces a score 0.0–1.0. The weighted sum maps to:
      score < 0.35  → beam 1 (greedy, fastest)
      score < 0.65  → beam 3 (moderate search)
      score >= 0.65 → beam 5 (full search, most accurate)

    Context hint override:
      "command"   → cap at beam 3 (short commands, speed priority)
      "dictation" → floor at beam 3 (accuracy priority)

    Cost: <1ms (numpy vectorized ops on the sample array).

    Args:
        pcm_bytes:    Raw PCM int16 mono audio bytes
        sample_rate:  Sample rate in Hz
        context_hint: Optional caller hint — "command" | "dictation" | None

    Returns:
        Beam size: 1, 3, or 5
    """
    try:
        import numpy as np
    except ImportError:
        return 5  # safe default if numpy unavailable

    n_bytes = len(pcm_bytes)
    if n_bytes < 3200:  # <0.1s — trivially short
        return 1

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    n_samples = len(samples)
    duration_s = n_samples / sample_rate

    # ── Signal 1: Duration score (0–1) ──────────────────────
    # Sigmoid-ish mapping: <2s → low, 2-8s → mid, >8s → high
    if duration_s < 2.0:
        dur_score = 0.1
    elif duration_s < 5.0:
        dur_score = 0.3 + 0.2 * ((duration_s - 2.0) / 3.0)
    elif duration_s < 10.0:
        dur_score = 0.5 + 0.3 * ((duration_s - 5.0) / 5.0)
    else:
        dur_score = 0.9

    # ── Signal 2: Energy variance (0–1) ─────────────────────
    # Concept: Split audio into 50ms windows, compute RMS per window.
    # High variance in RMS = speaker is modulating volume/emphasis
    # (common in complex sentences with multiple clauses).
    # Monotone = simple command, varied = complex intent.
    win_size = int(0.05 * sample_rate)  # 50ms windows
    if n_samples >= win_size * 2:
        # Trim to exact multiple of window size
        n_windows = n_samples // win_size
        trimmed = samples[:n_windows * win_size].reshape(n_windows, win_size)
        rms_per_window = np.sqrt(np.mean(trimmed ** 2, axis=1))
        # Coefficient of variation (std / mean) — normalized variance
        mean_rms = np.mean(rms_per_window)
        if mean_rms > 1.0:  # avoid div/0 on silence
            cv = np.std(rms_per_window) / mean_rms
            energy_var_score = min(cv / 1.5, 1.0)  # CV > 1.5 → max
        else:
            energy_var_score = 0.0
    else:
        energy_var_score = 0.3  # too short to analyze

    # ── Signal 3: Zero-crossing rate (0–1) ──────────────────
    # Concept: ZCR counts how often the signal crosses zero per second.
    # Fricatives (s, f, sh, z) and plosives (t, k, p) have high ZCR.
    # Technical vocabulary is dense in these consonants:
    # "HTTPS", "kubectl", "TypeScript", "PostgreSQL".
    # Simple words like "open", "hey", "yeah" have lower ZCR.
    zero_crossings = np.sum(np.abs(np.diff(np.sign(samples))) > 0)
    zcr = zero_crossings / n_samples
    # Typical speech ZCR: 0.05–0.15. Technical: 0.12–0.20
    zcr_score = min(max((zcr - 0.05) / 0.15, 0.0), 1.0)

    # ── Signal 4: SNR estimate (0–1, inverted — low SNR = high score)
    # Concept: Sort absolute amplitudes. Bottom 10% ≈ noise floor.
    # Ratio of mean signal to noise floor estimates SNR.
    # Noisy environments need wider beam to separate hypotheses.
    abs_samples = np.abs(samples)
    noise_floor = np.mean(np.sort(abs_samples)[:max(n_samples // 10, 1)])
    signal_level = np.mean(abs_samples)
    if noise_floor > 1.0:
        snr_ratio = signal_level / noise_floor
        # Low SNR (<5) → high complexity score
        snr_score = min(max(1.0 - (snr_ratio - 2.0) / 15.0, 0.0), 1.0)
    else:
        snr_score = 0.1  # clean signal

    # ── Weighted combination ────────────────────────────────
    # Weights reflect predictive power (empirically tuned):
    #   Duration: 25% — still relevant but not dominant
    #   Energy:   25% — speech dynamics correlate with complexity
    #   ZCR:      30% — strongest signal for technical vocabulary
    #   SNR:      20% — environmental noise compensation
    complexity = (
        0.25 * dur_score
        + 0.25 * energy_var_score
        + 0.30 * zcr_score
        + 0.20 * snr_score
    )

    # ── Context hint override ───────────────────────────────
    if context_hint == "command":
        # Short imperative commands — speed priority, cap beam
        beam = 1 if complexity < 0.45 else 3
    elif context_hint == "dictation":
        # Long-form text — accuracy priority, floor beam
        beam = 3 if complexity < 0.65 else 5
    else:
        # General case
        if complexity < 0.35:
            beam = 1
        elif complexity < 0.65:
            beam = 3
        else:
            beam = 5

    log.debug(
        "Beam estimation: dur=%.2f energy=%.2f zcr=%.2f snr=%.2f "
        "→ complexity=%.2f → beam=%d (hint=%s)",
        dur_score, energy_var_score, zcr_score, snr_score,
        complexity, beam, context_hint,
    )
    return beam


# Re-transcription confidence threshold.
# Concept: faster-whisper returns avg_logprob per segment. Values below
# this threshold indicate the greedy/narrow-beam decode is uncertain.
# We re-decode with beam=5 to get a more reliable result.
# Typical values: confident speech = -0.2 to -0.5, uncertain = < -0.8
_CONFIDENCE_RE_DECODE_THRESHOLD = -0.8


def transcribe_file(wav_path: str, context_hint: Optional[str] = None) -> str:
    """
    Transcribe a WAV file to text.

    Args:
        wav_path: Path to a WAV file (16kHz, mono, int16)

    Returns:
        Transcribed text string, or "" if failed
    """
    model = _get_model()
    if model is None:
        return _vosk_fallback_file(wav_path)

    try:
        start = time.time()

        # Read raw audio for complexity estimation (file path case)
        try:
            import wave as _wav
            with _wav.open(str(wav_path), "rb") as wf:
                raw_pcm = wf.readframes(wf.getnframes())
                sr = wf.getframerate()
        except Exception:
            raw_pcm = b""
            sr = 16000

        beam = _estimate_beam_size(raw_pcm, sr, context_hint)

        segments, info = model.transcribe(
            wav_path,
            beam_size=beam,
            language="en",
            vad_filter=True,  # Skip silence segments
            vad_parameters=dict(
                min_silence_duration_ms=500,
                speech_pad_ms=200,
            ),
        )

        seg_list = list(segments)
        text = " ".join(seg.text.strip() for seg in seg_list).strip()

        # Confidence-gated re-transcription.
        # Concept: If the initial decode has low confidence AND we used
        # a narrow beam, the greedy path may have missed the correct
        # hypothesis. Re-decoding with beam=5 explores 5x more paths.
        # Cost: ~2x latency, but only triggers on uncertain audio (~5% of calls).
        if beam < 5 and seg_list:
            avg_lp = sum(s.avg_logprob for s in seg_list) / len(seg_list)
            if avg_lp < _CONFIDENCE_RE_DECODE_THRESHOLD:
                log.info("Low confidence (avg_logprob=%.2f), re-decoding beam=5", avg_lp)
                segments2, info2 = model.transcribe(
                    wav_path, beam_size=5, language="en",
                    vad_filter=True,
                    vad_parameters=dict(
                        min_silence_duration_ms=500, speech_pad_ms=200,
                    ),
                )
                seg_list2 = list(segments2)
                text2 = " ".join(s.text.strip() for s in seg_list2).strip()
                if seg_list2:
                    avg_lp2 = sum(s.avg_logprob for s in seg_list2) / len(seg_list2)
                    if avg_lp2 > avg_lp:  # only use if actually better
                        text = text2
                        beam = 5
                        log.info("Re-decode improved: %.2f → %.2f", avg_lp, avg_lp2)

        elapsed = time.time() - start

        log.info("STT: '%s' (%.1fs, beam=%d, lang=%s, prob=%.2f)",
                 text[:80], elapsed, beam,
                 info.language, info.language_probability)
        return text

    except Exception as e:
        log.error("STT transcription failed: %s", e)
        return _vosk_fallback_file(wav_path)


def transcribe_pcm(
    pcm_bytes: bytes,
    sample_rate: int = 16000,
    context_hint: Optional[str] = None,
) -> str:
    """
    Transcribe raw PCM int16 audio bytes.

    Phase 2 improvement: Eliminated the disk round-trip.
    Concept: faster-whisper's CTranslate2 backend accepts any file-like
    object, not just file paths. By wrapping PCM bytes in an in-memory
    WAV via io.BytesIO, we skip the temp-file write+read cycle.

    Beam selection uses intent complexity estimation (audio signals +
    context hint), not just duration. See _estimate_beam_size().

    Args:
        pcm_bytes:    Raw PCM audio data (int16, mono)
        sample_rate:  Sample rate in Hz (default 16000)
        context_hint: Optional — "command" | "dictation" | None

    Returns:
        Transcribed text string
    """
    if not pcm_bytes or len(pcm_bytes) < 1600:  # Less than 0.1s
        return ""

    model = _get_model()
    if model is None:
        # Vosk fallback still needs a file — write temp
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm_bytes)
            return _vosk_fallback_file(tmp.name)
        finally:
            try:
                Path(tmp.name).unlink(missing_ok=True)
            except Exception:
                pass

    # Build in-memory WAV (no disk I/O)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16 = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    buf.seek(0)

    try:
        start = time.time()

        beam = _estimate_beam_size(pcm_bytes, sample_rate, context_hint)

        segments, info = model.transcribe(
            buf,
            beam_size=beam,
            language="en",
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=500,
                speech_pad_ms=200,
            ),
        )

        seg_list = list(segments)
        text = " ".join(seg.text.strip() for seg in seg_list).strip()

        # Confidence-gated re-transcription (same logic as transcribe_file)
        if beam < 5 and seg_list:
            avg_lp = sum(s.avg_logprob for s in seg_list) / len(seg_list)
            if avg_lp < _CONFIDENCE_RE_DECODE_THRESHOLD:
                log.info("Low confidence (avg_logprob=%.2f), re-decoding beam=5", avg_lp)
                buf.seek(0)
                segments2, _ = model.transcribe(
                    buf, beam_size=5, language="en",
                    vad_filter=True,
                    vad_parameters=dict(
                        min_silence_duration_ms=500, speech_pad_ms=200,
                    ),
                )
                seg_list2 = list(segments2)
                text2 = " ".join(s.text.strip() for s in seg_list2).strip()
                if seg_list2:
                    avg_lp2 = sum(s.avg_logprob for s in seg_list2) / len(seg_list2)
                    if avg_lp2 > avg_lp:
                        text = text2
                        beam = 5
                        log.info("Re-decode improved: %.2f → %.2f", avg_lp, avg_lp2)

        elapsed = time.time() - start

        log.info("STT (PCM in-memory, beam=%d): '%s' (%.1fs)",
                 beam, text[:80], elapsed)
        return text

    except Exception as e:
        log.error("PCM transcription failed: %s", e)
        return ""


# ── Vosk Fallback ───────────────────────────────────────────────

# Cached Vosk model (Phase 2: avoid ~1-2s reload per fallback call)
_vosk_model = None
_vosk_lock = threading.Lock()


def _vosk_fallback_file(wav_path: str) -> str:
    """
    Fallback STT using Vosk (already installed for wake word).
    Less accurate than Whisper but works offline without extra downloads.

    Phase 2: Vosk model is now cached at module level. The original code
    called vosk.Model(lang="en-us") on every fallback invocation, which
    loads ~40MB of model files from disk each time (~1-2s overhead).
    """
    global _vosk_model
    try:
        import vosk
        import json as _json

        vosk.SetLogLevel(-1)

        with _vosk_lock:
            if _vosk_model is None:
                _vosk_model = vosk.Model(lang="en-us")

        with wave.open(wav_path, "rb") as wf:
            rec = vosk.KaldiRecognizer(_vosk_model, wf.getframerate())
            rec.SetWords(True)

            results = []
            while True:
                data = wf.readframes(4000)
                if len(data) == 0:
                    break
                if rec.AcceptWaveform(data):
                    r = _json.loads(rec.Result())
                    results.append(r.get("text", ""))

            r = _json.loads(rec.FinalResult())
            results.append(r.get("text", ""))

        text = " ".join(r for r in results if r).strip()
        log.info("STT (Vosk fallback): '%s'", text[:80])
        return text

    except Exception as e:
        log.error("Vosk fallback failed: %s", e)
        return ""
