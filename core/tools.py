"""
Tool Declarations — Typed schemas for Gemini's native function calling.
Each tool has a name, description, and typed parameters.

v3.0: Added run_command, type_text, clipboard tools.
"""

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use when the user asks to open, launch, or start any app, website, or program."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Name of the application (e.g. 'Chrome', 'Notepad', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "close_app",
        "description": "Closes a running application by name.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Name of the application to close"
                }
            },
            "required": ["app_name"]
        }
    },

    {
        "name": "system_control",
        "description": (
            "Controls system settings: volume, brightness, battery status, "
            "WiFi, screenshot, shutdown, restart, sleep (PUTS THE COMPUTER TO SLEEP, do NOT use this if the user wants Jarvis to sleep/stop listening), lock screen."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "Action: volume_set | volume_up | volume_down | volume_mute | "
                        "media_play | media_pause | media_next | media_prev | "
                        "brightness_set | battery | wifi_on | wifi_off | "
                        "screenshot | shutdown | restart | sleep | lock | system_info"
                    )
                },
                "value": {
                    "type": "STRING",
                    "description": "Optional value (e.g. volume level 0-100)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "sleep_jarvis",
        "description": (
            "Puts the AI assistant (Jarvis) into sleep/mute mode. "
            "Use this ONLY when the user says 'jarvis sleep', 'stop listening', or 'go to sleep'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {}
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web and returns a text answer. "
            "Use when the user asks about facts, news, definitions, or anything requiring web lookup."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "The search query"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "file_manager",
        "description": "Manages files and folders: create, delete, move, copy, rename, list, read, find, largest, disk_usage. Can search for large files globally.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "Action: create_file | create_folder | delete | move | copy | rename | list | read | find | largest | disk_usage"
                },
                "path": {
                    "type": "STRING",
                    "description": "File or folder path. Shortcuts: c, desktop, downloads, documents, home"
                },
                "content": {
                    "type": "STRING",
                    "description": "Content for create_file"
                },
                "destination": {
                    "type": "STRING",
                    "description": "Destination path for move/copy"
                },
                "new_name": {
                    "type": "STRING",
                    "description": "New name for rename"
                },
                "query": {
                    "type": "STRING",
                    "description": "Search query for 'find' action"
                },
                "extension": {
                    "type": "STRING",
                    "description": "File extension filter for 'find' action (e.g. '.mp4')"
                },
                "count": {
                    "type": "INTEGER",
                    "description": "Number of files to return for 'largest' action (default 10)"
                },
                "min_size_gb": {
                    "type": "NUMBER",
                    "description": "Minimum size in GB for 'largest' action (e.g. 1.0 for files > 1GB)"
                }
            },
            "required": ["action", "path"]
        }
    },
    {
        "name": "screen_vision",
        "description": (
            "Captures the screen and sends it for visual analysis. "
            "Use when the user asks what is on screen, what they see, or to analyze the display."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "question": {
                    "type": "STRING",
                    "description": "What to analyze about the screen"
                }
            },
            "required": ["question"]
        }
    },
    {
        "name": "run_command",
        "description": (
            "Runs a PowerShell command on the computer and returns the output. "
            "Use when the user asks to run a command, check installed software, "
            "list processes, install packages, or perform any terminal operation."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {
                    "type": "STRING",
                    "description": "The PowerShell command to execute (e.g. 'Get-Process', 'pip list', 'dir')"
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "type_text",
        "description": (
            "Types text or presses keyboard shortcuts in the currently focused application. "
            "Use when the user asks to type something, press keys, or use keyboard shortcuts."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {
                    "type": "STRING",
                    "description": "Text to type (e.g. 'Hello World')"
                },
                "hotkey": {
                    "type": "STRING",
                    "description": "Hotkey combination to press (e.g. 'ctrl+c', 'alt+tab', 'ctrl+shift+n')"
                }
            }
        }
    },
    {
        "name": "clipboard",
        "description": (
            "Reads from or writes to the system clipboard. "
            "Use when the user asks to copy text, paste content, or read what's in the clipboard."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "Action: read | write"
                },
                "text": {
                    "type": "STRING",
                    "description": "Text to copy to clipboard (required for 'write' action)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call silently when the user reveals: name, age, city, preferences, hobbies, projects. "
            "Do NOT announce that you are saving."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": "identity | preferences | projects | relationships | notes"
                },
                "key": {
                    "type": "STRING",
                    "description": "Short snake_case key (e.g. name, favorite_color)"
                },
                "value": {
                    "type": "STRING",
                    "description": "Concise value (e.g. Siddharth, blue)"
                }
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "computer_control",
        "description": (
            "Universal UI automation — interact with ANY application on screen. "
            "Use when the user wants to click buttons, fill forms, navigate menus, "
            "play/pause media inside apps, scroll, drag, or do anything visual in any application. "
            "Supports AI-powered element finding: describe a UI element and it will be located and clicked. "
            "IMPORTANT: Use 'screen_click' with a description to click ANY button/element in ANY app."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "Action: click | double_click | right_click | "
                        "type | smart_type | hotkey | press | scroll | "
                        "move | drag | screenshot | wait | clear_field | "
                        "focus_window | screen_find | screen_click"
                    )
                },
                "x": {
                    "type": "INTEGER",
                    "description": "X pixel coordinate for click/move/drag"
                },
                "y": {
                    "type": "INTEGER",
                    "description": "Y pixel coordinate for click/move/drag"
                },
                "x1": {
                    "type": "INTEGER",
                    "description": "Start X for drag"
                },
                "y1": {
                    "type": "INTEGER",
                    "description": "Start Y for drag"
                },
                "x2": {
                    "type": "INTEGER",
                    "description": "End X for drag"
                },
                "y2": {
                    "type": "INTEGER",
                    "description": "End Y for drag"
                },
                "text": {
                    "type": "STRING",
                    "description": "Text for type/smart_type actions"
                },
                "keys": {
                    "type": "STRING",
                    "description": "Key combination for hotkey (e.g. 'ctrl+c', 'alt+tab')"
                },
                "key": {
                    "type": "STRING",
                    "description": "Single key for press action (e.g. 'enter', 'tab', 'escape')"
                },
                "direction": {
                    "type": "STRING",
                    "description": "Scroll direction: up | down | left | right"
                },
                "amount": {
                    "type": "INTEGER",
                    "description": "Scroll amount (default 3)"
                },
                "title": {
                    "type": "STRING",
                    "description": "Window title fragment for focus_window"
                },
                "description": {
                    "type": "STRING",
                    "description": (
                        "Natural-language description of UI element for screen_find/screen_click. "
                        "Example: 'the Play button', 'the search bar', 'the Send icon'"
                    )
                },
                "seconds": {
                    "type": "NUMBER",
                    "description": "Seconds to wait (max 30)"
                },
                "path": {
                    "type": "STRING",
                    "description": "Save path for screenshot"
                },
                "clear_first": {
                    "type": "BOOLEAN",
                    "description": "Clear field before smart_type (default true)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "agent_task",
        "description": (
            "Execute a complex multi-step task autonomously using a planner. "
            "Use when the user asks for something that requires MULTIPLE steps, like: "
            "'open TradingView and search for XAUUSD', 'send a message on WhatsApp', "
            "'find large files and delete them'. The planner will break the goal into steps "
            "and execute them sequentially at high speed. "
            "Do NOT use for simple one-step actions — use the specific tool directly instead."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {
                    "type": "STRING",
                    "description": "The complete task to accomplish (e.g. 'Open Chrome and search for Python tutorials')"
                }
            },
            "required": ["goal"]
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant. Call when the user says goodbye, "
            "close, shut down, or wants to end the session."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {}
        }
    },

    # ── V5.1: Unified UI Controller (primary for all UI automation) ──
    {
        "name": "ui_control",
        "description": (
            "Primary UI automation — interact with ANY app's buttons, textboxes, tabs, menus, and windows "
            "WITHOUT screenshots. Uses Windows UI Automation tree (instant, 98% accurate) with automatic "
            "fallback through keyboard shortcuts, Win32 API, OCR, and vision. "
            "ALWAYS prefer this over computer_control for clicking buttons, typing into fields, "
            "selecting tabs, or navigating menus. "
            "Example: ui_control(action='click_button', target='Submit', window='Chrome') "
            "Example: ui_control(action='type_into', target='Search', text='Python tutorials', window='Chrome') "
            "Example: ui_control(action='select_menu', menu_path='File > Save As', window='Notepad')"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "Action: click_button | type_into | select_tab | select_menu | "
                        "send_hotkey | send_keys | focus_window | list_windows | "
                        "list_elements | close_window | get_text"
                    )
                },
                "target": {
                    "type": "STRING",
                    "description": (
                        "Name of the UI element, window, or shortcut. "
                        "For click_button: button/link text (e.g. 'Submit', 'OK', 'File'). "
                        "For type_into: field name (e.g. 'Search', 'Email'). "
                        "For select_tab: tab name. For send_hotkey: key combo (e.g. 'ctrl+s') "
                        "or named shortcut (e.g. 'save', 'undo')."
                    )
                },
                "window": {
                    "type": "STRING",
                    "description": "Window title to scope the search (e.g. 'Chrome', 'Notepad')"
                },
                "text": {
                    "type": "STRING",
                    "description": "Text to type (for type_into and send_keys actions)"
                },
                "menu_path": {
                    "type": "STRING",
                    "description": "Menu path for select_menu (e.g. 'File > Save As', 'Edit > Find')"
                }
            },
            "required": ["action"]
        }
    },

    # ── V5: Windows UI Automation Tools (legacy, kept for backward compat) ──
    {
        "name": "click_element",
        "description": (
            "Find and click a UI element by its name or text using Windows UI Automation. "
            "Much more reliable than pixel-based clicking. "
            "Example: click_element(element_name='Submit', window_title='Chrome')"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "element_name": {
                    "type": "STRING",
                    "description": "The name/text of the UI element to click (e.g. 'Submit', 'OK', 'File')"
                },
                "window_title": {
                    "type": "STRING",
                    "description": "Optional: window title to search in (narrows scope)"
                }
            },
            "required": ["element_name"]
        }
    },
    {
        "name": "type_into",
        "description": (
            "Type text into a specific UI element found by Windows UI Automation. "
            "More reliable than pyautogui.typewrite because it targets the actual control. "
            "Example: type_into(element_name='Search', text='Python tutorials')"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "element_name": {
                    "type": "STRING",
                    "description": "Name of the text field to type into"
                },
                "text": {
                    "type": "STRING",
                    "description": "Text to type"
                },
                "window_title": {
                    "type": "STRING",
                    "description": "Optional: window title to search in"
                }
            },
            "required": ["element_name", "text"]
        }
    },
    {
        "name": "set_volume_precise",
        "description": (
            "Set system volume to an exact percentage using Windows audio API (pycaw). "
            "More reliable than keyboard shortcuts. "
            "Example: set_volume_precise(level=50)"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "level": {
                    "type": "NUMBER",
                    "description": "Volume level 0-100"
                }
            },
            "required": ["level"]
        }
    },
]
