"""Benchmark Jarvis local Ollama models for tool routing and planning JSON."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.planner import PLANNER_PROMPT
from core.providers import _coerce_ollama_json_tool_call, _to_openai_tools
from core.tools import TOOL_DECLARATIONS


TOOL_TESTS = [
    ("open recycle bin", "tool_call", "open_app"),
    ("pause the music", "tool_call", "system_control"),
    ("increase the volume", "tool_call", "system_control"),
    ("what is 2 plus 2", "text", None),
    ("tell me a short joke", "text", None),
    ("remember I prefer dark mode", "tool_call", "save_memory"),
    ("look at my screen and tell me what error is showing", "tool_call", "screen_vision"),
    ("open chrome and search for python tutorials", "tool_call", "agent_task"),
]

PLAN_TESTS = [
    "Open Spotify and play any random music",
    "Search YouTube for machine learning",
    "Open README.md in VS Code",
    "Find files larger than 1GB in Downloads",
]

SYSTEM = (
    "You are JARVIS, a Windows 11 assistant. Use tools only for actions on "
    "the user's computer. For ordinary conversation, answer with text."
)


def ollama_chat(url: str, model: str, messages: list[dict], tools: list | None = None) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.3,
            "top_p": 0.9,
            "top_k": 40,
            "repeat_penalty": 1.1,
            "num_predict": 256,
        },
    }
    if tools:
        payload["tools"] = tools

    response = httpx.post(f"{url}/api/chat", json=payload, timeout=60.0)
    response.raise_for_status()
    return response.json()


def ollama_generate_json(url: str, model: str, prompt: str, system: str) -> dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "format": "json",
        "stream": False,
        "options": {
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 40,
            "repeat_penalty": 1.1,
            "num_predict": 512,
        },
    }
    response = httpx.post(f"{url}/api/generate", json=payload, timeout=90.0)
    response.raise_for_status()
    text = response.json().get("response", "").strip()
    return json.loads(text)


def model_exists(url: str, model: str) -> bool:
    try:
        response = httpx.post(f"{url}/api/show", json={"model": model}, timeout=10.0)
        return response.status_code == 200
    except Exception:
        return False


def score_tool_response(data: dict, expected_type: str, expected_tool: str | None) -> tuple[bool, str]:
    message = data.get("message", {})
    tool_calls = message.get("tool_calls") or []
    content = (message.get("content") or "").strip()

    actual_type = "text"
    actual_tool = None

    if tool_calls:
        actual_type = "tool_call"
        actual_tool = tool_calls[0].get("function", {}).get("name", "")
    elif content:
        cleaned = content
        if cleaned.startswith("```"):
            first_newline = cleaned.find("\n")
            if first_newline != -1:
                cleaned = cleaned[first_newline + 1 :]
            if cleaned.rstrip().endswith("```"):
                cleaned = cleaned.rstrip()[:-3].rstrip()
        try:
            parsed = json.loads(cleaned)
            coerced = _coerce_ollama_json_tool_call(parsed)
            if coerced:
                actual_type = "tool_call"
                actual_tool = coerced[0]
        except (json.JSONDecodeError, TypeError):
            pass

    ok = actual_type == expected_type and (expected_tool is None or actual_tool == expected_tool)
    detail = actual_tool if actual_type == "tool_call" else content[:60]
    return ok, f"{actual_type}({detail})"


def run_model(url: str, model: str) -> dict:
    tools = _to_openai_tools(TOOL_DECLARATIONS)
    print(f"\n=== {model} ===")

    tool_correct = 0
    tool_latency = 0.0
    for query, expected_type, expected_tool in TOOL_TESTS:
        started = time.time()
        data = ollama_chat(
            url,
            model,
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": query}],
            tools=tools,
        )
        elapsed = time.time() - started
        tool_latency += elapsed
        ok, detail = score_tool_response(data, expected_type, expected_tool)
        tool_correct += int(ok)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] tool {elapsed:5.1f}s | {query:42s} -> {detail}")

    plan_correct = 0
    plan_latency = 0.0
    for goal in PLAN_TESTS:
        started = time.time()
        try:
            plan = ollama_generate_json(url, model, f"Goal: {goal}", PLANNER_PROMPT)
            steps = plan.get("steps", [])
            ok = isinstance(steps, list) and len(steps) > 0 and all("tool" in s for s in steps)
            detail = f"{len(steps)} steps"
        except Exception as exc:
            ok = False
            detail = str(exc)[:80]
        elapsed = time.time() - started
        plan_latency += elapsed
        plan_correct += int(ok)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] plan {elapsed:5.1f}s | {goal:42s} -> {detail}")

    return {
        "tool_accuracy": tool_correct / len(TOOL_TESTS),
        "plan_accuracy": plan_correct / len(PLAN_TESTS),
        "tool_avg_latency": tool_latency / len(TOOL_TESTS),
        "plan_avg_latency": plan_latency / len(PLAN_TESTS),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--models", nargs="+", default=["qwen2.5-coder:3b", "jarvis"])
    args = parser.parse_args()

    results = {}
    for model in args.models:
        if not model_exists(args.url, model):
            print(f"\nSKIP {model}: not installed or Ollama unavailable")
            continue
        results[model] = run_model(args.url, model)

    if results:
        print("\n=== Summary ===")
        for model, result in results.items():
            print(
                f"{model:24s} "
                f"tools={result['tool_accuracy']:.0%} "
                f"plans={result['plan_accuracy']:.0%} "
                f"tool_avg={result['tool_avg_latency']:.1f}s "
                f"plan_avg={result['plan_avg_latency']:.1f}s"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
