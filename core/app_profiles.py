"""
core/app_profiles.py -- Deterministic App Control Maps
======================================================
Provides keyboard-shortcut workflows for common Windows apps.
The planner uses these BEFORE calling the LLM, so known patterns
execute with zero LLM latency.

Concept -- Why App Profiles?
  The LLM planner generates plans by reasoning about apps it has
  never seen. This is slow (~5s) and error-prone. For the 6 most
  common apps, the correct keyboard shortcuts and UIA targets are
  KNOWN. We encode them as deterministic Python dicts and generate
  plans from templates -- instant, 100% reliable.

Architecture:
  voice/text -> intent_router (single-step commands)
             -> app_profiles.match_workflow() (multi-step known patterns)
             -> LLM planner (only for truly novel tasks)
"""

import logging
import re
from typing import Optional

log = logging.getLogger("JARVIS.app_profiles")

# ===================================================================
# APP PROFILES
# ===================================================================

APP_PROFILES: dict[str, dict] = {

    "chrome": {
        "exe": "chrome.exe",
        "aliases": ["chrome", "google chrome", "browser", "google"],
        "window_title_keywords": ["Chrome", "Google Chrome"],
        "controls": {
            "address_bar":  {"method": "hotkey", "keys": "ctrl+l"},
            "new_tab":      {"method": "hotkey", "keys": "ctrl+t"},
            "close_tab":    {"method": "hotkey", "keys": "ctrl+w"},
            "downloads":    {"method": "hotkey", "keys": "ctrl+j"},
            "history":      {"method": "hotkey", "keys": "ctrl+h"},
            "bookmark":     {"method": "hotkey", "keys": "ctrl+d"},
            "find":         {"method": "hotkey", "keys": "ctrl+f"},
            "refresh":      {"method": "hotkey", "keys": "f5"},
            "dev_tools":    {"method": "hotkey", "keys": "f12"},
            "incognito":    {"method": "hotkey", "keys": "ctrl+shift+n"},
        },
        "workflows": {
            "navigate_url": {
                "description": "Navigate to {url} in Chrome",
                "params": ["url"],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "chrome"},
                     "description": "Open Chrome"},
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+l"},
                     "description": "Focus address bar"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{url}"},
                     "description": "Type URL"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Navigate"},
                ],
            },
            "search_web": {
                "description": "Search for {query} in Chrome",
                "params": ["query"],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "chrome"},
                     "description": "Open Chrome"},
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+t"},
                     "description": "Open new tab"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{query}"},
                     "description": "Type search query"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Search"},
                ],
            },
            "find_on_page": {
                "description": "Find {text} on the current page",
                "params": ["text"],
                "steps": [
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+f"},
                     "description": "Open find bar"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{text}"},
                     "description": "Type search text"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Find next"},
                ],
            },
        },
    },

    "spotify": {
        "exe": "Spotify.exe",
        "aliases": ["spotify", "music", "music player"],
        "window_title_keywords": ["Spotify"],
        "controls": {
            "search":       {"method": "hotkey", "keys": "ctrl+k"},
            "play_pause":   {"method": "hotkey", "keys": "space"},
            "shuffle":      {"method": "hotkey", "keys": "ctrl+shift+s"},
            "repeat":       {"method": "hotkey", "keys": "ctrl+r"},
            "liked_songs":  {"method": "ui_control", "action": "click_button", "target": "Liked Songs"},
            "next":         {"method": "media_key", "action": "media_next"},
            "prev":         {"method": "media_key", "action": "media_prev"},
        },
        "workflows": {
            "search_and_play": {
                "description": "Search for {query} on Spotify and play",
                "params": ["query"],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "spotify"},
                     "description": "Open Spotify"},
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+k"},
                     "description": "Open search"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{query}"},
                     "description": "Type search query"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Confirm search"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Play top result"},
                ],
            },
            "play_random": {
                "description": "Play random music from Liked Songs",
                "params": [],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "spotify"},
                     "description": "Open Spotify"},
                    {"tool": "ui_control",
                     "params": {"action": "click_button", "target": "Liked Songs", "window": "Spotify"},
                     "description": "Open Liked Songs from sidebar"},
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+shift+s"},
                     "description": "Enable shuffle"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "space"},
                     "description": "Start playback"},
                ],
            },
        },
    },

    "vscode": {
        "exe": "Code.exe",
        "aliases": ["vscode", "vs code", "visual studio code", "code"],
        "window_title_keywords": ["Visual Studio Code", "VS Code"],
        "controls": {
            "quick_open":       {"method": "hotkey", "keys": "ctrl+p"},
            "terminal":         {"method": "hotkey", "keys": "ctrl+`"},
            "command_palette":  {"method": "hotkey", "keys": "ctrl+shift+p"},
            "search_files":     {"method": "hotkey", "keys": "ctrl+shift+f"},
            "find":             {"method": "hotkey", "keys": "ctrl+f"},
            "run":              {"method": "hotkey", "keys": "ctrl+f5"},
            "debug":            {"method": "hotkey", "keys": "f5"},
            "save":             {"method": "hotkey", "keys": "ctrl+s"},
            "close_tab":        {"method": "hotkey", "keys": "ctrl+w"},
            "toggle_sidebar":   {"method": "hotkey", "keys": "ctrl+b"},
        },
        "workflows": {
            "open_file": {
                "description": "Open file {filename} in VS Code",
                "params": ["filename"],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "Visual Studio Code"},
                     "description": "Open VS Code"},
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+p"},
                     "description": "Open quick file picker"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{filename}"},
                     "description": "Type filename"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Open selected file"},
                ],
            },
            "search_in_project": {
                "description": "Search for {query} across the project",
                "params": ["query"],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "Visual Studio Code"},
                     "description": "Open VS Code"},
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+shift+f"},
                     "description": "Open project search"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{query}"},
                     "description": "Type search query"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Search"},
                ],
            },
            "open_terminal": {
                "description": "Open the integrated terminal",
                "params": [],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "Visual Studio Code"},
                     "description": "Open VS Code"},
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+`"},
                     "description": "Toggle terminal"},
                ],
            },
        },
    },

    "file_explorer": {
        "exe": "explorer.exe",
        "aliases": ["file explorer", "explorer", "files", "my computer", "this pc"],
        "window_title_keywords": ["File Explorer", "This PC", "Documents", "Downloads"],
        "controls": {
            "address_bar":  {"method": "hotkey", "keys": "ctrl+l"},
            "search":       {"method": "hotkey", "keys": "ctrl+e"},
            "rename":       {"method": "hotkey", "keys": "f2"},
            "copy":         {"method": "hotkey", "keys": "ctrl+c"},
            "paste":        {"method": "hotkey", "keys": "ctrl+v"},
            "cut":          {"method": "hotkey", "keys": "ctrl+x"},
            "delete":       {"method": "hotkey", "keys": "delete"},
            "new_folder":   {"method": "hotkey", "keys": "ctrl+shift+n"},
            "select_all":   {"method": "hotkey", "keys": "ctrl+a"},
            "properties":   {"method": "hotkey", "keys": "alt+enter"},
        },
        "workflows": {
            "navigate_to": {
                "description": "Navigate to {path} in File Explorer",
                "params": ["path"],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "file explorer"},
                     "description": "Open File Explorer"},
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+l"},
                     "description": "Focus address bar"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{path}"},
                     "description": "Type path"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Navigate"},
                ],
            },
            "create_folder": {
                "description": "Create a new folder",
                "params": [],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "file explorer"},
                     "description": "Open File Explorer"},
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+shift+n"},
                     "description": "Create new folder"},
                ],
            },
        },
    },

    "whatsapp": {
        "exe": "WhatsApp.exe",
        "aliases": ["whatsapp", "whats app", "wa"],
        "window_title_keywords": ["WhatsApp"],
        "controls": {
            "search":       {"method": "hotkey", "keys": "ctrl+f"},
            "new_chat":     {"method": "hotkey", "keys": "ctrl+n"},
        },
        "workflows": {
            "send_message": {
                "description": "Send message to {contact} on WhatsApp",
                "params": ["contact", "message"],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "whatsapp"},
                     "description": "Open WhatsApp"},
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+f"},
                     "description": "Open search"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{contact}"},
                     "description": "Search for contact"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Select contact"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{message}", "clear_first": False},
                     "description": "Type message"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Send message"},
                ],
            },
            "open_chat": {
                "description": "Open chat with {contact}",
                "params": ["contact"],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "whatsapp"},
                     "description": "Open WhatsApp"},
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "ctrl+f"},
                     "description": "Open search"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{contact}"},
                     "description": "Search for contact"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Open chat"},
                ],
            },
        },
    },

    "tradingview": {
        "exe": "TradingView.exe",
        "aliases": ["tradingview", "trading view", "tv", "charts"],
        "window_title_keywords": ["TradingView"],
        "controls": {
            "symbol_search": {"method": "hotkey", "keys": "/"},
            "indicators":    {"method": "hotkey", "keys": "alt+i"},
            "interval_1m":   {"method": "press", "key": "1"},
            "interval_5m":   {"method": "press", "key": "5"},
            "interval_1h":   {"method": "press", "key": "6"},
            "interval_1d":   {"method": "press", "key": "8"},
            "interval_1w":   {"method": "press", "key": "9"},
            "fullscreen":    {"method": "hotkey", "keys": "shift+f"},
        },
        "workflows": {
            "search_symbol": {
                "description": "Open chart for {symbol} on TradingView",
                "params": ["symbol"],
                "steps": [
                    {"tool": "open_app", "params": {"app_name": "tradingview"},
                     "description": "Open TradingView"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "/"},
                     "description": "Open symbol search"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{symbol}"},
                     "description": "Type symbol"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Load chart"},
                ],
            },
            "add_indicator": {
                "description": "Add indicator {indicator} to the chart",
                "params": ["indicator"],
                "steps": [
                    {"tool": "computer_control", "params": {"action": "hotkey", "keys": "alt+i"},
                     "description": "Open indicators panel"},
                    {"tool": "computer_control", "params": {"action": "smart_type", "text": "{indicator}"},
                     "description": "Search indicator"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "enter"},
                     "description": "Add indicator"},
                    {"tool": "computer_control", "params": {"action": "press", "key": "escape"},
                     "description": "Close panel"},
                ],
            },
        },
    },
}

# ===================================================================
# Alias lookup index (built once at import)
# ===================================================================
_ALIAS_INDEX: dict[str, str] = {}
for _pname, _pdata in APP_PROFILES.items():
    _ALIAS_INDEX[_pname] = _pname
    for _alias in _pdata.get("aliases", []):
        _ALIAS_INDEX[_alias.lower()] = _pname


# ===================================================================
# PUBLIC API
# ===================================================================

def find_profile(app_name: str) -> Optional[dict]:
    """Find a profile by name or alias. Case-insensitive."""
    key = app_name.strip().lower()
    profile_name = _ALIAS_INDEX.get(key)
    if profile_name:
        return APP_PROFILES[profile_name]
    return None


def get_workflow(app_name: str, workflow_name: str, **params) -> Optional[dict]:
    """
    Generate a plan dict from a profile workflow.

    Returns plan compatible with execute_plan():
    {"goal": "...", "steps": [{step, tool, description, parameters}, ...]}
    """
    profile = find_profile(app_name)
    if not profile:
        return None

    workflow = profile.get("workflows", {}).get(workflow_name)
    if not workflow:
        return None

    required = workflow.get("params", [])
    missing = [p for p in required if p not in params]
    if missing:
        log.warning("Workflow '%s' missing params: %s", workflow_name, missing)
        return None

    steps = []
    for i, tmpl in enumerate(workflow["steps"], 1):
        step = {
            "step": i,
            "tool": tmpl["tool"],
            "description": _sub(tmpl["description"], params),
            "parameters": {},
        }
        for k, v in tmpl["params"].items():
            step["parameters"][k] = _sub(v, params) if isinstance(v, str) else v
        steps.append(step)

    goal = _sub(workflow["description"], params)
    log.info("APP_PROFILE: %s.%s -> %d steps", app_name, workflow_name, len(steps))
    return {"goal": goal, "steps": steps}


def match_workflow(goal: str) -> Optional[dict]:
    """
    Match a natural language goal to a profile workflow.
    Returns plan dict or None.
    """
    text = goal.strip().lower()

    # Chrome: search
    m = re.match(
        r"(?:search\s+(?:for\s+)?(.+?)\s+(?:on|in|using)\s+(?:chrome|google|browser)"
        r"|google\s+(.+)"
        r"|search\s+(?:for\s+)?(.+?)\s+(?:on|in)\s+google"
        r")", text)
    if m:
        query = (m.group(1) or m.group(2) or m.group(3)).strip()
        return get_workflow("chrome", "search_web", query=query)

    # Chrome: navigate URL
    m = re.match(
        r"(?:go\s+to|navigate\s+to|open)\s+"
        r"((?:https?://)?[\w][\w.\-]+\.(?:com|org|net|io|dev|edu|gov|co|ai|app|me|tv)[\w/\-?.=&%]*)",
        text)
    if m:
        return get_workflow("chrome", "navigate_url", url=m.group(1).strip())

    # Spotify: search and play
    m = re.match(r"(?:play|search(?:\s+for)?)\s+(.+?)\s+(?:on|in|using)\s+spotify", text)
    if m:
        return get_workflow("spotify", "search_and_play", query=m.group(1).strip())

    # Spotify: play random
    if re.match(
        r"(?:play\s+(?:random|some|any|my)\s+(?:music|songs?)"
        r"|shuffle\s+(?:my\s+)?(?:music|songs?|liked)"
        r"|play\s+liked\s+songs?)", text):
        return get_workflow("spotify", "play_random")

    # VS Code: open file
    m = re.match(
        r"open\s+(?:file\s+)?(.+?)\s+(?:in|on|using)\s+(?:vs\s*code|vscode|visual\s+studio\s+code)",
        text)
    if m:
        return get_workflow("vscode", "open_file", filename=m.group(1).strip())

    # TradingView: search symbol
    m = re.match(
        r"(?:search|open|chart(?:\s+for)?)\s+([A-Z0-9]{2,10})\s+(?:on|in)\s+(?:trading\s*view|tv)",
        text, re.IGNORECASE)
    if m:
        return get_workflow("tradingview", "search_symbol", symbol=m.group(1).upper())

    # WhatsApp: send message
    m = re.match(
        r"send\s+(?:a\s+)?message\s+to\s+(.+?)\s+(?:on|in|via)\s+(?:whatsapp|wa)"
        r"(?:\s+(?:saying|that|:)\s+(.+))?", text)
    if m:
        contact = m.group(1).strip()
        message = (m.group(2) or "").strip()
        if message:
            return get_workflow("whatsapp", "send_message", contact=contact, message=message)
        return get_workflow("whatsapp", "open_chat", contact=contact)

    # WhatsApp: open chat
    m = re.match(r"(?:open|message)\s+(.+?)\s+(?:on|in|via)\s+(?:whatsapp|wa)", text)
    if m:
        return get_workflow("whatsapp", "open_chat", contact=m.group(1).strip())

    return None


def list_profiles() -> list[str]:
    """List all available profile names."""
    return sorted(APP_PROFILES.keys())


def list_workflows(app_name: str) -> list[str]:
    """List workflow names for a profile."""
    profile = find_profile(app_name)
    return sorted(profile.get("workflows", {}).keys()) if profile else []


def _sub(template: str, params: dict) -> str:
    """Replace {placeholder} with actual values."""
    result = template
    for key, value in params.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


# ===================================================================
# SELF-TEST
# ===================================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(f"\n{'-' * 70}")
    print("  App Profiles Self-Test")
    print(f"{'-' * 70}")

    # Test find_profile
    print("\n[1] find_profile():")
    for name in ["chrome", "google chrome", "browser", "spotify", "music",
                 "vscode", "vs code", "file explorer", "whatsapp", "wa",
                 "tradingview", "tv", "unknown_app"]:
        p = find_profile(name)
        found = p["exe"] if p else "NOT FOUND"
        ok = "PASS" if (p is not None) != (name == "unknown_app") else "FAIL"
        print(f"  {name:<25} -> {found:<20} {ok}")

    # Test get_workflow
    print(f"\n[2] get_workflow():")
    for app, wf, kw in [
        ("chrome", "search_web", {"query": "Python tutorials"}),
        ("chrome", "navigate_url", {"url": "youtube.com"}),
        ("spotify", "search_and_play", {"query": "Coldplay"}),
        ("spotify", "play_random", {}),
        ("vscode", "open_file", {"filename": "README.md"}),
        ("tradingview", "search_symbol", {"symbol": "XAUUSD"}),
        ("whatsapp", "send_message", {"contact": "Mom", "message": "Hi!"}),
    ]:
        plan = get_workflow(app, wf, **kw)
        if plan:
            print(f"  {app}.{wf} -> {len(plan['steps'])} steps: {plan['goal']}")
        else:
            print(f"  {app}.{wf} -> FAILED")

    # Test match_workflow
    print(f"\n[3] match_workflow():")
    passed = 0
    tests = [
        ("search Python on chrome", True),
        ("google machine learning", True),
        ("go to youtube.com", True),
        ("play Coldplay on spotify", True),
        ("play random music", True),
        ("shuffle my music", True),
        ("open README.md in vscode", True),
        ("search XAUUSD on tradingview", True),
        ("send message to Mom on whatsapp saying hi", True),
        ("open Mom on whatsapp", True),
        ("what is the weather", False),
        ("tell me a joke", False),
        ("play some songs", True),
        ("search for AI on google", True),
    ]
    for goal, expect in tests:
        plan = match_workflow(goal)
        ok = (plan is not None) == expect
        passed += ok
        status = "PASS" if ok else "FAIL"
        r = f"{plan['goal'][:40]}" if plan else "None"
        print(f"  {goal:<50} -> {r:<42} {status}")

    print(f"\n{'-' * 70}")
    print(f"  match_workflow: {passed}/{len(tests)} passed")
    print(f"{'-' * 70}")
