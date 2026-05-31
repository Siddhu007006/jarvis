"""
Memory Manager — Unlimited persistent memory for Jarvis.
Remembers everything from day 1.

Three storage layers:
  1. long_term.json    — Identity, preferences, relationships (no size limit)
  2. conversations.jsonl — Append-only log of every interaction
  3. workflows.json    — Cached successful task plans for instant replay

v4.0: Removed the 2200-char limit. Memory grows indefinitely.
      Added conversation history and workflow caching.
"""

import json
from datetime import datetime
from threading import Lock
from pathlib import Path

MEMORY_DIR   = Path(__file__).parent
MEMORY_PATH  = MEMORY_DIR / "long_term.json"
CONVO_PATH   = MEMORY_DIR / "conversations.jsonl"
WORKFLOW_PATH = MEMORY_DIR / "workflows.json"

_lock = Lock()

# ── In-process cache (loaded once) ────────────────────────────
_cache: dict | None = None
_workflow_cache: dict | None = None


def _empty() -> dict:
    return {
        "identity": {},
        "preferences": {},
        "projects": {},
        "relationships": {},
        "notes": {},
    }


def _ensure_cache() -> dict:
    """Load memory into cache if not already loaded."""
    global _cache
    if _cache is not None:
        return _cache

    if not MEMORY_PATH.exists():
        _cache = _empty()
        return _cache

    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            base = _empty()
            for key in base:
                if key not in data:
                    data[key] = {}
            _cache = data
        except Exception:
            _cache = _empty()

    return _cache


def load() -> dict:
    """Load memory (from cache after first call)."""
    return _ensure_cache()


def save(memory: dict):
    """Save memory to disk. NO size limit — memory grows forever."""
    global _cache
    if not isinstance(memory, dict):
        return

    _cache = memory

    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )


def remember(category: str, key: str, value: str) -> str:
    """Save a fact to memory. No size limit."""
    valid = {"identity", "preferences", "projects", "relationships", "notes"}
    if category not in valid:
        category = "notes"

    memory = load()
    memory[category][key] = {
        "value": value[:500],  # individual values capped at 500 chars, but no overall limit
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    save(memory)
    print(f"[Memory] 💾 {category}/{key} = {value}")
    return f"Remembered: {key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    """Remove a fact from memory."""
    memory = load()
    cat = memory.get(category, {})
    if key in cat:
        del cat[key]
        save(memory)
        return f"Forgotten: {key}"
    return f"Not found: {key}"


# ── Conversation History (append-only log) ─────────────────────

def log_conversation(role: str, text: str, tools: list[str] | None = None):
    """
    Append a conversation entry to the log.
    Every Q&A is recorded with a timestamp — never deleted.
    
    role: "user" or "jarvis"
    text: what was said
    tools: optional list of tools that were used
    """
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "role": role,
        "text": text[:1000],  # cap individual entries
    }
    if tools:
        entry["tools"] = tools

    CONVO_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with open(CONVO_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_recent_conversations(count: int = 20) -> list[dict]:
    """Get the last N conversation entries."""
    if not CONVO_PATH.exists():
        return []
    try:
        lines = CONVO_PATH.read_text(encoding="utf-8").strip().split("\n")
        entries = []
        for line in lines[-count:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries
    except Exception:
        return []


# ── Workflow Memory (cached successful plans) ──────────────────

def _load_workflows() -> dict:
    """Load workflow cache."""
    global _workflow_cache
    if _workflow_cache is not None:
        return _workflow_cache

    if not WORKFLOW_PATH.exists():
        _workflow_cache = {}
        return _workflow_cache

    try:
        _workflow_cache = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    except Exception:
        _workflow_cache = {}

    return _workflow_cache


def save_workflow(goal: str, plan: dict):
    """
    Cache a successful task plan.
    Next time the same goal is requested, we can skip the planner
    and replay the cached steps instantly.
    """
    workflows = _load_workflows()
    key = goal.lower().strip()

    if key in workflows:
        workflows[key]["use_count"] = workflows[key].get("use_count", 0) + 1
        workflows[key]["last_success"] = datetime.now().strftime("%Y-%m-%d")
        workflows[key]["plan"] = plan
    else:
        workflows[key] = {
            "first_used": datetime.now().strftime("%Y-%m-%d"),
            "last_success": datetime.now().strftime("%Y-%m-%d"),
            "use_count": 1,
            "plan": plan,
        }

    global _workflow_cache
    _workflow_cache = workflows

    WORKFLOW_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        WORKFLOW_PATH.write_text(
            json.dumps(workflows, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )


def get_cached_workflow(goal: str) -> dict | None:
    """
    Check if we have a cached plan for this goal.
    Returns the plan dict if found, None otherwise.
    """
    workflows = _load_workflows()
    key = goal.lower().strip()

    # Exact match
    if key in workflows:
        return workflows[key]["plan"]

    # Fuzzy: check if any cached goal is a substring
    for cached_goal, data in workflows.items():
        if cached_goal in key or key in cached_goal:
            return data["plan"]

    return None


# ── Format for System Prompt ───────────────────────────────────

def format_for_prompt() -> str:
    """
    Format memory + recent conversations as context for the system prompt.
    This is what makes Jarvis 'remember' across sessions.
    """
    memory = load()
    lines = []

    # Identity
    identity = memory.get("identity", {})
    for field in ["name", "age", "city", "job", "language", "school"]:
        entry = identity.get(field)
        if entry:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"{field.title()}: {val}")

    # Preferences
    prefs = memory.get("preferences", {})
    if prefs:
        lines.append("")
        lines.append("Preferences:")
        for key, entry in list(prefs.items())[:15]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    # Projects
    projects = memory.get("projects", {})
    if projects:
        lines.append("")
        lines.append("Active Projects:")
        for key, entry in list(projects.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    # Relationships
    rels = memory.get("relationships", {})
    if rels:
        lines.append("")
        lines.append("People:")
        for key, entry in list(rels.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    # Recent conversations (last 5 for context)
    recent = get_recent_conversations(5)
    if recent:
        lines.append("")
        lines.append("Recent conversation:")
        for entry in recent:
            role = "You" if entry["role"] == "user" else "Jarvis"
            lines.append(f"  [{entry.get('ts', '')}] {role}: {entry['text'][:150]}")

    if not lines:
        return ""

    header = "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite]\n"
    return header + "\n".join(lines) + "\n"
