"""
Planner — Breaks complex goals into step-by-step plans.
Inspired by MK37's planner but built for V3 architecture.

Uses Gemini text model to create structured execution plans
using only V3's available tools.
"""

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent


LEGACY_PLANNER_PROMPT = """You are JARVIS's planning module running on a Windows 11 desktop.
Your job: break any user goal into a sequence of steps using ONLY the tools listed below.
You are a HUMAN sitting in front of the screen, driving the mouse and keyboard. Think like a human.

ABSOLUTE RULES:
- Max 8 steps. Use the minimum steps needed.
- Every step must be independent — don't reference previous step results in parameters.
- NEVER invent tools that aren't listed below.
- KEYBOARD SHORTCUTS FIRST: If an action can be done with a keyboard shortcut, ALWAYS prefer it over clicking UI elements. Keyboard shortcuts are 100% reliable. Visual clicks on floating/dynamic buttons can fail.

CRITICAL — SCREEN CONTEXT LIMITATION:
The [SCREEN CONTEXT] below is captured BEFORE the plan runs. If your plan opens an app,
the context does NOT show that app's UI. For steps INSIDE a newly opened app, you must
use keyboard shortcuts or very high-confidence landmark clicks (e.g. sidebar items by name).
Do NOT plan screen_click steps for buttons you have not seen in [SCREEN CONTEXT].
Example of WRONG planning: open Spotify → screen_click "Shuffle Play"  (you haven't seen Spotify's UI yet)
Example of RIGHT planning: open Spotify → click known sidebar "Liked Songs" → hotkey ctrl+shift+s → press space

HOW TO THINK ABOUT UI TASKS:
1. Open the app — the system waits for it to load and focuses it automatically.
2. CHECK [SCREEN CONTEXT] — but only trust it for apps ALREADY open before this plan runs.
3. Navigate using keyboard shortcuts or sidebar landmark clicks (sidebars don't change).
4. Interact with the target element.

MEDIA APPS — KEYBOARD-FIRST RULES (GLOBAL, ALL APPS):
These shortcuts work reliably. Use them instead of hunting visual buttons:
- Play/Pause:    press "space" (works in Spotify, YT Music, browsers)
- Next track:    system_control → media_next
- Prev track:    system_control → media_prev
- Volume:        system_control → volume_set

SPOTIFY PATTERNS (use these EXACTLY):
- "play any / random music":
    open_app(spotify) → screen_click("Liked Songs") → hotkey("ctrl+shift+s") → press("space")
    The "Liked Songs" sidebar item is ALWAYS visible. ctrl+shift+s enables shuffle. Space starts play.
    DO NOT click "Shuffle Play" as a visual floating button — it only appears inside a playlist view.
- "play [song or artist]":
    open_app(spotify) → hotkey("ctrl+k") → smart_type(song name) → press("enter") → press("space")
- "pause / resume":
    system_control(media_pause) or system_control(media_play) — no need to open Spotify
- "next song" / "previous song":
    system_control(media_next) or system_control(media_prev)
- "shuffle on/off":
    focus Spotify window → hotkey("ctrl+shift+s")
- "volume up/down":
    system_control(volume_set, value="70")

BROWSER PATTERNS (Chrome / Edge / Firefox):
- "go to [url]":           hotkey("ctrl+l") → smart_type(url) → press("enter")
- "search for [query]":    hotkey("ctrl+t") → smart_type(query) → press("enter")
- "new tab":               hotkey("ctrl+t")
- "close tab":             hotkey("ctrl+w")

TRADINGVIEW PATTERNS:
- "open chart for [symbol]": open_app(tradingview) → hotkey("/") → smart_type(symbol) → press("enter")
- "add indicator":           hotkey("alt+i")

VS CODE PATTERNS:
- "open file [name]":    hotkey("ctrl+p") → smart_type(filename) → press("enter")
- "open terminal":       hotkey("ctrl+backtick")
- "search in project":   hotkey("ctrl+shift+f")
- "run code":            hotkey("ctrl+f5")

GENERAL NAVIGATION RULES:
- SIDEBARS: Spotify, Discord, VS Code, TradingView have left sidebars with named items.
  These are safe to screen_click — their names are stable (e.g. "Liked Songs", "Home", "Search").
- For screen_click: ONLY use for elements visible in [SCREEN CONTEXT] OR permanent sidebar landmarks.
- For typing: click input field FIRST (separate step), THEN type. Never combine into one step.
- If content is not visible, scroll before clicking.
- 'open_app' means the app is open AND focused. The next step can interact immediately.

BACKGROUND CONTROLS (no app focus needed):
- media_play, media_pause, media_next, media_prev, volume_set, brightness_set → use system_control

AVAILABLE TOOLS:

open_app
  app_name: string — name of application to open

computer_control
  action: "screen_click" | "smart_type" | "type" | "hotkey" | "press" | "scroll" | "wait" | "screen_find" | "focus_window"
  description: string — for screen_click/screen_find: describe element by its EXACT visible text
  text: string — for type/smart_type
  keys: string — for hotkey (e.g. "ctrl+k", "ctrl+shift+s")
  key: string — for press (e.g. "enter", "tab", "space")
  direction: string — for scroll ("up" or "down")
  seconds: number — for wait
  title: string — for focus_window
  clear_first: boolean — for smart_type (default true)

web_search
  query: string — search query for information lookup

file_manager
  action: "create_file" | "list" | "find" | "read" | "delete" | "move" | "copy" | "largest" | "disk_usage"
  path: string — file/folder path (shortcuts: desktop, downloads, documents, home, c)
  content: string — for create_file
  count: integer — for largest
  min_size_gb: number — for largest

system_control
  action: string — volume_set | brightness_set | screenshot | shutdown | restart | sleep | lock | media_play | media_pause | media_next | media_prev
  value: string — optional value

run_command
  command: string — PowerShell command to execute

EXAMPLE PLANS:

Goal: "Open Spotify and play any random music"
{
  "goal": "Open Spotify and play any random music",
  "steps": [
    {"step": 1, "tool": "open_app", "description": "Open Spotify", "parameters": {"app_name": "spotify"}},
    {"step": 2, "tool": "computer_control", "description": "Click Liked Songs in sidebar", "parameters": {"action": "screen_click", "description": "Liked Songs"}},
    {"step": 3, "tool": "computer_control", "description": "Enable shuffle with keyboard shortcut", "parameters": {"action": "hotkey", "keys": "ctrl+shift+s"}},
    {"step": 4, "tool": "computer_control", "description": "Start playback", "parameters": {"action": "press", "key": "space"}}
  ]
}

Goal: "Search for Coldplay on Spotify"
{
  "goal": "Search for Coldplay on Spotify",
  "steps": [
    {"step": 1, "tool": "open_app", "description": "Open Spotify", "parameters": {"app_name": "spotify"}},
    {"step": 2, "tool": "computer_control", "description": "Open search with keyboard shortcut", "parameters": {"action": "hotkey", "keys": "ctrl+k"}},
    {"step": 3, "tool": "computer_control", "description": "Type artist name", "parameters": {"action": "smart_type", "text": "Coldplay"}},
    {"step": 4, "tool": "computer_control", "description": "Confirm search", "parameters": {"action": "press", "key": "enter"}}
  ]
}

Goal: "Pause the music"
{
  "goal": "Pause the music",
  "steps": [
    {"step": 1, "tool": "system_control", "description": "Pause media globally", "parameters": {"action": "media_pause"}}
  ]
}

OUTPUT — return ONLY valid JSON, no markdown, no code blocks:
{
  "goal": "...",
  "steps": [
    {
      "step": 1,
      "tool": "tool_name",
      "description": "what this step does",
      "parameters": {}
    }
  ]
}
"""
PLANNER_PROMPT = """You are JARVIS's Windows 11 planning module.
Convert a user goal into the shortest valid JSON plan using only the tools below.

Rules:
- Return ONLY valid JSON. No markdown, comments, or prose.
- Use 1-8 steps. Prefer fewer steps.
- Each step must be executable without referring to a prior step's result.
- Prefer keyboard shortcuts and ui_control over visual screen_click.
- [SCREEN CONTEXT] is captured before the plan runs. Trust it only for currently visible UI. If the plan opens an app, use keyboard shortcuts, stable sidebar landmarks, or ui_control after that.
- Use system_control for background media, volume, brightness, power, and screenshots.
- Use web_search only for information lookup. Use open_app for websites/apps.
- Use file_manager only for explicit file operations.

Tools:
open_app(app_name)
close_app(app_name)
ui_control(action, target?, window?, text?, menu_path?)
computer_control(action, description?, text?, keys?, key?, direction?, seconds?, title?, clear_first?)
system_control(action, value?)
web_search(query)
file_manager(action, path, content?, destination?, new_name?, query?, extension?, count?, min_size_gb?)
run_command(command)
clipboard(action, text?)
type_text(text?, hotkey?)

Common patterns:
- Browser URL: open_app("chrome") if needed, then computer_control hotkey ctrl+l, smart_type URL, press enter.
- Browser search: open_app("chrome") if needed, then hotkey ctrl+t, smart_type query, press enter.
- Spotify random music: open_app("spotify"), ui_control click_button target "Liked Songs", computer_control hotkey ctrl+shift+s, press space.
- Spotify song/artist: open_app("spotify"), computer_control hotkey ctrl+k, smart_type search text, press enter, press space.
- Pause/resume/next/previous media: system_control media_pause/media_play/media_next/media_prev.
- VS Code file: focus or open VS Code, hotkey ctrl+p, smart_type filename, press enter.
- TradingView symbol: open_app("tradingview"), hotkey /, smart_type symbol, press enter.

Examples:
Goal: "Open Spotify and play any random music"
{"goal":"Open Spotify and play any random music","steps":[{"step":1,"tool":"open_app","description":"Open Spotify","parameters":{"app_name":"spotify"}},{"step":2,"tool":"ui_control","description":"Open Liked Songs from the sidebar","parameters":{"action":"click_button","target":"Liked Songs","window":"Spotify"}},{"step":3,"tool":"computer_control","description":"Enable shuffle","parameters":{"action":"hotkey","keys":"ctrl+shift+s"}},{"step":4,"tool":"computer_control","description":"Start playback","parameters":{"action":"press","key":"space"}}]}

Goal: "Search YouTube for machine learning"
{"goal":"Search YouTube for machine learning","steps":[{"step":1,"tool":"open_app","description":"Open Chrome","parameters":{"app_name":"chrome"}},{"step":2,"tool":"computer_control","description":"Focus address bar","parameters":{"action":"hotkey","keys":"ctrl+l"}},{"step":3,"tool":"computer_control","description":"Type YouTube search URL","parameters":{"action":"smart_type","text":"youtube.com/results?search_query=machine+learning"}},{"step":4,"tool":"computer_control","description":"Navigate","parameters":{"action":"press","key":"enter"}}]}

Goal: "Pause the music"
{"goal":"Pause the music","steps":[{"step":1,"tool":"system_control","description":"Pause media globally","parameters":{"action":"media_pause"}}]}

Goal: "Open the README in VS Code"
{"goal":"Open the README in VS Code","steps":[{"step":1,"tool":"open_app","description":"Open Visual Studio Code","parameters":{"app_name":"Visual Studio Code"}},{"step":2,"tool":"computer_control","description":"Open quick file picker","parameters":{"action":"hotkey","keys":"ctrl+p"}},{"step":3,"tool":"computer_control","description":"Type file name","parameters":{"action":"smart_type","text":"README.md"}},{"step":4,"tool":"computer_control","description":"Open selected file","parameters":{"action":"press","key":"enter"}}]}

Goal: "Find large files in Downloads"
{"goal":"Find large files in Downloads","steps":[{"step":1,"tool":"file_manager","description":"List large files in Downloads","parameters":{"action":"largest","path":"downloads","count":10,"min_size_gb":1}}]}

Schema:
{"goal":"...","steps":[{"step":1,"tool":"tool_name","description":"what this step does","parameters":{}}]}
"""


def create_plan(goal: str) -> dict | None:
    """
    Create an execution plan for the given goal.
    
    Before planning, reads the UI accessibility tree so the planner
    can "see" what's on screen — sidebar items, buttons, menus, etc.
    Returns a plan dict with 'goal' and 'steps', or None on failure.
    """
    # Provider abstraction handles API keys internally

    # ── Gather screen context (what's visible right now) ──
    screen_context = ""
    try:
        from actions.ui_reader import get_screen_summary
        summary = get_screen_summary()
        if summary and "No readable UI elements" not in summary:
            # Cap to 15 elements to reduce tokens for speed
            lines = summary.split("\n")
            if len(lines) > 16:  # header + 15 elements
                lines = lines[:16] + [f"  ... ({len(lines) - 16} more elements)"]
                summary = "\n".join(lines)
            screen_context = f"\n\n[SCREEN CONTEXT]\n{summary}\n"
            log.info("👁️ Planner received screen context: %d elements", summary.count("- "))
        else:
            screen_context = f"\n\n[SCREEN CONTEXT]\n{summary}\n"
    except Exception as e:
        log.debug("Could not read screen context: %s", e)

    # ── Also get list of open windows ──
    try:
        from actions.window_manager import list_open_windows
        windows = list_open_windows()
        if windows:
            top_windows = windows[:8]  # cap to 8 to reduce tokens
            screen_context += f"\n[OPEN WINDOWS]\n" + "\n".join(f"  - {w}" for w in top_windows) + "\n"
    except Exception:
        pass

    try:
        from core.providers import llm_generate_json

        # Include screen context with the goal
        prompt_content = f"Goal: {goal}{screen_context}"

        plan = llm_generate_json(
            prompt=prompt_content,
            system=PLANNER_PROMPT,
            temperature=0.2,
        )

        if "steps" not in plan or not isinstance(plan["steps"], list):
            log.warning("Invalid plan structure")
            return None

        log.info("📋 Plan created: %d steps for '%s'", len(plan["steps"]), goal[:60])
        for s in plan["steps"]:
            log.info("  Step %s: [%s] %s", s.get("step"), s.get("tool"), s.get("description"))

        return plan

    except json.JSONDecodeError as e:
        log.error("Planner JSON parse failed: %s", e)
        return None
    except Exception as e:
        log.error("Planning failed: %s", e)
        return None


# Known tools and their required/optional parameters
# Blueprint Principle 1: "Gemini → structured JSON → validator → executor"
KNOWN_TOOLS = {
    "open_app":         {"required": {"app_name"}, "optional": set()},
    "close_app":        {"required": {"app_name"}, "optional": set()},
    "system_control":   {"required": {"action"}, "optional": {"value"}},
    "web_search":       {"required": {"query"}, "optional": set()},
    "file_manager":     {"required": {"action", "path"}, "optional": {"content", "destination", "new_name", "query", "extension", "count", "min_size_gb"}},
    "screen_vision":    {"required": {"question"}, "optional": set()},
    "run_command":      {"required": {"command"}, "optional": set()},
    "type_text":        {"required": set(), "optional": {"text", "hotkey"}},
    "clipboard":        {"required": {"action"}, "optional": {"text"}},
    "save_memory":      {"required": {"category", "key", "value"}, "optional": set()},
    "computer_control": {"required": {"action"}, "optional": {"x", "y", "x1", "y1", "x2", "y2", "text", "keys", "key", "direction", "amount", "title", "description", "seconds", "path", "clear_first"}},
    "ui_control":       {"required": {"action"}, "optional": {"target", "window", "text", "menu_path"}},
    "click_element":    {"required": {"element_name"}, "optional": {"window_title"}},
    "type_into":        {"required": {"element_name", "text"}, "optional": {"window_title"}},
    "set_volume_precise": {"required": {"level"}, "optional": set()},
    "agent_task":       {"required": {"goal"}, "optional": set()},
    "sleep_jarvis":     {"required": set(), "optional": set()},
    "shutdown_jarvis":  {"required": set(), "optional": set()},
}


def validate_plan(plan: dict) -> tuple[bool, list[str]]:
    """
    Validate a plan before execution.

    Blueprint Principle 1: AI is the planner, NOT the direct controller.
    Every plan must be validated before it reaches the executor.

    Checks:
      1. Plan has a 'steps' list
      2. Every step has a 'tool' field
      3. Every tool is in KNOWN_TOOLS
      4. Required parameters are present
      5. No unknown parameters are passed (warn only)
      6. Max 8 steps (planner rule)

    Args:
        plan: Dict from create_plan() with 'goal' and 'steps'

    Returns:
        (is_valid, list_of_warnings)
        is_valid is False only for hard failures (unknown tools).
        Warnings are logged but don't block execution.
    """
    warnings = []

    if not plan or "steps" not in plan:
        return False, ["Plan has no 'steps' field"]

    steps = plan["steps"]
    if not isinstance(steps, list) or len(steps) == 0:
        return False, ["Plan 'steps' is empty or not a list"]

    if len(steps) > 8:
        warnings.append(f"Plan has {len(steps)} steps (max 8). Truncating.")
        plan["steps"] = steps[:8]
        steps = plan["steps"]

    for i, step in enumerate(steps):
        tool = step.get("tool", "")
        params = step.get("parameters", {})

        # Check tool exists
        if tool not in KNOWN_TOOLS:
            return False, [f"Step {i+1}: Unknown tool '{tool}'. Valid tools: {list(KNOWN_TOOLS.keys())}"]

        # Check required params
        spec = KNOWN_TOOLS[tool]
        missing = spec["required"] - set(params.keys())
        if missing:
            warnings.append(f"Step {i+1} [{tool}]: Missing required params: {missing}")

        # Warn on unknown params (don't block)
        all_known = spec["required"] | spec["optional"]
        unknown = set(params.keys()) - all_known
        if unknown:
            warnings.append(f"Step {i+1} [{tool}]: Unknown params (ignored): {unknown}")

    if warnings:
        for w in warnings:
            log.warning("Plan validation: %s", w)

    log.info("✅ Plan validated: %d steps, %d warnings", len(steps), len(warnings))
    return True, warnings


def replan(goal: str, completed_steps: list, failed_step: dict, error: str) -> dict | None:
    """
    Create a revised plan after a step failure.
    Only plans the remaining work — does not repeat completed steps.
    """
    try:
        from core.providers import llm_generate_json

        completed_summary = "\n".join(
            f"  - Step {s.get('step')} ({s.get('tool')}): DONE — {s.get('description', '')}"
            for s in completed_steps
        )

        prompt = f"""Goal: {goal}

Already completed:
{completed_summary if completed_summary else '  (none)'}

Failed step: [{failed_step.get('tool')}] {failed_step.get('description')}
Error: {error}

Create a REVISED plan for the remaining work only. Do not repeat completed steps.
Try a DIFFERENT approach for the failed step — prefer keyboard shortcuts over visual clicks."""

        plan = llm_generate_json(
            prompt=prompt,
            system=PLANNER_PROMPT,
            temperature=0.3,
        )

        log.info("🔄 Revised plan: %d steps", len(plan.get("steps", [])))
        return plan

    except Exception as e:
        log.error("Replan failed: %s", e)
        return None
