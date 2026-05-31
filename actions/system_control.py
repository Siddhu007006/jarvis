"""
System Control — Volume, brightness, battery, power, WiFi, screenshots.

Volume: Uses Windows Core Audio API via pycaw (direct COM call, <1ms).
        Previous implementation sent 50 virtual keypresses via WScript.Shell
        which was ~200ms, imprecise, and could trigger focus changes.

All other actions: PowerShell where no native Python API exists.
"""

import logging
import subprocess
import psutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


# ── PowerShell helper (for actions without a Python API) ────────

def _ps(cmd: str, timeout: int = 10) -> str:
    """Run a PowerShell command and return stdout."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return r.stdout.strip()
    except Exception as e:
        return f"Error: {e}"


# ── pycaw helpers — Windows Core Audio API ──────────────────────
# Concept: pycaw wraps the Windows IMMDeviceEnumerator/IAudioEndpointVolume
# COM interfaces. A single COM call sets the exact scalar volume level.
# No keystrokes, no focus changes, no 200ms roundtrip through a shell.

def _get_volume_interface():
    """Return the IAudioEndpointVolume COM interface (cached per-call)."""
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL

    devices   = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def _set_volume_pycaw(level: int) -> str:
    """Set exact system volume using Core Audio API. <1ms, no keypresses."""
    try:
        vol = _get_volume_interface()
        level = max(0, min(100, int(level)))
        vol.SetMasterVolumeLevelScalar(level / 100.0, None)
        log.info("Volume set to %d%% via Core Audio", level)
        return f"Volume set to {level}%."
    except Exception as e:
        log.error("pycaw volume set failed: %s", e)
        return f"Volume control failed: {e}"


def _get_volume_pycaw() -> int:
    """Get current system volume (0–100)."""
    try:
        return int(_get_volume_interface().GetMasterVolumeLevelScalar() * 100)
    except Exception:
        return 50  # safe default


def _mute_pycaw() -> str:
    """Toggle system mute via Core Audio API."""
    try:
        vol     = _get_volume_interface()
        current = vol.GetMute()
        vol.SetMute(0 if current else 1, None)
        state = "unmuted" if current else "muted"
        log.info("Volume %s via Core Audio", state)
        return f"Volume {state}."
    except Exception as e:
        log.error("pycaw mute toggle failed: %s", e)
        return f"Mute control failed: {e}"


# ── WiFi adapter name helper ────────────────────────────────────

def _wifi_adapter_name() -> str:
    """
    Find the actual wireless adapter name via netsh.
    Falls back to 'Wi-Fi' (works on most English Windows installs).
    Prevents hardcoded 'Wi-Fi' failing on non-English systems.
    """
    try:
        out = _ps(
            "netsh interface show interface | "
            "Select-String -Pattern 'Wireless|Wi-Fi|WLAN' | "
            "ForEach-Object { ($_ -split '\\s+')[3] } | "
            "Select-Object -First 1"
        ).strip()
        return out if out else "Wi-Fi"
    except Exception:
        return "Wi-Fi"


# ── Main dispatcher ─────────────────────────────────────────────

def system_control(action: str, value: str = None) -> str:
    """Execute a system control action."""
    action = action.lower().strip()

    # ── Battery ─────────────────────────────────────────────────
    if action == "battery":
        bat = psutil.sensors_battery()
        if bat:
            status = "plugged in" if bat.power_plugged else "on battery"
            return f"Battery: {bat.percent:.0f}% ({status})"
        return "No battery detected (desktop PC)."

    # ── System info ─────────────────────────────────────────────
    elif action == "system_info":
        # interval=None: non-blocking, returns delta since last call
        # interval=1 would block the calling thread for 1 second — removed.
        cpu  = psutil.cpu_percent(interval=None)
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("C:\\")
        return (
            f"CPU: {cpu:.0f}% | "
            f"RAM: {mem.percent:.0f}% "
            f"({mem.used // (1024**3)}/{mem.total // (1024**3)} GB) | "
            f"Disk C: {disk.percent:.0f}% used"
        )

    # ── Volume (Core Audio API — no WScript.Shell keypresses) ───
    elif action == "volume_set":
        try:
            level = int(str(value or "50").strip())
        except ValueError:
            level = 50
        return _set_volume_pycaw(level)

    elif action == "volume_up":
        current = _get_volume_pycaw()
        return _set_volume_pycaw(min(100, current + 10))

    elif action == "volume_down":
        current = _get_volume_pycaw()
        return _set_volume_pycaw(max(0, current - 10))

    elif action == "volume_mute":
        return _mute_pycaw()

    elif action == "volume_get":
        level = _get_volume_pycaw()
        return f"Current volume: {level}%."

    # ── Media keys (no direct API — WScript.Shell is fine for media) ──
    elif action in ("media_play", "media_pause"):
        _ps('$w = New-Object -ComObject WScript.Shell; $w.SendKeys([char]179)')
        return "Media play/pause toggled."

    elif action == "media_next":
        _ps('$w = New-Object -ComObject WScript.Shell; $w.SendKeys([char]176)')
        return "Skipped to next track."

    elif action == "media_prev":
        _ps('$w = New-Object -ComObject WScript.Shell; $w.SendKeys([char]177)')
        return "Skipped to previous track."

    # ── Brightness ──────────────────────────────────────────────
    elif action == "brightness_set":
        level = value or "50"
        try:
            int(level)
        except ValueError:
            return f"Invalid brightness level: {level}"
        _ps(
            f"(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods)"
            f".WmiSetBrightness(0, {level})"
        )
        return f"Brightness set to {level}%."

    # ── Screenshot ──────────────────────────────────────────────
    elif action == "screenshot":
        ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_path = Path.home() / "Desktop" / f"screenshot_{ts}.png"
        _ps(
            f"Add-Type -AssemblyName System.Windows.Forms;"
            f"$s=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
            f"$b=New-Object System.Drawing.Bitmap($s.Width,$s.Height);"
            f"$g=[System.Drawing.Graphics]::FromImage($b);"
            f"$g.CopyFromScreen($s.Location,[System.Drawing.Point]::Empty,$s.Size);"
            f"$b.Save('{save_path}')"
        )
        return f"Screenshot saved to {save_path}"

    # ── Power — require explicit confirmation to prevent accidents ──
    elif action == "shutdown":
        if str(value or "").lower() != "confirmed":
            return (
                "Shutdown requires confirmation. "
                "Please say 'yes, shut down' to confirm."
            )
        log.warning("SHUTDOWN initiated by user command")
        _ps("Stop-Computer -Force")
        return "Shutting down..."

    elif action == "restart":
        if str(value or "").lower() != "confirmed":
            return (
                "Restart requires confirmation. "
                "Please say 'yes, restart' to confirm."
            )
        log.warning("RESTART initiated by user command")
        _ps("Restart-Computer -Force")
        return "Restarting..."

    elif action == "sleep":
        _ps("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        return "Going to sleep..."

    elif action == "lock":
        _ps("rundll32.exe user32.dll,LockWorkStation")
        return "Screen locked."

    # ── WiFi — enumerate adapter name dynamically ────────────────
    elif action == "wifi_on":
        adapter = _wifi_adapter_name()
        _ps(f"netsh interface set interface '{adapter}' enabled")
        return f"WiFi ({adapter}) enabled."

    elif action == "wifi_off":
        adapter = _wifi_adapter_name()
        _ps(f"netsh interface set interface '{adapter}' disabled")
        return f"WiFi ({adapter}) disabled."

    else:
        return f"Unknown system action: '{action}'"
