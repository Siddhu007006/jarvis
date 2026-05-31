"""
screen/watcher.py — Screen Monitor (V5.1 — Downgraded to Fallback)
─────────────────
Screen monitor that captures frames and detects visual changes via
perceptual hashing.

V5.1 changes:
    NEW ROLE: Visual fallback subsystem, NOT primary cognition.

    Before V5.1:
        Screen watcher = primary context engine (1fps, always on)
        This wasted CPU/GPU on screenshots that WorldState already explained.

    After V5.1:
        Screen watcher = visual fallback (0.2fps, conditional capture)
        WorldState is the primary context engine (250ms polling via UIA/Win32).
        Screen watcher only provides value when:
        - Unknown apps are running (not in WorldState's known-app list)
        - User explicitly asks a screen question
        - Vision model needs a fresh frame for analysis

    Concept: Perceptual Hashing for Change Detection
        Each frame is resized to 256x144 and hashed using pHash (DCT-based).
        Hamming distance between consecutive hashes detects visual change.
        Threshold of 12 (~18% change) filters out cursor blinks and clock
        ticks. Only significant changes (app switch, page load, dialog) pass.

    Concept: Adaptive Capture Rate
        Default capture rate is now 0.2fps (1 frame per 5 seconds).
        When the vision gate says context is unknown, rate stays at 0.2fps.
        When WorldState fully covers the context (known app), no capture
        happens at all — the watcher effectively sleeps.

Install:
    pip install mss imagehash Pillow
"""

import time
import threading
import logging
from datetime import datetime
import mss
import mss.tools
from PIL import Image
import imagehash

log = logging.getLogger("JARVIS.screen_watcher")

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# How different the hash must be to trigger analysis (0-64, higher = more different)
CHANGE_THRESHOLD = 12   # ~18% difference in image hash

# Minimum seconds between vision model analyses (even if screen keeps changing)
# V5.1: Increased from 8s to 15s — vision is now a fallback, not primary
MIN_ANALYSIS_INTERVAL = 15

# V5.1: Default capture interval (seconds between frames)
# Changed from 1.0s (1fps) to 5.0s (0.2fps) — WorldState handles fast context
DEFAULT_CAPTURE_INTERVAL = 5.0

# ── Capture gate uses vision.py's shared sufficiency logic ─────────
#
# Concept: Instead of duplicating the whitelist, the watcher delegates
# to the SAME capability-based sufficiency scoring as vision.py.
# This eliminates the "two lists out of sync" problem entirely.
#
# Tier 1: Known exe fast-path (shared from vision.py)
# Tier 2: Capability-based scoring (shared from vision.py)

# Import the shared sufficiency scorer (lazy, avoids circular imports)
_vision_gate_imported = False
_vision_needed_fn = None


def _load_vision_gate():
    """Lazy-load _vision_needed from vision.py to avoid circular imports."""
    global _vision_gate_imported, _vision_needed_fn
    if _vision_gate_imported:
        return
    try:
        from screen.vision import _vision_needed
        _vision_needed_fn = _vision_needed
    except ImportError:
        _vision_needed_fn = None
    _vision_gate_imported = True


def _should_capture(world_state) -> bool:
    """
    Check if the screen watcher should capture a frame.

    Concept: Uses the SAME capability-based sufficiency scoring as
    vision.py's _vision_needed(). If WorldState has enough signals
    to describe the context, there's no point capturing a screenshot.

    This is a thin wrapper that converts _vision_needed's (bool, str)
    result into a simple True/False for the capture loop.

    Returns:
        True if capture should proceed, False to skip this cycle.
    """
    if world_state is None:
        return True  # No WorldState — must capture

    # Lazy-load vision gate (avoid circular import on startup)
    _load_vision_gate()

    if _vision_needed_fn is not None:
        needed, _reason = _vision_needed_fn(world_state)
        return needed

    # Fallback if vision.py is unavailable — always capture
    return True


class ScreenWatcher:
    """
    Continuously captures screen at configurable fps.
    Fires events on the bus when screen changes significantly.
    Does NOT run vision model itself — just fires SCREEN_CHANGED event.

    V5.1: Downgraded from primary context engine to visual fallback.
    - Default rate: 0.2fps (was 1fps)
    - Conditional capture: skips frames when WorldState covers the context
    - Tracks skip/capture stats for debugging
    """

    def __init__(self, bus, capture_interval: float = DEFAULT_CAPTURE_INTERVAL,
                 world_state=None):
        self.bus              = bus
        self.interval         = capture_interval
        self._world_state     = world_state
        self.running          = False
        self._last_hash       = None
        self._last_analysis_time = 0
        self._last_frame      = None   # Keep latest frame for on-demand queries
        self._lock            = threading.Lock()

        # V5.1: Capture stats
        self._stats = {
            "total_cycles": 0,
            "captures": 0,
            "skipped": 0,
            "changes_detected": 0,
        }

        log.info("ScreenWatcher ready (interval=%.1fs, threshold=%d) [V5.1 FALLBACK MODE]",
                 capture_interval, CHANGE_THRESHOLD)

    # ── Start / stop ──────────────────────────────────────
    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True, name="ScreenWatcher")
        t.start()
        log.info("Screen watcher started (fallback mode, %.1fs interval)", self.interval)

    def stop(self):
        self.running = False
        log.info("Screen watcher stopped")

    # ── Main capture loop ─────────────────────────────────
    def _loop(self):
        with mss.mss() as sct:
            # Get primary monitor
            monitor = sct.monitors[1]

            while self.running:
                loop_start = time.monotonic()
                self._stats["total_cycles"] += 1

                try:
                    # V5.1: Check if capture is needed
                    ws = self._world_state
                    if ws is None:
                        try:
                            from core.world_state import get_world_state
                            ws = get_world_state()
                        except Exception:
                            pass

                    if _should_capture(ws):
                        self._capture_and_check(sct, monitor)
                        self._stats["captures"] += 1
                    else:
                        self._stats["skipped"] += 1
                        log.debug("Capture SKIPPED (WorldState sufficient, skipped=%d/%d)",
                                  self._stats["skipped"], self._stats["total_cycles"])

                except Exception as e:
                    log.error(f"Capture error: {e}", exc_info=True)

                # Maintain target interval
                elapsed = time.monotonic() - loop_start
                sleep = max(0, self.interval - elapsed)
                time.sleep(sleep)

    def _capture_and_check(self, sct, monitor):
        # ── Capture screenshot (very fast with mss) ────────
        raw = sct.grab(monitor)

        # Convert to small PIL Image for hashing (resize to 256px wide = fast)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        small = img.resize((256, 144), Image.BILINEAR)  # 16:9 thumbnail

        # ── Compute perceptual hash (0.1ms) ───────────────
        current_hash = imagehash.phash(small)

        with self._lock:
            # Store full-res frame for on-demand queries
            self._last_frame = img

        if self._last_hash is None:
            # First frame — just store it
            self._last_hash = current_hash
            self._fire_initial_context(img)
            return

        # ── Check difference ──────────────────────────────
        diff = current_hash - self._last_hash  # Hamming distance (0-64)

        if diff >= CHANGE_THRESHOLD:
            now = time.time()
            if now - self._last_analysis_time >= MIN_ANALYSIS_INTERVAL:
                self._last_hash = current_hash
                self._last_analysis_time = now
                self._stats["changes_detected"] += 1
                log.debug("Screen changed (diff=%d) — firing SCREEN_CHANGED", diff)
                # Fire event with the full-res image
                self.bus.emit("SCREEN_CHANGED", {
                    "image":     img,
                    "diff":      diff,
                    "timestamp": datetime.now().isoformat()
                })
            else:
                # Still update hash even if we skip analysis
                self._last_hash = current_hash

    def _fire_initial_context(self, img):
        """Fire once at startup so JARVIS knows initial screen state."""
        self.bus.emit("SCREEN_CHANGED", {
            "image":     img,
            "diff":      64,   # Max diff = treat as completely new
            "timestamp": datetime.now().isoformat(),
            "initial":   True
        })

    # ── On-demand screenshot ──────────────────────────────
    def get_current_frame(self) -> Image.Image | None:
        """
        Return the most recent captured frame.
        Called by vision module when user asks a screen question.
        No new screenshot taken — uses the one already in memory.
        """
        with self._lock:
            return self._last_frame

    def capture_now(self) -> Image.Image:
        """Force a fresh screenshot right now. For on-demand vision queries."""
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            with self._lock:
                self._last_frame = img
            return img

    # ── V5.1: Debug stats ─────────────────────────────────
    @property
    def stats(self) -> dict:
        """Return capture statistics for debugging."""
        return dict(self._stats)
