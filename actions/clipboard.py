"""
Clipboard — Read and write system clipboard.
"""

import logging

log = logging.getLogger(__name__)


def clipboard_action(action: str = "read", text: str = None) -> str:
    """Read from or write to the system clipboard."""
    try:
        import pyperclip
    except ImportError:
        return "pyperclip not installed. Run: pip install pyperclip"

    action = action.lower().strip()

    if action == "write" or action == "copy":
        if not text:
            return "No text provided to copy."
        pyperclip.copy(text)
        log.info("📋 Copied %d chars to clipboard", len(text))
        return f"Copied to clipboard: {text[:60]}..."

    elif action == "read" or action == "paste":
        content = pyperclip.paste()
        if content:
            log.info("📋 Read %d chars from clipboard", len(content))
            return content[:500]  # truncate for Gemini context
        return "Clipboard is empty."

    else:
        return f"Unknown clipboard action: {action}. Use 'read' or 'write'."
