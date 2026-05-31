"""
ui/dynamic_island.py — Native PyQt6 Dynamic Island overlay for Jarvis.

Blueprint Phase 1: Perfect Dynamic Island Overlay.

Features implemented from the Master Implementation Plan:
  - Frameless, transparent, always-on-top pill widget
  - 9 states: IDLE, HOVER, LISTENING, PROCESSING, SPEAKING, NOTIFICATION, DRAGGING, SLEEP, ERROR
  - Draggable positioning with edge snapping
  - Position memory (saved to config/ui_position.json)
  - Hover expansion with 6 quick-action buttons
  - Acrylic/frosted glass effect (approximated via gradient overlay)
  - GPU-accelerated animations at 60fps
  - Smooth morphing between states via OutBack easing
  - Audio-reactive waveforms from real mic/speaker data
  - Thread-safe state updates via Qt signals

Concept: The Dynamic Island is a realtime reactive overlay that communicates
with the backend exclusively through the event bus. All state transitions
are validated by the StateManager FSM before reaching the UI.

Engine interface (identical API to old DynamicIsland & NativeWindowBridge):
    set_state(state, text="")
    set_speak_text(text)
    set_audio_levels(deque)
    set_sleeping(sleeping)
    show_setup(callback)
    send_task(task_id, label, status)
    run()
    destroy()
"""

import collections
import json
import math
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QEasingCurve,
    QRect, pyqtSignal, QObject, QMetaObject, Q_ARG,
    pyqtSlot, QSize, QPoint, QRectF, QPointF,
)
from PyQt6.QtGui import (
    QColor, QPainter, QPainterPath, QFont, QFontMetrics,
    QPen, QBrush, QRadialGradient, QLinearGradient,
    QGuiApplication, QScreen, QCursor,
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QGraphicsDropShadowEffect,
    QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QHBoxLayout,
    QGraphicsBlurEffect,
)

from ui.animations import (
    GlowRenderer, WaveformRenderer, QuickActionRenderer,
    state_qcolor, breathing_value, spinner_angle, STATE_COLORS,
)

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
POSITION_PATH = BASE_DIR / "config" / "ui_position.json"

# ── Size presets per state ────────────────────────────────────

SIZE_MAP = {
    "IDLE":         (200, 40),
    "HOVER":        (380, 70),
    "LISTENING":    (320, 48),
    "PROCESSING":   (300, 48),
    "SPEAKING":     (320, 48),
    "NOTIFICATION": (360, 80),
    "DRAGGING":     (180, 38),
    "ERROR":        (340, 48),
    "SLEEP":        (160, 36),
}

ANIM_DURATION = 350   # ms for size transitions
EDGE_SNAP_PX = 30     # pixels from edge to trigger snap
HOVER_DELAY_MS = 400  # ms delay before hover collapse after mouse leaves
CANVAS_W = 520        # canvas wider than pill for glow overflow
CANVAS_H = 220


class DynamicIslandQt(QWidget):
    """
    Native PyQt6 Dynamic Island overlay.

    Blueprint compliance:
      - Frameless window (FramelessWindowHint)
      - Transparency (WA_TranslucentBackground)
      - Always-on-top (WindowStaysOnTopHint)
      - Draggable positioning (mouse events)
      - Position memory (config/ui_position.json)
      - Edge snapping (magnetic snap to screen edges)
      - Hover expansion (quick-action buttons)
      - Acrylic blur approximation (gradient glass overlay)
      - Smooth morphing (OutBack easing)
      - Reactive waveforms (real audio data)
      - GPU rendering via QPainter with antialiasing
    """

    # Signals for thread-safe state updates from engine thread
    _state_signal = pyqtSignal(str, str)
    _speak_signal = pyqtSignal(str)
    _sleep_signal = pyqtSignal(bool)

    def __init__(self):
        # QApplication must exist before any widget
        self._app = QApplication.instance()
        if self._app is None:
            import sys
            self._app = QApplication(sys.argv)

        super().__init__()

        # ── State ─────────────────────────────────────────
        self._state = "IDLE"
        self._prev_state = "IDLE"     # for returning after HOVER/DRAGGING
        self._state_text = ""
        self._speak_text = ""
        self._audio_levels: Optional[collections.deque] = None
        self._tick = 0
        self._sleeping = False

        # ── Dragging ──────────────────────────────────────
        self._dragging = False
        self._drag_offset = QPoint(0, 0)
        self._pill_screen_x = 0       # pill center X in screen coords
        self._pill_screen_y = 12      # pill top Y (default: near top)

        # ── Hover ─────────────────────────────────────────
        self._hovered = False
        self._hover_btn_index = -1    # which quick-action button is under mouse
        self._hover_hit_areas = []    # [(x, y, r, action_id), ...]
        self._hover_collapse_timer = QTimer(self)
        self._hover_collapse_timer.setSingleShot(True)
        self._hover_collapse_timer.timeout.connect(self._collapse_hover)

        # ── Fonts ─────────────────────────────────────────
        self._font = QFont("Segoe UI", 11)
        self._font.setWeight(QFont.Weight.DemiBold)
        self._font_small = QFont("Segoe UI", 9)
        self._font_small.setWeight(QFont.Weight.Medium)
        self._font_label = QFont("Segoe UI", 10)
        self._font_label.setWeight(QFont.Weight.DemiBold)
        self._font_speak = QFont("Segoe UI", 10)
        self._font_speak.setWeight(QFont.Weight.Normal)
        self._font_sleep = QFont("Segoe UI", 8)
        self._font_sleep.setWeight(QFont.Weight.Normal)

        # ── Current animated size ─────────────────────────
        self._pill_w, self._pill_h = SIZE_MAP["IDLE"]
        self._target_w, self._target_h = self._pill_w, self._pill_h

        # ── Window flags ──────────────────────────────────
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool              # no taskbar icon
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # Enable mouse tracking for hover detection
        self.setMouseTracking(True)

        # Load saved position or default to top-center
        self._load_position()
        self._apply_position()

        # ── Signals → Slots (thread-safe) ─────────────────
        self._state_signal.connect(self._on_state)
        self._speak_signal.connect(self._on_speak)
        self._sleep_signal.connect(self._on_sleep)

        # ── Animation timer (60 FPS) ─────────────────────
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(16)  # ~60fps

        # ── Size animation values ─────────────────────────
        self._anim_progress = 1.0  # 0→1 during size transition
        self._anim_start_w = self._pill_w
        self._anim_start_h = self._pill_h
        self._anim_start_time = 0.0

        # ── Notification auto-dismiss ─────────────────────
        self._notif_timer = QTimer(self)
        self._notif_timer.setSingleShot(True)
        self._notif_timer.timeout.connect(lambda: self._on_state("IDLE", ""))

        log.info("PyQt6 Dynamic Island initialized (with dragging, hover, edge snap)")

    # ══════════════════════════════════════════════════════════
    #  ENGINE INTERFACE — same API as old DynamicIsland
    # ══════════════════════════════════════════════════════════

    def set_state(self, state: str, text: str = ""):
        """Thread-safe state change. Can be called from any thread."""
        self._state_signal.emit(state, text)

    def set_speak_text(self, text: str):
        """Thread-safe speak text update."""
        self._speak_signal.emit(text)

    def set_audio_levels(self, levels):
        """Share the RMS levels deque from the engine."""
        self._audio_levels = levels

    def set_sleeping(self, sleeping: bool):
        """Thread-safe sleep toggle."""
        self._sleep_signal.emit(sleeping)

    def show_setup(self, callback: Callable):
        """Show API key setup dialog."""
        self._setup_callback = callback
        QTimer.singleShot(100, lambda: self._show_setup_dialog())

    def send_task(self, task_id: str, label: str, status: str = "running"):
        """Task cards — for future use. Currently a no-op."""
        pass

    def run(self):
        """Start the Qt event loop (blocks like tkinter mainloop)."""
        self.show()
        log.info("Dynamic Island visible — position: (%d, %d)", self._pill_screen_x, self._pill_screen_y)
        self._app.exec()

    def destroy(self):
        """Close the widget and quit the app."""
        import os as _os
        try:
            QTimer.singleShot(0, self._shutdown)
            import threading
            threading.Timer(2.0, lambda: _os._exit(0)).start()
        except Exception:
            _os._exit(0)

    def _shutdown(self):
        """Runs on the UI thread to cleanly stop everything."""
        self._save_position()
        self._timer.stop()
        self._notif_timer.stop()
        self._hover_collapse_timer.stop()
        self.close()
        self._app.quit()

    # ══════════════════════════════════════════════════════════
    #  SLOTS — run on UI thread (thread-safe via signals)
    # ══════════════════════════════════════════════════════════

    @pyqtSlot(str, str)
    def _on_state(self, state: str, text: str):
        # Don't interrupt HOVER/DRAGGING with minor state updates
        if self._state in ("HOVER", "DRAGGING") and state == "IDLE":
            self._prev_state = state
            return

        self._prev_state = self._state
        self._state = state
        self._state_text = text

        target = SIZE_MAP.get(state, SIZE_MAP["IDLE"])
        self._target_w, self._target_h = target

        # Start size animation
        self._anim_start_w = self._pill_w
        self._anim_start_h = self._pill_h
        self._anim_start_time = time.monotonic()
        self._anim_progress = 0.0

        # Notification auto-dismiss
        if state == "NOTIFICATION":
            self._notif_timer.start(4000)
        else:
            self._notif_timer.stop()

        # Resize canvas if needed for larger states
        self._resize_canvas_for_state(state)

        log.debug("UI state → %s %s", state, text)

    @pyqtSlot(str)
    def _on_speak(self, text: str):
        self._speak_text = text

    @pyqtSlot(bool)
    def _on_sleep(self, sleeping: bool):
        self._sleeping = sleeping
        if sleeping:
            self._on_state("SLEEP", "")
        else:
            self._on_state("LISTENING", "Listening...")
        log.info("Jarvis: %s", "sleeping" if sleeping else "awake")

    # ══════════════════════════════════════════════════════════
    #  POSITION MANAGEMENT — save/restore/edge-snap
    # ══════════════════════════════════════════════════════════

    def _load_position(self):
        """Load saved position from config/ui_position.json."""
        try:
            if POSITION_PATH.exists():
                data = json.loads(POSITION_PATH.read_text(encoding="utf-8"))
                self._pill_screen_x = data.get("x", 0)
                self._pill_screen_y = data.get("y", 12)
                log.info("Position loaded: (%d, %d)", self._pill_screen_x, self._pill_screen_y)
                return
        except Exception as e:
            log.warning("Failed to load position: %s", e)

        # Default: top-center
        screen = QGuiApplication.primaryScreen()
        sw = screen.geometry().width() if screen else 1920
        self._pill_screen_x = sw // 2
        self._pill_screen_y = 12

    def _save_position(self):
        """Save current position to config/ui_position.json."""
        try:
            POSITION_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {"x": self._pill_screen_x, "y": self._pill_screen_y}
            POSITION_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("Failed to save position: %s", e)

    def _apply_position(self):
        """Move the widget canvas so the pill center is at _pill_screen_x."""
        canvas_x = self._pill_screen_x - CANVAS_W // 2
        canvas_y = self._pill_screen_y - 12  # small offset above pill
        self.setGeometry(canvas_x, max(0, canvas_y), CANVAS_W, CANVAS_H)

    def _snap_to_edge(self):
        """
        Edge snapping — if the pill center is near a screen edge, snap to it.

        Concept: Magnetic snapping gives a polished, intentional feel.
        Without it, the pill can end up awkwardly positioned 5px from
        the edge, which looks unfinished.
        """
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        sg = screen.geometry()
        sw, sh = sg.width(), sg.height()
        pw = self._pill_w

        x = self._pill_screen_x
        y = self._pill_screen_y

        # Horizontal snapping
        if abs(x - sw // 2) < EDGE_SNAP_PX:
            x = sw // 2  # snap to center
        elif x < EDGE_SNAP_PX + pw // 2:
            x = int(pw // 2) + 12  # snap to left
        elif x > sw - EDGE_SNAP_PX - pw // 2:
            x = int(sw - pw // 2) - 12  # snap to right

        # Vertical snapping
        if y < EDGE_SNAP_PX:
            y = 12  # snap to top

        self._pill_screen_x = x
        self._pill_screen_y = y

    def _resize_canvas_for_state(self, state: str):
        """Ensure the canvas is large enough for expanded states."""
        tw, th = SIZE_MAP.get(state, SIZE_MAP["IDLE"])
        needed_w = max(CANVAS_W, int(tw) + 140)
        needed_h = max(CANVAS_H, int(th) + 100)
        if needed_w != self.width() or needed_h != self.height():
            canvas_x = self._pill_screen_x - needed_w // 2
            canvas_y = max(0, self._pill_screen_y - 12)
            self.setGeometry(canvas_x, canvas_y, needed_w, needed_h)

    # ══════════════════════════════════════════════════════════
    #  MOUSE EVENTS — Dragging & Hover
    # ══════════════════════════════════════════════════════════

    def _pill_rect(self) -> QRectF:
        """Get the current pill rectangle in widget coordinates."""
        pw, ph = self._pill_w, self._pill_h
        px = (self.width() - pw) / 2
        py = 12
        return QRectF(px, py, pw, ph)

    def _is_in_pill(self, pos) -> bool:
        """Check if a position is inside the pill shape."""
        return self._pill_rect().contains(QPointF(pos.x(), pos.y()))

    def enterEvent(self, event):
        """Mouse entered widget area — check if over the pill."""
        # Allow hover in ALL states so Stop button is always accessible
        if self._dragging:
            return
        self._hover_collapse_timer.stop()

    def leaveEvent(self, event):
        """Mouse left the widget — start collapse timer."""
        if self._hovered and not self._dragging:
            self._hover_collapse_timer.start(HOVER_DELAY_MS)

    def mousePressEvent(self, event):
        """Start drag if click is on the pill."""
        if event.button() == Qt.MouseButton.LeftButton and self._is_in_pill(event.pos()):
            # Check if clicking a quick-action button in HOVER state
            if self._state == "HOVER" and self._hover_hit_areas:
                click_x = event.pos().x()
                click_y = event.pos().y()
                for bx, by, br, action_id in self._hover_hit_areas:
                    dist = math.sqrt((click_x - bx) ** 2 + (click_y - by) ** 2)
                    if dist <= br + 4:  # small tolerance
                        self._on_quick_action(action_id)
                        return

            # Start dragging
            self._dragging = True
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            self._on_state("DRAGGING", "")
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))

    def mouseMoveEvent(self, event):
        """Handle drag movement and hover detection."""
        if self._dragging:
            new_pos = event.globalPosition().toPoint() - self._drag_offset
            self.move(new_pos)
            # Update pill screen position
            self._pill_screen_x = new_pos.x() + self.width() // 2
            self._pill_screen_y = new_pos.y() + 12
            return

        # Hover detection
        in_pill = self._is_in_pill(event.pos())

        if in_pill and not self._hovered:
            # Enter hover from any state (so Stop is always accessible)
            if self._state not in ("HOVER", "DRAGGING"):
                self._hovered = True
                self._hover_collapse_timer.stop()
                self._prev_state = self._state  # remember what we were doing
                self._on_state("HOVER", "")

        if not in_pill and self._hovered:
            # Check if still in expanded area (for button clicks)
            pr = self._pill_rect()
            expanded = QRectF(pr.x() - 20, pr.y() - 10, pr.width() + 40, pr.height() + 40)
            if not expanded.contains(QPointF(event.pos().x(), event.pos().y())):
                self._hover_collapse_timer.start(HOVER_DELAY_MS)

        # Track which button is hovered
        if self._state == "HOVER" and self._hover_hit_areas:
            mx, my = event.pos().x(), event.pos().y()
            self._hover_btn_index = -1
            for i, (bx, by, br, _) in enumerate(self._hover_hit_areas):
                dist = math.sqrt((mx - bx) ** 2 + (my - by) ** 2)
                if dist <= br + 4:
                    self._hover_btn_index = i
                    break

    def mouseReleaseEvent(self, event):
        """End drag — apply edge snapping and save position."""
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

            # Edge snapping
            self._snap_to_edge()
            self._apply_position()
            self._save_position()

            # Return to previous state
            self._hovered = False
            self._on_state("IDLE", "")

    def _collapse_hover(self):
        """Collapse the hover panel back to the previous working state."""
        if self._state == "HOVER" and not self._dragging:
            self._hovered = False
            self._hover_btn_index = -1
            self._hover_hit_areas = []
            # Return to whatever state we were in before hover
            restore = self._prev_state if self._prev_state not in ("HOVER", "DRAGGING") else "IDLE"
            self._on_state(restore, self._state_text)

    def _on_quick_action(self, action_id: str):
        """Handle a quick-action button click."""
        log.info("Quick action: %s", action_id)

        if action_id == "stop_jarvis":
            # Full shutdown — kill the application
            log.info("⏹ STOP button pressed — shutting down Jarvis")
            self._hovered = False
            self._on_state("IDLE", "Shutting down...")
            self.destroy()
            return

        if action_id == "sleep_jarvis":
            # Put Jarvis to sleep (stop listening, keep running)
            log.info("💤 SLEEP button pressed")
            self._hovered = False
            self._hover_hit_areas = []
            self._on_state("SLEEP", "")
            self._sleeping = True
            # Tell engine to sleep via event bus
            try:
                from core.events import get_bus, Event
                get_bus().emit(Event.QUICK_ACTION, {"action": "sleep_jarvis"})
            except Exception:
                pass
            return

        # Default: emit on the event bus
        try:
            from core.events import get_bus, Event
            get_bus().emit(Event.QUICK_ACTION, {"action": action_id})
        except Exception:
            pass

        # Collapse hover after action
        self._collapse_hover()

    # ══════════════════════════════════════════════════════════
    #  ANIMATION TICK (60fps)
    # ══════════════════════════════════════════════════════════

    def _on_tick(self):
        self._tick += 1

        # Animate pill size with OutBack easing (spring feel)
        if self._anim_progress < 1.0:
            elapsed = time.monotonic() - self._anim_start_time
            t = min(1.0, elapsed / (ANIM_DURATION / 1000.0))
            t_eased = self._ease_out_back(t, overshoot=1.2)
            self._pill_w = self._anim_start_w + (self._target_w - self._anim_start_w) * t_eased
            self._pill_h = self._anim_start_h + (self._target_h - self._anim_start_h) * t_eased
            self._anim_progress = t

        self.update()  # trigger repaint

    @staticmethod
    def _ease_out_back(t: float, overshoot: float = 1.2) -> float:
        """
        OutBack easing: overshoots then settles.

        Concept: This gives the Dynamic Island that satisfying "snap" feel
        from iOS, where the pill briefly overshoots its target size before
        settling. The overshoot parameter controls how bouncy it feels.
        """
        t = max(0.0, min(1.0, t))
        t1 = t - 1.0
        return 1.0 + t1 * t1 * ((overshoot + 1) * t1 + overshoot)

    # ══════════════════════════════════════════════════════════
    #  PAINT — Main render loop
    # ══════════════════════════════════════════════════════════

    def paintEvent(self, event):
        """Main render loop — called every frame by QTimer triggering update()."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()
        pw = self._pill_w
        ph = self._pill_h

        # Pill position: centered horizontally in canvas, 12px from top
        px = (w - pw) / 2
        py = 12
        radius = ph / 2  # fully rounded ends

        color = state_qcolor(self._state)

        # 1. Glow halo behind the pill
        glow_intensity = 1.0
        if self._state == "IDLE":
            glow_intensity = breathing_value(self._tick, speed=0.025, amplitude=0.3)
        elif self._state == "SLEEP":
            glow_intensity = 0.2
        elif self._state == "HOVER":
            glow_intensity = 1.4  # brighter on hover
        elif self._state == "DRAGGING":
            glow_intensity = 0.6

        GlowRenderer.draw(
            p, px + pw / 2, py + ph / 2,
            pw, ph, color, glow_intensity
        )

        # 2. Pill background — acrylic/frosted glass approximation
        pill_path = QPainterPath()
        pill_path.addRoundedRect(px, py, pw, ph, radius, radius)

        # Dark glass base
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(10, 10, 12, 215)))
        p.drawPath(pill_path)

        # Frosted glass gradient overlay (acrylic approximation)
        # Concept: Real acrylic blur requires DwmExtendFrameIntoClientArea on Win32.
        # We approximate it with a subtle gradient that simulates glass reflection.
        glass_grad = QLinearGradient(px, py, px, py + ph)
        glass_grad.setColorAt(0.0, QColor(255, 255, 255, 12))
        glass_grad.setColorAt(0.5, QColor(255, 255, 255, 4))
        glass_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(glass_grad))
        p.drawPath(pill_path)

        # Subtle border
        border_pen = QPen(QColor(255, 255, 255, 22))
        border_pen.setWidthF(1.0)
        p.setPen(border_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(pill_path)

        # 3. Inner content (state-specific)
        p.setClipPath(pill_path)
        self._draw_content(p, px, py, pw, ph)
        p.setClipping(False)

        # 4. Quick-action buttons (drawn OUTSIDE clip for labels below pill)
        if self._state == "HOVER":
            btn_cy = py + ph - 12
            self._hover_hit_areas = QuickActionRenderer.draw(
                p, px + pw / 2, btn_cy, pw,
                hover_index=self._hover_btn_index,
                tick=self._tick,
            )

        p.end()

    # ══════════════════════════════════════════════════════════
    #  STATE-SPECIFIC RENDERERS
    # ══════════════════════════════════════════════════════════

    def _draw_content(self, p: QPainter, px, py, pw, ph):
        """Draw state-specific inner content."""
        state = self._state
        color = state_qcolor(state)

        if state == "IDLE":
            self._draw_idle(p, px, py, pw, ph, color)
        elif state == "HOVER":
            self._draw_hover(p, px, py, pw, ph, color)
        elif state == "LISTENING":
            self._draw_listening(p, px, py, pw, ph, color)
        elif state == "PROCESSING":
            self._draw_processing(p, px, py, pw, ph, color)
        elif state == "SPEAKING":
            self._draw_speaking(p, px, py, pw, ph, color)
        elif state == "NOTIFICATION":
            self._draw_notification(p, px, py, pw, ph, color)
        elif state == "DRAGGING":
            self._draw_dragging(p, px, py, pw, ph, color)
        elif state == "ERROR":
            self._draw_error(p, px, py, pw, ph, color)
        elif state == "SLEEP":
            self._draw_sleep(p, px, py, pw, ph, color)

    def _draw_idle(self, p, px, py, pw, ph, color):
        """IDLE: Small pill with breathing dot + 'J.A.R.V.I.S' text + mini waveform."""
        cx = px + pw / 2
        cy = py + ph / 2

        # Breathing green dot
        dot_r = 3.5 * breathing_value(self._tick, 0.04, 0.2)
        p.setPen(Qt.PenStyle.NoPen)
        dot_color = QColor("#30D158")
        dot_color.setAlpha(200)
        p.setBrush(QBrush(dot_color))
        p.drawEllipse(int(px + 16), int(cy - dot_r), int(dot_r * 2), int(dot_r * 2))

        # Text
        p.setPen(QPen(QColor(255, 255, 255, 150)))
        p.setFont(self._font_small)
        text = self._state_text if self._state_text else "J.A.R.V.I.S"
        p.drawText(int(px + 30), int(py), int(pw - 70), int(ph),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)

        # Mini waveform (5 bars)
        levels = list(self._audio_levels)[-5:] if self._audio_levels else [0.1, 0.15, 0.2, 0.15, 0.1]
        bar_x = px + pw - 45
        for i, lv in enumerate(levels):
            idle_lv = 0.15 + 0.1 * math.sin(self._tick * 0.05 + i * 0.8)
            bh = max(3, idle_lv * 14)
            bar_color = QColor(255, 255, 255, 50)
            p.setBrush(QBrush(bar_color))
            p.setPen(Qt.PenStyle.NoPen)
            bx = bar_x + i * 5
            by = cy - bh / 2
            p.drawRoundedRect(int(bx), int(by), 2, int(bh), 1, 1)

    def _draw_hover(self, p, px, py, pw, ph, color):
        """
        HOVER: Expanded pill showing working status + quick-action buttons.

        Shows what Jarvis is currently doing (Listening, Processing, etc.)
        and a row of quick-action buttons including Stop.
        """
        cy = py + ph / 2

        # Build header text showing current working status
        status_map = {
            "LISTENING": "🟢 Listening",
            "PROCESSING": "🔵 Processing",
            "SPEAKING": "🔵 Speaking",
            "ERROR": "🔴 Error",
            "SLEEP": "🌙 Sleeping",
        }
        prev = self._prev_state
        status = status_map.get(prev, "")
        if status:
            header = f"J.A.R.V.I.S — {status}"
        else:
            header = "J.A.R.V.I.S — Quick Actions"

        # Header text
        p.setPen(QPen(QColor(255, 255, 255, 180)))
        p.setFont(self._font_small)
        p.drawText(int(px + 16), int(py + 2), int(pw - 32), 20,
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   header)

        # Subtle divider with state-aware color
        div_y = py + 22
        from ui.animations import state_qcolor
        divider_color = state_qcolor(prev) if prev in status_map else color
        grad = QLinearGradient(px + 16, div_y, px + pw - 16, div_y)
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        c_mid = QColor(divider_color)
        c_mid.setAlpha(80)
        grad.setColorAt(0.5, c_mid)
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(QPen(QBrush(grad), 1.0))
        p.drawLine(int(px + 16), int(div_y), int(px + pw - 16), int(div_y))

    def _draw_listening(self, p, px, py, pw, ph, color):
        """LISTENING: Pulsing green dot + 'Listening...' + live waveform."""
        cy = py + ph / 2

        # Pulsing dot
        pulse = breathing_value(self._tick, 0.08, 0.4)
        dot_r = 4.5 * pulse
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(int(px + 18 - dot_r), int(cy - dot_r), int(dot_r * 2), int(dot_r * 2))

        # Pulse ring
        ring_r = 4.5 + (self._tick % 40) * 0.3
        ring_alpha = max(0, 120 - (self._tick % 40) * 3)
        ring_color = QColor(color)
        ring_color.setAlpha(ring_alpha)
        ring_pen = QPen(ring_color)
        ring_pen.setWidthF(1.5)
        p.setPen(ring_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(int(px + 18 - ring_r), int(cy - ring_r), int(ring_r * 2), int(ring_r * 2))

        # Text
        p.setPen(QPen(QColor(255, 255, 255, 230)))
        p.setFont(self._font_label)
        p.drawText(int(px + 34), int(py), int(pw - 140), int(ph),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Listening…")

        # Live waveform
        levels = list(self._audio_levels) if self._audio_levels else []
        WaveformRenderer.draw(
            p, px + pw - 100, cy, 80, 22, levels, color, bar_count=16
        )

    def _draw_processing(self, p, px, py, pw, ph, color):
        """PROCESSING: Spinning arcs + status text + sparkle."""
        cy = py + ph / 2
        angle = spinner_angle(self._tick, speed=5.0)

        # Spinning arc
        arc_cx = px + 20
        arc_r = 7
        arc_rect = QRect(int(arc_cx - arc_r), int(cy - arc_r), int(arc_r * 2), int(arc_r * 2))
        arc_pen = QPen(color)
        arc_pen.setWidthF(2.0)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(arc_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(arc_rect, int(angle * 16), int(270 * 16))

        # Text
        p.setPen(QPen(QColor(255, 255, 255, 230)))
        p.setFont(self._font_label)
        text = self._state_text if self._state_text else "Processing…"
        p.drawText(int(px + 38), int(py), int(pw - 70), int(ph),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)

        # Sparkle dots on right
        sparkle_alpha = int(80 + 80 * math.sin(self._tick * 0.06))
        spark_color = QColor(color)
        spark_color.setAlpha(sparkle_alpha)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(spark_color))
        for i in range(3):
            sx = px + pw - 30 + i * 7
            sy = cy - 1.5
            p.drawEllipse(int(sx), int(sy), 3, 3)

    def _draw_speaking(self, p, px, py, pw, ph, color):
        """SPEAKING: Compact pill with pulsing blue dot + label + waveform."""
        cy = py + ph / 2

        # Pulsing blue dot
        pulse = breathing_value(self._tick, 0.1, 0.3)
        dot_r = 4 * pulse
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(int(px + 16 - dot_r), int(cy - dot_r), int(dot_r * 2), int(dot_r * 2))

        # "Speaking" label
        p.setPen(QPen(QColor(255, 255, 255, 230)))
        p.setFont(self._font_label)
        p.drawText(int(px + 30), int(py), int(pw - 140), int(ph),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Speaking")

        # Waveform on the right
        levels = list(self._audio_levels) if self._audio_levels else []
        WaveformRenderer.draw(
            p, px + pw - 100, cy, 80, 22, levels, color, bar_count=12
        )

    def _draw_notification(self, p, px, py, pw, ph, color):
        """NOTIFICATION: Bell icon + notification text."""
        cy = py + ph / 2

        # Bell icon (simple circle)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(int(px + 14), int(py + 14), 12, 12)

        # Icon symbol
        p.setPen(QPen(QColor(0, 0, 0, 220)))
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.drawText(int(px + 14), int(py + 14), 12, 12,
                   Qt.AlignmentFlag.AlignCenter, "!")

        # Title
        p.setPen(QPen(QColor(255, 255, 255, 230)))
        p.setFont(self._font_label)
        p.drawText(int(px + 34), int(py + 8), int(pw - 50), 22,
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Notification")

        # Body text
        p.setPen(QPen(QColor(255, 255, 255, 150)))
        p.setFont(self._font_small)
        body = self._state_text if self._state_text else ""
        p.drawText(int(px + 34), int(py + 32), int(pw - 50), int(ph - 40),
                   Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                   body)

    def _draw_dragging(self, p, px, py, pw, ph, color):
        """DRAGGING: Compact pill with subtle grip indicator."""
        cy = py + ph / 2

        # Grip dots (3 vertical dots)
        p.setPen(Qt.PenStyle.NoPen)
        grip_color = QColor(255, 255, 255, 80)
        p.setBrush(QBrush(grip_color))
        for i in range(3):
            dy = cy - 6 + i * 6
            p.drawEllipse(int(px + pw / 2 - 1), int(dy), 3, 3)

    def _draw_error(self, p, px, py, pw, ph, color):
        """ERROR: Warning icon + red text, slight shake via offset."""
        cy = py + ph / 2
        # Shake effect
        shake = 2 * math.sin(self._tick * 0.5) if self._tick % 60 < 10 else 0

        # Warning triangle
        p.setPen(Qt.PenStyle.NoPen)
        flash_alpha = int(200 + 55 * math.sin(self._tick * 0.15))
        err_color = QColor(color)
        err_color.setAlpha(flash_alpha)
        p.setBrush(QBrush(err_color))
        p.drawEllipse(int(px + 16 + shake), int(cy - 5), 10, 10)

        # "!" in center
        p.setPen(QPen(QColor(0, 0, 0, 230)))
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.drawText(int(px + 16 + shake), int(cy - 5), 10, 10,
                   Qt.AlignmentFlag.AlignCenter, "!")

        # Error text
        p.setPen(QPen(err_color))
        p.setFont(self._font_label)
        text = self._state_text if self._state_text else "Error"
        p.drawText(int(px + 34 + shake), int(py), int(pw - 50), int(ph),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)

    def _draw_sleep(self, p, px, py, pw, ph, color):
        """SLEEP: Tiny dim pill with moon symbol."""
        cy = py + ph / 2

        # Moon crescent
        p.setPen(Qt.PenStyle.NoPen)
        moon_color = QColor(color)
        moon_color.setAlpha(120)
        p.setBrush(QBrush(moon_color))
        p.drawEllipse(int(px + 14), int(cy - 5), 10, 10)
        # Cut-out for crescent
        p.setBrush(QBrush(QColor(10, 10, 12, 215)))
        p.drawEllipse(int(px + 18), int(cy - 6), 8, 8)

        # "Sleep" text
        p.setPen(QPen(QColor(255, 255, 255, 80)))
        p.setFont(self._font_sleep)
        p.drawText(int(px + 30), int(py), int(pw - 40), int(ph),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Sleep")

    # ══════════════════════════════════════════════════════════
    #  SETUP DIALOG
    # ══════════════════════════════════════════════════════════

    def _show_setup_dialog(self):
        """Modal dialog for API key entry."""
        dialog = QDialog()
        dialog.setWindowTitle("J.A.R.V.I.S — Setup")
        dialog.setFixedSize(420, 200)
        dialog.setStyleSheet("""
            QDialog { background: #0a0a0a; }
            QLabel { color: #8ffcff; font-family: 'Segoe UI'; }
            QLineEdit {
                background: #111; color: #fff; border: 1px solid #333;
                border-radius: 6px; padding: 8px; font-size: 13px;
                font-family: 'Segoe UI';
            }
            QLineEdit:focus { border-color: #0A84FF; }
            QPushButton {
                background: #0A84FF; color: #fff; border: none;
                border-radius: 6px; padding: 10px 20px; font-size: 13px;
                font-weight: 600; font-family: 'Segoe UI';
            }
            QPushButton:hover { background: #0070E0; }
        """)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = QLabel("◈  Initialisation Required")
        title.setStyleSheet("font-size: 15px; font-weight: 700;")
        layout.addWidget(title)

        subtitle = QLabel("Enter your Gemini API key to activate J.A.R.V.I.S.")
        subtitle.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(subtitle)

        entry = QLineEdit()
        entry.setPlaceholderText("Gemini API Key")
        entry.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(entry)

        btn = QPushButton("▸  INITIALISE SYSTEMS")
        layout.addWidget(btn)

        def on_submit():
            key = entry.text().strip()
            if key and hasattr(self, '_setup_callback'):
                dialog.accept()
                self._setup_callback(key)

        btn.clicked.connect(on_submit)
        entry.returnPressed.connect(on_submit)
        dialog.exec()
