"""Generate a small, validated seed dataset for Jarvis fine-tuning.

This is intentionally deterministic. It gives the Colab notebook and future
teacher-model pipeline a clean JSONL shape without requiring API keys.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


SYSTEM_CONTEXT = (
    "[CURRENT TIME] Saturday, May 30, 2026 - 2:49 PM IST\n"
    "[WORLD STATE]\nActive: Desktop (explorer.exe)\n[END WORLD STATE]"
)


def message(system: str, user: str, assistant: str, tool_calls: list | None = None) -> dict:
    assistant_msg = {"role": "assistant", "content": assistant}
    if tool_calls:
        assistant_msg["tool_calls"] = tool_calls
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}, assistant_msg]}


def tool_call(name: str, arguments: dict) -> list:
    return [{"function": {"name": name, "arguments": arguments}}]


def tool_calling_examples() -> list[dict]:
    return [
        message(SYSTEM_CONTEXT, "Open Spotify and play some music", "Opening Spotify for you, Siddhu.", tool_call("agent_task", {"goal": "Open Spotify and play music"})),
        message(SYSTEM_CONTEXT, "Pause the music", "Pausing it.", tool_call("system_control", {"action": "media_pause"})),
        message(SYSTEM_CONTEXT, "Turn the volume to 35", "Setting volume to 35.", tool_call("system_control", {"action": "volume_set", "value": "35"})),
        message(SYSTEM_CONTEXT, "Open Chrome", "Opening Chrome.", tool_call("open_app", {"app_name": "Chrome"})),
        message(SYSTEM_CONTEXT, "What is two plus two?", "Four."),
        message(SYSTEM_CONTEXT, "Remember I prefer dark mode", "", tool_call("save_memory", {"category": "preferences", "key": "theme", "value": "dark mode"})),
        message(SYSTEM_CONTEXT, "What error is on my screen?", "I'll inspect the screen.", tool_call("screen_vision", {"question": "Identify the visible error and explain it briefly"})),
        message(SYSTEM_CONTEXT, "Search the web for current gold price", "Looking that up.", tool_call("web_search", {"query": "current gold price"})),
    ]


def planning_examples() -> list[dict]:
    planning_system = "You are JARVIS's planning module. Return only valid JSON."
    plans = [
        (
            "Goal: Search for 'machine learning' on YouTube",
            {
                "goal": "Search for machine learning on YouTube",
                "steps": [
                    {"step": 1, "tool": "open_app", "description": "Open Chrome", "parameters": {"app_name": "Chrome"}},
                    {"step": 2, "tool": "computer_control", "description": "Focus address bar", "parameters": {"action": "hotkey", "keys": "ctrl+l"}},
                    {"step": 3, "tool": "computer_control", "description": "Type YouTube search URL", "parameters": {"action": "smart_type", "text": "youtube.com/results?search_query=machine+learning"}},
                    {"step": 4, "tool": "computer_control", "description": "Navigate", "parameters": {"action": "press", "key": "enter"}},
                ],
            },
        ),
        (
            "Goal: Open README.md in VS Code",
            {
                "goal": "Open README.md in VS Code",
                "steps": [
                    {"step": 1, "tool": "open_app", "description": "Open VS Code", "parameters": {"app_name": "Visual Studio Code"}},
                    {"step": 2, "tool": "computer_control", "description": "Open file picker", "parameters": {"action": "hotkey", "keys": "ctrl+p"}},
                    {"step": 3, "tool": "computer_control", "description": "Type file name", "parameters": {"action": "smart_type", "text": "README.md"}},
                    {"step": 4, "tool": "computer_control", "description": "Open file", "parameters": {"action": "press", "key": "enter"}},
                ],
            },
        ),
        (
            "Goal: Pause the music",
            {
                "goal": "Pause the music",
                "steps": [
                    {"step": 1, "tool": "system_control", "description": "Pause media globally", "parameters": {"action": "media_pause"}},
                ],
            },
        ),
    ]
    return [message(planning_system, user, json.dumps(plan, separators=(",", ":"))) for user, plan in plans]


def conversation_examples() -> list[dict]:
    return [
        message("[CURRENT TIME] Saturday, May 30, 2026 - 2:49 PM IST", "Good morning Jarvis", "Good afternoon, Siddhu. What can I help with?"),
        message(SYSTEM_CONTEXT, "Explain threads vs processes", "A process is an isolated running program. A thread is a lighter execution path inside a process that shares its memory."),
        message(SYSTEM_CONTEXT, "Can you delete System32?", "No. I can help clean temporary files safely instead."),
    ]


def proactive_examples() -> list[dict]:
    return [
        message("The user's screen shows an error. Comment naturally.", "Screen context: VS Code terminal shows TypeError: cannot read properties of undefined\nActive app: Visual Studio Code\nUser name: Siddhu", "That TypeError usually means something is null or undefined before a property access, Siddhu. Want me to inspect the code path?"),
        message("Comment only if useful; otherwise say NOTHING.", "Screen context: Browser is showing a normal news article\nActive app: Chrome\nUser name: Siddhu", "NOTHING"),
        message("A long-running operation completed. Acknowledge it.", "Screen context: pytest reports 42 passed in 18.4s\nActive app: Terminal\nUser name: Siddhu", "Tests are green, Siddhu."),
    ]


DATASETS = {
    "tool_calling.jsonl": tool_calling_examples,
    "planning.jsonl": planning_examples,
    "conversation.jsonl": conversation_examples,
    "proactive.jsonl": proactive_examples,
}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="training_data")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {}
    for filename, factory in DATASETS.items():
        rows = factory()
        write_jsonl(out_dir / filename, rows)
        summary[filename] = len(rows)

    report = out_dir / "quality_report.json"
    report.write_text(json.dumps({"files": summary, "total": sum(summary.values())}, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out_dir), "files": summary, "total": sum(summary.values())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
