"""
tools/ui_controller.py — Primary UI Automation Orchestration Layer
═══════════════════════════════════════════════════════════════════

Architecture position:
    Gemini/Planner  →  ui_controller.py  →  win_automation.py  →  Windows APIs

This is the SINGLE ENTRY POINT for all desktop UI automation in Jarvis.
No other module should directly import win_automation.py for new code.

Execution hierarchy (tried in order, stops at first success):
    Tier 1: UI Automation  — uiautomation + pywinauto element tree (10ms, 98% accuracy)
    Tier 2: Keyboard       — pyautogui hotkeys / SendKeys (5ms, 100% for known shortcuts)
    Tier 3: Win32 API      — ctypes / win32gui direct window manipulation (5ms)
    Tier 4: OCR            — pytesseract screen text matching (~1s)
    Tier 5: Vision         — moondream2 or Gemini screenshot analysis (~3s, last resort)

Concept — Why this ordering matters:
    Screenshot-based automation (the old default) is fundamentally unreliable because:
      1. Vision models hallucinate coordinates
      2. You can't screenshot a minimized window
      3. DPI scaling and resolution changes break pixel coordinates
      4. It takes 2-5 seconds per action (screenshot + model inference)

    UI Automation accesses the real OS widget tree directly — like a screen
    reader. It knows every button, textbox, tab, and menu by NAME, not pixels.
    It works on minimized windows, ignores DPI, and takes <10ms.

Dependencies (all in requirements.txt):
    uiautomation, pywinauto, pygetwindow, pywin32, pyautogui, comtypes, pycaw
"""

import logging
import time
import re
from typing import Optional

log = logging.getLogger("JARVIS.ui_controller")

# ── Tier 1 imports: UI Automation ─────────────────────────────

try:
    import uiautomation as auto
    _UIA_OK = True
except ImportError:
    _UIA_OK = False
    log.warning("uiautomation not installed — Tier 1 disabled")

try:
    from pywinauto import Desktop, Application
    from pywinauto.findwindows import ElementNotFoundError
    _PYWINAUTO_OK = True
except ImportError:
    _PYWINAUTO_OK = False
    log.warning("pywinauto not installed — advanced patterns disabled")

# ── Tier 2 imports: Keyboard ──────────────────────────────────

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.02
    _PYAUTOGUI_OK = True
except ImportError:
    _PYAUTOGUI_OK = False

# ── Tier 3 imports: Win32 ─────────────────────────────────────

try:
    import win32gui
    import win32con
    _WIN32_OK = True
except ImportError:
    _WIN32_OK = False

try:
    import pygetwindow as gw
    _GW_OK = True
except ImportError:
    _GW_OK = False

# ── Backend layer import ──────────────────────────────────────

try:
    from tools.win_automation import WinAutomation
    _BACKEND_OK = True
except ImportError:
    _BACKEND_OK = False
    log.warning("win_automation backend not available")


# ═══════════════════════════════════════════════════════════════
#  COMMON KEYBOARD SHORTCUTS DATABASE
# ═══════════════════════════════════════════════════════════════

SHORTCUTS = {
    # File operations
    "new file":       ("ctrl", "n"),
    "open file":      ("ctrl", "o"),
    "save":           ("ctrl", "s"),
    "save as":        ("ctrl", "shift", "s"),
    "close":          ("ctrl", "w"),
    "close tab":      ("ctrl", "w"),
    "undo":           ("ctrl", "z"),
    "redo":           ("ctrl", "y"),
    "copy":           ("ctrl", "c"),
    "paste":          ("ctrl", "v"),
    "cut":            ("ctrl", "x"),
    "select all":     ("ctrl", "a"),
    "find":           ("ctrl", "f"),
    "replace":        ("ctrl", "h"),
    "print":          ("ctrl", "p"),
    # Navigation
    "new tab":        ("ctrl", "t"),
    "next tab":       ("ctrl", "tab"),
    "prev tab":       ("ctrl", "shift", "tab"),
    "address bar":    ("ctrl", "l"),
    "refresh":        ("f5",),
    "back":           ("alt", "left"),
    "forward":        ("alt", "right"),
    "home":           ("alt", "home"),
    # Window management
    "minimize":       ("win", "down"),
    "maximize":       ("win", "up"),
    "switch app":     ("alt", "tab"),
    "task view":      ("win", "tab"),
    "close window":   ("alt", "f4"),
    "snap left":      ("win", "left"),
    "snap right":     ("win", "right"),
    # System
    "lock screen":    ("win", "l"),
    "run dialog":     ("win", "r"),
    "file explorer":  ("win", "e"),
    "settings":       ("win", "i"),
    "desktop":        ("win", "d"),
    "screenshot":     ("win", "shift", "s"),
    "task manager":   ("ctrl", "shift", "escape"),
}


# ═══════════════════════════════════════════════════════════════
#  UI CONTROLLER — Primary Orchestration Class
# ═══════════════════════════════════════════════════════════════

class UIController:
    """
    Primary UI automation orchestration layer.

    All Jarvis UI interactions route through this class. It decides
    which tier to use, handles retries, and delegates low-level
    operations to win_automation.py as the backend driver.

    Architecture:
        Planner/Engine → UIController.interact() → [5-tier cascade]
                                                        ↓
                                                  win_automation.py
                                                        ↓
                                                  Windows APIs
    """

    # ── Retry / confidence config ─────────────────────────
    MAX_RETRIES = 2
    RETRY_DELAY = 0.3  # seconds between retries

    # ═══════════════════════════════════════════════════════
    #  MASTER DISPATCHER
    # ═══════════════════════════════════════════════════════

    @classmethod
    def interact(cls, action: str, target: str = "",
                 window: str = "", text: str = "",
                 menu_path: str = "") -> str:
        """
        Master entry point for ALL UI interactions.

        This is the ONLY function external modules should call.
        Routes to the correct handler based on action type.

        Args:
            action: What to do — click_button, type_into, select_tab,
                    select_menu, send_hotkey, focus_window, list_windows,
                    list_elements, close_window, get_text
            target: Name of the UI element / shortcut / window
            window: Optional window title to scope the search
            text:   Text to type (for type_into)
            menu_path: Menu path like "File > Save As" (for select_menu)

        Returns:
            Human-readable result string
        """
        action = action.lower().strip()
        log.info("🎯 UIController.interact(action=%s, target='%s', "
                 "window='%s')", action, target[:60], window[:40])

        try:
            if action == "click_button":
                return cls.click_button(target, window)

            elif action == "type_into":
                return cls.type_into_field(target, text, window)

            elif action == "select_tab":
                return cls.select_tab(target, window)

            elif action == "select_menu":
                path = menu_path or target
                return cls.select_menu_item(path, window)

            elif action == "send_hotkey":
                return cls.send_hotkey(target)

            elif action == "send_keys":
                return cls.send_keys(text or target)

            elif action == "focus_window":
                return cls.focus_window(target or window)

            elif action == "list_windows":
                return cls.list_windows()

            elif action == "list_elements":
                return cls.get_element_tree(window)

            elif action == "close_window":
                return cls.close_window(target or window)

            elif action == "get_text":
                return cls.get_element_text(target, window)

            else:
                return f"Unknown ui_control action: '{action}'"

        except Exception as e:
            log.error("UIController.interact failed: %s", e, exc_info=True)
            return f"UI interaction failed: {e}"

    # ═══════════════════════════════════════════════════════
    #  WINDOW MANAGEMENT
    # ═══════════════════════════════════════════════════════

    @classmethod
    def focus_window(cls, name: str) -> str:
        """
        Find, raise, and focus a window by name. Works even if minimized.

        Tier cascade:
          1. pygetwindow (title substring match)
          2. win32gui EnumWindows (direct Windows API)
          3. Backend WinAutomation.raise_window fallback

        Concept: Windows can be minimized, behind other windows, or on
        another virtual desktop. We try multiple APIs because no single
        one works in all scenarios.
        """
        if not name:
            return "No window name provided."

        name_lower = name.lower().strip()

        # ── Tier 1: pygetwindow ──
        if _GW_OK:
            try:
                for win in gw.getAllWindows():
                    if win.title and name_lower in win.title.lower():
                        if win.isMinimized:
                            win.restore()
                            time.sleep(0.3)
                        win.activate()
                        time.sleep(0.2)
                        log.info("✅ Focused '%s' via pygetwindow", win.title)
                        return f"Focused window: {win.title}"
            except Exception as e:
                log.debug("pygetwindow focus failed: %s", e)

        # ── Tier 2: win32gui ──
        if _WIN32_OK:
            try:
                found_hwnd = cls._find_hwnd(name_lower)
                if found_hwnd:
                    win32gui.ShowWindow(found_hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(found_hwnd)
                    time.sleep(0.2)
                    title = win32gui.GetWindowText(found_hwnd)
                    log.info("✅ Focused '%s' via win32gui", title)
                    return f"Focused window: {title}"
            except Exception as e:
                log.debug("win32gui focus failed: %s", e)

        # ── Tier 3: Backend fallback ──
        if _BACKEND_OK:
            if WinAutomation.raise_window(name):
                return f"Focused window: {name}"

        return f"Could not find window matching '{name}'"

    @classmethod
    def list_windows(cls) -> str:
        """Return all visible window titles as a formatted string."""
        titles = []

        if _GW_OK:
            try:
                for win in gw.getAllWindows():
                    t = win.title.strip()
                    if t and len(t) > 2:
                        titles.append(t)
            except Exception:
                pass

        if not titles and _WIN32_OK:
            try:
                def _cb(hwnd, results):
                    if win32gui.IsWindowVisible(hwnd):
                        t = win32gui.GetWindowText(hwnd).strip()
                        if t and len(t) > 2:
                            results.append(t)
                    return True
                win32gui.EnumWindows(_cb, titles)
            except Exception:
                pass

        if titles:
            return "Open windows:\n" + "\n".join(f"  • {t}" for t in titles[:30])
        return "Could not enumerate windows."

    @classmethod
    def close_window(cls, name: str) -> str:
        """Close a window by name. Delegates to backend."""
        if _BACKEND_OK:
            return WinAutomation.close_app(name)

        # Direct win32 approach
        if _WIN32_OK:
            hwnd = cls._find_hwnd(name.lower())
            if hwnd:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                return f"Sent close signal to '{name}'."

        return f"Could not close '{name}'."

    # ═══════════════════════════════════════════════════════
    #  ELEMENT INTERACTION — 5-TIER CASCADE
    # ═══════════════════════════════════════════════════════

    @classmethod
    def click_button(cls, name: str, window: str = "") -> str:
        """
        Find and click a button/link/element by its accessible name.

        5-tier cascade:
          1. uiautomation tree walk → InvokePattern.Invoke() or Click()
          2. pywinauto child_window match → click_input()
          3. Keyboard shortcut (if name matches known shortcuts)
          4. OCR text search on screen → coordinate click
          5. Vision model fallback (absolute last resort)

        Concept — InvokePattern:
            Windows UI Automation exposes "patterns" on controls. A button
            exposes InvokePattern, which means "this thing can be clicked."
            Calling Invoke() is like a real click but doesn't need coordinates
            and works even if the button is partially obscured.
        """
        if not name:
            return "No element name provided."

        # Focus the target window first
        if window:
            cls.focus_window(window)
            time.sleep(0.3)

        for attempt in range(cls.MAX_RETRIES + 1):
            # ── Tier 1: uiautomation ──
            result = cls._click_via_uia(name, window)
            if result:
                return result

            # ── Tier 2: pywinauto ──
            result = cls._click_via_pywinauto(name, window)
            if result:
                return result

            # ── Tier 3: Keyboard shortcut ──
            shortcut_keys = SHORTCUTS.get(name.lower().strip())
            if shortcut_keys:
                return cls.send_hotkey("+".join(shortcut_keys))

            if attempt < cls.MAX_RETRIES:
                log.debug("Retry %d/%d for '%s'",
                          attempt + 1, cls.MAX_RETRIES, name)
                time.sleep(cls.RETRY_DELAY)

        # ── Tier 4: OCR text matching ──
        result = cls._click_via_ocr(name)
        if result:
            return result

        # ── Tier 5: Vision fallback (last resort) ──
        result = cls._click_via_vision(name)
        if result:
            return result

        return (f"Could not find '{name}' using any method "
                f"(UI Automation, pywinauto, keyboard, OCR, vision).")

    @classmethod
    def type_into_field(cls, field_name: str, text: str,
                        window: str = "") -> str:
        """
        Type text into a named field.

        Strategy:
          1. Find the edit control via uiautomation
          2. Try ValuePattern.SetValue() (direct, no key simulation)
          3. Fallback: click the field, then Ctrl+A → paste via clipboard

        Concept — ValuePattern:
            Edit controls expose ValuePattern, which lets you set their
            text content directly without simulating keystrokes. This is
            faster, more reliable, and handles Unicode correctly.
        """
        if not text:
            return "No text provided to type."

        if window:
            cls.focus_window(window)
            time.sleep(0.3)

        # ── Tier 1: uiautomation SetValue ──
        if _UIA_OK:
            try:
                root = cls._get_search_root(window)
                element = cls._find_element_in_tree(
                    root, field_name, control_types=["EditControl"])

                if element:
                    # Try ValuePattern first (direct text injection)
                    try:
                        vp = element.GetValuePattern()
                        if vp:
                            vp.SetValue(text)
                            log.info("✅ Typed into '%s' via ValuePattern "
                                     "[Tier 1]", field_name)
                            return (f"Typed into '{field_name}' via "
                                    f"ValuePattern [Tier 1]")
                    except Exception:
                        pass

                    # Fallback: click + paste
                    try:
                        element.Click()
                        time.sleep(0.1)
                        cls._paste_text(text)
                        log.info("✅ Typed into '%s' via click+paste "
                                 "[Tier 1b]", field_name)
                        return (f"Typed into '{field_name}' via click+paste "
                                f"[Tier 1b]")
                    except Exception as e:
                        log.debug("UIA click+paste failed: %s", e)
            except Exception as e:
                log.debug("Tier 1 type_into failed: %s", e)

        # ── Tier 2: pywinauto ──
        if _PYWINAUTO_OK and window:
            try:
                app = Application(backend="uia").connect(
                    title_re=f".*{re.escape(window)}.*", timeout=3)
                dlg = app.top_window()
                ctrl = dlg.child_window(
                    title_re=f".*{re.escape(field_name)}.*",
                    control_type="Edit")
                ctrl.set_edit_text(text)
                log.info("✅ Typed into '%s' via pywinauto [Tier 2]",
                         field_name)
                return f"Typed into '{field_name}' via pywinauto [Tier 2]"
            except Exception as e:
                log.debug("pywinauto type_into failed: %s", e)

        # ── Tier 3: Blind type (focus last known field, paste) ──
        if _PYAUTOGUI_OK:
            cls._paste_text(text)
            log.info("✅ Typed text via blind paste [Tier 3]")
            return "Typed text into focused field via paste [Tier 3]"

        return f"Could not type into '{field_name}'."

    @classmethod
    def select_tab(cls, tab_name: str, window: str = "") -> str:
        """
        Switch to a named tab in an application.

        Strategy:
          1. Find TabItemControl via uiautomation → SelectionItemPattern
          2. pywinauto child_window match → select()
          3. Keyboard: Ctrl+Tab cycling (last resort)

        Concept — SelectionItemPattern:
            Tabs expose SelectionItemPattern. Calling Select() on a tab
            item switches to it without clicking coordinates.
        """
        if window:
            cls.focus_window(window)
            time.sleep(0.3)

        # ── Tier 1: uiautomation ──
        if _UIA_OK:
            try:
                root = cls._get_search_root(window)
                element = cls._find_element_in_tree(
                    root, tab_name, control_types=["TabItemControl"])

                if element:
                    try:
                        sp = element.GetSelectionItemPattern()
                        if sp:
                            sp.Select()
                            log.info("✅ Selected tab '%s' via "
                                     "SelectionItemPattern [Tier 1]",
                                     tab_name)
                            return (f"Selected tab '{tab_name}' "
                                    f"[Tier 1: SelectionItemPattern]")
                    except Exception:
                        pass

                    # Fallback: click the tab
                    try:
                        element.Click()
                        log.info("✅ Clicked tab '%s' [Tier 1b]", tab_name)
                        return f"Selected tab '{tab_name}' [Tier 1b: Click]"
                    except Exception:
                        pass
            except Exception as e:
                log.debug("Tab selection via UIA failed: %s", e)

        # ── Tier 2: pywinauto ──
        if _PYWINAUTO_OK and window:
            try:
                app = Application(backend="uia").connect(
                    title_re=f".*{re.escape(window)}.*", timeout=3)
                dlg = app.top_window()
                tab = dlg.child_window(
                    title_re=f".*{re.escape(tab_name)}.*",
                    control_type="TabItem")
                tab.select()
                log.info("✅ Selected tab '%s' via pywinauto [Tier 2]",
                         tab_name)
                return f"Selected tab '{tab_name}' [Tier 2: pywinauto]"
            except Exception as e:
                log.debug("pywinauto tab select failed: %s", e)

        return f"Could not find tab '{tab_name}'."

    @classmethod
    def select_menu_item(cls, menu_path: str, window: str = "") -> str:
        """
        Navigate and select a menu item.

        Args:
            menu_path: Path like "File > Save As" or "Edit > Find"
            window: Target window name

        Strategy:
          1. Parse the path into segments
          2. Walk the UIA MenuBar → MenuItem hierarchy
          3. Fallback: keyboard Alt+key navigation
        """
        if window:
            cls.focus_window(window)
            time.sleep(0.3)

        parts = [p.strip() for p in menu_path.split(">")]
        if not parts:
            return "No menu path provided."

        # ── Tier 1: pywinauto menu_select ──
        if _PYWINAUTO_OK and window:
            try:
                app = Application(backend="uia").connect(
                    title_re=f".*{re.escape(window)}.*", timeout=3)
                dlg = app.top_window()
                dlg.menu_select("->".join(parts))
                log.info("✅ Selected menu '%s' via pywinauto [Tier 1]",
                         menu_path)
                return (f"Selected menu '{menu_path}' "
                        f"[Tier 1: pywinauto]")
            except Exception as e:
                log.debug("pywinauto menu_select failed: %s", e)

        # ── Tier 2: UIA sequential click ──
        if _UIA_OK:
            try:
                root = cls._get_search_root(window)
                for i, part in enumerate(parts):
                    el = cls._find_element_in_tree(
                        root, part,
                        control_types=["MenuItemControl", "MenuBarItemControl"])
                    if el:
                        el.Click()
                        time.sleep(0.3)
                        # After first click, search from desktop root
                        # because menus may open as new top-level windows
                        root = auto.GetRootControl()
                    else:
                        return (f"Menu item '{part}' not found "
                                f"at depth {i} of '{menu_path}'.")

                log.info("✅ Selected menu '%s' via UIA chain [Tier 2]",
                         menu_path)
                return f"Selected menu '{menu_path}' [Tier 2: UIA chain]"
            except Exception as e:
                log.debug("UIA menu chain failed: %s", e)

        # ── Tier 3: Keyboard Alt-navigation ──
        if _PYAUTOGUI_OK and len(parts) >= 1:
            try:
                # Press Alt to activate the menu bar
                pyautogui.press("alt")
                time.sleep(0.3)
                for part in parts:
                    pyautogui.typewrite(part[0], interval=0.1)
                    time.sleep(0.3)
                log.info("✅ Navigated menu '%s' via Alt keys [Tier 3]",
                         menu_path)
                return (f"Navigated menu '{menu_path}' "
                        f"[Tier 3: Alt key nav]")
            except Exception as e:
                log.debug("Alt key menu navigation failed: %s", e)

        return f"Could not navigate menu '{menu_path}'."

    # ═══════════════════════════════════════════════════════
    #  KEYBOARD
    # ═══════════════════════════════════════════════════════

    @classmethod
    def send_hotkey(cls, keys_str: str) -> str:
        """
        Send a keyboard shortcut.

        Args:
            keys_str: Key combination like "ctrl+s", "alt+f4", "ctrl+shift+n"
                      Also accepts natural language like "save", "undo"

        Concept: Keyboard shortcuts are the most reliable automation method
        after UI Automation. They bypass the UI entirely and work in any
        application, even custom-rendered ones without accessibility trees.
        """
        if not _PYAUTOGUI_OK:
            return "pyautogui not available for keyboard input."

        # Check if it's a named shortcut
        shortcut_keys = SHORTCUTS.get(keys_str.lower().strip())
        if shortcut_keys:
            pyautogui.hotkey(*shortcut_keys)
            log.info("✅ Sent named shortcut '%s' → %s", keys_str,
                     shortcut_keys)
            return f"Sent shortcut: {keys_str} ({'+'.join(shortcut_keys)})"

        # Parse explicit key combination
        keys = [k.strip() for k in keys_str.split("+")]
        pyautogui.hotkey(*keys)
        log.info("✅ Sent hotkey: %s", keys_str)
        return f"Sent hotkey: {keys_str}"

    @classmethod
    def send_keys(cls, text: str) -> str:
        """Type text into the currently focused field."""
        if not _PYAUTOGUI_OK:
            return "pyautogui not available for keyboard input."

        cls._paste_text(text)
        return f"Typed: {text[:60]}"

    # ═══════════════════════════════════════════════════════
    #  ELEMENT TREE (for planner context)
    # ═══════════════════════════════════════════════════════

    @classmethod
    def get_element_tree(cls, window: str = "") -> str:
        """
        Return a structured list of all interactive elements in a window.
        Used by the Planner to understand what's on screen WITHOUT
        taking a screenshot.

        Returns a formatted string suitable for LLM context injection.
        """
        if not _UIA_OK:
            return "UI Automation not available."

        try:
            root = cls._get_search_root(window)
            if not root:
                return "Could not find target window."

            try:
                title = root.Name or "Unknown"
            except Exception:
                title = "Unknown"

            elements = []
            cls._walk_tree(root, elements, depth=0, max_depth=5,
                           max_elements=80)

            if not elements:
                return (f"Window '{title}': No readable UI elements. "
                        f"This app may need OCR or vision fallback.")

            lines = [f"Window: {title} ({len(elements)} elements):"]
            for el in elements:
                line = f"  {el['type']}: \"{el['name']}\""
                if el.get("value"):
                    line += f" [value=\"{el['value']}\"]"
                if not el.get("enabled"):
                    line += " [DISABLED]"
                lines.append(line)

            return "\n".join(lines)

        except Exception as e:
            return f"Failed to read element tree: {e}"

    @classmethod
    def get_element_text(cls, element_name: str,
                         window: str = "") -> str:
        """Get the current text/value of a named element."""
        if not _UIA_OK:
            return "UI Automation not available."

        try:
            root = cls._get_search_root(window)
            el = cls._find_element_in_tree(root, element_name)
            if el:
                try:
                    vp = el.GetValuePattern()
                    if vp:
                        return vp.Value or "(empty)"
                except Exception:
                    pass
                return el.Name or "(no text)"
            return f"Element '{element_name}' not found."
        except Exception as e:
            return f"get_text failed: {e}"

    # ═══════════════════════════════════════════════════════
    #  PRIVATE: Tier 1 — uiautomation
    # ═══════════════════════════════════════════════════════

    @classmethod
    def _click_via_uia(cls, name: str, window: str) -> Optional[str]:
        """Tier 1: Find and click via uiautomation tree."""
        if not _UIA_OK:
            return None

        try:
            root = cls._get_search_root(window)
            element = cls._find_element_in_tree(root, name)

            if not element:
                return None

            # Try InvokePattern first (cleanest, no coordinates)
            try:
                ip = element.GetInvokePattern()
                if ip:
                    ip.Invoke()
                    log.info("✅ Clicked '%s' via InvokePattern [Tier 1]",
                             name)
                    return (f"Clicked '{name}' [Tier 1: "
                            f"InvokePattern — no coordinates]")
            except Exception:
                pass

            # Try TogglePattern (for checkboxes)
            try:
                tp = element.GetTogglePattern()
                if tp:
                    tp.Toggle()
                    log.info("✅ Toggled '%s' via TogglePattern [Tier 1]",
                             name)
                    return f"Toggled '{name}' [Tier 1: TogglePattern]"
            except Exception:
                pass

            # Fallback: coordinate click
            try:
                element.Click()
                log.info("✅ Clicked '%s' via UIA Click [Tier 1b]", name)
                return f"Clicked '{name}' [Tier 1b: UIA Click]"
            except Exception:
                pass

        except Exception as e:
            log.debug("UIA click failed: %s", e)

        return None

    @classmethod
    def _click_via_pywinauto(cls, name: str,
                             window: str) -> Optional[str]:
        """Tier 2: Find and click via pywinauto."""
        if not _PYWINAUTO_OK or not window:
            return None

        try:
            app = Application(backend="uia").connect(
                title_re=f".*{re.escape(window)}.*", timeout=3)
            dlg = app.top_window()
            ctrl = dlg.child_window(
                title_re=f".*{re.escape(name)}.*",
                found_index=0)
            ctrl.click_input()
            log.info("✅ Clicked '%s' via pywinauto [Tier 2]", name)
            return f"Clicked '{name}' [Tier 2: pywinauto]"
        except Exception as e:
            log.debug("pywinauto click failed: %s", e)
            return None

    @classmethod
    def _click_via_ocr(cls, name: str) -> Optional[str]:
        """Tier 4: Find text on screen via OCR and click it."""
        try:
            import pytesseract
            from PIL import Image
            import mss

            with mss.mss() as sct:
                raw = sct.grab(sct.monitors[1])
                img = Image.frombytes("RGB", raw.size, raw.bgra,
                                      "raw", "BGRX")

            data = pytesseract.image_to_data(
                img, output_type=pytesseract.Output.DICT)
            name_lower = name.lower()

            best = None
            for i, word in enumerate(data["text"]):
                if not word:
                    continue
                w = str(word).strip()
                if not w:
                    continue
                if (name_lower in w.lower() or w.lower() in name_lower):
                    conf = int(data["conf"][i]) if data["conf"][i] != -1 else 0
                    if conf >= 50 and data["width"][i] > 5:
                        cx = data["left"][i] + data["width"][i] // 2
                        cy = data["top"][i] + data["height"][i] // 2
                        if best is None or conf > best[2]:
                            best = (cx, cy, conf)

            if best and _PYAUTOGUI_OK:
                pyautogui.click(best[0], best[1])
                log.info("✅ Clicked '%s' at (%d,%d) via OCR [Tier 4, "
                         "conf=%d%%]", name, best[0], best[1], best[2])
                return (f"Clicked '{name}' at ({best[0]},{best[1]}) "
                        f"[Tier 4: OCR, {best[2]}% confidence]")

        except ImportError:
            log.debug("pytesseract not available — Tier 4 skipped")
        except Exception as e:
            log.debug("OCR click failed: %s", e)

        return None

    @classmethod
    def _click_via_vision(cls, name: str) -> Optional[str]:
        """Tier 5: Screenshot + vision model (absolute last resort)."""
        try:
            from actions.computer_control import _screen_find, _click

            coords = _screen_find(name)
            if coords:
                _click(x=coords[0], y=coords[1])
                log.info("⚠️ Clicked '%s' at (%d,%d) via VISION "
                         "[Tier 5 — last resort]",
                         name, coords[0], coords[1])
                return (f"Clicked '{name}' at ({coords[0]},{coords[1]}) "
                        f"[Tier 5: Vision — last resort]")

        except Exception as e:
            log.debug("Vision fallback failed: %s", e)

        return None

    # ═══════════════════════════════════════════════════════
    #  PRIVATE: UIA Tree Utilities
    # ═══════════════════════════════════════════════════════

    @classmethod
    def _get_search_root(cls, window: str = ""):
        """Get the UIA root control — scoped to a window if specified."""
        if not _UIA_OK:
            return None

        if not window:
            return auto.GetForegroundControl() or auto.GetRootControl()

        # Find the matching window in the root children
        root = auto.GetRootControl()
        window_lower = window.lower()
        for ctrl in root.GetChildren():
            try:
                ctrl_name = (ctrl.Name or "").lower()
                if window_lower in ctrl_name:
                    return ctrl
            except Exception:
                continue

        # Not found — return foreground window
        return auto.GetForegroundControl() or root

    @classmethod
    def _find_element_in_tree(cls, node, name: str,
                              control_types: list = None,
                              max_depth: int = 12) -> Optional[object]:
        """
        Semantic element search in the UIA tree.

        Uses a 3-tier match strategy:
          1. Exact name match (case-insensitive)
          2. Substring match (name in element or element in name)
          3. Word overlap match (any significant word matches)
        """
        if not node:
            return None

        name_lower = name.lower().strip()

        # Collect all candidates with a score
        candidates = []
        cls._collect_candidates(node, name_lower, control_types,
                                candidates, depth=0,
                                max_depth=max_depth)

        if not candidates:
            return None

        # Sort by score (highest first) and return best
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    @classmethod
    def _collect_candidates(cls, node, name_lower: str,
                            control_types: list,
                            candidates: list,
                            depth: int, max_depth: int):
        """Walk tree and score element matches."""
        if depth > max_depth or len(candidates) > 50:
            return

        try:
            node_name = (node.Name or "").strip()
            node_name_lower = node_name.lower()
            ctrl_type = node.ControlTypeName or ""

            # Filter by control type if specified
            type_ok = True
            if control_types:
                type_ok = any(ct.lower() in ctrl_type.lower()
                              for ct in control_types)

            if node_name and type_ok:
                score = 0
                # Exact match
                if node_name_lower == name_lower:
                    score = 100
                # Substring match
                elif name_lower in node_name_lower:
                    score = 80
                elif node_name_lower in name_lower:
                    score = 70
                # Word overlap
                else:
                    name_words = {w for w in name_lower.split()
                                  if len(w) > 2}
                    node_words = {w for w in node_name_lower.split()
                                  if len(w) > 2}
                    overlap = name_words & node_words
                    if overlap:
                        score = 40 + (len(overlap) * 10)

                if score > 0:
                    candidates.append((node, score))

        except Exception:
            pass

        try:
            for child in node.GetChildren():
                cls._collect_candidates(
                    child, name_lower, control_types,
                    candidates, depth + 1, max_depth)
        except Exception:
            pass

    @classmethod
    def _walk_tree(cls, node, elements: list, depth: int,
                   max_depth: int, max_elements: int):
        """Walk tree and collect interactive elements for context."""
        if depth > max_depth or len(elements) >= max_elements:
            return

        interactive_types = {
            "ButtonControl", "EditControl", "TextControl",
            "HyperlinkControl", "ListItemControl", "MenuItemControl",
            "TabItemControl", "CheckBoxControl", "RadioButtonControl",
            "ComboBoxControl", "SliderControl", "TreeItemControl",
            "DataItemControl",
        }

        try:
            name = (node.Name or "").strip()
            ctrl_type = node.ControlTypeName or ""

            if ctrl_type in interactive_types and name:
                value = ""
                if ctrl_type == "EditControl":
                    try:
                        vp = node.GetValuePattern()
                        value = vp.Value or ""
                    except Exception:
                        pass

                elements.append({
                    "name": name[:80],
                    "type": ctrl_type.replace("Control", ""),
                    "enabled": node.IsEnabled,
                    "value": value[:100] if value else "",
                })
        except Exception:
            pass

        try:
            for child in node.GetChildren():
                if len(elements) >= max_elements:
                    break
                cls._walk_tree(child, elements, depth + 1,
                               max_depth, max_elements)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════
    #  PRIVATE: Helpers
    # ═══════════════════════════════════════════════════════

    @classmethod
    def _find_hwnd(cls, name_lower: str) -> Optional[int]:
        """Find a window handle by title substring via win32gui."""
        if not _WIN32_OK:
            return None

        found = []

        def _cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd).lower()
                if name_lower in title:
                    found.append(hwnd)
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass

        return found[0] if found else None

    @staticmethod
    def _paste_text(text: str):
        """Type text via clipboard paste (handles Unicode, fast)."""
        try:
            import pyperclip
            pyperclip.copy(text)
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "v")
        except ImportError:
            # No pyperclip — type character by character
            pyautogui.typewrite(text, interval=0.03)
