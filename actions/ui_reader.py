"""
UI Reader — Reads the Windows UI Accessibility Tree to "see" the screen
like a human, without taking screenshots.

Every Windows app exposes its UI elements (buttons, text fields, menus,
labels, etc.) through the UI Automation framework. This module reads
that tree and returns structured data about what's on screen.

This is INSTANT (no API call), FREE (no cloud), and PRECISE (exact
coordinates). Falls back to screenshot+vision only when the accessibility
tree is empty (games, custom-rendered UIs).

Used by: computer_control, planner (for context-aware planning)
"""

import logging
import time

log = logging.getLogger(__name__)

try:
    import uiautomation as auto
    _UIA = True
except ImportError:
    _UIA = False
    log.warning("uiautomation not installed — UI reading disabled")


def get_focused_window_elements(max_depth: int = 4, max_elements: int = 60) -> list[dict]:
    """
    Read all interactive UI elements from the currently focused window.
    
    Returns a list of dicts:
    {
        "name": "Play",           # visible text / label
        "type": "Button",         # control type
        "x": 450, "y": 320,      # center coordinates
        "enabled": True,
        "value": "",              # current value (for text fields)
    }
    
    This is what Jarvis "sees" — like a human scanning the screen.
    """
    if not _UIA:
        return []

    try:
        # Get the focused window
        window = auto.GetForegroundControl()
        if not window:
            return []

        elements = []
        _walk(window, elements, depth=0, max_depth=max_depth, max_elements=max_elements)
        return elements

    except Exception as e:
        log.warning("UI reader failed: %s", e)
        return []


def _walk(control, elements: list, depth: int, max_depth: int, max_elements: int):
    """Recursively walk the UI tree and collect interactive elements."""
    if depth > max_depth or len(elements) >= max_elements:
        return

    try:
        name = control.Name or ""
        ctrl_type = control.ControlTypeName or ""
        
        # Only collect meaningful elements (skip containers, groups, etc.)
        interactive_types = {
            "ButtonControl", "EditControl", "TextControl",
            "HyperlinkControl", "ListItemControl", "MenuItemControl",
            "TabItemControl", "CheckBoxControl", "RadioButtonControl",
            "ComboBoxControl", "SliderControl", "TreeItemControl",
            "DataItemControl", "ImageControl",
        }
        
        if ctrl_type in interactive_types and name.strip():
            rect = control.BoundingRectangle
            if rect and rect.width() > 0 and rect.height() > 0:
                cx = rect.left + rect.width() // 2
                cy = rect.top + rect.height() // 2
                
                # Get value for text fields
                value = ""
                if ctrl_type == "EditControl":
                    try:
                        value = control.GetValuePattern().Value or ""
                    except Exception:
                        pass
                
                elements.append({
                    "name": name.strip()[:80],  # cap length
                    "type": ctrl_type.replace("Control", ""),
                    "x": cx,
                    "y": cy,
                    "w": rect.width(),
                    "h": rect.height(),
                    "enabled": control.IsEnabled,
                    "value": value[:100] if value else "",
                })

        # Recurse into children
        for child in control.GetChildren():
            if len(elements) >= max_elements:
                break
            _walk(child, elements, depth + 1, max_depth, max_elements)

    except Exception:
        pass  # Some controls throw — skip silently


def get_screen_summary(app_name: str = None) -> str:
    """
    Get a human-readable summary of what's visible on screen.
    This is what gets sent to the planner for context-aware planning.
    
    Returns something like:
    "Spotify window is active. Visible elements:
     - Button: 'Play' at (450, 320)
     - Button: 'Shuffle' at (380, 320)
     - ListItem: 'Songs' at (120, 200)
     - EditControl: 'Search' at (200, 50)"
    """
    if not _UIA:
        return "UI Automation not available"

    try:
        window = auto.GetForegroundControl()
        window_title = window.Name if window else "Unknown"
    except Exception:
        window_title = "Unknown"

    elements = get_focused_window_elements()
    
    if not elements:
        return f"Window: {window_title}. No readable UI elements found (may need screenshot+vision)."

    lines = [f"Window: {window_title}. Visible elements ({len(elements)}):"]
    for el in elements:
        line = f"  - {el['type']}: '{el['name']}' at ({el['x']}, {el['y']})"
        if el.get("value"):
            line += f" [value: '{el['value']}']"
        if not el.get("enabled"):
            line += " [disabled]"
        lines.append(line)
    
    return "\n".join(lines)


def find_element_by_name(name: str) -> dict | None:
    """
    Find a specific UI element by name (case-insensitive substring match).
    Returns the element dict with exact coordinates, or None.
    
    This replaces the slow screenshot→Gemini Vision→coordinates flow
    for elements that have proper accessibility labels.
    """
    elements = get_focused_window_elements()
    name_lower = name.lower()
    
    # Tier 1: Exact match
    for el in elements:
        if el["name"].lower() == name_lower:
            return el
    
    # Tier 2: Substring match
    for el in elements:
        if name_lower in el["name"].lower():
            return el
    
    # Tier 3: Any word matches
    name_words = [w for w in name_lower.split() if len(w) > 2]
    for el in elements:
        el_lower = el["name"].lower()
        if any(w in el_lower for w in name_words):
            return el
    
    return None


def click_element_by_name(name: str) -> str:
    """
    Find and click a UI element by name using accessibility tree.
    FAST PATH: No screenshot, no API call, instant click.
    
    Returns result string or None if element not found
    (caller should fall back to screenshot+vision).
    """
    el = find_element_by_name(name)
    if not el:
        return None  # Signal: fall back to vision
    
    if not el.get("enabled", True):
        return f"Element '{el['name']}' found but is disabled"
    
    try:
        import pyautogui
        pyautogui.click(el["x"], el["y"])
        log.info("⚡ Fast-clicked '%s' at (%d, %d) via UI Automation",
                 el["name"], el["x"], el["y"])
        return f"Clicked '{el['name']}' at ({el['x']}, {el['y']}) [instant]"
    except Exception as e:
        return f"Click failed: {e}"
