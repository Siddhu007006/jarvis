"""Verification test for Jarvis v4.0 — Speed-optimized pipeline."""
import sys
import time
sys.path.insert(0, r'c:\Users\Siddharth Reddy\projects\jarvis')

print("=== VERIFICATION: Jarvis v4.0 Speed Pipeline ===")
print()

# Test 1: Providers (LLM text generation)
print("[1/4] Testing providers.py (LLM generate)...")
try:
    t0 = time.time()
    from core.providers import llm_generate
    result = llm_generate("Reply with exactly the word: OK", system="You are a test assistant. Reply concisely.")
    elapsed = time.time() - t0
    print(f"  PASS — LLM responded: {result[:80]}  ({elapsed:.1f}s)")
except Exception as e:
    print(f"  FAIL — {e}")

print()

# Test 2: Planner (JSON planning)
print("[2/4] Testing planner.py (JSON planning)...")
try:
    t0 = time.time()
    from core.planner import create_plan
    plan = create_plan("open spotify")
    elapsed = time.time() - t0
    if plan and plan.get("steps"):
        steps = plan["steps"]
        print(f"  PASS — Plan has {len(steps)} steps  ({elapsed:.1f}s)")
        for s in steps[:3]:
            print(f"         Step {s.get('step')}: [{s.get('tool')}] {s.get('description')}")
    else:
        print(f"  FAIL — No plan returned: {plan}  ({elapsed:.1f}s)")
except Exception as e:
    print(f"  FAIL — {e}")

print()

# Test 3: TTS
print("[3/4] Testing tts.py (edge-tts)...")
try:
    t0 = time.time()
    from core.tts import speak_to_file
    path = speak_to_file("Hello, I am Jarvis, your local assistant.")
    elapsed = time.time() - t0
    if path:
        import os
        size = os.path.getsize(path)
        print(f"  PASS — TTS generated {size} bytes  ({elapsed:.1f}s)")
        os.unlink(path)
    else:
        print(f"  FAIL — No audio file generated  ({elapsed:.1f}s)")
except Exception as e:
    print(f"  FAIL — {e}")

print()

# Test 4: STT module
print("[4/4] Testing stt.py (faster-whisper)...")
try:
    t0 = time.time()
    from core.stt import _get_model
    model = _get_model()
    elapsed = time.time() - t0
    if model:
        print(f"  PASS — faster-whisper model loaded  ({elapsed:.1f}s)")
    else:
        print(f"  WARN — faster-whisper unavailable, Vosk fallback  ({elapsed:.1f}s)")
except Exception as e:
    print(f"  FAIL — {e}")

print()
print("=== DONE ===")
