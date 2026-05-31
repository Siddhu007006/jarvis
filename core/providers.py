"""
core/providers.py — Unified LLM Provider Interface.

Routes LLM calls to the fastest available provider:
  1. Groq   (free cloud, ~500 tok/s, sub-1s)   — if GROQ_API_KEY env var set
  2. Gemini (free tier,  ~200 tok/s, ~1-2s)     — if GEMINI_API_KEY env var set
  3. Ollama (local,      ~10 tok/s,  slow)       — offline fallback

Concept: All LLM calls go through llm_generate() or llm_generate_json().
API keys are loaded from the .env file (never from JSON config).
Switching providers = changing config/settings.json.
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

# Load .env before anything else so os.environ has the keys
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass  # python-dotenv not installed; fall back to JSON config

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
SETTINGS_PATH = BASE_DIR / "config" / "settings.json"
API_KEYS_PATH = BASE_DIR / "config" / "api_keys.json"

# ── Singletons (lazy init, all guarded by locks) ───────────────
_router_instance  = None
_router_lock      = threading.Lock()
_context_manager_instance = None

# ── Gemini client singleton ────────────────────────────────────
_gemini_client      = None
_gemini_client_lock = threading.Lock()


def _get_router():
    """Lazy singleton for the LLM Router. Thread-safe."""
    global _router_instance
    if _router_instance is None:
        with _router_lock:
            if _router_instance is None:  # double-checked locking
                try:
                    from core.llm_router import LLMRouter
                    settings = _load_settings()
                    _router_instance = LLMRouter(
                        fast_model=settings.get("fast_ollama_model", "phi3:mini"),
                        smart_model=settings.get("ollama_model", "llama3"),
                    )
                except Exception as e:
                    log.warning("LLM Router unavailable: %s", e)
                    class _DummyRouter:
                        def choose(self, *a, **kw):
                            return {"model": "", "provider": "ollama", "reason": "no router"}
                        def record_latency(self, *a):
                            pass
                    _router_instance = _DummyRouter()
    return _router_instance


def set_context_manager(cm):
    """Called by main.py to register the ContextManager singleton."""
    global _context_manager_instance
    _context_manager_instance = cm


def get_screen_context() -> str:
    """
    Get current context injection string for LLM system prompt.

    V5.1: Prefers WorldState's richer injection (includes active window,
    workflow, clipboard, browser URL, etc.) over the old ContextManager's
    screen-only injection. Falls back to ContextManager if WorldState
    is not initialized yet.
    """
    # V5.1: Try WorldState first (richer context)
    try:
        from core.world_state import get_world_context
        world_ctx = get_world_context()
        if world_ctx:
            return world_ctx
    except Exception:
        pass

    # Fallback: old ContextManager (screen context only)
    if _context_manager_instance is not None:
        return _context_manager_instance.build_screen_injection()
    return ""

# ── Config Loading ─────────────────────────────────────────────

_settings_cache = None
_settings_mtime = 0.0
_settings_lock  = threading.Lock()


def _load_settings() -> dict:
    """
    Load provider settings with file-level caching.
    Thread-safe: concurrent callers share one cached copy.

    NOTE: API keys (groq_api_key, gemini_api_key) are intentionally
    NOT stored in settings.json. They are loaded from the .env file
    via os.environ at module import time.
    """
    global _settings_cache, _settings_mtime

    defaults = {
        "llm_provider": "groq",
        "llm_model": "llama-3.3-70b-versatile",
        "ollama_url": "http://localhost:11434",
        "planner_provider": "groq",
        "planner_model": "llama-3.3-70b-versatile",
        "vision_provider": "ollama",
        "vision_model": "moondream",
        "groq_model": "llama-3.3-70b-versatile",
        "fallback_to_gemini": True,
        "fallback_to_ollama": True,
        "ollama_model": "qwen2.5-coder:3b",
        "fast_ollama_model": "qwen2.5-coder:3b",
    }

    with _settings_lock:
        try:
            mtime = SETTINGS_PATH.stat().st_mtime
            if _settings_cache is not None and mtime == _settings_mtime:
                return _settings_cache
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            # Strip any accidental key leaks from old settings files
            data.pop("groq_api_key", None)
            data.pop("gemini_api_key", None)
            defaults.update(data)
            _settings_cache = defaults
            _settings_mtime = mtime
        except Exception:
            pass
        return defaults


def _ollama_options(temperature: float, num_predict: int) -> dict:
    """Common Ollama sampling options tuned for Jarvis's short, structured turns."""
    return {
        "temperature": temperature,
        "top_p": 0.9,
        "top_k": 40,
        "repeat_penalty": 1.1,
        "num_predict": num_predict,
    }


# ── Key helpers — env var first, JSON file fallback ───────────
# Keys are cached at first access; never read from disk on hot paths.
_groq_key_cache   = None
_gemini_key_cache = None
_key_cache_lock   = threading.Lock()


def _get_groq_key() -> str:
    """
    Load Groq API key. Priority:
      1. GROQ_API_KEY environment variable (.env file)
      2. groq_api_key field in config/settings.json (legacy, deprecated)
    Cached after first read — no repeated disk I/O.
    """
    global _groq_key_cache
    with _key_cache_lock:
        if _groq_key_cache is not None:
            return _groq_key_cache
        # 1. Env var (preferred)
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            # 2. Legacy JSON fallback (read once, log deprecation warning)
            try:
                raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                key = raw.get("groq_api_key", "").strip()
                if key:
                    log.warning(
                        "DEPRECATION: groq_api_key found in settings.json. "
                        "Move it to GROQ_API_KEY in your .env file."
                    )
            except Exception:
                pass
        _groq_key_cache = key
        return key


def _get_gemini_key() -> str:
    """
    Load Gemini API key. Priority:
      1. GEMINI_API_KEY environment variable (.env file)
      2. gemini_api_key field in config/api_keys.json (legacy, deprecated)
    Cached after first read.
    """
    global _gemini_key_cache
    with _key_cache_lock:
        if _gemini_key_cache is not None:
            return _gemini_key_cache
        # 1. Env var (preferred)
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            # 2. Legacy JSON fallback
            try:
                key = json.loads(
                    API_KEYS_PATH.read_text(encoding="utf-8")
                ).get("gemini_api_key", "").strip()
                if key:
                    log.warning(
                        "DEPRECATION: gemini_api_key found in api_keys.json. "
                        "Move it to GEMINI_API_KEY in your .env file."
                    )
            except Exception:
                pass
        _gemini_key_cache = key
        return key


def _get_gemini_client():
    """
    Singleton Gemini genai.Client.
    Creating the client is ~50ms (SDK init). Reuse it across all calls.
    Thread-safe via double-checked locking.
    """
    global _gemini_client
    if _gemini_client is None:
        with _gemini_client_lock:
            if _gemini_client is None:
                api_key = _get_gemini_key()
                if not api_key:
                    raise ValueError(
                        "No Gemini API key. Set GEMINI_API_KEY in your .env file."
                    )
                from google import genai
                _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


# ── Ollama Provider (Offline Fallback) ─────────────────────────

def _ollama_generate(prompt: str, system: str = "", model: str = "",
                     format_json: bool = False, images: list = None,
                     temperature: float = 0.3) -> str:
    """
    Call Ollama's local HTTP API at localhost:11434.
    Used as offline fallback when cloud APIs are unavailable.
    """
    import httpx

    settings = _load_settings()
    url = settings.get("ollama_url", "http://localhost:11434")
    if not model:
        model = settings.get("ollama_model", settings.get("llm_model", "qwen2.5-coder:3b"))

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": _ollama_options(
            temperature,
            512 if format_json else 256,
        ),
    }

    if system:
        payload["system"] = system
    if format_json:
        payload["format"] = "json"
    if images:
        payload["images"] = images

    try:
        t0 = time.time()
        resp = httpx.post(
            f"{url}/api/generate",
            json=payload,
            timeout=180.0,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("response", "").strip()
        elapsed = time.time() - t0
        log.info("🦙 Ollama [%s]: %d chars in %.1fs", model, len(text), elapsed)
        return text

    except httpx.ConnectError:
        log.warning("Ollama not reachable at %s", url)
        raise ConnectionError(f"Ollama not running at {url}")
    except Exception as e:
        log.error("Ollama generate failed: %s", e)
        raise


# ── Groq Provider (Fastest Free Cloud) ─────────────────────────

def _groq_generate(prompt: str, system: str = "", model: str = "",
                   format_json: bool = False, temperature: float = 0.3) -> str:
    """
    Call Groq's free API. Extremely fast (~500 tokens/sec).
    Free tier: 30 RPM, 14,400 RPD for Llama 3.3 70B.
    """
    import httpx

    settings = _load_settings()
    api_key = _get_groq_key()
    if not api_key:
        raise ValueError(
            "No Groq API key found. Set GROQ_API_KEY in your .env file."
        )

    if not model:
        model = settings.get("groq_model", "llama-3.3-70b-versatile")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 2048,
    }
    if format_json:
        payload["response_format"] = {"type": "json_object"}

    try:
        t0 = time.time()
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        elapsed = time.time() - t0
        log.info("⚡ Groq [%s]: %d chars in %.1fs", model, len(text), elapsed)
        return text

    except Exception as e:
        log.error("Groq generate failed: %s", e)
        raise


# ── Gemini Provider (Fast Cloud, Free Tier) ────────────────────

def _gemini_generate(prompt: str, system: str = "", model: str = "gemini-2.5-flash",
                     format_json: bool = False, temperature: float = 0.3,
                     images: list = None) -> str:
    """
    Call Gemini API. Fast (~1-2s for short prompts).
    Free tier: 30 RPM, 1,500 RPD.

    Uses singleton client — no SDK init overhead on repeated calls.
    """
    from google.genai import types as gtypes
    client = _get_gemini_client()

    contents = []
    if images:
        import base64
        for img_b64 in images:
            img_bytes = base64.b64decode(img_b64)
            contents.append(gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
    contents.append(prompt)

    config = {
        "system_instruction": system,
        "temperature": temperature,
    }
    if format_json:
        config["response_mime_type"] = "application/json"

    try:
        t0 = time.time()
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        text = (response.text or "").strip()
        elapsed = time.time() - t0
        log.info("🔷 Gemini [%s]: %d chars in %.1fs", model, len(text), elapsed)
        return text

    except Exception as e:
        log.error("Gemini generate failed: %s", e)
        raise


# ── Streaming Generators (yield tokens one at a time) ──────────

def _ollama_stream(prompt: str, system: str = "", model: str = "",
                   temperature: float = 0.3):
    """
    Stream tokens from local Ollama using httpx.

    Concept: Ollama's /api/generate endpoint supports streaming via
    NDJSON — each line is a JSON object with a "response" field containing
    one token. We yield each token as it arrives, enabling the streaming
    pipeline to start TTS on the first complete sentence.
    """
    import httpx

    settings = _load_settings()
    url = settings.get("ollama_url", "http://localhost:11434")
    if not model:
        model = settings.get("ollama_model", settings.get("llm_model", "qwen2.5-coder:3b"))

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": _ollama_options(temperature, 1024),
    }
    if system:
        payload["system"] = system

    t0 = time.time()
    total_tokens = 0

    with httpx.stream("POST", f"{url}/api/generate", json=payload, timeout=180.0) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
                token = data.get("response", "")
                if token:
                    total_tokens += 1
                    yield token
                if data.get("done", False):
                    break
            except json.JSONDecodeError:
                continue

    elapsed = time.time() - t0
    log.info("🦙 Ollama stream [%s]: %d tokens in %.1fs", model, total_tokens, elapsed)


def _groq_stream(prompt: str, system: str = "", model: str = "",
                 temperature: float = 0.3):
    """
    Stream tokens from Groq API using httpx SSE.

    Concept: Groq's chat completions endpoint supports streaming via
    Server-Sent Events (SSE). Each event contains a "delta" with one
    token. We parse the SSE format and yield each content delta.
    """
    import httpx

    settings = _load_settings()
    api_key = _get_groq_key()
    if not api_key:
        raise ValueError(
            "No Groq API key found. Set GROQ_API_KEY in your .env file."
        )
    if not model:
        model = settings.get("groq_model", "llama-3.3-70b-versatile")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 2048,
        "stream": True,
    }

    t0 = time.time()
    total_tokens = 0

    with httpx.stream(
        "POST",
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30.0,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]  # Strip "data: " prefix
            if data_str.strip() == "[DONE]":
                break
            try:
                data = json.loads(data_str)
                delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if delta:
                    total_tokens += 1
                    yield delta
            except json.JSONDecodeError:
                continue

    elapsed = time.time() - t0
    log.info("⚡ Groq stream [%s]: %d tokens in %.1fs", model, total_tokens, elapsed)


def _gemini_stream(prompt: str, system: str = "", model: str = "gemini-2.5-flash",
                   temperature: float = 0.3):
    """
    Stream tokens from Gemini API using google.genai streaming.

    Concept: The google.genai SDK supports streaming via
    stream=True in generate_content(). It yields response chunks,
    each containing partial text.

    Uses singleton client — no SDK init overhead on repeated calls.
    """
    client = _get_gemini_client()

    t0 = time.time()
    total_tokens = 0

    try:
        response = client.models.generate_content_stream(
            model=model,
            contents=prompt,
            config={
                "system_instruction": system,
                "temperature": temperature,
            },
        )

        for chunk in response:
            text = chunk.text or ""
            if text:
                total_tokens += 1
                yield text

    except Exception as e:
        log.error("Gemini stream failed: %s", e)
        raise

    elapsed = time.time() - t0
    log.info("🔷 Gemini stream [%s]: %d chunks in %.1fs", model, total_tokens, elapsed)


# ── Native Function Calling ───────────────────────────────────
# Concept: Instead of injecting tool descriptions as text and hoping
# the LLM generates <tool_call> XML (unreliable), we use each provider's
# native function calling API. The model was fine-tuned for this format,
# so tool selection is ~10x more reliable.

_openai_tools_cache = None
_openai_tools_lock = threading.Lock()

_TYPE_MAP = {
    "STRING": "string", "INTEGER": "integer", "NUMBER": "number",
    "BOOLEAN": "boolean", "OBJECT": "object", "ARRAY": "array",
}


def _to_openai_tools(declarations: list) -> list:
    """
    Convert TOOL_DECLARATIONS (Gemini format) to OpenAI function calling format.
    Cached on first call — tool list never changes at runtime.
    """
    global _openai_tools_cache
    if _openai_tools_cache is not None:
        return _openai_tools_cache

    with _openai_tools_lock:
        if _openai_tools_cache is not None:
            return _openai_tools_cache

        tools = []
        for decl in declarations:
            params = decl.get("parameters", {})
            properties = {}
            for pname, pinfo in params.get("properties", {}).items():
                prop = {"type": _TYPE_MAP.get(pinfo.get("type", "STRING"), "string")}
                if "description" in pinfo:
                    prop["description"] = pinfo["description"]
                properties[pname] = prop

            tool = {
                "type": "function",
                "function": {
                    "name": decl["name"],
                    "description": decl.get("description", ""),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": params.get("required", []),
                    },
                },
            }
            tools.append(tool)

        _openai_tools_cache = tools
        return tools


def _coerce_ollama_json_tool_call(parsed: dict) -> tuple[str, dict] | None:
    """
    Recover obvious tool calls when small local models emit bare arguments
    instead of Ollama's native tool_calls shape.
    """
    if not isinstance(parsed, dict):
        return None

    name = parsed.get("name") or parsed.get("tool")
    if name:
        arguments = parsed.get("arguments", parsed.get("parameters", {}))
        if not isinstance(arguments, dict):
            arguments = {}
        return str(name), arguments

    action = parsed.get("action")
    if isinstance(action, str):
        system_actions = {
            "volume_set", "volume_up", "volume_down", "volume_mute",
            "media_play", "media_pause", "media_next", "media_prev",
            "brightness_set", "battery", "wifi_on", "wifi_off",
            "screenshot", "shutdown", "restart", "sleep", "lock",
            "system_info",
        }
        if action in system_actions:
            args = {"action": action}
            if "value" in parsed:
                args["value"] = parsed["value"]
            return "system_control", args

    if "app_name" in parsed:
        return "open_app", {"app_name": parsed["app_name"]}
    if "goal" in parsed:
        return "agent_task", {"goal": parsed["goal"]}
    if "query" in parsed:
        return "web_search", {"query": parsed["query"]}
    if "question" in parsed:
        return "screen_vision", {"question": parsed["question"]}

    return None


def _groq_with_tools(messages: list, tools: list, system: str = "",
                     model: str = "", temperature: float = 0.3) -> dict:
    """
    Call Groq with native function calling.
    Returns {"type": "tool_call", "name": ..., "arguments": ...}
    or {"type": "text", "content": ...}
    """
    import httpx

    settings = _load_settings()
    api_key = _get_groq_key()
    if not api_key:
        raise ValueError("No Groq API key. Set GROQ_API_KEY in .env.")

    if not model:
        model = settings.get("groq_model", "llama-3.3-70b-versatile")

    full_messages = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    payload = {
        "model": model,
        "messages": full_messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": temperature,
        "max_tokens": 2048,
    }

    try:
        t0 = time.time()
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.time() - t0

        choice = data["choices"][0]
        message = choice["message"]

        tool_calls = message.get("tool_calls")
        if tool_calls and len(tool_calls) > 0:
            tc = tool_calls[0]
            func = tc["function"]
            try:
                arguments = json.loads(func["arguments"])
            except (json.JSONDecodeError, TypeError):
                arguments = {}

            log.info("⚡ Groq tool_call [%s]: %s(%s) in %.1fs",
                     model, func["name"], arguments, elapsed)
            return {
                "type": "tool_call",
                "name": func["name"],
                "arguments": arguments,
                "pre_text": message.get("content", ""),
            }

        content = message.get("content", "").strip()
        log.info("⚡ Groq text [%s]: %d chars in %.1fs", model, len(content), elapsed)
        return {"type": "text", "content": content}

    except Exception as e:
        log.error("Groq with_tools failed: %s", e)
        raise


def _gemini_with_tools(messages: list, declarations: list, system: str = "",
                       model: str = "gemini-2.5-flash",
                       temperature: float = 0.3) -> dict:
    """
    Call Gemini with native function calling.
    Returns {"type": "tool_call", "name": ..., "arguments": ...}
    or {"type": "text", "content": ...}
    """
    from google.genai import types as gtypes
    client = _get_gemini_client()

    func_decls = []
    for decl in declarations:
        params = decl.get("parameters", {})
        properties = {}
        for pname, pinfo in params.get("properties", {}).items():
            properties[pname] = gtypes.Schema(
                type=pinfo.get("type", "STRING"),
                description=pinfo.get("description", ""),
            )

        schema = None
        if properties:
            schema = gtypes.Schema(
                type="OBJECT",
                properties=properties,
                required=params.get("required", []),
            )

        func_decls.append(gtypes.FunctionDeclaration(
            name=decl["name"],
            description=decl.get("description", ""),
            parameters=schema,
        ))

    contents = []
    for msg in messages:
        contents.append(msg["content"])

    config = {
        "system_instruction": system,
        "temperature": temperature,
        "tools": [gtypes.Tool(function_declarations=func_decls)],
    }

    try:
        t0 = time.time()
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        elapsed = time.time() - t0

        if response.candidates:
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'function_call') and part.function_call:
                    fc = part.function_call
                    args = dict(fc.args) if fc.args else {}
                    log.info("🔷 Gemini tool_call [%s]: %s(%s) in %.1fs",
                             model, fc.name, args, elapsed)
                    return {
                        "type": "tool_call",
                        "name": fc.name,
                        "arguments": args,
                        "pre_text": "",
                    }

        content = (response.text or "").strip()
        log.info("🔷 Gemini text [%s]: %d chars in %.1fs", model, len(content), elapsed)
        return {"type": "text", "content": content}

    except Exception as e:
        log.error("Gemini with_tools failed: %s", e)
        raise


def _ollama_with_tools(messages: list, tools: list, system: str = "",
                       model: str = "", temperature: float = 0.3) -> dict:
    """
    Call Ollama with native function calling via /api/chat.

    Concept: Ollama v0.22+ supports native tool calling. The model's
    response can contain tool_calls (llama3.1, mistral) or JSON-in-content
    (qwen2.5). We handle both formats for maximum compatibility.

    Uses /api/chat (not /api/generate) which supports the `tools` parameter.

    Returns:
        {"type": "tool_call", "name": str, "arguments": dict}
        or {"type": "text", "content": str}
    """
    import httpx

    settings = _load_settings()
    url = settings.get("ollama_url", "http://localhost:11434")
    if not model:
        model = settings.get("ollama_model", settings.get("llm_model", "qwen2.5-coder:3b"))

    full_messages = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    payload = {
        "model": model,
        "messages": full_messages,
        "tools": tools,
        "stream": False,
        "options": _ollama_options(temperature, 256),
    }

    try:
        t0 = time.time()
        resp = httpx.post(
            f"{url}/api/chat",
            json=payload,
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.time() - t0

        message = data.get("message", {})

        # Format 1: Structured tool_calls array (llama3.1, mistral)
        tool_calls = message.get("tool_calls")
        if tool_calls and len(tool_calls) > 0:
            tc = tool_calls[0]
            func = tc.get("function", {})
            name = func.get("name", "")
            arguments = func.get("arguments", {})
            # Arguments may be a string or dict
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}

            log.info("🦙 Ollama tool_call [%s]: %s(%s) in %.1fs",
                     model, name, arguments, elapsed)
            return {
                "type": "tool_call",
                "name": name,
                "arguments": arguments,
                "pre_text": "",
            }

        # Format 2: JSON-in-content (qwen2.5-coder wraps in ```json fences)
        content = message.get("content", "").strip()
        if content:
            # Strip markdown code fences: ```json ... ``` or ``` ... ```
            cleaned = content
            if cleaned.startswith("```"):
                # Remove opening fence (```json or ```)
                first_newline = cleaned.find("\n")
                if first_newline != -1:
                    cleaned = cleaned[first_newline + 1:]
                # Remove closing fence
                if cleaned.rstrip().endswith("```"):
                    cleaned = cleaned.rstrip()[:-3].rstrip()

            # Try to parse as JSON tool call
            try:
                parsed = json.loads(cleaned)
                coerced = _coerce_ollama_json_tool_call(parsed)
                if coerced:
                    name, arguments = coerced
                    log.info("🦙 Ollama tool_call (json-content) [%s]: %s(%s) in %.1fs",
                             model, name, arguments, elapsed)
                    return {
                        "type": "tool_call",
                        "name": name,
                        "arguments": arguments,
                        "pre_text": "",
                    }
            except (json.JSONDecodeError, TypeError):
                pass  # Not JSON — treat as text response

        # No tool call — text response
        log.info("🦙 Ollama text [%s]: %d chars in %.1fs", model, len(content), elapsed)
        return {"type": "text", "content": content}

    except httpx.ConnectError:
        log.warning("Ollama not reachable at %s", url)
        raise ConnectionError(f"Ollama not running at {url}")
    except Exception as e:
        log.error("Ollama with_tools failed: %s", e)
        raise


def llm_with_tools(messages: list, system: str = "", provider: str = "",
                   model: str = "", temperature: float = 0.3,
                   tool_declarations: list = None) -> dict:
    """
    Generate a response with native function calling support.

    This is the PRIMARY way Jarvis talks to LLMs when tool execution
    is possible. Fallback chain: Groq -> Gemini -> Ollama native tools.

    Returns:
        {"type": "tool_call", "name": str, "arguments": dict}
        or {"type": "text", "content": str}
    """
    settings = _load_settings()
    if not provider:
        provider = settings.get("llm_provider", "groq")

    if tool_declarations is None:
        from core.tools import TOOL_DECLARATIONS
        tool_declarations = TOOL_DECLARATIONS

    openai_tools = _to_openai_tools(tool_declarations)

    providers_to_try = []
    if provider == "groq":
        providers_to_try.append(("groq", model))
    elif provider == "gemini":
        providers_to_try.append(("gemini", model))
    elif provider == "ollama":
        providers_to_try.append(("ollama", model))

    if provider != "groq" and _get_groq_key():
        providers_to_try.append(("groq", ""))
    if provider != "gemini" and _get_gemini_key():
        providers_to_try.append(("gemini", ""))
    if provider != "ollama" and settings.get("fallback_to_ollama"):
        providers_to_try.append(("ollama", ""))

    last_error = None
    for prov, mdl in providers_to_try:
        try:
            if prov == "groq":
                return _groq_with_tools(
                    messages, openai_tools, system, mdl,
                    temperature=temperature,
                )
            elif prov == "gemini":
                return _gemini_with_tools(
                    messages, tool_declarations, system,
                    mdl or "gemini-2.5-flash",
                    temperature=temperature,
                )
            elif prov == "ollama":
                return _ollama_with_tools(
                    messages, openai_tools, system, mdl,
                    temperature=temperature,
                )

        except Exception as e:
            log.warning("Provider '%s' with_tools failed: %s", prov, e)
            last_error = e

    raise RuntimeError(f"All providers failed for tool calling. Last: {last_error}")


# ── Public API ─────────────────────────────────────────────────


def llm_generate(prompt: str, system: str = "", provider: str = "",
                 model: str = "", temperature: float = 0.3,
                 screen_context: str = "") -> str:
    """
    Generate text from the fastest available LLM provider.

    Concept — LLM Router integration:
      When using Ollama, the LLMRouter picks the right model per request:
        - "open Chrome" → phi3:mini (3.8B, 0.4s)  — fast path
        - "explain quantum computing" → llama3 (8B, 1.5s) — smart path
      This prevents every request going to the largest model.

    Fallback chain: configured → Groq → Gemini → Ollama.
    """
    settings = _load_settings()
    if not provider:
        provider = settings.get("llm_provider", "gemini")

    # ── LLM Router: auto-select Ollama model if not specified ──
    route_info = None
    if provider == "ollama" and not model:
        try:
            route_info = _get_router().choose(
                prompt, has_screen_context=bool(screen_context)
            )
            model = route_info["model"]
            provider = route_info["provider"]
            log.debug("Router: %s on %s (%s)", model, provider, route_info["reason"])
        except Exception:
            pass  # Router failed, use defaults

    # ── Inject screen context into system prompt ──
    if screen_context:
        system = f"{system}\n\n{screen_context}" if system else screen_context

    providers_to_try = []

    # Build ordered fallback list
    if provider == "groq":
        providers_to_try.append(("groq", model))
    elif provider == "gemini":
        providers_to_try.append(("gemini", model))
    elif provider == "ollama":
        providers_to_try.append(("ollama", model))

    # Add fallbacks
    if provider != "groq" and _get_groq_key():
        providers_to_try.append(("groq", ""))
    if provider != "gemini" and _get_gemini_key():
        providers_to_try.append(("gemini", ""))
    if provider != "ollama" and settings.get("fallback_to_ollama"):
        providers_to_try.append(("ollama", ""))

    last_error = None
    for prov, mdl in providers_to_try:
        try:
            t0 = time.time()
            if prov == "groq":
                result = _groq_generate(prompt, system, mdl, temperature=temperature)
            elif prov == "gemini":
                result = _gemini_generate(prompt, system, mdl or "gemini-2.5-flash", temperature=temperature)
            elif prov == "ollama":
                result = _ollama_generate(prompt, system, mdl, temperature=temperature)
            else:
                continue

            # Record latency for router auto-escalation
            if prov == "ollama":
                _get_router().record_latency(int((time.time() - t0) * 1000))

            return result
        except Exception as e:
            log.warning("Provider '%s' failed: %s, trying next...", prov, e)
            last_error = e

    raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")


def llm_stream(prompt: str, system: str = "", provider: str = "",
               model: str = "", temperature: float = 0.3):
    """
    Stream text tokens from the LLM, yielding one token at a time.

    Concept: Instead of waiting for the full response (2-3s), we stream
    tokens as they're generated. The streaming pipeline buffers tokens
    into sentences and pipes each sentence to TTS immediately.
    Result: user hears first words at ~500ms instead of ~2.8s.

    Same fallback chain as llm_generate().

    Yields:
        str: individual tokens as they arrive from the LLM
    """
    settings = _load_settings()
    if not provider:
        provider = settings.get("llm_provider", "gemini")

    providers_to_try = []
    if provider == "groq":
        providers_to_try.append(("groq", model))
    elif provider == "gemini":
        providers_to_try.append(("gemini", model))
    elif provider == "ollama":
        providers_to_try.append(("ollama", model))

    if provider != "groq" and _get_groq_key():
        providers_to_try.append(("groq", ""))
    if provider != "gemini" and _get_gemini_key():
        providers_to_try.append(("gemini", ""))
    if provider != "ollama" and settings.get("fallback_to_ollama"):
        providers_to_try.append(("ollama", ""))

    last_error = None
    for prov, mdl in providers_to_try:
        try:
            if prov == "groq":
                yield from _groq_stream(prompt, system, mdl, temperature=temperature)
                return
            elif prov == "gemini":
                yield from _gemini_stream(prompt, system, mdl or "gemini-2.5-flash", temperature=temperature)
                return
            elif prov == "ollama":
                yield from _ollama_stream(prompt, system, mdl, temperature=temperature)
                return
        except Exception as e:
            log.warning("Stream provider '%s' failed: %s, trying next...", prov, e)
            last_error = e

    raise RuntimeError(f"All streaming providers failed. Last error: {last_error}")


def llm_generate_json(prompt: str, system: str = "", provider: str = "",
                      model: str = "", temperature: float = 0.2) -> dict:
    """
    Generate JSON from the fastest available LLM provider.
    Same fallback chain as llm_generate but requests JSON format.
    """
    settings = _load_settings()
    if not provider:
        provider = settings.get("planner_provider", settings.get("llm_provider", "gemini"))
    if not model:
        model = settings.get("planner_model", "")

    providers_to_try = []

    # Build ordered fallback list
    if provider == "groq":
        providers_to_try.append(("groq", model))
    elif provider == "gemini":
        providers_to_try.append(("gemini", model))
    elif provider == "ollama":
        providers_to_try.append(("ollama", model))

    # Add fallbacks
    if provider != "groq" and _get_groq_key():
        providers_to_try.append(("groq", ""))
    if provider != "gemini" and _get_gemini_key():
        providers_to_try.append(("gemini", ""))
    if provider != "ollama" and settings.get("fallback_to_ollama"):
        providers_to_try.append(("ollama", ""))

    last_error = None
    text = None

    for prov, mdl in providers_to_try:
        try:
            if prov == "groq":
                text = _groq_generate(prompt, system, mdl, format_json=True, temperature=temperature)
            elif prov == "gemini":
                text = _gemini_generate(prompt, system, mdl or "gemini-2.5-flash",
                                        format_json=True, temperature=temperature)
            elif prov == "ollama":
                text = _ollama_generate(prompt, system, mdl, format_json=True, temperature=temperature)

            if text:
                break
        except Exception as e:
            log.warning("JSON provider '%s' failed: %s, trying next...", prov, e)
            last_error = e

    if text is None:
        raise RuntimeError(f"All providers failed for JSON generation. Last error: {last_error}")

    # Parse JSON — strip markdown fences if present
    text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.error("JSON parse failed: %s\nRaw text: %s", e, text[:500])
        raise


def vision_find(image_bytes: bytes, description: str) -> tuple | None:
    """
    Find a UI element on screen using a vision model.
    Uses Ollama+moondream locally, falls back to Gemini Vision.
    """
    import base64

    settings = _load_settings()
    provider = settings.get("vision_provider", "ollama")
    model = settings.get("vision_model", "moondream")

    img_b64 = base64.b64encode(image_bytes).decode("ascii")

    prompt = (
        f"Look at this screenshot. Find the UI element: '{description}'. "
        f"Reply with ONLY the center pixel coordinates as: x,y "
        f"If the element is not visible, reply: NOT_FOUND"
    )

    try:
        if provider == "ollama":
            text = _ollama_generate(
                prompt=prompt,
                model=model,
                images=[img_b64],
                temperature=0.1,
            )
        elif provider == "gemini":
            text = _gemini_generate(
                prompt=prompt,
                model="gemini-2.5-flash",
                images=[img_b64],
                temperature=0.1,
            )
        else:
            text = _ollama_generate(
                prompt=prompt,
                model=model,
                images=[img_b64],
                temperature=0.1,
            )

        log.info("👁️ vision_find('%s') → %s", description, text[:80])

        if "NOT_FOUND" in text.upper():
            return None

        match = re.search(r"(\d+)\s*,\s*(\d+)", text)
        if match:
            return int(match.group(1)), int(match.group(2))

        return None

    except Exception as e:
        log.error("vision_find failed: %s", e)
        return None
