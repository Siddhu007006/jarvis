"""
ui/animations.py — Animation helpers for the PyQt6 Dynamic Island.

Provides:
  SpringEasing    — Custom QEasingCurve that mimics spring physics
  GlowRenderer    — Draws radial glow gradients behind the pill
  WaveformRenderer— Draws audio-reactive bar visualization
  lerp_color()    — Smooth color interpolation
"""

import math
from PyQt6.QtCore import QPointF, Qt, QEasingCurve, QRectF
from PyQt6.QtGui import (
    QColor, QPainter, QRadialGradient, QBrush, QPen,
    QLinearGradient,
)


# ── Spring Easing ─────────────────────────────────────────────

def create_spring_easing() -> QEasingCurve:
    """
    Returns a custom easing curve that approximates spring physics.

    The curve overshoots by ~8% then settles — this gives the Dynamic
    Island size transitions that satisfying "snap" feel, similar to
    iOS Dynamic Island spring animations.

    Technical: Uses QEasingCurve.Type.OutBack with a mild overshoot
    amplitude. Qt's OutBack is defined as:
        f(t) = t * t * ((s+1)*t - s)  where s = overshoot amount
    We set s ≈ 1.2 (subtle, not bouncy).
    """
    curve = QEasingCurve(QEasingCurve.Type.OutBack)
    curve.setOvershoot(1.2)
    return curve


# ── Color Helpers ─────────────────────────────────────────────

def lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    """
    Linear interpolation between two QColors.

    t=0 → c1, t=1 → c2, t=0.5 → midpoint.
    Clamps t to [0, 1]. Interpolates in RGBA space.
    """
    t = max(0.0, min(1.0, t))
    return QColor(
        int(c1.red()   + (c2.red()   - c1.red())   * t),
        int(c1.green() + (c2.green() - c1.green()) * t),
        int(c1.blue()  + (c2.blue()  - c1.blue())  * t),
        int(c1.alpha() + (c2.alpha() - c1.alpha()) * t),
    )


def hex_to_qcolor(hex_str: str, alpha: int = 255) -> QColor:
    """Convert '#RRGGBB' string to QColor with optional alpha."""
    c = QColor(hex_str)
    c.setAlpha(alpha)
    return c


# ── State Color Palette ───────────────────────────────────────

STATE_COLORS = {
    "IDLE":         "#8E8E93",
    "HOVER":        "#A0A0A8",
    "LISTENING":    "#30D158",
    "PROCESSING":   "#5E5CE6",
    "SPEAKING":     "#0A84FF",
    "NOTIFICATION": "#FF9F0A",
    "DRAGGING":     "#636366",
    "ERROR":        "#FF453A",
    "SLEEP":        "#48484A",
}


def state_qcolor(state: str, alpha: int = 255) -> QColor:
    """Get the accent QColor for a given state."""
    hex_str = STATE_COLORS.get(state, "#8E8E93")
    return hex_to_qcolor(hex_str, alpha)


# ── Glow Renderer ─────────────────────────────────────────────

class GlowRenderer:
    """
    Draws a radial gradient glow behind the pill shape.

    The glow is a soft, radially-fading halo of the state color.
    It extends beyond the pill bounds to create a "floating" effect
    against the transparent desktop.
    """

    @staticmethod
    def draw(
        painter: QPainter,
        center_x: float,
        center_y: float,
        width: float,
        height: float,
        color: QColor,
        intensity: float = 1.0,
    ):
        """
        Draw a radial glow centered at (center_x, center_y).

        Args:
            painter:   Active QPainter
            center_x:  Horizontal center of the glow
            center_y:  Vertical center of the glow
            width:     Horizontal radius of the glow ellipse
            height:    Vertical radius of the glow ellipse
            color:     Base color (alpha will be modulated by intensity)
            intensity: 0.0–1.0, controls glow opacity
        """
        # Glow extends 30px beyond pill in each direction
        glow_w = width + 60
        glow_h = height + 40

        gradient = QRadialGradient(
            QPointF(center_x, center_y),
            max(glow_w, glow_h) / 2,
        )

        # Inner: state color at ~20% opacity
        inner_alpha = int(50 * intensity)
        inner = QColor(color)
        inner.setAlpha(min(255, inner_alpha))
        gradient.setColorAt(0.0, inner)

        # Mid: state color at ~8% opacity
        mid = QColor(color)
        mid.setAlpha(int(20 * intensity))
        gradient.setColorAt(0.5, mid)

        # Outer: fully transparent
        gradient.setColorAt(1.0, QColor(0, 0, 0, 0))

        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawEllipse(
            QPointF(center_x, center_y),
            glow_w / 2,
            glow_h / 2,
        )
        painter.restore()


# ── Waveform Renderer ─────────────────────────────────────────

class WaveformRenderer:
    """
    Draws audio-reactive waveform bars.

    Takes RMS level data (0.0–1.0 per bar) and renders rounded
    vertical bars with the specified accent color. Used in LISTENING
    and SPEAKING states to show live audio activity.
    """

    @staticmethod
    def draw(
        painter: QPainter,
        x: float,
        y: float,
        total_width: float,
        max_height: float,
        levels: list,
        color: QColor,
        bar_count: int = 16,
        bar_gap: float = 2.0,
    ):
        """
        Draw waveform bars.

        Args:
            painter:     Active QPainter
            x:           Left edge X position
            y:           Vertical center Y position
            total_width: Total width available for all bars
            max_height:  Maximum bar height (peak)
            levels:      List of float values 0.0–1.0
            color:       Bar fill color
            bar_count:   Number of bars to draw
            bar_gap:     Pixel gap between bars
        """
        if not levels:
            return

        bar_width = max(1.5, (total_width - (bar_count - 1) * bar_gap) / bar_count)
        step = max(1, len(levels) // bar_count)

        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        for i in range(bar_count):
            level_idx = min(i * step, len(levels) - 1)
            level = levels[level_idx] if level_idx < len(levels) else 0.0
            level = max(0.0, min(1.0, level))

            bar_h = max(3.0, level * max_height)

            # Slight opacity variation based on level
            bar_color = QColor(color)
            bar_color.setAlpha(int(180 + level * 75))

            painter.setBrush(QBrush(bar_color))

            bx = x + i * (bar_width + bar_gap)
            by = y - bar_h / 2

            # Draw rounded rect bar
            painter.drawRoundedRect(
                int(bx), int(by), int(bar_width), int(bar_h),
                bar_width / 2, bar_width / 2,
            )

        painter.restore()


# ── Breathing Animation Helper ────────────────────────────────

def breathing_value(tick: int, speed: float = 0.03, amplitude: float = 0.15) -> float:
    """
    Returns a value oscillating smoothly between (1 - amplitude) and (1 + amplitude).

    Used for the IDLE state breathing glow effect. The sine wave gives
    a natural, organic pulsing feel.

    Args:
        tick:      Frame counter (incremented each paint call)
        speed:     Oscillation speed (radians per tick)
        amplitude: Oscillation range (0.15 = ±15%)

    Returns:
        Float between ~0.85 and ~1.15 (with default params)
    """
    return 1.0 + amplitude * math.sin(tick * speed)


def spinner_angle(tick: int, speed: float = 4.0) -> float:
    """
    Returns a continuously increasing angle for spinner animations.

    Args:
        tick:  Frame counter
        speed: Degrees per tick

    Returns:
        Angle in degrees (0–360, wrapping)
    """
    return (tick * speed) % 360.0


# ── Quick Action Button Renderer ──────────────────────────────

class QuickActionRenderer:
    """
    Renders circular quick-action buttons for the HOVER state.

    Each button is a filled circle with an icon character (emoji/symbol),
    an optional label below, and a hover glow effect.

    Concept: The hover panel shows 6 quick actions arranged in a row.
    Each is an independently clickable circular button with a subtle
    glow that intensifies when the mouse is near.
    """

    # Quick action definitions: (icon, label, action_id, color_hex_or_None)
    # color_hex_or_None: if set, overrides the default white button style
    ACTIONS = [
        ("⚙", "Settings", "settings", None),
        ("🧠", "Memory", "memory", None),
        ("📋", "Clipboard", "clipboard", None),
        ("💤", "Sleep", "sleep_jarvis", None),
        ("🌐", "Browser", "browser", None),
        ("⏹", "Stop", "stop_jarvis", "#FF453A"),
    ]

    BUTTON_RADIUS = 16
    BUTTON_SPACING = 52

    @classmethod
    def draw(
        cls,
        painter: QPainter,
        center_x: float,
        center_y: float,
        total_width: float,
        hover_index: int = -1,
        tick: int = 0,
    ) -> list:
        """
        Draw all quick-action buttons centered around (center_x, center_y).

        Args:
            painter:      Active QPainter
            center_x:     Horizontal center of the button row
            center_y:     Vertical center of the buttons
            total_width:  Available width for buttons
            hover_index:  Index of button under mouse (-1 = none)
            tick:         Animation tick for glow effects

        Returns:
            List of (x, y, radius, action_id) tuples for hit testing
        """
        n = len(cls.ACTIONS)
        start_x = center_x - (n - 1) * cls.BUTTON_SPACING / 2
        hit_areas = []

        painter.save()

        for i, (icon, label, action_id, color_override) in enumerate(cls.ACTIONS):
            bx = start_x + i * cls.BUTTON_SPACING
            by = center_y
            r = cls.BUTTON_RADIUS

            # Determine button accent color
            if color_override:
                btn_accent = QColor(color_override)
            else:
                btn_accent = QColor(255, 255, 255)

            # Hover glow
            is_hovered = (i == hover_index)
            if is_hovered:
                glow_alpha = int(60 + 30 * math.sin(tick * 0.08))
                glow_color = QColor(btn_accent)
                glow_color.setAlpha(glow_alpha)
                glow_grad = QRadialGradient(QPointF(bx, by), r * 2.5)
                glow_grad.setColorAt(0.0, glow_color)
                glow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(glow_grad))
                painter.drawEllipse(QPointF(bx, by), r * 2.5, r * 2.5)

            # Button circle — use accent color for colored buttons
            if color_override:
                bg_alpha = 160 if is_hovered else 100
                bg_color = QColor(btn_accent)
                bg_color.setAlpha(bg_alpha)
            else:
                bg_alpha = 80 if is_hovered else 40
                bg_color = QColor(255, 255, 255, bg_alpha)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(bg_color))
            painter.drawEllipse(QPointF(bx, by), r, r)

            # Border
            if color_override:
                border_color = QColor(btn_accent)
                border_color.setAlpha(180 if is_hovered else 80)
            else:
                border_color = QColor(255, 255, 255, 100 if is_hovered else 30)

            border_pen = QPen(border_color)
            border_pen.setWidthF(1.0)
            painter.setPen(border_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(bx, by), r, r)

            # Icon (emoji)
            from PyQt6.QtGui import QFont
            icon_font = QFont("Segoe UI Emoji", 11)
            painter.setFont(icon_font)
            icon_alpha = 255 if is_hovered else (200 if color_override else 160)
            painter.setPen(QPen(QColor(255, 255, 255, icon_alpha)))
            icon_rect = QRectF(bx - r, by - r, r * 2, r * 2)
            painter.drawText(icon_rect, Qt.AlignmentFlag.AlignCenter, icon)

            # Label below
            label_font = QFont("Segoe UI", 7)
            painter.setFont(label_font)
            if color_override:
                label_color = QColor(btn_accent)
                label_color.setAlpha(200 if is_hovered else 120)
            else:
                label_color = QColor(255, 255, 255, 140 if is_hovered else 70)
            painter.setPen(QPen(label_color))
            label_rect = QRectF(bx - 30, by + r + 2, 60, 14)
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, label)

            hit_areas.append((bx, by, r, action_id))

        painter.restore()
        return hit_areas
