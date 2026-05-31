"""
Command Runner — Executes PowerShell commands and returns output to Gemini.
Sandboxed with timeout and output truncation.
"""

import logging
import subprocess

log = logging.getLogger(__name__)

MAX_OUTPUT = 800  # chars returned to Gemini


def run_command(command: str) -> str:
    """Run a PowerShell command and return stdout/stderr."""
    if not command.strip():
        return "No command provided."

    # Safety: block destructive commands
    blocked = ["format", "del /s", "rd /s", "remove-item -recurse c:\\"]
    for b in blocked:
        if b.lower() in command.lower():
            return f"Blocked dangerous command: {command}"

    log.info("💻 Running: %s", command[:80])

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode == 0 and stdout:
            output = stdout[:MAX_OUTPUT]
            log.info("✅ Command succeeded: %d chars", len(stdout))
            return output
        elif stderr:
            output = stderr[:MAX_OUTPUT]
            log.warning("⚠️ Command error: %s", stderr[:100])
            return f"Error: {output}"
        else:
            return "Command completed with no output."

    except subprocess.TimeoutExpired:
        log.warning("⏰ Command timed out: %s", command[:60])
        return "Command timed out after 15 seconds."
    except Exception as e:
        log.error("Command failed: %s", e)
        return f"Failed to run command: {e}"
