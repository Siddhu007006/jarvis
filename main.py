"""
J.A.R.V.I.S v5.0 -- Just A Rather Very Intelligent System
Built by Siddharth Reddy

Entry point: Launches the native PyQt6 Dynamic Island with local-first voice pipeline,
streaming TTS, screen vision (moondream2), and proactive AI commentary.

Usage:
  python main.py           → Native PyQt6 Dynamic Island (top-center, always-on-top)
"""

import asyncio
import json
import sys
import os
import io
import logging
import subprocess
import signal
import threading
from pathlib import Path

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# Fix Windows console encoding (sys.stdout is None in pythonw.exe)
if sys.platform == "win32":
    if sys.stdout is None or sys.stderr is None:
        # We are running windowless (e.g. pythonw.exe)
        # Redirect all output to a file to prevent silent crashes on print()
        log_file = open(BASE_DIR / "pythonw_crash.log", "a", encoding="utf-8")
        sys.stdout = log_file
        sys.stderr = log_file
    else:
        # We are running in a terminal
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Logging ────────────────────────────────────────────────────
log_handlers = []
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))
else:
    # When running windowless (pythonw.exe), write logs to a file
    log_handlers.append(logging.FileHandler(BASE_DIR / "jarvis.log", encoding="utf-8"))

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
    handlers=log_handlers
)
log = logging.getLogger("jarvis")

# Suppress noisy library warnings
logging.getLogger("httpx").setLevel(logging.WARNING)


def _has_api_key() -> bool:
    """Check if we have an API key (Groq or Gemini) or if Ollama is reachable."""
    # Check Gemini key
    try:
        if json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("gemini_api_key"):
            return True
    except Exception:
        pass
    
    # Check Groq key
    try:
        settings_path = BASE_DIR / "config" / "settings.json"
        if json.loads(settings_path.read_text(encoding="utf-8")).get("groq_api_key"):
            return True
    except Exception:
        pass

    # Check Ollama as a fallback (Phase 2: non-blocking with timeout thread)
    # Concept: httpx.get with timeout=1.0 still blocks the calling thread
    # for 1 full second when Ollama isn't running. By wrapping in a thread
    # with a tighter timeout we avoid holding up startup.
    try:
        import httpx
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
        def _check_ollama():
            return httpx.get("http://localhost:11434/api/tags", timeout=1.0).status_code == 200
        with ThreadPoolExecutor(1) as pool:
            try:
                return pool.submit(_check_ollama).result(timeout=1.5)
            except (FutTimeout, Exception):
                return False
    except Exception:
        return False


def _save_api_key(key: str):
    """Legacy: save Gemini key. Now also saves to settings.json."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"gemini_api_key": key}, f, indent=4)


def _start_global_hotkey(engine):
    """Register Win+J to toggle mute. Runs in daemon thread."""
    try:
        from pynput import keyboard

        def toggle_mute():
            engine.muted = not engine.muted
            state = "MUTED" if engine.muted else "LISTENING"
            log.info("Hotkey: %s", state)
            if engine.muted:
                engine.ui.set_state("IDLE", "Muted")
            else:
                engine.ui.set_state("LISTENING")

        hotkey = keyboard.GlobalHotKeys({"<cmd>+j": toggle_mute})
        hotkey.daemon = True
        hotkey.start()
        log.info("Global hotkey Win+J registered")
    except ImportError:
        log.warning("pynput not installed - Win+J hotkey disabled")
    except Exception as e:
        log.warning("Could not register hotkey: %s", e)


def main():
    # ── Single-Instance Guard ─────────────────────────────────
    # Uses a Windows Named Mutex so only ONE Jarvis can run at a time.
    # A second click on the shortcut will exit here silently.
    from system.single_instance import ensure_single_instance
    if not ensure_single_instance():
        log.info("Jarvis is already running. Exiting duplicate.")
        sys.exit(0)

    # ── Phase 6: Startup management ───────────────────────────
    if "--install" in sys.argv:
        from system.startup import install_auto_start
        if install_auto_start():
            print("Jarvis will now start automatically at login")
        else:
            print("Auto-start setup failed")
        return

    if "--uninstall" in sys.argv:
        from system.startup import uninstall_startup
        if uninstall_startup():
            print("✅ Jarvis auto-start removed")
        else:
            print("❌ Failed to remove auto-start")
        return

    if "--startup-status" in sys.argv:
        from system.startup import is_installed
        print(f"Auto-start: {'installed' if is_installed() else 'not installed'}")
        return

    # ── Native PyQt6 Dynamic Island ────────────────────────────────
    from ui.dynamic_island import DynamicIslandQt

    island = DynamicIslandQt()

    if not _has_api_key():
        def on_key(key):
            _save_api_key(key)
            island.set_state("PROCESSING", "Connecting...")
            threading.Thread(target=_start_engine, args=(island,), daemon=True).start()
        island.show_setup(on_key)
    else:
        island.set_state("PROCESSING", "Connecting...")
        threading.Thread(target=_start_engine, args=(island,), daemon=True).start()

    log.info("Launching Jarvis native PyQt6 Dynamic Island...")
    island.run()  # blocks (QApplication.exec())


def _start_engine(ui):
    """
    Start the full Jarvis system stack:
      1. Event bus (pub/sub backbone)
      2. State manager (FSM wired to UI + bus)
      3. Context memory (sliding window, persisted)
      4. Wake word detector (offline, low-CPU)
      5. Automation engine (command executor)
      6. Gemini Live Audio engine (voice AI)
    """

    # ── Phase 2: Event Bus + State Manager ────────────────────
    from core.events import get_bus, Event
    from core.state import StateManager

    bus = get_bus()
    state_mgr = StateManager(ui=ui)
    log.info("Event bus + state manager online")

    # Phase 2: Parallel subsystem initialization.
    # Concept: Memory, ProcessMonitor, WakeWord, and AutomationEngine
    # are independent subsystems with zero inter-dependencies at init
    # time. Loading them sequentially wastes ~2s. ThreadPoolExecutor
    # runs all four imports + init concurrently, so total time equals
    # the slowest one (~800ms) instead of the sum (~2.5s).
    import time
    from concurrent.futures import ThreadPoolExecutor

    memory = None
    process_monitor = None
    wake = None
    automation = None

    def _init_memory():
        try:
            from core.memory import ContextMemory
            m = ContextMemory(bus=bus)
            log.info("Context memory loaded (%d tokens est.)", m.get_token_estimate())
            return m
        except Exception as e:
            log.warning("Context memory unavailable: %s", e)
            return None

    def _init_process_monitor():
        try:
            from core.process_monitor import ProcessMonitor
            pm = ProcessMonitor(bus=bus)
            stats = pm.get_system_stats()
            log.info("Process monitor online (CPU: %.1f%%, RAM: %.1f GB)",
                     stats["cpu_percent"], stats["ram_used_gb"])
            return pm
        except Exception as e:
            log.warning("Process monitor unavailable: %s", e)
            return None

    def _init_wake_word():
        try:
            from core.wake_word import WakeWordDetector
            w = WakeWordDetector(bus=bus, wake_phrase="jarvis")
            w.start()
            log.info("Wake word detector started")
            return w
        except Exception as e:
            log.warning("Wake word detector unavailable: %s", e)
            return None

    def _init_automation():
        try:
            from core.automation import AutomationEngine
            a = AutomationEngine(bus=bus)
            log.info("Automation engine ready")
            return a
        except Exception as e:
            log.warning("Automation engine unavailable: %s", e)
            return None

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="jarvis-init") as pool:
        fut_memory = pool.submit(_init_memory)
        fut_pm = pool.submit(_init_process_monitor)
        fut_wake = pool.submit(_init_wake_word)
        fut_auto = pool.submit(_init_automation)

        memory = fut_memory.result()
        process_monitor = fut_pm.result()
        wake = fut_wake.result()
        automation = fut_auto.result()

    # ── V5: Screen Awareness Pipeline ─────────────────────────
    # Concept: Four modules form a pipeline connected via the event bus:
    #   ContextManager (subscribes to events, stores context for LLM)
    #   ScreenWatcher (1fps capture + perceptual hash) ──SCREEN_CHANGED──▶
    #   VisionEngine (moondream2 analysis) ──SCREEN_CONTEXT_UPDATED──▶
    #   ProactiveAgent (decides if/when to speak unprompted)
    screen_watcher = None
    vision_engine = None
    proactive_agent = None

    # Load settings for enable flags
    try:
        _settings = json.loads((BASE_DIR / "config" / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        _settings = {}

    # ContextManager MUST be created FIRST — it subscribes to events
    # that VisionEngine will emit. Register it with providers so every
    # LLM call gets automatic screen context injection.
    context_manager = None
    try:
        from core.context_manager import ContextManager
        from core.providers import set_context_manager
        context_manager = ContextManager(bus=bus)
        set_context_manager(context_manager)
        log.info("📋 Context manager online (screen → LLM bridge)")
    except Exception as e:
        log.warning("Context manager unavailable: %s", e)

    # ── V5.1: World State Engine ───────────────────────────────
    # THE SINGLE SOURCE OF TRUTH for all system context.
    # Aggregates process_monitor + context_manager + active window +
    # browser URL + clipboard + workflow detection into one object.
    # Must be initialized AFTER ProcessMonitor and ContextManager.
    world_state = None
    try:
        from core.world_state import WorldState, set_world_state
        world_state = WorldState(
            bus=bus,
            process_monitor=process_monitor,
            context_manager=context_manager,
        )
        set_world_state(world_state)
        world_state.start()
        log.info("🌍 World State Engine online (3 update loops)")
    except Exception as e:
        log.warning("World State Engine unavailable: %s", e)

    # ── FIX 1A: Terminal Observer ──────────────────────────────
    # Monitors terminal windows for error patterns (Traceback, npm ERR!,
    # Build failed, git conflicts) and emits semantic events.
    # Must be initialized AFTER WorldState (reads active_exe from it).
    terminal_observer = None
    try:
        from core.terminal_observer import TerminalObserver
        terminal_observer = TerminalObserver(bus=bus, world_state=world_state)
        terminal_observer.start()
        log.info("📟 Terminal observer online (error pattern detection)")
    except Exception as e:
        log.warning("Terminal observer unavailable: %s", e)

    # ── FIX 1B: Accessibility Observer ─────────────────────────
    # Monitors the accessibility tree for dialogs, modals, notifications,
    # and permission prompts that block automation.
    # Must be initialized AFTER WorldState.
    accessibility_observer = None
    try:
        from core.accessibility_observer import AccessibilityObserver
        accessibility_observer = AccessibilityObserver(bus=bus, world_state=world_state)
        accessibility_observer.start()
        log.info("♿ Accessibility observer online (dialog/modal detection)")
    except Exception as e:
        log.warning("Accessibility observer unavailable: %s", e)

    if _settings.get("screen_watcher_enabled", True):
        try:
            from screen.watcher import ScreenWatcher
            from screen.vision import VisionEngine
            # V5.1: Pass world_state so watcher + vision can check context
            # before capturing/analyzing (conditional vision gate)
            screen_watcher = ScreenWatcher(bus=bus, world_state=world_state)
            vision_engine = VisionEngine(bus=bus, world_state=world_state)
            screen_watcher.start()
            log.info("👁️ Screen watcher + vision engine online (V5.1 fallback mode)")
        except Exception as e:
            log.warning("Screen awareness unavailable: %s", e)
            screen_watcher = None
            vision_engine = None

    if _settings.get("proactive_agent_enabled", True) and vision_engine:
        try:
            from screen.proactive import ProactiveAgent
            proactive_agent = ProactiveAgent(
                bus=bus,
                brain=vision_engine,
                tts=None,  # Will use core.tts.speak_now() internally
                profile={"name": "sir"}
            )
            log.info("🧠 Proactive agent online")
        except Exception as e:
            log.warning("Proactive agent unavailable: %s", e)
            proactive_agent = None

    # ── Phase 4: Local Voice Pipeline Engine ──────────────────
    from core.engine import JarvisEngine
    engine = JarvisEngine(ui)
    ui.set_audio_levels(engine.rms_levels)

    # Bridge engine state changes through the event bus
    _bridge_engine_events(engine, bus)

    # Bridge external wake word events TO the engine
    def _on_wake_word(data):
        """External wake word detected → wake the engine."""
        if not engine.awake:
            log.info("🔔 External wake word → engine.awake = True")
            engine.awake = True
            engine.last_awake_time = time.time()
            ui.set_state("LISTENING", "Listening...")

    def _on_deactivate(data):
        """External deactivation phrase → sleep the engine."""
        log.info("💤 External deactivate → engine.awake = False")
        engine.awake = False
        ui.set_state("SLEEP", "")

    bus.on(Event.WAKE_WORD_DETECTED, _on_wake_word)
    bus.on(Event.DEACTIVATE_REQUESTED, _on_deactivate)

    # Register global hotkey (Win+J mute toggle)
    _start_global_hotkey(engine)

    log.info("🚀 Jarvis V5 online (Kokoro TTS + streaming + screen vision)")
    log.info("Say 'Jarvis' to wake — fully local-first.")

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(engine.run())
        except Exception as e:
            log.error("Fatal engine error: %s", e, exc_info=True)
            bus.emit(Event.ERROR_OCCURRED, {"message": "Engine failed"})

    threading.Thread(target=run, daemon=True, name="jarvis-engine").start()


def _bridge_engine_events(engine, bus):
    """
    Monkey-patch the engine's set_state so every state change
    is also broadcast on the event bus.

    This decouples the engine from the UI — any component can
    react to state changes by subscribing to the bus.
    """
    from core.events import Event

    original_set_state = engine.set_state if hasattr(engine, 'set_state') else None

    def patched_set_state(state, text=""):
        # Map engine states to bus events
        event_map = {
            "LISTENING":  Event.LISTENING_STARTED,
            "THINKING":   Event.PROCESSING_STARTED,
            "PROCESSING": Event.PROCESSING_STARTED,
            "SPEAKING":   Event.SPEAKING_STARTED,
            "IDLE":       None,
            "ERROR":      Event.ERROR_OCCURRED,
        }
        ev = event_map.get(state)
        if ev:
            bus.emit(ev, {"text": text})

        if original_set_state:
            original_set_state(state, text)

    if hasattr(engine, 'set_state'):
        engine.set_state = patched_set_state


if __name__ == "__main__":
    main()
