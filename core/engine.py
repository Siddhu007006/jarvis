"""
Jarvis Engine v4.0 - Local-First Voice Pipeline.

Replaces the Gemini Live Audio websocket with a decomposed local pipeline:
  Mic -> Vosk (wake word) -> Record until silence -> faster-whisper (STT)
  -> Ollama/Groq (LLM brain) -> edge-tts (TTS) -> Speaker

Concept: Instead of streaming audio to Google's servers (slow, rate-limited),
each component runs independently and locally:
  - STT: faster-whisper (CTranslate2, <1s on CPU)
  - LLM: Ollama (local) or Groq (free cloud, <1s)
  - TTS: edge-tts (free Microsoft, <1s)

All tool execution logic is preserved from v3.0.
"""

import asyncio
import base64
import collections
import json
import logging
import os
import queue
import re
import sys
import threading
import traceback
import time
import wave
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

from actions.app_launcher import open_app, close_app
from actions.system_control import system_control
from actions.web_search import web_search
from actions.file_manager import file_manager
from actions.screen_vision import capture_screen
from actions.command_runner import run_command
from actions.computer_control import computer_control as cc_tool
from actions.keyboard import type_text
from actions.clipboard import clipboard_action
from core.tools import TOOL_DECLARATIONS
from core.planner import create_plan
from core.executor import execute_plan
from memory.manager import (
    load, remember, format_for_prompt,
    get_cached_workflow, save_workflow, log_conversation,
)

log = logging.getLogger(__name__)

# -- Robust Control Components (Phase 5) ------------------------------------
# Loaded lazily to avoid circular imports, cached after first use.
_intent_router = None
_failure_logger = None

def _get_router():
    global _intent_router
    if _intent_router is None:
        try:
            from core.intent_router import IntentRouter
            _intent_router = IntentRouter()
            log.info("IntentRouter loaded")
        except Exception as e:
            log.warning("IntentRouter unavailable: %s", e)
    return _intent_router

def _get_flogger():
    global _failure_logger
    if _failure_logger is None:
        try:
            from core.failure_logger import get_failure_logger
            _failure_logger = get_failure_logger()
            log.info("FailureLogger loaded")
        except Exception as e:
            log.warning("FailureLogger unavailable: %s", e)
    return _failure_logger

# -- Constants ---------------------------------------------------------------
BASE_DIR          = Path(__file__).parent.parent
CONFIG_PATH       = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH       = BASE_DIR / "core" / "prompt.txt"
CHANNELS          = 1
SEND_SAMPLE_RATE  = 16000
RECV_SAMPLE_RATE  = 24000
CHUNK_SIZE        = 1024
SILENCE_THRESHOLD = 400      # RMS below this = silence
SILENCE_DURATION  = 0.8      # seconds of silence to stop recording
MIN_RECORD_TIME   = 0.5      # minimum recording time in seconds


def _load_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return "You are JARVIS, a professional AI assistant. Be concise and direct."


# -- Clean transcript artifacts ----------------------------------------------
_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean(text: str) -> str:
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()


def _build_legacy_tool_descriptions() -> str:
    """Build a text description of available tools for the LLM system prompt."""
    lines = []
    for tool in TOOL_DECLARATIONS:
        name = tool.get("name", "")
        desc = tool.get("description", "")
        params = tool.get("parameters", {}).get("properties", {})
        required = tool.get("parameters", {}).get("required", [])

        param_strs = []
        for pname, pinfo in params.items():
            req = "(required)" if pname in required else "(optional)"
            param_strs.append(f"    - {pname}: {pinfo.get('description', '')} {req}")

        lines.append(f"  {name}: {desc}")
        if param_strs:
            lines.extend(param_strs)

    return "\n".join(lines)


# -- Tool call prompt injection ----------------------------------------------
LEGACY_TOOL_CALL_INSTRUCTION = """
TOOL CALLING:
You have access to these tools. When you need to perform an action on the user's computer, respond with a JSON block wrapped in <tool_call> tags:

<tool_call>
{"tool": "tool_name", "parameters": {"param1": "value1"}}
</tool_call>

For complex multi-step tasks (opening apps, navigating UI, etc.), use agent_task:
<tool_call>
{"tool": "agent_task", "parameters": {"goal": "description of what to do"}}
</tool_call>

IMPORTANT RULES:
- For simple conversation (greetings, questions, jokes), just respond naturally with text. NO tool calls needed.
- For tasks that require action on the computer, use tool calls.
- You can include text BEFORE the tool call to acknowledge the user.
- Only ONE tool call per response.
- After a tool executes, you'll get the result and can respond.

AVAILABLE TOOLS:
"""


class JarvisEngine:
    """Main voice engine - local-first pipeline with Ollama/Groq."""

    def __init__(self, ui):
        self.ui = ui
        self.audio_in_queue: queue.Queue = queue.Queue()
        self._loop: asyncio.AbstractEventLoop = None

        # -- Thread-safe state flags --
        self._state_lock = threading.Lock()
        self._awake      = False
        self._muted      = False

        # Speaking state (thread-safe event)
        self._speaking  = threading.Event()
        self._turn_done = threading.Event()

        # RMS levels for reactive UI waveform
        self.rms_levels: collections.deque = collections.deque([0.0] * 40, maxlen=40)

        # Wake Word state
        self.last_awake_time = time.time()

        # Conversation history
        self._history: list[dict] = []
        self._history_lock = threading.Lock()
        self._max_history  = 20

        # Recording state
        self._recording     = False
        self._audio_buffer: list[bytes] = []

        # Wake word detection delegated to core/wake_word.py
        self._vosk_available = False

    # -- Thread-safe state properties ----------------------------------------

    @property
    def awake(self) -> bool:
        with self._state_lock:
            return self._awake

    @awake.setter
    def awake(self, value: bool) -> None:
        with self._state_lock:
            self._awake = bool(value)

    @property
    def muted(self) -> bool:
        with self._state_lock:
            return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        with self._state_lock:
            self._muted = bool(value)

    def set_speaking(self, value: bool):
        if value:
            self._speaking.set()
            self.ui.set_state("SPEAKING")
        else:
            self._speaking.clear()
            self.ui.set_state("LISTENING")

    def _build_system_prompt(self) -> str:
        """
        Build system prompt with time context, memory, tool descriptions,
        and screen context (V5 - from moondream2 vision).
        """
        mem_str = format_for_prompt()
        sys_prompt = _load_prompt()
        now = datetime.now().astimezone().strftime("%A, %B %d, %Y - %I:%M %p %Z")
        time_ctx = f"[CURRENT TIME] {now}\n\n"

        parts = [time_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        # Tool schemas are sent through providers.llm_with_tools().
        # Keep the runtime prompt limited to identity, memory, time, and state.

        # -- V5: Inject screen context from moondream2 vision --
        try:
            from core.providers import get_screen_context
            screen_ctx = get_screen_context()
            if screen_ctx:
                parts.append(screen_ctx)
        except Exception:
            pass

        return "\n".join(parts)

    # -- Tool Execution (kept from v3.0) -------------------------------------

    def _execute_tool_local(self, tool_name: str, params: dict) -> str:
        """Execute a tool call and return the result string."""
        log.info("Tool call: %s %s", tool_name, params)
        self.ui.set_state("THINKING", f"Running {tool_name}...")

        # -- V5.1: Track task in WorldState --
        task_desc = f"{tool_name}({', '.join(f'{k}={v}' for k, v in list(params.items())[:3])})"
        try:
            from core.world_state import get_world_state
            ws = get_world_state()
            if ws:
                ws.set_task(task_desc)
        except Exception:
            pass

        # -- V5.1: Snapshot WorldState BEFORE execution --
        before_snapshot = {}
        try:
            from core.validator import Validator
            _validator = Validator()
            before_snapshot = _validator.snapshot_before(tool_name)
        except Exception:
            _validator = None

        result = "Done."

        try:
            if tool_name == "open_app":
                result = open_app(params.get("app_name", ""))

            elif tool_name == "close_app":
                result = close_app(params.get("app_name", ""))

            elif tool_name == "system_control":
                result = system_control(
                    params.get("action", ""), params.get("value"))

            elif tool_name == "web_search":
                result = web_search(params.get("query", ""))

            elif tool_name == "file_manager":
                result = file_manager(
                    params.get("action", ""),
                    params.get("path", ""),
                    params.get("content", ""),
                    params.get("destination", ""),
                    params.get("new_name", ""),
                    params.get("query", ""),
                    params.get("extension", ""),
                    params.get("count", 10),
                    params.get("min_size_gb", 0.0))

            elif tool_name == "screen_vision":
                from actions.screen_vision import get_active_window_context
                win_ctx = get_active_window_context()
                result = f"Current screen context: {win_ctx}" if win_ctx else "Could not determine active window."

            elif tool_name == "run_command":
                result = run_command(params.get("command", ""))

            elif tool_name == "type_text":
                result = type_text(
                    params.get("text", ""),
                    params.get("hotkey"))

            elif tool_name == "clipboard":
                result = clipboard_action(
                    params.get("action", "read"),
                    params.get("text"))

            elif tool_name == "save_memory":
                result = remember(
                    params.get("category", "notes"),
                    params.get("key", ""),
                    params.get("value", ""))

            elif tool_name == "computer_control":
                cc_action = params.get("action", "")
                cc_kwargs = {k: v for k, v in params.items() if k != "action"}
                result = cc_tool(cc_action, **cc_kwargs)

            elif tool_name == "agent_task":
                goal = params.get("goal", "")
                log.info("Agent task: %s", goal[:80])
                self.ui.set_state("THINKING", f"Planning: {goal[:40]}...")

                # _execute_tool_local is the canonical tool runner.
                def _run_tool(tn, p):
                    return self._execute_tool_local(tn, p)

                # -- Phase 5 Tier 2: App Profile workflow (deterministic) --
                # Check if goal matches a known app workflow BEFORE hitting
                # the LLM planner. Instant, 100% reliable.
                plan = None
                plan_source = "llm"
                try:
                    from core.app_profiles import match_workflow
                    profile_plan = match_workflow(goal)
                    if profile_plan:
                        log.info("APP_PROFILE plan for: %s (%d steps)",
                                 goal[:60], len(profile_plan.get("steps", [])))
                        plan = profile_plan
                        plan_source = "app_profile"
                except Exception as e:
                    log.debug("App profile check failed: %s", e)

                # Check workflow cache
                if not plan:
                    try:
                        cached_plan = get_cached_workflow(goal)
                        if cached_plan:
                            log.info("Using cached workflow for: %s", goal[:60])
                            plan = cached_plan
                            plan_source = "cache"
                    except Exception:
                        pass

                # Tier 3: LLM planner (last resort)
                if not plan:
                    plan = create_plan(goal)
                    plan_source = "llm"

                if plan:
                    result = execute_plan(plan, _run_tool)
                    if result and "failed" not in result.lower():
                        try:
                            save_workflow(goal, plan)
                        except Exception:
                            pass
                else:
                    result = f"Could not create a plan for: {goal}"

            elif tool_name == "sleep_jarvis":
                self.awake = False
                self.ui.set_state("SLEEP", "")
                if hasattr(self.ui, "set_sleeping"):
                    self.ui.set_sleeping(True)
                result = "Jarvis is now sleeping."

            elif tool_name == "shutdown_jarvis":
                self.ui.set_state("IDLE")
                self._speak_text("Goodbye, sir.")
                def _exit():
                    time.sleep(1.5)
                    os._exit(0)
                threading.Thread(target=_exit, daemon=True).start()
                result = "Shutting down."

            # -- V5.1: Unified UI Controller (primary path) --
            elif tool_name == "ui_control":
                try:
                    from tools.ui_controller import UIController
                    result = UIController.interact(
                        action=params.get("action", ""),
                        target=params.get("target", ""),
                        window=params.get("window", ""),
                        text=params.get("text", ""),
                        menu_path=params.get("menu_path", ""),
                    )
                except ImportError:
                    result = "UI Controller not available (check tools/ui_controller.py)"
                except Exception as e:
                    result = f"ui_control failed: {e}"

            # -- V5: Legacy tools routed through UIController --
            elif tool_name == "click_element":
                try:
                    from tools.ui_controller import UIController
                    element = params.get("element_name", "")
                    window = params.get("window_title", "")
                    result = UIController.click_button(element, window)
                except ImportError:
                    result = "UI Controller not available"
                except Exception as e:
                    result = f"click_element failed: {e}"

            elif tool_name == "type_into":
                try:
                    from tools.ui_controller import UIController
                    element = params.get("element_name", "")
                    text = params.get("text", "")
                    window = params.get("window_title", "")
                    result = UIController.type_into_field(element, text, window)
                except ImportError:
                    result = "UI Controller not available"
                except Exception as e:
                    result = f"type_into failed: {e}"

            elif tool_name == "set_volume_precise":
                try:
                    from tools.win_automation import WinAutomation
                    wa = WinAutomation()
                    level = int(params.get("level", 50))
                    result = wa.set_volume(level)
                except ImportError:
                    result = "Volume control not available (pip install pycaw)"
                except Exception as e:
                    result = f"set_volume failed: {e}"

            else:
                result = f"Unknown tool: {tool_name}"

        except Exception as e:
            result = f"Tool '{tool_name}' failed: {e}"
            log.error("Tool %s failed: %s", tool_name, e, exc_info=True)

        # -- V5.1: Clear task in WorldState --
        try:
            from core.world_state import get_world_state
            ws = get_world_state()
            if ws:
                ws.clear_task()
        except Exception:
            pass

        # -- V5.1: Post-execution validation --
        # Concept: Observe -> Execute -> Verify. The validator checks
        # if the expected state transition actually occurred.
        validation_dict = {}
        try:
            if _validator is not None:
                validation = _validator.verify(tool_name, params, str(result), before_snapshot)
                validation_dict = {
                    "success": validation.success,
                    "confidence": validation.confidence,
                    "failure_type": getattr(validation.failure_type, 'value', ''),
                    "detail": validation.detail[:100],
                }
                if validation.success:
                    log.info("Validated %s: %s (conf=%.2f, method=%s)",
                             tool_name, validation.detail[:60],
                             validation.confidence, validation.verification_method)
                else:
                    log.warning("Validation failed for %s: %s (type=%s, conf=%.2f)",
                                tool_name, validation.detail[:60],
                                validation.failure_type.value, validation.confidence)
                # Append verification info to result for LLM context
                result = str(result) + validation.to_result_string()
        except Exception as ve:
            log.debug("Validation skipped for %s: %s", tool_name, ve)

        # -- Phase 5: Log to FailureLogger for training data --
        # Every execution (success or failure) is logged as structured
        # JSONL for future fine-tuning and regression analysis.
        try:
            flogger = _get_flogger()
            if flogger and validation_dict:
                if validation_dict.get("success"):
                    from core.failure_logger import log_tool_success
                    log_tool_success(
                        tool_name=tool_name,
                        params=params,
                        result=str(result),
                        validation=validation_dict,
                    )
                else:
                    from core.failure_logger import log_tool_failure
                    log_tool_failure(
                        tool_name=tool_name,
                        params=params,
                        result=str(result),
                        validation=validation_dict,
                        user_input=task_desc,
                    )
        except Exception as fle:
            log.debug("FailureLogger error: %s", fle)

        log.info("%s -> %s", tool_name, str(result)[:100])
        return result

    # -- Voice Pipeline ------------------------------------------------------

    def _record_until_silence(self) -> bytes:
        """
        Record audio from mic until silence is detected.

        Uses Voice Activity Detection (VAD): stops recording after
        SILENCE_DURATION seconds of consecutive silence.

        Returns:
            Raw PCM bytes (int16, 16kHz, mono)
        """
        log.info("Recording started...")
        self.ui.set_state("LISTENING", "Listening...")

        audio_chunks = []
        silence_start = None
        record_start = time.time()

        def callback(indata, frames, time_info, status):
            nonlocal silence_start

            if self._speaking.is_set() or self.muted:
                return

            data = indata.tobytes()
            audio_chunks.append(data)

            # Compute RMS for UI waveform
            try:
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(samples ** 2)))
                self.rms_levels.append(min(1.0, (rms / 32768.0) * 5.0))
            except Exception:
                self.rms_levels.append(0.0)

            # VAD: detect silence
            try:
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(samples ** 2)))

                if rms < SILENCE_THRESHOLD:
                    if silence_start is None:
                        silence_start = time.time()
                else:
                    silence_start = None  # Reset on speech
            except Exception:
                pass

        with sd.InputStream(
            samplerate=SEND_SAMPLE_RATE, channels=CHANNELS,
            dtype="int16", blocksize=CHUNK_SIZE, callback=callback
        ):
            while True:
                time.sleep(0.05)

                elapsed = time.time() - record_start

                # Don't stop too early
                if elapsed < MIN_RECORD_TIME:
                    continue

                # Stop on sustained silence
                if silence_start and (time.time() - silence_start) >= SILENCE_DURATION:
                    break

                # Safety cap: 30 seconds max recording
                if elapsed > 30:
                    log.warning("Max recording time reached (30s)")
                    break

        pcm_bytes = b"".join(audio_chunks)
        duration = len(pcm_bytes) / (SEND_SAMPLE_RATE * 2)  # 2 bytes per sample
        log.info("Recording stopped: %.1fs, %d bytes", duration, len(pcm_bytes))

        return pcm_bytes

    def _transcribe(self, pcm_bytes: bytes) -> str:
        """Transcribe PCM audio to text using faster-whisper."""
        self.ui.set_state("THINKING", "Transcribing...")

        from core.stt import transcribe_pcm
        text = transcribe_pcm(
            pcm_bytes,
            sample_rate=SEND_SAMPLE_RATE,
            context_hint="command",  # Phase 2: voice commands, speed priority
        )

        if text:
            log.info("[You] %s", text)
            try:
                log_conversation("user", text)
            except Exception:
                pass

        return text

    def _think(self, user_text: str) -> str:
        """
        Core LLM reasoning pipeline with 3-tier routing (Phase 5).

        Tier 1: IntentRouter - regex match for obvious commands (<0.1ms)
        Tier 2: AppProfiles - deterministic workflow for known app patterns
        Tier 3: LLM - only for truly novel or ambiguous requests

        Flow:
          0. Try IntentRouter fast-path (media, launch, system, memory)
          1. Build messages + system prompt
          2. Call llm_with_tools() - API decides: tool_call or text
          3. If tool_call -> execute -> feed result back -> get follow-up
          4. If text -> speak via streaming or _speak_text

        Returns:
            The final text response to speak (empty string for tool-only responses)
        """
        # =====================================================================
        # TIER 1: Intent Router Fast-Path
        # Catches obvious commands (pause, open X, volume 50) in pure Python
        # regex. Bypasses LLM entirely for ~0.02ms response.
        # =====================================================================
        router = _get_router()
        if router:
            _rt_start = time.perf_counter()
            route = router.route(user_text)
            _rt_ms = (time.perf_counter() - _rt_start) * 1000

            if route:
                tool_name = route["tool"]
                tool_params = route["params"]
                log.info("INTENT_ROUTER [%.2fms]: %s -> %s",
                         _rt_ms, user_text[:50], tool_name)

                # Log routing decision to FailureLogger
                flogger = _get_flogger()
                if flogger:
                    flogger.log_routing({
                        "user_input": user_text,
                        "route": "intent_router",
                        "tool": tool_name,
                        "params": tool_params,
                        "reason": "regex_match",
                        "latency_ms": round(_rt_ms, 3),
                    })

                # Execute directly - no LLM needed
                try:
                    result = self._execute_tool_local(tool_name, tool_params)

                    # For sleep/shutdown, don't speak
                    if tool_name in ("sleep_jarvis", "shutdown_jarvis"):
                        return ""

                    # Generate a brief spoken confirmation
                    confirmations = {
                        "system_control": "Done.",
                        "open_app": f"Opening {tool_params.get('app_name', 'app')}.",
                        "close_app": f"Closing {tool_params.get('app_name', 'app')}.",
                        "save_memory": "Got it, I'll remember that.",
                    }
                    response = confirmations.get(tool_name, "Done.")

                    with self._history_lock:
                        self._history.append({"role": "user", "content": user_text})
                        self._history.append({"role": "assistant", "content": response})

                    return response

                except Exception as e:
                    log.warning("Intent router exec failed, falling to LLM: %s", e)
                    # Fall through to LLM on failure

        # =====================================================================
        # TIER 3: Full LLM Reasoning (Native Function Calling)
        # =====================================================================
        self.ui.set_state("THINKING", "Thinking...")

        system_prompt = self._build_system_prompt()

        # Build conversation messages in chat format
        messages = []
        with self._history_lock:
            history_snapshot = list(self._history[-self._max_history:])
        for turn in history_snapshot:
            messages.append({
                "role": turn["role"],
                "content": turn["content"],
            })
        messages.append({"role": "user", "content": user_text})

        # Add to history (thread-safe)
        with self._history_lock:
            self._history.append({"role": "user", "content": user_text})
            if len(self._history) > self._max_history * 2:
                self._history = self._history[-self._max_history:]

        # -- Native function calling - all providers --
        from core.providers import llm_with_tools
        max_tool_rounds = 5
        response_text = ""

        for round_num in range(max_tool_rounds):
            try:
                llm_result = llm_with_tools(
                    messages=messages,
                    system=system_prompt,
                    temperature=0.4,
                )
            except Exception as e:
                log.error("LLM with_tools failed: %s", e)
                return "I'm sorry, I'm having trouble thinking right now."

            if llm_result["type"] == "text":
                # No tool call - conversational response
                response_text = llm_result["content"]
                break

            # -- Tool call detected --
            tc_tool_name = llm_result["name"]
            tc_tool_params = llm_result["arguments"]
            pre_text = llm_result.get("pre_text", "")

            log.info("Native tool call: %s(%s)", tc_tool_name, tc_tool_params)

            # Speak any acknowledgment text before executing
            if pre_text and pre_text.strip():
                self._speak_text(pre_text.strip())

            # Execute the tool
            try:
                tool_result = self._execute_tool_local(tc_tool_name, tc_tool_params)
            except Exception as e:
                log.error("Tool execution failed: %s", e)
                response_text = f"The action failed: {e}"
                break

            # If it was sleep/shutdown, don't continue
            if tc_tool_name in ("sleep_jarvis", "shutdown_jarvis"):
                return ""

            # Feed tool result back to LLM for follow-up
            messages.append({
                "role": "assistant",
                "content": f"[Executed {tc_tool_name}] Result: {tool_result}",
            })
            messages.append({
                "role": "user",
                "content": "Now respond to the user based on the tool result. Be brief.",
            })

        # Save to history (thread-safe)
        response_text = self._sanitize_for_speech(response_text)
        with self._history_lock:
            self._history.append({"role": "assistant", "content": response_text[:500]})

        return response_text

    def _legacy_think_xml_fallback(self, full_prompt: str, system_prompt: str) -> str:
        """
        Legacy XML-based tool calling - ONLY used for Ollama (no native tools).

        Concept: Ollama doesn't support the `tools` API parameter, so we
        fall back to the old method: inject tool descriptions in the prompt,
        hope the model generates <tool_call> XML, and regex-parse it.
        This is less reliable but it's the only option for local models.
        """
        from core.providers import llm_generate

        max_tool_rounds = 5
        response = ""

        for round_num in range(max_tool_rounds):
            try:
                response = llm_generate(
                    prompt=full_prompt,
                    system=system_prompt,
                    temperature=0.4,
                )
            except Exception as e:
                log.error("LLM failed: %s", e)
                return "I'm sorry, I'm having trouble thinking right now."

            tool_match = re.search(
                r"<tool_call>\s*({.*?})\s*</tool_call>",
                response,
                re.DOTALL
            )

            if not tool_match:
                # No tool call - this is the final response
                response = re.sub(r"</?tool_call>", "", response).strip()
                break

            # Parse and execute tool call
            try:
                tool_json = json.loads(tool_match.group(1))
                tool_name = tool_json.get("tool", "")
                tool_params = tool_json.get("parameters", {})

                # Extract any text before the tool call (acknowledgment)
                pre_text = response[:tool_match.start()].strip()
                if pre_text:
                    self._speak_text(pre_text)

                # Execute the tool
                xml_result = self._execute_tool_local(tool_name, tool_params)

                # If it was sleep/shutdown, don't continue
                if tool_name in ("sleep_jarvis", "shutdown_jarvis"):
                    return ""

                # Feed result back to LLM for next round
                full_prompt += f"\nJarvis: [Executed {tool_name}]\nTool result: {xml_result}\nNow respond to the user based on the tool result."

            except json.JSONDecodeError as e:
                log.error("Failed to parse tool call JSON: %s", e)
                response = "I tried to run a command but something went wrong. Could you try again?"
                break
            except Exception as e:
                log.error("Tool execution failed: %s", e)
                response = f"The action failed: {e}"
                break

        # Save to history (thread-safe) - save the clean version
        response = self._sanitize_for_speech(response)
        with self._history_lock:
            self._history.append({"role": "assistant", "content": response[:500]})

        return response

    def _speak_text(self, text: str):
        """
        Convert text to speech and play it.

        Uses core.tts.speak_now() which handles the full fallback chain:
          Kokoro (local) -> edge-tts (network) -> pyttsx3 (SAPI5)
        """
        if not text or not text.strip():
            return

        # Sanitize BEFORE speaking or displaying in the UI.
        text = self._sanitize_for_speech(text)
        if not text:
            return

        log.info("[Jarvis] %s", text[:100])
        self.ui.set_speak_text(text)

        try:
            log_conversation("jarvis", text)
        except Exception:
            pass

        self.ui.set_state("SPEAKING")
        self.set_speaking(True)

        try:
            from core.tts import speak_now
            speak_now(text)
        except Exception as e:
            log.error("TTS failed: %s", e)
        finally:
            self.set_speaking(False)
            self._last_spoke_time = time.time()  # for post-speech buffer

    @staticmethod
    def _sanitize_for_speech(text: str) -> str:
        """
        Strip all non-speakable artifacts from LLM output.

        Concept: The LLM may embed <tool_call> JSON blocks, markdown
        formatting, code fences, URLs, or internal function references
        in its response. None of these should be spoken aloud or shown
        in the UI bubble.
        """
        if not text:
            return ""

        # Tool-call artifacts (primary fix for the spoken-JSON bug)
        text = re.sub(r'<tool_call>[\s\S]*?</tool_call>', '', text)
        text = re.sub(r'</?tool_call>', '', text)           # orphaned tags
        text = re.sub(r'_call_\w+', '', text)               # _call_goal etc.
        text = re.sub(r'\{[^}]*"tool"[^}]*\}', '', text)   # orphaned JSON

        # Markdown artifacts (visual-only, meaningless when spoken)
        text = re.sub(r'```[\s\S]*?```', 'code block', text)
        text = re.sub(r'`[^`]+`', '', text)
        text = re.sub(r'\*+', '', text)
        text = re.sub(r'#+\s*', '', text)
        text = re.sub(r'https?://\S+', 'link', text)
        text = re.sub(r'\n+', ' ', text)

        return text.strip()

    # -- Wake Word + Main Loop -----------------------------------------------
    # Wake Word: _check_wake_word() removed in Phase 1 (P1-2).
    # Detection now runs in core/wake_word.py; events wired via _on_wake_word().

    def _check_deactivation(self, text: str) -> bool:
        """Check if the user wants Jarvis to go into passive sleep mode.

        Sleep mode means: stop responding to voice, but keep watching
        the screen and noting context so Jarvis has awareness when
        the user wakes it up again.
        """
        text_lower = text.lower().strip()
        _DEACT = [
            "go to sleep", "sleep mode", "jarvis sleep",
            "goodnight jarvis", "good night",
            "bye jarvis", "stop listening",
            "jarvis stop", "jarvis deactivate",
            "deactivate",
        ]
        # Also match bare "sleep" as a standalone command
        if text_lower in ("sleep", "sleep."):
            return True
        return any(p in text_lower for p in _DEACT)

    def _enter_sleep_mode(self):
        """Enter passive sleep mode.

        In sleep mode Jarvis:
          - Stops responding to voice
          - Keeps watching the screen (ContextManager + WorldState keep running)
          - Saves a snapshot of current context so it has awareness on wake
          - Waits for wake word to resume
        """
        self.awake = False
        self.ui.set_state("SLEEP", "")
        if hasattr(self.ui, "set_sleeping"):
            self.ui.set_sleeping(True)

        # Snapshot current screen/app context for when user wakes us up
        try:
            from core.world_state import get_world_state
            ws = get_world_state()
            if ws:
                snap = ws.snapshot()
                active = snap.get("desktop", {}).get("active_window", "unknown")
                log.info("SLEEP MODE: Watching screen (active: %s). "
                         "WorldState + ContextManager still running.", active)
                # Store what was happening when user said sleep
                self._sleep_context = {
                    "entered_at": time.time(),
                    "active_app": active,
                    "snapshot": snap,
                }
        except Exception as e:
            log.debug("Could not snapshot on sleep: %s", e)
            self._sleep_context = {"entered_at": time.time()}

    async def _voice_loop(self):
        """
        Main voice interaction loop.

        Behavior:
        - Once activated by wake word, Jarvis stays awake FOREVER
        - Empty transcriptions are silently ignored (no auto-sleep)
        - Only explicit 'sleep' commands trigger passive sleep mode
        - In sleep mode: screen watching continues, wake word re-activates

        Flow:
        1. Subscribe to WAKE_WORD_DETECTED from core/wake_word.py
        2. Wait until awake (event sets self.awake = True)
        3. Record until silence (VAD)
        4. Transcribe (faster-whisper)
        5. If empty -> silently continue listening (NO sleep)
        6. If 'sleep' command -> enter passive mode
        7. Think (Groq/Gemini) -> Speak (TTS)
        8. Go back to step 3 (stay awake)
        """
        log.info("Voice loop started - subscribing to Event Bus")
        loop = asyncio.get_event_loop()

        # Subscribe to wake word events from WakeWordDetector.
        try:
            from core.events import get_bus, Event
            _bus = get_bus()
            _bus.on(Event.WAKE_WORD_DETECTED,  self._on_wake_word)
            _bus.on(Event.DEACTIVATE_REQUESTED, self._on_deactivate)
            log.info("Event subscriptions: WAKE_WORD_DETECTED, DEACTIVATE_REQUESTED")
        except Exception as e:
            # Event bus unavailable - start in always-awake fallback mode
            log.warning("Event bus unavailable (%s) - starting always-awake", e)
            self.awake = True
            self.ui.set_state("LISTENING", "Listening...")

        # Continuous mic stream for RMS levels only.
        def wake_callback(indata, frames, time_info, status):
            if self._speaking.is_set() or self.muted:
                self.rms_levels.append(0.0)
                return
            try:
                samples = np.frombuffer(indata, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(samples ** 2))) / 32768.0
                self.rms_levels.append(min(1.0, rms * 5.0))
            except Exception:
                self.rms_levels.append(0.0)

        with sd.InputStream(
            samplerate=SEND_SAMPLE_RATE, channels=CHANNELS,
            dtype="int16", blocksize=CHUNK_SIZE, callback=wake_callback
        ):
            while True:
                await asyncio.sleep(0.1)

                if not self.awake:
                    continue

                if self.muted:
                    continue

                # We're awake - record, transcribe, think, speak
                try:
                    # Stamp a new correlation ID for this voice turn
                    try:
                        from core.logger import new_turn_id
                        new_turn_id()
                    except Exception:
                        pass

                    # -- Post-speech buffer --
                    # After Jarvis speaks, give the user 1.5s to start
                    # talking before we begin recording. Without this,
                    # we record pure silence and waste a STT cycle.
                    time_since_spoke = time.time() - getattr(self, '_last_spoke_time', 0)
                    if time_since_spoke < 1.5:
                        wait = 1.5 - time_since_spoke
                        log.debug("Post-speech buffer: waiting %.1fs", wait)
                        await asyncio.sleep(wait)

                    # Step 1: Record until silence
                    pcm_bytes = await loop.run_in_executor(
                        None, self._record_until_silence
                    )

                    if not pcm_bytes or len(pcm_bytes) < 3200:
                        continue  # too short, ignore

                    # Step 2: Transcribe
                    user_text = await loop.run_in_executor(
                        None, self._transcribe, pcm_bytes
                    )

                    # -- Empty transcription: silently continue --
                    # Jarvis stays awake. No auto-sleep. Ever.
                    # Only explicit 'sleep' command puts Jarvis to sleep.
                    if not user_text or not user_text.strip():
                        log.debug("Empty transcription - still listening")
                        continue

                    # -- Check for explicit sleep command --
                    if self._check_deactivation(user_text):
                        log.info("Sleep command: '%s'", user_text)
                        self._speak_text("Going to sleep. I'll keep watching. Say Jarvis to wake me.")
                        self._enter_sleep_mode()
                        continue

                    # Step 3: Think (LLM)
                    response = await loop.run_in_executor(
                        None, self._think, user_text
                    )

                    # Step 4: Speak
                    if response and response.strip():
                        await loop.run_in_executor(
                            None, self._speak_text, response
                        )

                    # Stay awake for follow-up (always)
                    self.last_awake_time = time.time()

                except Exception as e:
                    log.error("Voice loop error: %s", e, exc_info=True)
                    self.ui.set_state("ERROR", "Something went wrong")
                    await asyncio.sleep(1)
                    # Stay awake even after errors - never auto-sleep
                    self.ui.set_state("LISTENING")
                finally:
                    try:
                        from core.logger import clear_turn_id
                        clear_turn_id()
                    except Exception:
                        pass

    # -- Main Run Loop -------------------------------------------------------

    async def run(self):
        """Start the local voice pipeline."""
        log.info("Starting Jarvis Engine v4.0 - Local-First Pipeline")
        log.info("   STT: faster-whisper (local)")
        log.info("   LLM: Ollama / Groq (local/free)")
        log.info("   TTS: edge-tts (free)")

        self.ui.set_state("LISTENING")
        self._loop = asyncio.get_event_loop()

        # Check Ollama availability
        try:
            import httpx
            resp = httpx.get("http://localhost:11434/api/tags", timeout=5.0)
            if resp.status_code == 200:
                models = [m["name"] for m in resp.json().get("models", [])]
                log.info("Ollama available with models: %s", models[:5])
            else:
                log.warning("Ollama responded with status %d", resp.status_code)
        except Exception as e:
            log.warning("Ollama not reachable: %s. Will try Groq fallback.", e)

        # Pre-load STT model in background
        def _preload_stt():
            try:
                from core.stt import _get_model
                _get_model()
            except Exception as e:
                log.warning("STT preload failed: %s", e)

        threading.Thread(target=_preload_stt, daemon=True).start()

        # Start voice loop
        await self._voice_loop()

    # -- Event Bus Handlers --------------------------------------------------
    # Called by the Event Bus thread. Only set flags - never block or do I/O.

    def _on_wake_word(self, data: dict) -> None:
        """Handle WAKE_WORD_DETECTED emitted by core/wake_word.py WakeWordDetector."""
        if not self.awake:
            # Build context from what happened during sleep
            sleep_ctx = getattr(self, '_sleep_context', {})
            sleep_dur = 0
            if sleep_ctx.get('entered_at'):
                sleep_dur = time.time() - sleep_ctx['entered_at']

            log.info("Wake word -> activating (was asleep for %.0fs)", sleep_dur)

            # Inject sleep observations into history so LLM has context
            if sleep_dur > 5 and sleep_ctx:
                try:
                    from core.world_state import get_world_state
                    ws = get_world_state()
                    current_snap = ws.snapshot() if ws else {}
                    active_app = current_snap.get('desktop', {}).get('active_window', 'unknown')
                    sleep_note = (f"[System note: You were in sleep mode for "
                                  f"{int(sleep_dur)}s. User is now on: {active_app}. "
                                  f"They just called you back.]")
                    with self._history_lock:
                        self._history.append({"role": "system", "content": sleep_note})
                    log.info("Injected sleep context: %s", sleep_note[:80])
                except Exception as e:
                    log.debug("Sleep context injection failed: %s", e)

            self.awake = True
            self._sleep_context = {}  # clear
            self.last_awake_time = time.time()
            self.ui.set_state("LISTENING", "Listening...")
            if hasattr(self.ui, "set_sleeping"):
                self.ui.set_sleeping(False)

    def _on_deactivate(self, data: dict) -> None:
        """Handle DEACTIVATE_REQUESTED emitted by core/wake_word.py."""
        if self.awake:
            phrase = data.get("phrase", "") if isinstance(data, dict) else ""
            log.info("Deactivate event (phrase='%s') -> sleeping", phrase)
            self.awake = False
            self.ui.set_state("SLEEP", "")
            if hasattr(self.ui, "set_sleeping"):
                self.ui.set_sleeping(True)
