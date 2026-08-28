from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from PyQt6.QtCore import (
    QEasingCurve, QMimeData, QObject, QPointF, QRectF, QSize, Qt,
    QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QFontDatabase,
    QFontMetrics, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPalette, QPen, QPixmap, QRadialGradient, QShortcut, QTextBlockFormat,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar,
    QGraphicsDropShadowEffect,
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWebEngineWidgets import QWebEngineView


# ─────────────────────────────────────────────────────────────────────────────
#  Paths & constants
# ─────────────────────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "core" / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"

_DEFAULT_W, _DEFAULT_H = 1180, 760
_MIN_W,     _MIN_H     = 900, 620
_LEFT_W  = 200
_RIGHT_W = 368   # widened — gives the activity log more room

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


# ─────────────────────────────────────────────────────────────────────────────
#  Design tokens — premium dark glass (Apple / OpenAI / Microsoft / Linear)
# ─────────────────────────────────────────────────────────────────────────────

class C:
    """Premium palette — two accents (blue / green) + grayscale glass tones."""

    # Surfaces
    BG        = "#050505"   # deep matte black
    GLASS     = "#101214"   # glass fill
    PANEL     = "#14181F"   # panel fill
    PANEL2    = "#0C0E11"   # deeper glass base

    # Accents (the only two)
    BLUE      = "#00AFFF"
    GREEN     = "#00FF88"

    # Text hierarchy
    TEXT      = "#FFFFFF"
    WHITE     = "#FFFFFF"
    SECONDARY = "#B8BDC5"
    TEXT_MED  = "#B8BDC5"
    MUTED     = "#7A7F87"
    TEXT_DIM  = "#5E646B"

    # Hairline borders (one shared token — was previously undefined)
    BORDER    = "rgba(255, 255, 255, 0.06)"

    # Status
    SUCCESS   = "#00FF88"
    WARN      = "#E0A13C"
    ERROR     = "#E05555"

    # Chat
    CHAT_AI   = "#00AFFF"
    CHAT_USER = "#EAF0F6"

    # Legacy compatibility aliases
    PRI       = BLUE
    PRI_DIM   = "#0088BB"
    PRI_GHO   = "#001728"
    SEC       = GREEN
    SEC_DIM   = "#00AA66"
    WARN_     = WARN
    ERROR_    = ERROR
    MUTED_C   = ERROR
    BAR_BG    = BLUE
    ACC       = BLUE
    ACC2      = GREEN
    C_GREEN   = GREEN
    BORDER_B  = BLUE
    BORDER_G  = GREEN
    RED       = ERROR
    GLOW      = BLUE


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


# ─────────────────────────────────────────────────────────────────────────────
#  Paint caches — avoid re-allocating brushes / pens / fonts / gradients
# ─────────────────────────────────────────────────────────────────────────────

_BRUSHES: dict = {}
_PENS:    dict = {}
_FONTS:   dict = {}
_GRADS:   dict = {}


def _brush(hexc: str, alpha: int = 255) -> QBrush:
    key = (hexc, alpha)
    b = _BRUSHES.get(key)
    if b is None:
        c = QColor(hexc)
        if alpha < 255:
            c.setAlpha(alpha)
        b = QBrush(c)
        _BRUSHES[key] = b
    return b


def _pen(hexc: str, width: float = 1.0, alpha: int = 255,
         style=Qt.PenStyle.SolidLine) -> QPen:
    sv = getattr(style, "value", style)   # PyQt6 enums need .value
    key = (hexc, round(width, 2), alpha, sv)
    p = _PENS.get(key)
    if p is None:
        c = QColor(hexc)
        if alpha < 255:
            c.setAlpha(alpha)
        p = QPen(c)
        p.setWidthF(width)
        p.setStyle(style)
        _PENS[key] = p
    return p


_FONT_FAMILY: str | None = None


def _ff() -> str:
    """Pick the platform's premium UI font family once (no DB query)."""
    global _FONT_FAMILY
    if _FONT_FAMILY is None:
        if _OS == "Windows":
            _FONT_FAMILY = "Segoe UI Variable Text"
        elif _OS == "Darwin":
            _FONT_FAMILY = "SF Pro Display"
        else:
            _FONT_FAMILY = "Inter"
    return _FONT_FAMILY


def _font(px: int, weight=QFont.Weight.Normal, ls: float = 0.0) -> QFont:
    key = (px, weight, round(ls, 2))
    f = _FONTS.get(key)
    if f is None:
        f = QFont(_ff())
        f.setPixelSize(px)
        f.setWeight(weight)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, ls)
        f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        _FONTS[key] = f
    return f


def _radial(cx: float, cy: float, r: float, stops: list[tuple[float, QColor]]):
    """Cached radial gradient keyed by geometry + color stops."""
    key = (round(cx, 1), round(cy, 1), round(r, 1),
           tuple((round(s, 3), c.name(), c.alpha()) for s, c in stops))
    g = _GRADS.get(key)
    if g is None:
        g = QRadialGradient(cx, cy, max(r, 0.1))
        for s, c in stops:
            g.setColorAt(s, c)
        _GRADS[key] = g
    return g


def _linear(x1, y1, x2, y2, stops):
    key = (round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1),
           tuple((round(s, 3), c.name(), c.alpha()) for s, c in stops))
    g = _GRADS.get(key)
    if g is None:
        g = QLinearGradient(x1, y1, x2, y2)
        for s, c in stops:
            g.setColorAt(s, c)
        _GRADS[key] = g
    return g


# ─────────────────────────────────────────────────────────────────────────────
#  Icon system — single SVG outline library (Lucide), rendered & cached
# ─────────────────────────────────────────────────────────────────────────────

_ICON_SVG: dict[str, str] = {
    "activity":    '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "archive":     '<rect x="2" y="3" width="20" height="5" rx="1"/>'
                   '<path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/>'
                   '<path d="M10 12h4"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "arrow-up":    '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
    "audio":       '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/>'
                   '<circle cx="18" cy="16" r="3"/>',
    "battery":     '<rect x="2" y="7" width="16" height="10" rx="2"/>'
                   '<line x1="22" x2="22" y1="11" y2="13"/>'
                   '<line x1="6" x2="6" y1="11" y2="13"/>',
    "bell":        '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/>'
                   '<path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    "bot":         '<path d="M12 8V4H8"/>'
                   '<rect width="16" height="12" x="4" y="8" rx="2"/>'
                   '<path d="M2 14h2"/><path d="M20 14h2"/>'
                   '<path d="M15 13v2"/><path d="M9 13v2"/>',
    "calendar":    '<rect x="3" y="4" width="18" height="18" rx="2"/>'
                   '<line x1="16" x2="16" y1="2" y2="6"/>'
                   '<line x1="8" x2="8" y1="2" y2="6"/>'
                   '<line x1="3" x2="21" y1="10" y2="10"/>',
    "check":       '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>'
                   '<polyline points="22 4 12 14.01 9 11.01"/>',
    "chip":        '<rect x="5" y="5" width="14" height="14" rx="2"/>'
                   '<rect x="9" y="9" width="6" height="6"/>'
                   '<path d="M9 2v2"/><path d="M15 2v2"/>'
                   '<path d="M9 20v2"/><path d="M15 20v2"/>'
                   '<path d="M2 9h2"/><path d="M2 15h2"/>'
                   '<path d="M20 9h2"/><path d="M20 15h2"/>',
    "clock":       '<circle cx="12" cy="12" r="10"/>'
                   '<polyline points="12 6 12 12 16 14"/>',
    "code":        '<polyline points="16 18 22 12 16 6"/>'
                   '<polyline points="8 6 2 12 8 18"/>',
    "cpu":         '<rect x="4" y="4" width="16" height="16" rx="2"/>'
                   '<rect x="9" y="9" width="6" height="6"/>'
                   '<path d="M9 2v2"/><path d="M15 2v2"/>'
                   '<path d="M9 20v2"/><path d="M15 20v2"/>'
                   '<path d="M2 9h2"/><path d="M2 15h2"/>'
                   '<path d="M20 9h2"/><path d="M20 15h2"/>',
    "database":    '<ellipse cx="12" cy="5" rx="9" ry="3"/>'
                   '<path d="M3 5V19A9 3 0 0 0 21 19V5"/>'
                   '<path d="M3 12A9 3 0 0 0 21 12"/>',
    "eye":         '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>'
                   '<circle cx="12" cy="12" r="3"/>',
    "file":        '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>'
                   '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
    "file-text":   '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>'
                   '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>'
                   '<path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    "folder":      '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    "gauge":       '<path d="m12 14 4-4"/>'
                   '<path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    "globe":       '<circle cx="12" cy="12" r="10"/>'
                   '<path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/>'
                   '<path d="M2 12h20"/>',
    "hand":        '<path d="M11 12V5a2 2 0 0 1 4 0v7"/>'
                   '<path d="M15 12V4a2 2 0 0 1 4 0v8"/>'
                   '<path d="M19 12V6a2 2 0 0 1 4 0v6a7 7 0 0 1-7 7h-2a7 7 0 0 1-5-2L4.2 13.8a2 2 0 0 1 2.83-2.83L11 15"/>',
    "hand-control": '<path d="M18 11V6a2 2 0 0 0-2-2 2 2 0 0 0-2 2"/>'
                   '<path d="M14 10V4a2 2 0 0 0-2-2 2 2 0 0 0-2 2v2"/>'
                   '<path d="M10 10.5V6a2 2 0 0 0-2-2 2 2 0 0 0-2 2v8"/>'
                   '<path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.8-2.2L3 17.6a2 2 0 0 1 3-2.7L7 17"/>',
    "camera":      '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/>'
                   '<circle cx="12" cy="13" r="3"/>',
    "layout-template": '<rect width="18" height="7" x="3" y="3" rx="1"/>'
                       '<rect width="9" height="7" x="3" y="14" rx="1"/>'
                       '<rect width="5" height="7" x="16" y="14" rx="1"/>',
    "hash":        '<line x1="4" x2="20" y1="9" y2="9"/>'
                   '<line x1="4" x2="20" y1="15" y2="15"/>'
                   '<line x1="10" x2="8" y1="3" y2="21"/>'
                   '<line x1="16" x2="14" y1="3" y2="21"/>',
    "home":        '<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
                   '<polyline points="9 22 9 12 15 12 15 22"/>',
    "image":       '<rect x="3" y="3" width="18" height="18" rx="2"/>'
                   '<circle cx="9" cy="9" r="2"/>'
                   '<path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
    "layout":      '<rect x="3" y="3" width="18" height="18" rx="2"/>'
                   '<line x1="3" x2="21" y1="9" y2="9"/>'
                   '<line x1="9" x2="9" y1="21" y2="9"/>',
    "list":        '<line x1="8" x2="21" y1="6" y2="6"/>'
                   '<line x1="8" x2="21" y1="12" y2="12"/>'
                   '<line x1="8" x2="21" y1="18" y2="18"/>'
                   '<line x1="3" x2="3.01" y1="6" y2="6"/>'
                   '<line x1="3" x2="3.01" y1="12" y2="12"/>'
                   '<line x1="3" x2="3.01" y1="18" y2="18"/>',
    "mail":        '<rect x="2" y="4" width="20" height="16" rx="2"/>'
                   '<path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
    "maximize":    '<path d="M8 3H5a2 2 0 0 0-2 2v3"/>'
                   '<path d="M21 8V5a2 2 0 0 0-2-2h-3"/>'
                   '<path d="M3 16v3a2 2 0 0 0 2 2h3"/>'
                   '<path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
    "message":     '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
    "message-sq":  '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "mic":         '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>'
                   '<path d="M19 10v2a7 7 0 0 1-14 0v-2"/>'
                   '<line x1="12" x2="12" y1="19" y2="22"/>',
    "mic-off":     '<line x1="2" x2="22" y1="2" y2="22"/>'
                   '<path d="M18.89 13.23A7.12 7.12 0 0 0 19 12v-2"/>'
                   '<path d="M5 10v2a7 7 0 0 0 12 5"/>'
                   '<path d="M15 9.34V5a3 3 0 0 0-5.68-1.33"/>'
                   '<path d="M9 9v3a3 3 0 0 0 5.12 2.12"/>'
                   '<line x1="12" x2="12" y1="19" y2="22"/>',
    "monitor":     '<rect x="2" y="3" width="20" height="14" rx="2"/>'
                   '<line x1="8" x2="16" y1="21" y2="21"/>'
                   '<line x1="12" x2="12" y1="17" y2="21"/>',
    "music":       '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/>'
                   '<circle cx="18" cy="16" r="3"/>',
    "newspaper":   '<path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"/>'
                   '<path d="M18 14h-8"/><path d="M15 18h-5"/>'
                   '<path d="M10 6h8v4h-8V6Z"/>',
    "pen":         '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    "pie":         '<path d="M21.21 15.89A10 10 0 1 1 8 2.83"/>'
                   '<path d="M22 12A10 10 0 0 0 12 2v10z"/>',
    "plane":       '<path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z"/>',
    "presentation": '<path d="M2 3h20"/><path d="M21 3v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V3"/>'
                    '<path d="m7 21 5-5 5 5"/>',
    "rss":         '<path d="M4 11a9 9 0 0 1 9 9"/><path d="M4 4a16 16 0 0 1 16 16"/>'
                   '<circle cx="5" cy="19" r="1"/>',
    "search":      '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "send":        '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    "server":      '<rect x="2" y="2" width="20" height="8" rx="2"/>'
                   '<rect x="2" y="14" width="20" height="8" rx="2"/>'
                   '<line x1="6" x2="6.01" y1="6" y2="6"/>'
                   '<line x1="6" x2="6.01" y1="18" y2="18"/>',
    "settings":    '<circle cx="12" cy="12" r="3"/>'
                   '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "smartphone":  '<rect x="5" y="2" width="14" height="20" rx="2"/>'
                   '<line x1="12" x2="12.01" y1="18" y2="18"/>',
    "sparkles":    '<path d="M12 3l1.9 5.8a2 2 0 0 0 1.3 1.3L21 12l-5.8 1.9a2 2 0 0 0-1.3 1.3L12 21l-1.9-5.8a2 2 0 0 0-1.3-1.3L3 12l5.8-1.9a2 2 0 0 0 1.3-1.3Z"/>',
    "stop":        '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "sunrise":     '<path d="M12 2v8"/><path d="m4.93 10.93 1.41 1.41"/>'
                   '<path d="M2 18h2"/><path d="M20 18h2"/>'
                   '<path d="m19.07 10.93-1.41 1.41"/>'
                   '<path d="M22 22H2"/><path d="m8 6 4-4 4 4"/>'
                   '<path d="M16 18a4 4 0 0 0-8 0"/>',
    "table":       '<rect x="3" y="3" width="18" height="18" rx="2"/>'
                   '<path d="M3 9h18"/><path d="M3 15h18"/><path d="M12 3v18"/>',
    "terminal":    '<polyline points="4 17 10 11 4 5"/>'
                   '<line x1="12" x2="20" y1="19" y2="19"/>',
    "thermometer": '<path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/>',
    "timer":       '<line x1="10" x2="14" y1="2" y2="2"/>'
                   '<line x1="12" x2="15" y1="14" y2="11"/>'
                   '<circle cx="12" cy="14" r="8"/>',
    "upload":      '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
                   '<polyline points="17 8 12 3 7 8"/>'
                   '<line x1="12" x2="12" y1="3" y2="15"/>',
    "users":       '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>'
                   '<circle cx="9" cy="7" r="4"/>'
                   '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/>'
                   '<path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "video":       '<polygon points="23 7 16 12 23 17 23 7"/>'
                   '<rect x="1" y="5" width="15" height="14" rx="2"/>',
    "waveform":    '<path d="M2 10v3"/><path d="M6 7v7"/><path d="M10 4v13"/>'
                   '<path d="M14 6v9"/><path d="M18 8v6"/><path d="M22 10v3"/>',
    "wallet":      '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/>'
                   '<path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
    "wifi":        '<path d="M5 13a10 10 0 0 1 14 0"/><path d="M8.5 16.5a5 5 0 0 1 7 0"/>'
                   '<path d="M2 8.82a15 15 0 0 1 20 0"/>'
                   '<line x1="12" x2="12.01" y1="20" y2="20"/>',
    "x":           '<line x1="18" x2="6" y1="6" y2="18"/>'
                   '<line x1="6" x2="18" y1="6" y2="18"/>',
    "youtube":     '<path d="M2.5 17a24.12 24.12 0 0 1 0-10 2 2 0 0 1 1.4-1.4 49.56 49.56 0 0 1 16.2 0A2 2 0 0 1 21.5 7a24.12 24.12 0 0 1 0 10 2 2 0 0 1-1.4 1.4 49.55 49.55 0 0 1-16.2 0A2 2 0 0 1 2.5 17"/>'
                   '<path d="m10 15 5-3-5-3z"/>',
    "zap":         '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
    "circle":      '<circle cx="12" cy="12" r="8"/>',
    "book":        '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/>',
    "cake":        '<path d="M20 21v-8a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8"/>'
                   '<path d="M4 16s.5-1 2-1 2.5 2 4 2 2.5-2 4-2 2.5 2 4 2 2-1 2-1"/>'
                   '<path d="M2 21h20"/><path d="M7 8v3"/><path d="M12 8v3"/>'
                   '<path d="M17 8v3"/><path d="M7 4h.01"/><path d="M12 4h.01"/>'
                   '<path d="M17 4h.01"/>',
    "eye-off":     '<path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/>'
                   '<path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/>'
                   '<path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/>'
                   '<line x1="2" x2="22" y1="2" y2="22"/>',
    "heart":       '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>',
    "key":         '<path d="m21 2-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
    "map-pin":     '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
    "shield":      '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
    "user":        '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "fan":         '<path d="M10.827 16.379a6.082 6.082 0 0 1-8.618-7.002l5.412 1.45a6.082 6.082 0 0 1 7.002-8.618l-1.45 5.412a6.082 6.082 0 0 1 8.618 7.002l-5.412-1.45a6.082 6.082 0 0 1-7.002 8.618l1.45-5.412Z"/><path d="M12 12v.01"/>',
    "plug":        '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
    "lightbulb":   '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>',
    "tv":          '<rect width="20" height="15" x="2" y="7" rx="2" ry="2"/><polyline points="17 2 12 7 7 2"/>',
    "snowflake":   '<line x1="2" x2="22" y1="12" y2="12"/><line x1="12" x2="12" y1="2" y2="22"/><path d="m20 16-4-4 4-4"/><path d="m4 8 4 4-4 4"/><path d="m16 4-4 4-4-4"/><path d="m8 20 4-4 4 4"/>',
    "radio":       '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.2 19.1 19.1"/>',
}

_ICON_PM: dict = {}


def icon_pm(name: str, color: str = C.TEXT, size: int = 16,
            weight: float = 1.8) -> QPixmap:
    """Return a cached, crisp (2×) outline-icon pixmap."""
    key = (name, color, size, round(weight, 2))
    pm = _ICON_PM.get(key)
    if pm is not None:
        return pm
    body = _ICON_SVG.get(name)
    if not body:
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
    else:
        scale = 2
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
               f'fill="none" stroke="{color}" stroke-width="{weight}" '
               f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
        pm = QPixmap(int(size * scale), int(size * scale))
        pm.fill(Qt.GlobalColor.transparent)
        r = QSvgRenderer()
        r.load(svg.encode("utf-8"))
        if r.isValid():
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r.render(p, QRectF(0, 0, size * scale, size * scale))
            p.end()
        pm.setDevicePixelRatio(scale)
    _ICON_PM[key] = pm
    return pm


# ─────────────────────────────────────────────────────────────────────────────
#  Glass / shadow helpers
# ─────────────────────────────────────────────────────────────────────────────

def _add_glow(widget, color: str = C.BLUE, radius: int = 20):
    glow = QGraphicsDropShadowEffect()
    glow.setBlurRadius(radius)
    glow.setColor(QColor(color))
    glow.setOffset(0, 0)
    widget.setGraphicsEffect(glow)
    return widget


def _add_glass_shadow(widget, blur: int = 26, alpha: int = 120):
    """Ultra-soft floating shadow — panels appear to hover above the desk."""
    glow = QGraphicsDropShadowEffect()
    glow.setBlurRadius(blur)
    c = QColor(0, 0, 0)
    c.setAlpha(alpha)
    glow.setColor(c)
    glow.setOffset(0, 6)
    widget.setGraphicsEffect(glow)
    return widget


def _paint_glass(p: QPainter, rect: QRectF, radius: float,
                 fill: str = C.GLASS, fill_a: int = 170,
                 border_a: int = 24, border_col: str = "#FFFFFF"):
    """Draw a premium glass panel — dark fill, hairline border, top sheen."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(_brush(fill, fill_a))
    p.setPen(_pen(border_col, 1.0, border_a))
    p.drawRoundedRect(rect, radius, radius)
    # top edge highlight (soft reflection)
    hi = QRectF(rect.x() + 1, rect.y() + 1, rect.width() - 2, max(1.0, rect.height() * 0.35))
    g = _linear(hi.x(), hi.y(), hi.x(), hi.y() + hi.height(),
                [(0.0, QColor(255, 255, 255, 14)),
                 (1.0, QColor(255, 255, 255, 0))])
    p.setBrush(QBrush(g))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(hi, max(1.0, radius - 1), max(1.0, radius - 1))


# ─────────────────────────────────────────────────────────────────────────────
#  Ambient environment — matte black + blue (left) / green (right) + vignette
# ─────────────────────────────────────────────────────────────────────────────

class _AmbientGlow(QWidget):
    """Full-window ambient light. The four radial fills are rendered ONCE into
    a cached pixmap (debounced 60ms after resize) — painting is a single blit
    instead of four full-window gradient fills per frame, which made window
    dragging stutter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._bg_cache: QPixmap | None = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(60)
        self._debounce.timeout.connect(self._rebuild)

    def _rebuild(self):
        W, H = self.width(), self.height()
        if W <= 1 or H <= 1:
            return
        pm = QPixmap(W, H)
        pm.fill(QColor("#050505"))   # central background — ambient sits on top
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Blue ambient light from the left
        blue = _radial(0, H * 0.45, max(W * 0.62, 1), [
            (0.0, QColor(0, 175, 255, 18)),
            (0.45, QColor(0, 175, 255, 7)),
            (1.0, QColor(0, 175, 255, 0)),
        ])
        p.setBrush(QBrush(blue))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(self.rect())

        # Green ambient light from the right
        green = _radial(W, H * 0.45, max(W * 0.62, 1), [
            (0.0, QColor(0, 255, 136, 15)),
            (0.45, QColor(0, 255, 136, 6)),
            (1.0, QColor(0, 255, 136, 0)),
        ])
        p.setBrush(QBrush(green))
        p.drawRect(self.rect())

        # Extremely subtle central lift — soft depth behind the AI core
        core = _radial(W * 0.5, H * 0.42, min(W, H) * 0.5, [
            (0.0, QColor(255, 255, 255, 7)),
            (1.0, QColor(255, 255, 255, 0)),
        ])
        p.setBrush(QBrush(core))
        p.drawRect(self.rect())

        # Very subtle vignette — darker, quieter edges
        vg = _radial(W * 0.5, H * 0.5, max(W, H) * 0.72, [
            (0.55, QColor(0, 0, 0, 0)),
            (1.0, QColor(0, 0, 0, 110)),
        ])
        p.setBrush(QBrush(vg))
        p.drawRect(self.rect())
        p.end()

        self._bg_cache = pm
        self.update()

    def resizeEvent(self, e):
        # keep the old cache — paintEvent stretches it while dragging (soft
        # gradients make this invisible) and _rebuild fires 60ms after the
        # drag settles with the exact-size render
        self._debounce.start()
        super().resizeEvent(e)

    def paintEvent(self, _):
        c = self._bg_cache
        if c is None:
            self._rebuild()
            c = self._bg_cache
        p = QPainter(self)
        if c is not None and c.size() == self.size():
            p.drawPixmap(0, 0, c)
        elif c is not None:
            # mid-drag with a stale cache — stretch the soft glow (invisible)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(self.rect(), c)
        p.end()


# ─────────────────────────────────────────────────────────────────────────────
#  System metrics collector
# ─────────────────────────────────────────────────────────────────────────────

class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0
        self.gpu  = -1.0
        self.tmp  = -1.0
        self.batt = -1.0
        self.stor = 0.0
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(1.5)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        gpu = self._get_gpu()
        tmp = self._get_temp()
        batt = self._get_battery()
        try:
            stor = psutil.disk_usage("/").percent
        except Exception:
            stor = 0.0

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp
            self.batt = batt
            self.stor = stor

    def _get_battery(self) -> float:
        try:
            b = psutil.sensors_battery()
            if b:
                return float(b.percent)
        except Exception:
            pass
        return -1.0

    def _get_gpu(self) -> float:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2
            )
            if r.returncode == 0:
                vals = [float(v.strip()) for v in r.stdout.strip().split("\n") if v.strip()]
                if vals:
                    return sum(vals) / len(vals)
        except Exception:
            pass

        if _OS == "Linux":
            try:
                r = subprocess.run(
                    ["rocm-smi", "--showuse", "--csv"],
                    capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0:
                    for line in r.stdout.strip().split("\n"):
                        parts = line.split(",")
                        if len(parts) >= 2:
                            try:
                                return float(parts[1].strip().replace("%", ""))
                            except ValueError:
                                pass
            except Exception:
                pass
            try:
                r = subprocess.run(
                    ["intel_gpu_top", "-J", "-s", "500"],
                    capture_output=True, text=True, timeout=1
                )
                if r.returncode == 0 and "Render/3D" in r.stdout:
                    import re
                    m = re.search(r'"busy":\s*([\d.]+)', r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass

        if _OS == "Darwin":
            try:
                r = subprocess.run(
                    ["sudo", "-n", "powermetrics", "-n", "1", "-i", "500",
                     "--samplers", "gpu_power"],
                    capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0 and "GPU" in r.stdout:
                    import re
                    m = re.search(r'GPU\s+Active:\s+([\d.]+)%', r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass
        return -1.0

    def _get_temp(self) -> float:
        try:
            temps = psutil.sensors_temperatures()
            candidates = ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                          "cpu-thermal", "zenpower", "it8688"]
            for name in candidates:
                if name in temps:
                    entries = temps[name]
                    if entries:
                        return entries[0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass
        if _OS == "Darwin":
            try:
                r = subprocess.run(
                    ["osx-cpu-temp"], capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0:
                    import re
                    m = re.search(r"([\d.]+)", r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass
        if _OS == "Windows":
            try:
                r = subprocess.run(
                    ["powershell", "-Command",
                     "(Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace root/wmi).CurrentTemperature"],
                    capture_output=True, text=True, timeout=3
                )
                if r.returncode == 0 and r.stdout.strip():
                    raw = float(r.stdout.strip().split("\n")[0])
                    return (raw / 10.0) - 273.15
            except Exception:
                pass
        return -1.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
                "batt": self.batt,
                "stor": self.stor,
            }


_metrics = _SysMetrics()


# ─────────────────────────────────────────────────────────────────────────────
#  Particle AI Core — volumetric, organic, two-tone suspended particle sphere
# ─────────────────────────────────────────────────────────────────────────────

class HudCanvas(QWidget):
    _NUM_PARTICLES = 1200
    _SPEC_N = 44

    def __init__(self, face_path: str, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"

        self._tick       = 0
        self._blink      = True
        self._blink_tick = 0
        self._rot_x      = 0.0
        self._rot_y      = 0.0

        self._particles = self._init_particles(HudCanvas._NUM_PARTICLES)
        self._glow_cache: dict[int, QPixmap] = {}
        self._dot_cache: dict[tuple, QPixmap] = {}
        self._ambient_cache: dict[int, QPixmap] = {}
        self._ambient_size = (0, 0)
        self._energy = 0.0
        self._target_energy = 0.0
        self._burst_particles: list[dict] = []
        self._spec_heights: list[float] = [0.0] * HudCanvas._SPEC_N

        self._idle_mode = True
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(50)

    # ── Particle generation ─────────────────────────────────────────────────

    def _init_particles(self, count: int) -> list[dict]:
        particles = []
        for _ in range(count):
            theta = random.uniform(0, 2 * math.pi)
            phi = math.acos(2 * random.uniform(0, 1) - 1)
            # three radial bands: a soft inner core (denser centre), a clustered
            # mid shell, and the outer organic shell — keeps the volume alive
            roll = random.random()
            if roll < 0.14:
                r_base = random.uniform(0.52, 0.78)
            elif roll < 0.49:
                r_base = 0.92 + random.uniform(-0.06, 0.08)
            else:
                r_base = 1.0
            wf1 = random.uniform(1.5, 4.0)
            wf2 = random.uniform(2.0, 5.0)
            wa  = random.uniform(0.02, 0.06)
            da  = random.uniform(0.0, 0.15)
            df  = random.uniform(0.3, 1.0)
            dp  = random.uniform(0, 2 * math.pi)
            dt  = random.uniform(0, 2 * math.pi)
            dphi = math.acos(2 * random.uniform(0, 1) - 1)
            particles.append({
                "theta": theta,
                "phi": phi,
                "r_factor": r_base + random.uniform(-0.06, 0.06),
                "size": (
                    random.uniform(4.0, 5.5) if random.random() < 0.13
                    else random.uniform(2.8, 3.8) if random.random() < 0.25
                    else random.uniform(1.6, 2.4)
                ),
                "color_blend": random.uniform(0, 1),
                "br": random.uniform(0.50, 1.0),
                "use_glow": random.random() < 0.16,
                "phase": random.uniform(0, 2 * math.pi),
                "wf1": wf1,
                "wf2": wf2,
                "wa": wa,
                "da": da,
                "df": df,
                "dp": dp,
                "dt": dt,
                "dphi": dphi,
                # static trig precomputed once — saves ~8 trig calls per
                # particle per frame in the paint hot path
                "ct": math.cos(theta), "st": math.sin(theta),
                "cp": math.cos(phi), "sp": math.sin(phi),
                "cdt": math.cos(dt), "sdt": math.sin(dt),
                "cdp": math.cos(dphi), "sdp": math.sin(dphi),
                "tw1": theta * wf1, "tw2": phi * wf2,
                "tp": (theta + phi) * 2.5,
            })
        return particles

    def _rotate(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        cos_y, sin_y = math.cos(self._rot_y), math.sin(self._rot_y)
        x, z = x * cos_y + z * sin_y, -x * sin_y + z * cos_y
        cos_x, sin_x = math.cos(self._rot_x), math.sin(self._rot_x)
        y, z = y * cos_x - z * sin_x, y * sin_x + z * cos_x
        return x, y, z

    def _get_glow_sprite(self, size: int) -> QPixmap:
        key = min(max(size, 2), 40)
        if key not in self._glow_cache:
            d = key * 2 + 4
            pm = QPixmap(d, d)
            pm.fill(Qt.GlobalColor.transparent)
            qp = QPainter(pm)
            qp.setRenderHint(QPainter.RenderHint.Antialiasing)
            g = QRadialGradient(key + 2, key + 2, key)
            g.setColorAt(0.0, QColor(255, 255, 255, 255))
            g.setColorAt(0.2, QColor(255, 255, 255, 120))
            g.setColorAt(0.6, QColor(255, 255, 255, 30))
            g.setColorAt(1.0, QColor(255, 255, 255, 0))
            qp.setBrush(QBrush(g))
            qp.setPen(Qt.PenStyle.NoPen)
            qp.drawEllipse(QPointF(key + 2, key + 2), key, key)
            qp.end()
            self._glow_cache[key] = pm
        return self._glow_cache[key]

    def _get_tinted_glow(self, size: int, cb: int, eb: int) -> QPixmap:
        """Tinted halo sprite (halo + core merged) for shell glow particles —
        keyed by (size, blend, energy) so the per-frame path is one blit."""
        key = (min(max(size, 2), 40), cb, eb)
        pm = self._glow_cache.get(key)
        if pm is None:
            d = key[0] * 2 + 4
            pm = QPixmap(d, d)
            pm.fill(Qt.GlobalColor.transparent)
            q = QPainter(pm)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            blend = cb / 7.0
            g_ = int((1.0 - blend * 0.314) * 255)
            b_ = int((0.533 + blend * 0.467) * 255)
            r_ = min(255, 40 + int((eb + 0.5) / 3.0 * 60))
            c = QColor(r_, g_, b_)
            hi = QColor(min(255, r_ + 70), min(255, g_ + 70), min(255, b_ + 70))
            g = QRadialGradient(d / 2, d / 2, key[0])
            g.setColorAt(0.0, QColor(hi.red(), hi.green(), hi.blue(), 255))
            g.setColorAt(0.2, QColor(c.red(), c.green(), c.blue(), 190))
            g.setColorAt(0.6, QColor(c.red(), c.green(), c.blue(), 60))
            g.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
            q.setBrush(QBrush(g))
            q.setPen(Qt.PenStyle.NoPen)
            q.drawEllipse(QPointF(d / 2, d / 2), key[0], key[0])
            q.end()
            self._glow_cache[key] = pm
        return pm

    def _render_ambient(self, W: int, H: int, cx: float, cy: float,
                        fw: float, eq: float) -> QPixmap:
        """Pre-render the volumetric glow layer into a half-resolution pixmap
        once per (size, energy-bucket). The layer is pure soft gradient — the
        half-res render is visually identical and costs 1/4 the fill work."""
        sw, sh = max(1, W // 2), max(1, H // 2)
        pm = QPixmap(sw, sh)
        pm.fill(Qt.GlobalColor.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        q.scale(sw / W, sh / H)   # draw in widget coords, rasterised at 0.5×

        glow_r = fw * 0.42
        gi = 12 + int(eq * 16)
        q.setBrush(QBrush(_radial(cx, cy, glow_r, [
            (0.0,  QColor(120, 220, 255, gi)),
            (0.35, QColor(0, 175, 255, max(0, gi - 5))),
            (0.75, QColor(0, 255, 136, max(0, gi - 12))),
            (1.0,  QColor(0, 255, 136, 0)),
        ])))
        q.setPen(Qt.PenStyle.NoPen)
        q.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

        side_r = fw * 0.5
        q.setBrush(QBrush(_radial(cx - fw * 0.24, cy + fw * 0.10, side_r, [
            (0.0, QColor(0, 175, 255, 8 + int(eq * 6))),
            (1.0, QColor(0, 175, 255, 0)),
        ])))
        q.drawEllipse(QPointF(cx, cy), side_r, side_r)
        q.setBrush(QBrush(_radial(cx + fw * 0.24, cy + fw * 0.10, side_r, [
            (0.0, QColor(0, 255, 136, 7 + int(eq * 6))),
            (1.0, QColor(0, 255, 136, 0)),
        ])))
        q.drawEllipse(QPointF(cx, cy), side_r, side_r)
        q.end()
        return pm

    def _make_dot_sprite(self, sb: int, cb: int, eb: int) -> QPixmap:
        """Pre-render a soft round particle sprite, colour-bucketed by
        (size, blend, energy) — replaces the per-particle AA ellipse."""
        d = sb * 2 + 4
        pm = QPixmap(d, d)
        pm.fill(Qt.GlobalColor.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        blend = cb / 7.0
        g_ = int((1.0 - blend * 0.314) * 255)
        b_ = int((0.533 + blend * 0.467) * 255)
        r_ = min(255, 40 + int((eb + 0.5) / 3.0 * 60))
        c = QColor(r_, g_, b_)
        hi = QColor(min(255, r_ + 70), min(255, g_ + 70), min(255, b_ + 70))
        grad = QRadialGradient(d / 2, d / 2, sb + 1)
        grad.setColorAt(0.0, QColor(hi.red(), hi.green(), hi.blue(), 255))
        grad.setColorAt(0.45, QColor(c.red(), c.green(), c.blue(), 235))
        grad.setColorAt(0.75, QColor(c.red(), c.green(), c.blue(), 110))
        grad.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
        q.setBrush(QBrush(grad))
        q.setPen(Qt.PenStyle.NoPen)
        q.drawEllipse(QPointF(d / 2, d / 2), sb, sb)
        q.end()
        return pm

    def _step(self):
        self._tick += 1

        # Organic energy envelope — exponential ease gives a snappy attack when
        # speech starts and a gentle, natural decay when it trails off.
        target = 1.0 if self.speaking else 0.0
        k = 9.0 if target >= self._energy else 5.0
        self._energy += (target - self._energy) * (1.0 - math.exp(-k * 0.033))
        if abs(target - self._energy) < 0.002:
            self._energy = target

        e = self._energy

        should_be_idle = e < 0.05
        if should_be_idle != self._idle_mode:
            self._idle_mode = should_be_idle
            # idle 20fps / speaking 30fps — the motion rates are slow enough
            # that higher frame rates buy nothing, only CPU burn
            self._tmr.setInterval(50 if should_be_idle else 33)

        speed = 0.7 + e * 2.0
        # steady spin + a slow organic wander so the shell never reads as rigid
        self._rot_y += 0.005 * speed + 0.0012 * math.sin(self._tick * 0.013)
        self._rot_x += 0.002 * speed + 0.0006 * math.cos(self._tick * 0.017)

        if e > 0.1 and random.random() < e * 0.12:
            theta = random.uniform(0, 2 * math.pi)
            phi = math.acos(2 * random.uniform(0, 1) - 1)
            speed_out = random.uniform(2.0, 5.0) * e
            self._burst_particles.append({
                "x": math.sin(phi) * math.cos(theta),
                "y": math.sin(phi) * math.sin(theta),
                "z": math.cos(phi),
                "vx": math.sin(phi) * math.cos(theta) * speed_out,
                "vy": math.sin(phi) * math.sin(theta) * speed_out,
                "vz": math.cos(phi) * speed_out,
                "life": 1.0,
                "size": random.uniform(2.0, 4.0),
                "color_blend": random.uniform(0, 1),
            })

        dt = 0.033
        decay = 0.03 + e * 0.04
        alive = []
        for bp in self._burst_particles:
            bp["life"] -= decay
            if bp["life"] <= 0:
                continue
            bp["x"] += bp["vx"] * dt
            bp["y"] += bp["vy"] * dt
            bp["z"] += bp["vz"] * dt
            alive.append(bp)
        self._burst_particles = alive

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0

        self.update()

    # ── Rendering ───────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)
        t = self._tick * 0.016
        e = self._energy

        # ── Volumetric ambient behind the sphere (two-tone + bright core) ──
        # Pre-rendered per (size, energy-bucket): these three radial fills are
        # the heaviest static content on screen — render once per bucket and
        # blit, instead of filling three large AA ellipses every frame.
        eb = int(e * 11.99)
        if self._ambient_size != (W, H):
            self._ambient_cache.clear()
            self._ambient_size = (W, H)
        amb = self._ambient_cache.get(eb)
        if amb is None:
            amb = self._render_ambient(W, H, cx, cy, fw, (eb + 0.5) / 12.0)
            self._ambient_cache[eb] = amb
        # no SmoothPixmapTransform: the layer is pure soft gradient, an exact
        # 2x integer stretch is visually identical and far cheaper
        p.drawPixmap(QRectF(0, 0, W, H), amb,
                     QRectF(0, 0, amb.width(), amb.height()))

        # ── Breathing ───────────────────────────────────────────────────────
        # idle breathing — a slow dominant breath plus a barely-there harmonic
        breath_idle = 1.0 + 0.016 * math.sin(t * 0.5) + 0.004 * math.sin(t * 0.13 + 2.0)
        pulse_idle  = 1.0 + 0.008 * math.sin(t * 1.0 + 1.0) + 0.003 * math.sin(t * 0.31)
        breath_speak = 1.0 + 0.04 * math.sin(t * 0.8)
        pulse_speak  = 1.0 + 0.025 * math.sin(t * 1.4 + 0.5)
        if self.muted:
            breath_idle = 1.0 + 0.005 * math.sin(t * 0.3)
            pulse_idle  = 1.0 + 0.003 * math.sin(t * 0.5)
            breath_speak = breath_idle
            pulse_speak  = pulse_idle
        breath = breath_idle + (breath_speak - breath_idle) * e
        pulse  = pulse_idle + (pulse_speak - pulse_idle) * e
        sphere_r = fw * 0.30 * breath * pulse

        # rotation trig hoisted once per frame — the old per-particle
        # _rotate() call re-computed 4 trig calls × 2000 particles/frame
        cy_, sy_ = math.cos(self._rot_y), math.sin(self._rot_y)
        cx_, sx_ = math.cos(self._rot_x), math.sin(self._rot_x)
        _sin = math.sin
        _cos = math.cos

        # ── Project particles ───────────────────────────────────────────────
        draw_list: list[tuple[float, float, float, float, float, float, bool]] = []
        # idle: half the field per frame — the drift is slow enough that the
        # shell reads identically at 20fps, at half the draw + sort cost
        stride = 2 if e < 0.05 else 1
        for i in range(0, len(self._particles), stride):
            pt = self._particles[i]
            wave_mul = 1.0 + e * 3.0
            wave = wave_mul * (pt["wa"] * _sin(pt["tw1"] + t * 0.6 + e * 2.0)
                    + pt["wa"] * 0.7 * _sin(pt["tw2"] + t * 0.8 + e * 1.5)
                    + pt["wa"] * 0.5 * _sin(pt["tp"] + t * 0.4))
            r = pt["r_factor"] + wave

            x = r * pt["sp"] * pt["ct"]
            y = r * pt["sp"] * pt["st"]
            z = r * pt["cp"]

            drift = pt["da"] * (1.0 + e * 4.0) * _sin(t * (pt["df"] + e * 1.5) + pt["dp"])
            if abs(drift) > 0.001:
                x += drift * pt["sdp"] * pt["cdt"]
                y += drift * pt["sdp"] * pt["sdt"]
                z += drift * pt["cdp"]

            # inline rotation
            xx = x * cy_ + z * sy_
            zz = -x * sy_ + z * cy_
            yy = y * cx_ - zz * sx_
            z  = y * sx_ + zz * cx_
            x, y = xx, yy

            depth_val = z + 2.5
            persp     = 2.5 / depth_val
            px = cx + x * sphere_r * persp
            py = cy - y * sphere_r * persp

            # organic sway — phase is tied to each particle's depth so the shell
            # undulates gently instead of rotating as one rigid body
            px += _sin(t * 0.18 + z) * fw * 0.010
            py += _cos(t * 0.22 + 1.7 + z) * fw * 0.007

            d3 = (z + 1.5) / 3.0
            if d3 < 0.3:
                depth_fac = 0.3
            elif d3 > 1.0:
                depth_fac = 1.0
            else:
                depth_fac = d3
            psize = pt["size"] * persp * depth_fac

            if psize < 0.4 or px < -20 or px > W + 20 or py < -20 or py > H + 20:
                continue
            draw_list.append((z, px, py, psize, depth_fac,
                              pt["color_blend"], pt["use_glow"], pt["br"]))

        draw_list.sort()   # z is the first tuple element — no key lambda

        glow_boost = 1.0 + e * 0.8
        eb_dot = int(e * 2.99)
        for _z, px, py, psize, depth_fac, color_blend, use_glow, br in draw_list:
            de = depth_fac
            # per-particle brightness × depth falloff → richer tonal variation
            bri = br * (0.72 + 0.28 * de)
            opacity = de * bri * glow_boost
            cb = int(color_blend * 7.99)

            # glow particles: one tinted halo sprite (halo + core merged) —
            # a single blit instead of two, and the colour carries the warmth
            if use_glow and psize >= 1.5:
                gs = self._get_tinted_glow(int(psize * (1.3 + e * 0.3)), cb, eb_dot)
                p.setOpacity(de * (0.85 + e * 0.15) * bri)
                p.drawPixmap(int(px - gs.width() * 0.5),
                             int(py - gs.height() * 0.5), gs)
            else:
                # cached soft-dot sprite instead of a per-particle AA ellipse —
                # a pixmap blit is ~20x cheaper than drawEllipse + antialiasing
                p.setOpacity(opacity)
                sb = psize * 1.4
                if sb < 1.0:
                    sb = 1
                elif sb > 8.0:
                    sb = 8
                else:
                    sb = int(sb)
                dkey = (sb, cb, eb_dot)
                spr = self._dot_cache.get(dkey)
                if spr is None:
                    spr = self._make_dot_sprite(sb, cb, eb_dot)
                    self._dot_cache[dkey] = spr
                p.drawPixmap(int(px - spr.width() / 2),
                             int(py - spr.height() / 2), spr)

        p.setOpacity(1.0)

        # ── Burst particles ─────────────────────────────────────────────────
        for bp in self._burst_particles:
            bx, by, bz = self._rotate(bp["x"], bp["y"], bp["z"])
            depth_val = bz + 2.5
            persp = 2.5 / depth_val
            bpx = cx + bx * sphere_r * 1.0 * persp
            bpy = cy - by * sphere_r * 1.0 * persp
            bsize = bp["size"] * persp
            bdepth = max(0.3, min(1.0, (bz + 1.5) / 3.0))
            balpha = int(180 * bp["life"] * bdepth * glow_boost)
            col_blend = bp["color_blend"]
            bg = int((1.0 - col_blend * 0.314) * 255 * bdepth)
            bb = int((0.533 + col_blend * 0.467) * 255 * bdepth)

            sprite = self._get_glow_sprite(int(bsize * 2.5))
            p.setOpacity(bp["life"] * 0.7)
            p.drawPixmap(int(bpx - sprite.width() / 2),
                         int(bpy - sprite.height() / 2), sprite)
            p.setOpacity(1.0)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(
                min(255, int(60 * bdepth)),
                min(255, bg + 80),
                min(255, bb + 80),
                balpha,
            ))
            core_sz_b = max(0.5, bsize * 0.5)
            p.drawEllipse(QPointF(bpx, bpy), core_sz_b, core_sz_b)

        # ── Status capsule ──────────────────────────────────────────────────
        self._paint_status(p, cx, cy, fw, e)

        # ── Audio spectrum ──────────────────────────────────────────────────
        self._paint_spectrum(p, cx, cy, fw)

    def _paint_status(self, p: QPainter, cx: float, cy: float, fw: float, e: float):
        if self.muted:
            txt, col = "MUTED",     C.ERROR
        elif self.speaking:
            txt, col = "SPEAKING",  C.GREEN
        elif self.state == "THINKING":
            txt, col = "THINKING",  C.BLUE
        elif self.state == "PROCESSING":
            txt, col = "PROCESSING", C.BLUE
        elif self.state == "LISTENING":
            txt, col = "LISTENING",  C.GREEN
        else:
            txt, col = self.state, C.BLUE

        sy = cy + fw * 0.42
        f = _font(8, QFont.Weight.DemiBold, ls=1.2)
        fm = QFontMetrics(f)
        pad_x = 34
        pill_w = fm.horizontalAdvance(txt) + pad_x
        pill_h = 26
        pill_x = (self.width() - pill_w) / 2
        pill_y = sy - pill_h / 2

        # soft glow behind capsule — tinted to the active state
        gcol = qcol(col)
        eq = int(e * 11.99) / 12.0
        gg = _radial(pill_x + pill_w / 2, pill_y + pill_h / 2, pill_w * 0.9, [
            (0.0, QColor(gcol.red(), gcol.green(), gcol.blue(), 26 + int(eq * 18))),
            (1.0, QColor(gcol.red(), gcol.green(), gcol.blue(), 0)),
        ])
        p.setBrush(QBrush(gg))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(pill_x - pill_w * 0.2, pill_y - pill_h * 0.4,
                             pill_w * 1.4, pill_h * 1.8))

        # capsule
        p.setBrush(_brush(C.GLASS, 185))
        p.setPen(_pen(col, 1.0, 120 if self.muted else 150))
        p.drawRoundedRect(QRectF(pill_x, pill_y, pill_w, pill_h), pill_h / 2, pill_h / 2)

        # state dot
        dot_r = 3.0
        pulse = 0.6 + 0.4 * math.sin(self._tick * 0.10)
        dot_col = qcol(col, int(120 + 120 * pulse))
        p.setBrush(dot_col)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(pill_x + 12, pill_y + pill_h / 2), dot_r, dot_r)

        # text
        p.setPen(_pen(col, 1))
        p.setFont(f)
        p.drawText(QRectF(pill_x + 20, pill_y, pill_w - 24, pill_h),
                   Qt.AlignmentFlag.AlignCenter, txt)

    def _paint_spectrum(self, p: QPainter, cx: float, cy: float, fw: float):
        wy = cy + fw * 0.42 + 24
        N, gap = HudCanvas._SPEC_N, 2
        bw = 4
        max_h = 14
        total = N * bw + (N - 1) * gap
        wx0 = (self.width() - total) / 2

        # ease each bar toward its target — smooth idle, snappy speech attack
        k = 14.0 if self.speaking else (8.0 if not self.muted else 4.0)
        a = 1.0 - math.exp(-k * 0.033)
        prev = self._spec_heights

        for i in range(N):
            left = i < N // 2
            if self.muted:
                tgt = 2.0
                cl = C.MUTED
            elif self.speaking:
                # smooth traveling wave (no random flicker) — reads as speech energy
                tgt = 3.0 + max_h * 0.5 + (
                    math.sin(self._tick * 0.11 + i * 0.6) * max_h * 0.32
                    + math.sin(self._tick * 0.19 + i * 1.7) * max_h * 0.22)
                base = C.BLUE if left else C.GREEN
                cl = base if tgt > max_h * 0.55 else C.TEXT_DIM
            else:
                tgt = 3.0 + 2.2 * math.sin(self._tick * 0.09 + i * 0.55)
                base = C.BLUE if left else C.GREEN
                cl = base if tgt > 4.0 else C.TEXT_DIM

            hgt = prev[i] + (tgt - prev[i]) * a
            prev[i] = hgt

            x = wx0 + i * (bw + gap)
            y = wy - hgt / 2
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_brush(cl, 200))
            p.drawRoundedRect(QRectF(x, y, bw, hgt), 2, 2)


# ─────────────────────────────────────────────────────────────────────────────
#  Glass icon button (rounded, outline SVG icon, soft hover glow)
# ─────────────────────────────────────────────────────────────────────────────

class _IconButton(QPushButton):
    def __init__(self, text: str, icon: str | None = None,
                 color: str = C.TEXT_MED, fixed_h: int = 30,
                 square: bool = False, parent=None):
        super().__init__(parent)
        self._txt = text
        self._icon = icon
        self._color = color
        self._hover = False
        self._hover_amt = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")
        if square:
            self.setFixedSize(fixed_h, fixed_h)
        else:
            self.setFixedHeight(fixed_h)
        self._isize = max(13, fixed_h - 13)
        # smooth hover fade — the timer only runs while a transition is in flight
        self._hover_tmr = QTimer(self)
        self._hover_tmr.setInterval(16)
        self._hover_tmr.timeout.connect(self._hover_tick)

    def _hover_tick(self):
        target = 1.0 if self._hover else 0.0
        d = target - self._hover_amt
        if abs(d) < 0.02:
            self._hover_amt = target
            self._hover_tmr.stop()
        else:
            self._hover_amt += d * 0.28
        self.update()

    # public API used by the mute toggle / dynamic labels
    def set_color(self, color: str):
        self._color = color
        self.update()

    def set_text(self, text: str):
        self._txt = text
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self._hover_tmr.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._hover_tmr.start()
        super().leaveEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        r = min(11, H / 2 - 1)

        down = self.isDown()
        ha = self._hover_amt
        if down or ha > 0.01:
            p.setBrush(_brush(C.WHITE, 30 if down else int(15 * ha)))
            p.setPen(_pen(self._color, 1.0, 38 if down else int(28 * ha)))
            p.drawRoundedRect(QRectF(0.5, 0.5, W - 1, H - 1), r, r)

        lift = 1 if (self._hover or down) else 0   # gentle hover lift (1px)
        draw_color = self._color
        if self._icon:
            size = self._isize
            pm = icon_pm(self._icon, draw_color, size)
            if self._txt:
                p.drawPixmap(int(11), int((H - size) / 2) - lift, pm)
                tx = 11 + size + 9
            else:
                p.drawPixmap(int((W - size) / 2), int((H - size) / 2) - lift, pm)
                tx = W
        else:
            tx = 12

        if self._txt and tx < W - 6:
            p.setPen(_pen(draw_color, 1))
            p.setFont(_font(9, QFont.Weight.DemiBold if self._hover else QFont.Weight.Normal))
            p.drawText(QRectF(tx, -lift, W - tx - 10, H),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       self._txt)


# ─────────────────────────────────────────────────────────────────────────────
#  System status dashboard — 3 × 2 premium metric cells
# ─────────────────────────────────────────────────────────────────────────────

class _StatCell(QWidget):
    def __init__(self, label: str, icon: str, color: str, parent=None):
        super().__init__(parent)
        self._label = label.upper()
        self._icon  = icon
        self._color = color
        self._value = "--"
        self._pct   = 0.0
        self.setMinimumHeight(38)

    def set(self, value: str, pct: float):
        self._value = value
        self._pct   = max(0.0, min(1.0, pct))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        # value
        p.setPen(_pen(C.TEXT, 1))
        p.setFont(_font(13, QFont.Weight.Bold))
        p.drawText(QRectF(0, 0, W, 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._value)

        # label
        p.setPen(_pen(C.MUTED, 1))
        p.setFont(_font(7, QFont.Weight.DemiBold))
        p.drawText(QRectF(0, 17, W, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._label)

        # icon
        pm = icon_pm(self._icon, self._color, 11)
        p.drawPixmap(W - 14, 2, pm)

        # thin progress
        bar_y = H - 3
        p.setBrush(_brush(C.WHITE, 12))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(0, bar_y, W, 1.8), 1, 1)
        if self._pct > 0:
            p.setBrush(_brush(self._color, 200))
            p.drawRoundedRect(QRectF(0, bar_y, W * self._pct, 1.8), 1, 1)


class SysStatusCard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(112)
        self.setMinimumWidth(360)

        lay = QGridLayout(self)
        lay.setContentsMargins(20, 14, 20, 12)
        lay.setHorizontalSpacing(26)
        lay.setVerticalSpacing(6)

        self._cells: dict[str, _StatCell] = {}
        specs = [
            ("cpu",  "CPU",     "cpu",   C.BLUE),
            ("gpu",  "GPU",     "chip",  C.BLUE),
            ("batt", "BATTERY", "battery", C.GREEN),
            ("stor", "STORAGE", "database", C.GREEN),
            ("proc", "PROCESS", "list",  C.BLUE),
            ("up",   "UPTIME",  "clock", C.GREEN),
        ]
        for i, (key, label, icon, color) in enumerate(specs):
            cell = _StatCell(label, icon, color)
            self._cells[key] = cell
            lay.addWidget(cell, i // 3, i % 3)

    def paintEvent(self, _):
        p = QPainter(self)
        _paint_glass(p, QRectF(0, 0, self.width(), self.height()), 16,
                     fill=C.PANEL, fill_a=120, border_a=20)

    def update_data(self, d: dict):
        self._cells["cpu"].set(f"{d['cpu']:.0f}%", d["cpu"] / 100)
        gpu = d["gpu"]
        if gpu >= 0:
            self._cells["gpu"].set(f"{gpu:.0f}%", gpu / 100)
        else:
            self._cells["gpu"].set("N/A", 0)
        batt = d["batt"]
        if batt >= 0:
            self._cells["batt"].set(f"{batt:.0f}%", batt / 100)
        else:
            self._cells["batt"].set("AC", 0.4)
        self._cells["stor"].set(f"{d['stor']:.0f}%", d["stor"] / 100)
        self._cells["proc"].set(d.get("proc", "--"), 0)
        self._cells["up"].set(d.get("up_text", "--"), d.get("up_pct", 0))


# ─────────────────────────────────────────────────────────────────────────────
#  Chat log — glass surface, refined bubbles, typewriter reveal
# ─────────────────────────────────────────────────────────────────────────────

class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(_font(10))
        # bound the document so long sessions never degrade into a slow QTextEdit
        self.document().setMaximumBlockCount(600)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(16, 18, 20, 0.42);
                color: {C.TEXT};
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 16px;
                padding: 8px 6px;
                selection-background-color: rgba(0, 24, 40, 0.5);
            }}
            QScrollBar:vertical {{
                background: transparent; width: 4px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(255, 255, 255, 0.12);
                border-radius: 2px; min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._msg_color = C.TEXT
        self._ts      = ""
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text = self._queue.pop(0)
        self._pos = 0
        tl = self._text.lower()
        if   tl.startswith("you:"):    self._tag = "you"
        elif tl.startswith("ira:"):    self._tag = "ai"
        elif tl.startswith("file:"):   self._tag = "file"
        elif "err" in tl:              self._tag = "err"
        else:                          self._tag = "sys"

        self._insert_bubble()
        self._tmr.start(14)

    def _insert_bubble(self):
        from datetime import datetime

        if self._tag == "you":
            bg_col    = QColor(255, 255, 255, 13)     # ≈ rgba(255,255,255,0.05)
            text_c    = "#EAF0F6"
            align     = Qt.AlignmentFlag.AlignRight
        elif self._tag == "ai":
            bg_col    = QColor(0, 175, 255, 18)       # ≈ rgba(0,175,255,0.07)
            text_c    = "#B9DCFF"
            align     = Qt.AlignmentFlag.AlignLeft
        else:
            c_map = {"file": "#00AFFF", "err": "#E05555", "sys": "#8A8F96"}
            cc = c_map.get(self._tag, "#8A8F96")
            clean = self._escape(self._text)
            html = (f'<div style="color:{cc};font-size:8px;padding:2px 10px;'
                    f'text-align:center;letter-spacing:0.4px;">{clean}</div>')
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertHtml(html + "<br>")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos = len(self._text)
            self._tmr.stop()
            QTimer.singleShot(10, self._next)
            return

        self._msg_color = text_c
        self._ts = datetime.now().strftime("%H:%M")

        # Bubble = one tinted glass block. The typewriter fills it and the
        # timestamp lands on a dim line beneath the message, in the same block,
        # so message ordering is always correct (message → time).
        cur = self.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        if not self.document().isEmpty():
            cur.insertBlock()
        bf = QTextBlockFormat()
        bf.setBackground(QBrush(bg_col))
        bf.setAlignment(align)
        bf.setTopMargin(7.0)
        bf.setBottomMargin(3.0)
        bf.setLeftMargin(8.0)
        bf.setRightMargin(8.0)
        cur.setBlockFormat(bf)
        # pin the message font — a fresh block otherwise inherits the previous
        # block's (dim, 7px) timestamp format
        cf = cur.charFormat()
        cf.setFont(QFont(self.font()))
        cf.setForeground(QBrush(qcol(text_c)))
        cur.setCharFormat(cf)
        self.setTextCursor(cur)
        self.ensureCursorVisible()

        self._type_pos = self.document().characterCount() - 1

    def _escape(self, text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _step(self):
        if self._pos < len(self._text):
            ch = self._text[self._pos]
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)

            fmt = cur.charFormat()
            fmt.setForeground(QBrush(qcol(self._msg_color)))
            fmt.setFont(QFont(self.font()))
            cur.insertText(ch, fmt)

            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)

            # small, dim timestamp inside the bubble, beneath the message
            f = QFont(self.font())
            f.setPixelSize(7)
            fmt = cur.charFormat()
            fmt.setForeground(QBrush(qcol("#9AA0A8", 175)))
            fmt.setFont(f)
            cur.insertText("\n  " + self._ts, fmt)

            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)


# ─────────────────────────────────────────────────────────────────────────────
#  File upload card
# ─────────────────────────────────────────────────────────────────────────────

_FILE_ICONS = {
    "image":   ("image",      "#00d4ff"),
    "video":   ("video",      "#ff6b5e"),
    "audio":   ("audio",      "#cc6bff"),
    "pdf":     ("file-text",  "#ff5a5a"),
    "word":    ("file-text",  "#5a8bff"),
    "excel":   ("table",      "#44bb66"),
    "code":    ("code",       "#e5c44a"),
    "archive": ("archive",    "#ff9a44"),
    "pptx":    ("presentation", "#ff7a3d"),
    "text":    ("file-text",  "#b0b4ba"),
    "data":    ("database",   "#66ccf0"),
    "unknown": ("file",       "#9aa0a6"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(92)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(80)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        # same visual dash speed at half the frame rate
        self._dash_offset = (self._dash_offset + 1.6) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for IRA", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        R    = 16
        pad  = 3
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        # glass fill
        if z._drag_over:
            fill_a, border_a = 30, 95
        elif z._hovering:
            fill_a, border_a = 150, 60
        else:
            fill_a, border_a = 120, 20
        p.setBrush(_brush(C.GLASS, fill_a))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, R, R)

        # dashed hairline border
        if z._current_file:   bc = C.GREEN
        elif z._drag_over:    bc = C.BLUE
        elif z._hovering:     bc = C.BLUE
        else:                 bc = C.WHITE
        pen = QPen(_pen(bc, 1.0, border_a))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, R, R)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2 - 4
        col = C.PRI if hover else C.MUTED
        pm = icon_pm("upload", col, 20)
        p.drawPixmap(int(cx - 10), int(cy - 24), pm)

        p.setFont(_font(9, QFont.Weight.DemiBold))
        p.setPen(_pen(C.TEXT if hover else C.SECONDARY, 1))
        p.drawText(QRectF(0, cy + 4, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Drop file or click to browse")
        p.setFont(_font(8))
        p.setPen(_pen(C.MUTED, 1))
        p.drawText(QRectF(0, cy + 22, W, 12), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        pm = icon_pm("upload", C.BLUE, 26)
        p.drawPixmap(int(cx - 13), int(cy - 40), pm)
        p.setFont(_font(12, QFont.Weight.Bold))
        p.setPen(_pen(C.BLUE, 1))
        p.drawText(QRectF(0, cy - 6, W, 18), Qt.AlignmentFlag.AlignCenter, "DROP")
        p.setFont(_font(9))
        p.drawText(QRectF(0, cy + 16, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon_name, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        try:
            size_str = _fmt_size(path.stat().st_size)
        except OSError:
            size_str = "--"          # file vanished after selection — never crash in paint
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        pm = icon_pm(icon_name, icon_col, 22)
        p.drawPixmap(int(16), int((H - 22) / 2), pm)

        tx = 50
        tw = W - tx - 40

        p.setFont(_font(10, QFont.Weight.DemiBold))
        p.setPen(_pen(C.TEXT, 1))
        name = path.name if len(path.name) <= 26 else path.name[:23] + "..."
        p.drawText(QRectF(tx, H * 0.16, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(_font(8))
        p.setPen(_pen(C.MUTED, 1))
        p.drawText(QRectF(tx, H * 0.16 + 17, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        # remove button
        xpm = icon_pm("x", C.MUTED, 12)
        p.drawPixmap(int(W - 26), int((H - 12) / 2), xpm)

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


# ─────────────────────────────────────────────────────────────────────────────
#  Header widgets — brand logo, online pill, floating stat cards
# ─────────────────────────────────────────────────────────────────────────────

class _BrandLogo(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(32, 32)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, 31, 31)
        g = _linear(0, 0, 31, 31,
                    [(0.0, qcol(C.BLUE, 60)), (1.0, qcol(C.GREEN, 36))])
        p.setBrush(QBrush(g))
        p.setPen(_pen(C.WHITE, 1.0, 26))
        p.drawRoundedRect(r, 9, 9)
        pm = icon_pm("sparkles", C.WHITE, 17)
        p.drawPixmap(int((32 - 17) / 2), int((32 - 17) / 2), pm)


class _StatusPill(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(20)
        # width depends only on the fixed "ONLINE" label — compute once here
        # instead of mutating geometry inside paintEvent
        fm = QFontMetrics(_font(8, QFont.Weight.DemiBold))
        self._tw = fm.horizontalAdvance("ONLINE")
        self.setFixedWidth(int(self._tw) + 24)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        tw = self._tw
        r = QRectF(0.5, 0.5, w - 1, 19)

        p.setBrush(_brush(C.GREEN, 12))
        p.setPen(_pen(C.GREEN, 1.0, 60))
        p.drawRoundedRect(r, 10, 10)

        dot_r = 2.6
        p.setBrush(_brush(C.GREEN, 230))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(10, 10), dot_r, dot_r)

        p.setPen(_pen(C.GREEN, 1))
        p.setFont(_font(8, QFont.Weight.DemiBold))
        p.drawText(QRectF(16, 0, tw, 20),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "ONLINE")


class _HeaderCard(QWidget):
    def __init__(self, icon: str, label: str, value: str, parent=None):
        super().__init__(parent)
        self._icon = icon
        self._label = label.upper()
        self._value = value
        self._hover = False
        self._hover_amt = 0.0
        self.setFixedHeight(54)   # identical card heights — optical centre in header
        self._hover_tmr = QTimer(self)
        self._hover_tmr.setInterval(16)
        self._hover_tmr.timeout.connect(self._hover_tick)

    def _hover_tick(self):
        target = 1.0 if self._hover else 0.0
        d = target - self._hover_amt
        if abs(d) < 0.02:
            self._hover_amt = target
            self._hover_tmr.stop()
        else:
            self._hover_amt += d * 0.28
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self._hover_tmr.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._hover_tmr.start()
        super().leaveEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        R = 16

        _paint_glass(p, QRectF(0.5, 0.5, W - 1, H - 1), R,
                     fill=C.GLASS, fill_a=130, border_a=24)
        if self._hover_amt > 0.01:
            ha = self._hover_amt
            p.setBrush(QBrush(qcol(C.BLUE, int(8 * ha))))
            p.setPen(_pen(C.BLUE, 1.0, int(30 * ha)))
            p.drawRoundedRect(QRectF(0.5, 0.5, W - 1, H - 1), R, R)

        pad = 12
        # icon
        pm = icon_pm(self._icon, C.BLUE, 15)
        p.drawPixmap(pad, 9, pm)

        # value (large)
        p.setPen(_pen(C.TEXT, 1))
        p.setFont(_font(12, QFont.Weight.Bold))
        p.drawText(QRectF(pad, 25, W - pad * 2, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._value)

        # label (small)
        p.setPen(_pen(C.MUTED, 1))
        p.setFont(_font(7, QFont.Weight.DemiBold))
        p.drawText(QRectF(pad, 40, W - pad * 2, 11),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._label)


# ─────────────────────────────────────────────────────────────────────────────
#  Setup overlay
# ─────────────────────────────────────────────────────────────────────────────

class SetupOverlay(QWidget):
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(10, 11, 13, 240);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 22px;
            }}
        """)
        _add_glass_shadow(self, blur=40, alpha=160)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(10)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(_font(font_size, QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("Configure I.R.A. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(_font(11))
        self._key_input.setFixedHeight(38)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(16, 18, 20, 0.8);
                color: {C.TEXT};
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 12px;
                padding: 4px 14px;
            }}
            QLineEdit:focus {{ border: 1px solid rgba(0, 175, 255, 0.45); }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","Windows"),("mac","macOS"),("linux","Linux")]:
            btn = QPushButton(label)
            btn.setFont(_font(9, QFont.Weight.Bold))
            btn.setFixedHeight(34)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("INITIALISE SYSTEMS")
        init_btn.setFont(_font(9, QFont.Weight.DemiBold))
        init_btn.setFixedHeight(38)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(0, 175, 255, 0.07);
                color: {C.PRI};
                border: 1px solid rgba(0, 175, 255, 0.16);
                border-radius: 12px;
                letter-spacing: 0.6px;
            }}
            QPushButton:hover {{
                background: rgba(0, 175, 255, 0.11);
                border: 1px solid rgba(0, 175, 255, 0.30);
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"rgba(0,175,255,0.15)"),
               "mac":(C.SEC,"rgba(0,255,136,0.15)"),
               "linux":(C.SEC,"rgba(0,255,136,0.15)")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {bg}; color: {fg};
                        border: 1px solid {fg}; border-radius: 9px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(16, 18, 20, 0.5);
                        color: {C.TEXT_DIM};
                        border: 1px solid rgba(255, 255, 255, 0.06);
                        border-radius: 9px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT_MED}; border: 1px solid rgba(0, 175, 255, 0.2); }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os)


# ─────────────────────────────────────────────────────────────────────────────
#  Remote-key overlay
# ─────────────────────────────────────────────────────────────────────────────

class RemoteKeyOverlay(QWidget):
    closed = pyqtSignal()

    _OW, _OH = 400, 468

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(10, 11, 13, 240);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 22px;
            }}
        """)
        _add_glass_shadow(self, blur=40, alpha=160)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 18, 26, 18)
        lay.setSpacing(6)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(_font(fs, QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(178, 178)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 12px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Courier New", 8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(_font(28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.PRI};
            background: rgba(16, 18, 20, 0.5);
            border: 1px solid rgba(0, 175, 255, 0.14);
            border-radius: 14px;
            padding: 6px 4px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(_font(8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(30)
        new_btn.setFont(_font(7, QFont.Weight.DemiBold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(16, 18, 20, 0.5);
                color: {C.PRI};
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 9px;
                letter-spacing: 0.6px;
            }}
            QPushButton:hover {{
                background: rgba(0, 175, 255, 0.07);
                border: 1px solid rgba(0, 175, 255, 0.18);
            }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(30)
        close_btn.setFont(_font(7, QFont.Weight.DemiBold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid rgba(255, 255, 255, 0.06); border-radius: 9px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid rgba(0, 175, 255, 0.14); }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(172, 172,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 12px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 12px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(0, 255, 136, 0.08);
            border: 1px solid rgba(0, 255, 136, 0.40);
            border-radius: 9px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            f"color: {C.GREEN}; background: #00140A; border-radius: 12px;"
        )
        self._timer_lbl.setText("Phone connected — IRA ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self._manual_url     = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                self._update_qr(auto or url)
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: rgba(16, 18, 20, 0.5);
                    border: 1px solid rgba(0, 175, 255, 0.16);
                    border-radius: 14px;
                    padding: 6px 4px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


# ─────────────────────────────────────────────────────────────────────────────
#  Settings overlay — premium glass settings dialog
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
#  Smart-home overlay (Brahma Echo port — premium device-control page)
# ─────────────────────────────────────────────────────────────────────────────

class SmartHomeOverlay(QWidget):
    closed = pyqtSignal()

    _OW, _OH = 980, 640

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SmartHomeOverlay {{
                background: rgba(10, 11, 13, 242);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 22px;
            }}
        """)
        _add_glass_shadow(self, blur=40, alpha=160)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(0)
        from smart_home_page import SmartHomePage
        self._page = SmartHomePage()
        lay.addWidget(self._page, 1)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._do_close()
        else:
            super().keyPressEvent(event)

    def _do_close(self):
        self.closed.emit()
        self.close()


class HandControlOverlay(QWidget):
    """Full-window IRA Hand Control — embeds the barehands air-board stage.

    The stage (hand-tracked 3D notes/3D-models board) is served by the external
    barehands server; IRA brands it (ring = IRA) and points its orbs at the
    active user's per-user folder. We render it inside a QWebEngineView so it
    is a true in-app panel (Approach A). barehands code is NOT copied into IRA.
    """

    closed = pyqtSignal()

    def __init__(self, user: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            HandControlOverlay {{
                background: rgba(8, 9, 11, 246);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 22px;
            }}
        """)
        _add_glass_shadow(self, blur=44, alpha=170)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        # ── Header (IRA brand, status, close) ──
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        chip = QLabel()
        chip.setPixmap(icon_pm("hand-control", C.PRI, 18))
        hdr.addWidget(chip)
        t = QLabel("HAND CONTROL")
        t.setFont(_font(12, QFont.Weight.Bold))
        t.setStyleSheet(f"color: {C.TEXT}; background: transparent; letter-spacing: 1.5px;")
        hdr.addWidget(t)
        s = QLabel(f"Air-board for {user} — allow camera, then use your hands")
        s.setFont(_font(9))
        s.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        hdr.addWidget(s)
        hdr.addStretch()
        self._status = QLabel("connecting…")
        self._status.setFont(_font(8, QFont.Weight.DemiBold))
        self._status.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        hdr.addWidget(self._status)
        x_btn = _IconButton("", "x", color=C.TEXT_MED, fixed_h=26, square=True)
        x_btn.clicked.connect(self._do_close)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        # ── Web view (the barehands stage) ──
        self._view = QWebEngineView()
        self._view.setStyleSheet("border-radius: 16px; background: #050505;")
        self._view.page().loadFinished.connect(self._on_load)
        lay.addWidget(self._view, stretch=1)

        self._user = user
        self._bridge = HandControlBridge()
        self._load_stage()

    def _load_stage(self):
        from PyQt6.QtCore import QUrl
        if not self._bridge.write_config(self._user):
            self._status.setText("barehands not found at D:/barehands")
            self._status.setStyleSheet(f"color: {C.ERROR}; background: transparent;")
            return
        self._bridge.start_server()
        self._bridge.set_state("idle")
        self._view.setUrl(QUrl(self._bridge.server_url("stage")))

    def _on_load(self, ok: bool):
        if ok:
            self._status.setText("live — camera ready")
            self._status.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
            self._bridge.set_state("listening")
        else:
            self._status.setText("stage failed to load")
            self._status.setStyleSheet(f"color: {C.ERROR}; background: transparent;")

    def set_ring_state(self, state: str):
        """Let IRA drive the ring (its face on the board)."""
        self._bridge.set_state(state)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._do_close()
        else:
            super().keyPressEvent(event)

    def _do_close(self):
        self._bridge.set_state("idle")
        self.closed.emit()
        self.close()


class _NavButton(QPushButton):
    """Checkable nav item for the Settings rail — custom-painted icon + label."""

    def __init__(self, icon: str, text: str, parent=None):
        super().__init__(text, parent)
        self._icon = icon
        self._active = False
        self._hover = False
        self._hover_amt = 0.0
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedHeight(38)
        self._tmr = QTimer(self)
        self._tmr.setInterval(16)
        self._tmr.timeout.connect(self._tick)

    def set_active(self, v: bool):
        self._active = bool(v)
        self.setChecked(v)
        self.update()

    def _tick(self):
        target = 1.0 if self._hover else 0.0
        d = target - self._hover_amt
        if abs(d) < 0.02:
            self._hover_amt = target
            self._tmr.stop()
        else:
            self._hover_amt += d * 0.28
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self._tmr.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._tmr.start()
        super().leaveEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        ha = self._hover_amt

        if self._active:
            p.setBrush(_brush(C.PRI, 26))
            p.setPen(_pen(C.PRI, 1.0, 80))
            p.drawRoundedRect(QRectF(0.5, 0.5, W - 1, H - 1), 11, 11)
        elif ha > 0.01:
            p.setBrush(_brush(C.WHITE, int(9 * ha)))
            p.setPen(_pen(C.WHITE, 1.0, int(20 * ha)))
            p.drawRoundedRect(QRectF(0.5, 0.5, W - 1, H - 1), 11, 11)

        col = C.PRI if self._active else (C.TEXT_MED if ha > 0.01 else C.TEXT_DIM)
        pm = icon_pm(self._icon, col, 15)
        p.drawPixmap(14, int((H - 15) / 2), pm)

        p.setPen(_pen(col, 1))
        p.setFont(_font(9, QFont.Weight.DemiBold if self._active else QFont.Weight.Normal))
        p.drawText(QRectF(40, 0, W - 44, H),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.text())


class _SecretEdit(QLineEdit):
    """Password field with an inline reveal toggle (eye / eye-off)."""

    def __init__(self, value="", placeholder="", parent=None):
        super().__init__(value, parent)
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.setFont(_font(10))
        self.setFixedHeight(34)
        self.setPlaceholderText(placeholder)
        self.setStyleSheet(
            SettingsOverlay._INPUT_QSS + " QLineEdit { padding-right: 34px; }"
        )
        self._visible = False
        self._toggle = _IconButton("", "eye", color=C.TEXT_DIM, fixed_h=22, square=True)
        self._toggle.setParent(self)
        self._toggle.clicked.connect(self._reveal)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        s = 22
        self._toggle.setGeometry(self.width() - s - 8, (self.height() - s) // 2, s, s)

    def _reveal(self):
        self._visible = not self._visible
        self.setEchoMode(QLineEdit.EchoMode.Normal
                         if self._visible else QLineEdit.EchoMode.Password)
        self._toggle._icon = "eye-off" if self._visible else "eye"
        self._toggle.update()


class _Dim(QWidget):
    """Full-window scrim behind the settings dialog — click to dismiss."""
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: rgba(4, 5, 6, 172);")

    def mousePressEvent(self, _):
        self.closed.emit()


class ClipboardPanel(QWidget):
    """Floating panel shown when text is copied — one-click JARVIS actions."""

    action_requested = pyqtSignal(str)
    _W, _H = 330, 118

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 8px;
            }}
        """)
        self.setFixedSize(self._W, self._H)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 9)
        lay.setSpacing(5)

        hdr = QHBoxLayout(); hdr.setSpacing(6)
        icon_lbl = QLabel("CLIPBOARD DETECTED")
        icon_lbl.setFont(_font(7, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; letter-spacing: 1px;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = _IconButton("", "x", color=C.TEXT_DIM, fixed_h=18, square=True)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(_font(8))
        self._preview.setStyleSheet(
            f"color: {C.TEXT}; background: {C.PANEL2}; border: 1px solid {C.BORDER};"
            f" border-radius: 4px; padding: 5px 8px;"
        )
        self._preview.setFixedHeight(30)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout(); btn_row.setSpacing(5)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 3px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN",   "Explain this: {text}"),
            ("FIX",       "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(24)
            b.setFont(_font(7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace("\n", " ")
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)


class SettingsOverlay(QWidget):
    """Modal settings dialog: AI providers, user profile, rules, remote control."""
    closed = pyqtSignal()
    remoteRequested = pyqtSignal()
    autostart_check = None      # callable: () -> bool (set by MainWindow)
    autostart_requested = None  # callable: () -> bool

    _OW, _OH = 800, 566

    _INPUT_QSS = f"""
        QLineEdit {{
            background: rgba(16, 18, 20, 0.82);
            color: {C.TEXT};
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            padding: 0 12px;
            selection-background-color: rgba(0, 175, 255, 0.35);
        }}
        QLineEdit:focus {{ border: 1px solid rgba(0, 175, 255, 0.45); }}
    """

    _TEXT_QSS = f"""
        QTextEdit {{
            background: rgba(16, 18, 20, 0.82);
            color: {C.TEXT};
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            padding: 7px 11px;
            selection-background-color: rgba(0, 175, 255, 0.35);
        }}
        QTextEdit:focus {{ border: 1px solid rgba(0, 175, 255, 0.45); }}
    """

    _COMBO_QSS = f"""
        QComboBox {{
            background: rgba(16, 18, 20, 0.82);
            color: {C.TEXT};
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            padding: 0 12px;
        }}
        QComboBox:focus {{ border: 1px solid rgba(0, 175, 255, 0.45); }}
        QComboBox::drop-down {{ border: none; width: 28px; }}
        QComboBox::down-arrow {{
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid {C.TEXT_DIM};
            margin-right: 9px;
        }}
        QComboBox QAbstractItemView {{
            background: #13161B;
            color: {C.TEXT};
            border: 1px solid rgba(255, 255, 255, 0.10);
            border-radius: 10px;
            selection-background-color: rgba(0, 175, 255, 0.18);
            selection-color: {C.WHITE};
            outline: none;
        }}
    """

    _LANG_OPTIONS = [
        "English", "Hindi", "Telugu", "Tamil", "Malayalam", "Kannada",
        "Bengali", "Marathi", "Gujarati", "Punjabi", "Urdu", "Spanish",
        "French", "German", "Arabic", "Chinese",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SettingsOverlay {{
                background: rgba(11, 12, 15, 246);
                border: 1px solid rgba(255, 255, 255, 0.09);
                border-radius: 22px;
            }}
        """)
        _add_glass_shadow(self, blur=48, alpha=170)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addLayout(self._build_header())
        root.addLayout(self._build_body(), stretch=1)
        root.addLayout(self._build_footer())

        QShortcut(QKeySequence("Escape"), self, activated=self._do_close)

    # ── construction ─────────────────────────────────────────────────────

    def _build_header(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(24, 18, 18, 10)
        h.setSpacing(12)

        brand = QHBoxLayout(); brand.setSpacing(10)
        chip = QLabel()
        chip.setFixedSize(32, 32)
        chip.setPixmap(icon_pm("settings", C.PRI, 17))
        chip.setStyleSheet(
            "background: rgba(0, 175, 255, 0.10); border: 1px solid rgba(0, 175, 255, 0.22);"
            " border-radius: 10px;"
        )
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.addWidget(chip)

        col = QVBoxLayout(); col.setSpacing(0)
        t = QLabel("SETTINGS")
        t.setFont(_font(13, QFont.Weight.Bold))
        t.setStyleSheet(f"color: {C.TEXT}; background: transparent; letter-spacing: 1.2px;")
        col.addWidget(t)
        s = QLabel("Configure IRA — providers, profile & preferences")
        s.setFont(_font(8))
        s.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        col.addWidget(s)
        brand.addLayout(col)
        h.addLayout(brand)
        h.addStretch(1)

        close = _IconButton("", "x", color=C.TEXT_DIM, fixed_h=28, square=True)
        close.clicked.connect(self._do_close)
        h.addWidget(close)
        return h

    def _build_body(self) -> QHBoxLayout:
        body = QHBoxLayout()
        body.setContentsMargins(18, 2, 18, 4)
        body.setSpacing(0)

        # Nav rail
        nav = QVBoxLayout()
        nav.setContentsMargins(4, 6, 14, 6)
        nav.setSpacing(4)
        self._nav_buttons = []
        for icon, label in [("server", "Providers"),
                            ("user", "Profile"),
                            ("shield", "Rules"),
                            ("smartphone", "Remote")]:
            btn = _NavButton(icon, label)
            btn.clicked.connect(lambda _, b=btn: self._switch(b))
            nav.addWidget(btn)
            self._nav_buttons.append(btn)
        nav.addStretch(1)
        body.addLayout(nav)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"color: rgba(255, 255, 255, 0.06);")
        body.addWidget(sep)

        self._stack = QStackedWidget()
        self._stack.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._stack.setStyleSheet("QStackedWidget { background: transparent; }")
        self._build_provider_page()
        self._build_profile_page()
        self._build_rules_page()
        self._build_remote_page()
        body.addWidget(self._stack, stretch=1)

        self._nav_buttons[0].set_active(True)
        return body

    def _build_footer(self) -> QHBoxLayout:
        f = QHBoxLayout()
        f.setContentsMargins(24, 10, 24, 16)
        f.setSpacing(10)

        self._status = QLabel("")
        self._status.setFont(_font(8, QFont.Weight.DemiBold))
        self._status.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        f.addWidget(self._status)
        f.addStretch(1)

        discard = QPushButton("DISCARD")
        discard.setFixedHeight(32)
        discard.setFont(_font(7, QFont.Weight.DemiBold))
        discard.setCursor(Qt.CursorShape.PointingHandCursor)
        discard.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid rgba(255, 255, 255, 0.07); border-radius: 9px;
                letter-spacing: 0.6px; padding: 0 16px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid rgba(0, 175, 255, 0.16); }}
        """)
        discard.clicked.connect(self._do_close)
        f.addWidget(discard)

        save = QPushButton("SAVE CHANGES")
        save.setFixedHeight(32)
        save.setFont(_font(7, QFont.Weight.DemiBold))
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.setStyleSheet(f"""
            QPushButton {{
                background: rgba(0, 175, 255, 0.10);
                color: {C.PRI};
                border: 1px solid rgba(0, 175, 255, 0.22); border-radius: 9px;
                letter-spacing: 0.6px; padding: 0 18px;
            }}
            QPushButton:hover {{
                background: rgba(0, 175, 255, 0.16);
                border: 1px solid rgba(0, 175, 255, 0.40);
            }}
        """)
        save.clicked.connect(self._save)
        f.addWidget(save)
        return f

    # ── field helpers ─────────────────────────────────────────────────────

    def _make_field(self, icon: str, title: str, subtitle: str = ""):
        box = QWidget()
        box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)

        hdr = QHBoxLayout(); hdr.setSpacing(9)
        ic = QLabel()
        ic.setPixmap(icon_pm(icon, C.TEXT_DIM, 13))
        ic.setFixedSize(13, 13)
        hdr.addWidget(ic, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout(); col.setSpacing(1)
        t = QLabel(title)
        t.setFont(_font(8, QFont.Weight.DemiBold))
        t.setStyleSheet(f"color: {C.SECONDARY}; background: transparent; letter-spacing: 0.5px;")
        col.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setFont(_font(7))
            s.setWordWrap(True)
            s.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
            col.addWidget(s)
        hdr.addLayout(col)
        hdr.addStretch(1)
        lay.addLayout(hdr)
        return box, lay

    def _line(self, value="", placeholder=""):
        ed = QLineEdit(value)
        ed.setFont(_font(10))
        ed.setFixedHeight(34)
        ed.setPlaceholderText(placeholder)
        ed.setStyleSheet(self._INPUT_QSS)
        return ed

    def _secret(self, value="", placeholder=""):
        return _SecretEdit(value, placeholder)

    def _textarea(self, value="", placeholder="", height=86):
        te = QTextEdit(value)
        te.setFont(_font(10))
        te.setFixedHeight(height)
        te.setPlaceholderText(placeholder)
        te.setStyleSheet(self._TEXT_QSS)
        return te

    def _combo(self, value="", items=None, editable=True):
        cb = QComboBox()
        cb.setFont(_font(10))
        cb.setFixedHeight(34)
        cb.setEditable(editable)
        cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        cb.addItems(items or self._LANG_OPTIONS)
        if value:
            idx = cb.findText(value, Qt.MatchFlag.MatchFixedString)
            if idx >= 0:
                cb.setCurrentIndex(idx)
            else:
                cb.setEditText(value)
        cb.setStyleSheet(self._COMBO_QSS)
        return cb

    def _page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        body = QWidget()
        body.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        body.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 14, 24, 16)
        lay.setSpacing(14)
        scroll.setWidget(body)
        return scroll, lay

    def _page_title(self, title: str, subtitle: str):
        col = QVBoxLayout(); col.setSpacing(2)
        t = QLabel(title)
        t.setFont(_font(12, QFont.Weight.Bold))
        t.setStyleSheet(f"color: {C.TEXT}; background: transparent; letter-spacing: 0.8px;")
        col.addWidget(t)
        s = QLabel(subtitle)
        s.setFont(_font(8))
        s.setWordWrap(True)
        s.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        col.addWidget(s)
        return col

    # ── pages ─────────────────────────────────────────────────────────────

    def _build_provider_page(self):
        scroll, lay = self._page()
        self._stack.addWidget(scroll)
        lay.addLayout(self._page_title("AI PROVIDERS", "Keys are stored locally. Changes apply immediately."))

        box, bl = self._make_field("sparkles", "GEMINI", "Live voice + text fallback")
        self._gem_key = self._secret()
        self._gem_key.setPlaceholderText("AIza…")
        self._gem_model = self._line()
        self._gem_model.setPlaceholderText("gemini-2.5-flash")
        bl.addWidget(self._gem_key)
        bl.addWidget(self._gem_model)
        lay.addWidget(box)

        box, bl = self._make_field("key", "OMNIROUTE", "OpenAI-compatible router — 237+ models")
        self._omni_key = self._secret()
        self._omni_key.setPlaceholderText("sk-…")
        self._omni_url = self._line()
        self._omni_url.setPlaceholderText("http://localhost:20128/v1")
        self._omni_model = self._line()
        self._omni_model.setPlaceholderText("auto")
        bl.addWidget(self._omni_key)
        bl.addWidget(self._omni_url)
        bl.addWidget(self._omni_model)
        lay.addWidget(box)
        lay.addStretch(1)

    def _build_profile_page(self):
        scroll, lay = self._page()
        self._stack.addWidget(scroll)
        lay.addLayout(self._page_title("PROFILE", "What IRA knows about you — injected into every conversation."))

        self._name = self._line()
        box, bl = self._make_field("user", "NAME")
        bl.addWidget(self._name)
        lay.addWidget(box)

        self._city = self._line()
        box, bl = self._make_field("map-pin", "CITY / PLACE", "Where you live")
        bl.addWidget(self._city)
        lay.addWidget(box)

        self._lang = self._combo()
        box, bl = self._make_field("globe", "SPOKEN LANGUAGE", "How IRA should respond aloud")
        bl.addWidget(self._lang)
        lay.addWidget(box)

        self._dob = self._line()
        self._dob.setPlaceholderText("YYYY-MM-DD")
        box, bl = self._make_field("calendar", "DATE OF BIRTH")
        bl.addWidget(self._dob)
        lay.addWidget(box)

        self._fav = self._textarea()
        self._fav.setPlaceholderText("One favorite per line")
        box, bl = self._make_field("heart", "FAVORITES")
        bl.addWidget(self._fav)
        lay.addWidget(box)

        # Auto-start on boot (Mark-L)
        self._autostart_btn = QPushButton("AUTO-START: …")
        self._autostart_btn.setFixedHeight(30)
        self._autostart_btn.setFont(_font(8, QFont.Weight.Bold))
        self._autostart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autostart_btn.setStyleSheet(
            f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
            f"border: 1px solid {C.BORDER}; border-radius: 10px; }}"
            f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}"
        )
        self._autostart_btn.clicked.connect(self._on_autostart_click)
        box, bl = self._make_field("power", "AUTO-START ON BOOT", "Launch IRA when Windows starts")
        bl.addWidget(self._autostart_btn)
        lay.addWidget(box)
        lay.addStretch(1)

    def _on_autostart_click(self):
        if self.autostart_requested:
            self.autostart_requested()
        self.refresh_autostart()

    def refresh_autostart(self):
        if self.autostart_check:
            on = self.autostart_check()
            self._autostart_btn.setText("AUTO-START: ON" if on else "AUTO-START: OFF")

    def _build_rules_page(self):
        scroll, lay = self._page()
        self._stack.addWidget(scroll)
        lay.addLayout(self._page_title("RULES", "Standing instructions IRA must follow in every conversation."))
        self._rules = self._textarea()
        self._rules.setPlaceholderText(
            "Never call before 9 am\n"
            "Always confirm before sending emails\n"
            "Keep replies under 3 sentences"
        )
        box, bl = self._make_field("shield", "YOUR RULES")
        bl.addWidget(self._rules)
        lay.addWidget(box)
        lay.addStretch(1)

    def _build_remote_page(self):
        scroll, lay = self._page()
        self._stack.addWidget(scroll)
        lay.addLayout(self._page_title("REMOTE & NEWS", "Control IRA from your phone, and set your news region."))

        self._news = self._line()
        self._news.setPlaceholderText("e.g. Kothagudem, Telangana")
        box, bl = self._make_field("rss", "NEWS CITY", "City used for local news & weather")
        bl.addWidget(self._news)
        lay.addWidget(box)

        rbox = QWidget()
        rbox.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        rbox.setStyleSheet(f"""
            background: rgba(0, 175, 255, 0.05);
            border: 1px solid rgba(0, 175, 255, 0.14);
            border-radius: 14px;
        """)
        rl = QVBoxLayout(rbox)
        rl.setContentsMargins(16, 14, 16, 14)
        rl.setSpacing(8)
        field_box, _ = self._make_field("smartphone", "REMOTE CONTROL", "Scan a QR from your phone to control this desktop")
        rl.addWidget(field_box)
        btn = QPushButton("OPEN REMOTE CONTROL")
        btn.setFixedHeight(34)
        btn.setFont(_font(8, QFont.Weight.DemiBold))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(0, 175, 255, 0.12);
                color: {C.PRI};
                border: 1px solid rgba(0, 175, 255, 0.30); border-radius: 10px;
                letter-spacing: 0.6px;
            }}
            QPushButton:hover {{
                background: rgba(0, 175, 255, 0.20);
                border: 1px solid rgba(0, 175, 255, 0.50);
            }}
        """)
        btn.clicked.connect(self.remoteRequested.emit)
        rl.addWidget(btn)
        lay.addWidget(rbox)
        lay.addStretch(1)

    # ── behaviour ─────────────────────────────────────────────────────────

    def _switch(self, btn: _NavButton):
        for i, b in enumerate(self._nav_buttons):
            active = b is btn
            b.set_active(active)
            if active:
                self._stack.setCurrentIndex(i)

    def _focus(self):
        self.raise_()
        self.activateWindow()

    def _gather_providers(self) -> dict:
        out = {}
        for attr, key in [("_gem_key", "gemini_api_key"),
                          ("_gem_model", "gemini_model"),
                          ("_omni_key", "omniroute_api_key"),
                          ("_omni_url", "omniroute_url"),
                          ("_omni_model", "omniroute_model")]:
            v = getattr(self, attr).text().strip()
            if v:
                out[key] = v
        return out

    def _gather_profile(self) -> dict:
        fav = "\n".join(l for l in self._fav.toPlainText().splitlines() if l.strip())
        return {
            "name":       self._name.text().strip(),
            "city":       self._city.text().strip(),
            "language":   self._lang.currentText().strip(),
            "dob":        self._dob.text().strip(),
            "favorites":  fav,
            "rules":      self._rules.toPlainText().strip(),
            "news_city":  self._news.text().strip(),
        }

    def _save(self):
        from core.settings_store import save_providers, save_profile

        prov = self._gather_providers()
        prof = self._gather_profile()
        r1 = save_providers(prov)
        r2 = save_profile(prof)

        errs = r1["errors"] + r2["errors"]
        if errs:
            self._status.setText("  ".join(errs)[:56])
            self._status.setStyleSheet(f"color: {C.ERROR}; background: transparent;")
            return

        self._status.setText("Saved")
        self._status.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        QTimer.singleShot(750, self._do_close)

    def _do_close(self):
        self.hide()
        self.closed.emit()

    def load_current_values(self):
        from core.settings_store import load_providers, load_profile
        p = load_providers()
        prof = load_profile()
        self._gem_key.setText(p.get("gemini_api_key", ""))
        self._gem_model.setText(p.get("gemini_model", ""))
        self._omni_key.setText(p.get("omniroute_api_key", ""))
        self._omni_url.setText(p.get("omniroute_url", ""))
        self._omni_model.setText(p.get("omniroute_model", ""))
        self._name.setText(prof.get("name", ""))
        self._city.setText(prof.get("city", ""))
        lang = prof.get("language", "")
        if lang:
            idx = self._lang.findText(lang, Qt.MatchFlag.MatchFixedString)
            if idx >= 0:
                self._lang.setCurrentIndex(idx)
            else:
                self._lang.setEditText(lang)
        self._dob.setText(prof.get("dob", ""))
        self._fav.setPlainText(prof.get("favorites", ""))
        self._rules.setPlainText(prof.get("rules", ""))
        self._news.setText(prof.get("news_city", ""))


# ─────────────────────────────────────────────────────────────────────────────
#  Main window
# ─────────────────────────────────────────────────────────────────────────────

_BTN_ICON = {
    "STOP": "stop", "MUTE": "mic-off", "SEE SCREEN": "eye",
    "LIVE SCREEN": "video", "GESTURES": "hand", "SNAP": "camera",
    "System Status": "gauge", "Settings": "settings", "Browser": "globe",
    "Files": "folder", "Terminal": "terminal", "Desktop": "monitor",
    "Web Search": "search", "News": "newspaper", "Scrape": "code",
    "RSS Feeds": "rss", "YouTube": "youtube",
    "Send Message": "send", "Read Gmail": "mail", "Telegram": "send",
    "Discord": "message-sq", "Slack": "hash",
    "Write": "pen", "Images": "image", "Word Doc": "file-text",
    "PDF": "file-text", "PPT": "presentation", "Template PPT": "layout-template",
    "Spreadsheet": "table",
    "Website": "layout",
    "Task Manager": "list", "Expenses": "wallet", "Reminder": "bell",
    "Scheduler": "clock", "Briefing": "sunrise", "Meeting": "users",
    "Code Helper": "code", "Dev Agent": "bot", "Claude Code": "terminal",
    "Flights": "plane", "Games": "music", "Analyze": "pie",
    "Smart Home": "home", "Self Check": "check", "Provider Status": "server",
}


class MainWindow(QMainWindow):
    _log_sig     = pyqtSignal(str)
    _state_sig   = pyqtSignal(str)
    _content_sig = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _clipboard_sig = pyqtSignal(str)      # clipboard text changed (thread-safe)

    def __init__(self, face_path: str):
        super().__init__()
        self.setWindowTitle("I.R.A — V1.0")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command  = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_screen_captured = None  # callable: (image_bytes, mime) -> None
        self.on_interrupt       = None  # callable: () -> None
        self.on_live_screen     = None  # callable: (image_bytes, mime) -> None
        self.on_hand_gesture    = None  # callable: () -> None (toggle gesture control)
        self._muted           = False
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._settings_overlay: SettingsOverlay | None = None

        # ── Clipboard intelligence panel (Mark-L) ─────────────────────────
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        try:
            QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)
        except Exception:
            pass
        self._settings_dim: QWidget | None = None
        self._gesture_active  = False

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        self._ambient = _AmbientGlow(central)
        self._ambient.lower()

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        # Center column: HUD on top + system status + content panel below
        _center = QWidget()
        _center.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        _center_lay = QVBoxLayout(_center)
        _center_lay.setContentsMargins(0, 0, 0, 0)
        _center_lay.setSpacing(8)
        self.hud = HudCanvas(face_path)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        _center_lay.addWidget(self.hud, stretch=1)

        self._sys_card = SysStatusCard()
        _center_lay.addWidget(self._sys_card)

        self._content_panel = self._build_content_panel()
        _center_lay.addWidget(self._content_panel)
        body.addWidget(_center, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if hasattr(self, '_ambient'):
            self._ambient.setGeometry(cw.rect())
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 420
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._settings_dim and self._settings_dim.isVisible():
            self._settings_dim.setGeometry(cw.rect())
        if self._settings_overlay and self._settings_overlay.isVisible():
            ow, oh = SettingsOverlay._OW, SettingsOverlay._OH
            self._settings_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )

    def _update_metrics(self):
        snap = _metrics.snapshot()

        cpu = snap["cpu"]

        gpu = snap["gpu"]

        # uptime
        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            up_text = f"{h:02d}:{m:02d}"
            up_pct  = (elapsed % 86400) / 86400
        except Exception:
            up_text, up_pct = "--:--", 0.0

        # process count
        try:
            proc_count = len(psutil.pids())
        except Exception:
            proc_count = 0

        self._sys_card.update_data({
            "cpu": cpu, "gpu": gpu, "batt": snap["batt"], "stor": snap["stor"],
            "proc": f"{proc_count}", "up_text": up_text, "up_pct": up_pct,
        })

    # ── Header ──────────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(76)
        w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(12)

        # Left: brand + version + online pill
        brand = QHBoxLayout(); brand.setSpacing(9)
        brand.addWidget(_BrandLogo())
        name_col = QVBoxLayout(); name_col.setSpacing(0)
        name = QLabel("IRA")
        name.setFont(_font(16, QFont.Weight.Bold))
        name.setStyleSheet(f"color: {C.WHITE}; background: transparent; letter-spacing: 2px;")
        name_col.addWidget(name)
        ver = QLabel("V1.0  ·  Made by Yuvan")
        ver.setFont(_font(8))
        ver.setStyleSheet(f"color: {C.MUTED}; background: transparent; letter-spacing: 0.4px;")
        name_col.addWidget(ver)
        brand.addLayout(name_col)
        brand.setAlignment(name_col, Qt.AlignmentFlag.AlignVCenter)
        brand.addSpacing(4)
        self._online_pill = _StatusPill()
        brand.addWidget(self._online_pill, alignment=Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(brand)
        lay.setAlignment(brand, Qt.AlignmentFlag.AlignVCenter)

        # Center: four floating glass stat cards
        cards = QHBoxLayout(); cards.setSpacing(8)
        self._header_cards = {}
        for icon, label, value in [
            ("sparkles", "AI MODEL", "Gemini 2.5 Flash"),
            ("zap",      "LATENCY",  "12ms"),
            ("database", "CONTEXT",  "2.1GB"),
            ("waveform", "VOICE",    "Iris"),
        ]:
            card = _HeaderCard(icon, label, value)
            self._header_cards[label] = card
            cards.addWidget(card, stretch=1)
        lay.addLayout(cards, stretch=1)
        lay.setAlignment(cards, Qt.AlignmentFlag.AlignVCenter)

        # Right: clock + date + settings
        right = QHBoxLayout(); right.setSpacing(10)
        clock_col = QVBoxLayout(); clock_col.setSpacing(0)
        self._clock_lbl = QLabel("00:00")
        self._clock_lbl.setFont(_font(22, QFont.Weight.DemiBold))
        self._clock_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent; letter-spacing: 1px;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(_font(8))
        self._date_lbl.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock_col.addWidget(self._date_lbl)
        right.addLayout(clock_col)
        right.setAlignment(clock_col, Qt.AlignmentFlag.AlignVCenter)
        settings_btn = _IconButton("", "settings", color=C.TEXT_DIM, fixed_h=30, square=True)
        settings_btn.clicked.connect(self._on_settings)
        self._settings_btn = settings_btn
        right.addWidget(settings_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(right)
        lay.setAlignment(right, Qt.AlignmentFlag.AlignVCenter)
        return w

    def _on_settings(self):
        self._open_settings()

    def _open_settings(self):
        if self._settings_overlay and self._settings_overlay.isVisible():
            self._settings_overlay._focus()
            return
        cw = self.centralWidget()

        dim = _Dim(cw)
        dim.setGeometry(cw.rect())
        dim.closed.connect(self._close_settings)
        dim.show()
        dim.raise_()
        self._settings_dim = dim

        ov = SettingsOverlay(parent=cw)
        ov.closed.connect(self._close_settings)
        ov.remoteRequested.connect(
            lambda: (self._close_settings(), self._open_remote())
        )
        ov.autostart_check = self._check_autostart
        ov.autostart_requested = self._toggle_autostart
        ov.refresh_autostart()
        ow, oh = SettingsOverlay._OW, SettingsOverlay._OH
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.load_current_values()
        ov.show()
        ov.raise_()
        ov._focus()
        self._settings_overlay = ov

    def _close_settings(self):
        if self._settings_dim:
            self._settings_dim.hide()
            self._settings_dim = None
        if self._settings_overlay:
            self._settings_overlay.hide()
            self._settings_overlay = None

    # ── Clipboard intelligence (Mark-L) ───────────────────────────────────────

    def _on_clipboard_changed(self):
        try:
            text = QApplication.clipboard().text().strip()
            if text:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        if self._clipboard_panel:
            self._clipboard_panel.show_clipboard(text)

    def _on_clipboard_action(self, cmd: str):
        self._log.append_log(f"CMD: {cmd[:80]}")
        if self.on_text_command:
            self._run_text(cmd)

    # ── Auto-start on boot (Mark-L) ───────────────────────────────────────────

    def _check_autostart(self) -> bool:
        """True if auto-start is registered in HKCU Run."""
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            try:
                winreg.QueryValueEx(key, "IRA")
                return True
            except FileNotFoundError:
                return False
            finally:
                winreg.CloseKey(key)
        except Exception:
            return False

    def _toggle_autostart(self) -> bool:
        """Toggle auto-start-on-boot; returns the new state."""
        currently_on = self._check_autostart()
        try:
            import winreg
            reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
            if currently_on:
                winreg.DeleteValue(reg, "IRA")
            else:
                script = str(Path(__file__).resolve().parent / "main.py")
                pythonw = Path(sys.executable).parent / "pythonw.exe"
                exe = str(pythonw if pythonw.exists() else sys.executable)
                winreg.SetValueEx(reg, "IRA", 0, winreg.REG_SZ, f'"{exe}" "{script}"')
            winreg.CloseKey(reg)
            return not currently_on
        except Exception as e:
            print(f"[Autostart] toggle failed: {e}")
            return currently_on

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M"))
        self._date_lbl.setText(time.strftime("%a %d %b"))

    def _make_feature_btn(self, text: str, command: str,
                          color: str = None, bg: str = None) -> QPushButton:
        if color is None:
            color = C.BLUE
        btn = _IconButton(text, _BTN_ICON.get(text, "circle"), color=color, fixed_h=28)
        btn.clicked.connect(lambda: self._on_feature_command(command))
        return btn

    def _run_text(self, command: str):
        """Run a text command on a worker thread; log failures instead of dying
        silently (thread exceptions would otherwise vanish into the void)."""

        def _work():
            try:
                self.on_text_command(command)
            except Exception as e:
                self._log.append_log(f"SYS: Command failed — {type(e).__name__}: {e}")

        threading.Thread(target=_work, daemon=True).start()

    def _on_feature_command(self, command: str):
        self._log.append_log(f"CMD: {command}")

        if command == "[STOP]":
            if self.on_interrupt:
                threading.Thread(target=self.on_interrupt, daemon=True).start()
            return
        elif command == "[TOGGLE_MUTE]":
            self._toggle_mute()
            return
        elif command == "[LIVE_SCREEN]":
            self._on_live_screen()
            return
        elif command == "[TOGGLE_GESTURE]":
            self._on_hand_gesture()
            return
        elif command == "[SCREEN_CAPTURE]":
            self._on_screen_capture()
            return
        elif command == "[HAND_CONTROL]":
            self._open_hand_control()
            return
        elif command == "[SMART_HOME]":
            self._open_smart_home()
            return

        if self.on_text_command:
            self._run_text(command)

    def _on_screen_capture(self):
        """Show the floating capture button; a click captures the screen."""
        from actions.screen_overlay import FloatingCaptureButton

        if getattr(self, "_snap_button", None):
            self._snap_button.hide_button()
            self._snap_button = None
            self._log.append_log("SYS: Screen capture closed")
            return
        self._snap_button = FloatingCaptureButton(on_capture=self._on_snap_captured)
        self._snap_button.show_button()
        self._log.append_log("SYS: Screen capture ready — click the crosshair to capture")

    def _on_snap_captured(self, image_bytes: bytes, mime: str):
        """Save a captured screenshot and log it."""
        import datetime as _dt
        from pathlib import Path

        try:
            out_dir = Path(__file__).resolve().parent.parent / "Data" / "screen_captures"
            out_dir.mkdir(parents=True, exist_ok=True)
            ext = "png" if "png" in (mime or "") else "jpg"
            path = out_dir / f"snap_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
            path.write_bytes(image_bytes)
            print(f"[Snap] saved {path}")
            self._log.append_log(f"SYS: Screen capture saved → {path.name}")
        except Exception as e:
            print(f"[Snap] save error: {e}")
        finally:
            if getattr(self, "_snap_button", None):
                self._snap_button.hide_button()
                self._snap_button = None

    def _add_btn_group(self, layout, title: str, color: str, buttons: list):
        hdr = QLabel(title)
        hdr.setFont(_font(8, QFont.Weight.DemiBold))
        hdr.setStyleSheet(f"color: {C.MUTED}; background: transparent;"
                          f" padding: 10px 6px 3px 6px; letter-spacing: 0.8px;")
        layout.addWidget(hdr)
        for text, command in buttons:
            layout.addWidget(self._make_feature_btn(text, command, color))
        layout.addSpacing(2)

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(w)
        outer.setContentsMargins(10, 10, 10, 0)
        outer.setSpacing(8)

        # ── Feature controls only — system stats live in the center
        #    SysStatusCard, so no duplicate metric bars in the sidebar ───────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: transparent; width: 3px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(0, 175, 255, 0.18); border-radius: 2px; min-height: 12px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)

        btn_container = QWidget()
        btn_container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        btn_lay = QVBoxLayout(btn_container)
        btn_lay.setContentsMargins(2, 2, 2, 4)
        btn_lay.setSpacing(2)

        self._add_btn_group(btn_lay, "CONTROLS", C.ERROR, [
            ("STOP", "[STOP]"),
            ("MUTE", "[TOGGLE_MUTE]"),
            ("SEE SCREEN", "Use screen_process to look at my screen and tell me what you see"),
            ("LIVE SCREEN", "[LIVE_SCREEN]"),
            ("GESTURES", "[TOGGLE_GESTURE]"),
            ("SNAP", "[SCREEN_CAPTURE]"),
            ("HAND CONTROL", "[HAND_CONTROL]"),
        ])

        self._add_btn_group(btn_lay, "SYSTEM", C.PRI, [
            ("System Status", "Check system status"),
            ("Settings", "Open computer settings"),
            ("Browser", "Open browser"),
            ("Files", "Open file manager"),
            ("Terminal", "Run a command"),
            ("Desktop", "Manage desktop"),
        ])

        self._add_btn_group(btn_lay, "SEARCH", C.SEC, [
            ("Web Search", "Search the web"),
            ("News", "Search the web for latest news"),
            ("Scrape", "Scrape a website"),
            ("RSS Feeds", "Collect RSS feeds"),
            ("YouTube", "Open YouTube"),
        ])

        self._add_btn_group(btn_lay, "COMMS", C.PRI, [
            ("Send Message", "Send a message"),
            ("Read Gmail", "Check my email"),
            ("Telegram", "Send a Telegram message"),
            ("Discord", "Send a Discord message"),
            ("Slack", "Send a Slack message"),
        ])

        self._add_btn_group(btn_lay, "CREATE", C.PRI, [
            ("Write", "Write content"),
            ("Images", "Generate an image"),
            ("Word Doc", "Create a Word document"),
            ("PDF", "Create a PDF"),
            ("PPT", "Create a presentation"),
            ("Template PPT", "Build a PowerPoint with a premium design template"),
            ("Spreadsheet", "Create a spreadsheet"),
            ("Website", "Build a website"),
        ])

        self._add_btn_group(btn_lay, "TASKS", C.SEC, [
            ("Task Manager", "Show my tasks"),
            ("Expenses", "Track expenses"),
            ("Reminder", "Set a reminder"),
            ("Scheduler", "Schedule an automation"),
            ("Briefing", "Give me the daily briefing"),
            ("Meeting", "Start meeting mode"),
        ])

        self._add_btn_group(btn_lay, "DEV", C.PRI, [
            ("Code Helper", "Help me with code"),
            ("Dev Agent", "Build a project"),
            ("Claude Code", "Use Claude Code"),
        ])

        self._add_btn_group(btn_lay, "MORE", C.TEXT_MED, [
            ("Flights", "Search for flights"),
            ("Games", "Update games"),
            ("Analyze", "Analyze a folder"),
            ("Smart Home", "[SMART_HOME]"),
            ("Self Check", "Run a self evaluation"),
            ("Provider Status", "Check AI provider status"),
        ])

        btn_lay.addStretch()
        scroll.setWidget(btn_container)
        outer.addWidget(scroll, stretch=1)

        # ── Bottom: STOP ────────────────────────────────────────────────────
        bottom = QWidget()
        bottom.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        btm_lay = QVBoxLayout(bottom)
        btm_lay.setContentsMargins(0, 4, 0, 8)
        btm_lay.setSpacing(0)

        self._interrupt_btn = _IconButton("STOP", "stop", color=C.ERROR, fixed_h=30)
        self._interrupt_btn.clicked.connect(self._on_interrupt)
        btm_lay.addWidget(self._interrupt_btn)

        outer.addWidget(bottom)
        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        def _sec(txt, icon=None):
            h = QHBoxLayout(); h.setSpacing(6)
            if icon:
                pm = icon_pm(icon, C.MUTED, 10)
                _icon = QLabel(); _icon.setPixmap(pm); _icon.setFixedWidth(12)
                h.addWidget(_icon)
            l = QLabel(txt)
            l.setFont(_font(8, QFont.Weight.DemiBold))
            l.setStyleSheet(f"color: {C.MUTED}; background: transparent;"
                            f" letter-spacing: 1.2px; padding: 0 2px;")
            h.addWidget(l)
            h.addStretch()
            row_w = QWidget(); row_w.setLayout(h)
            return row_w

        lay.addWidget(_sec("ACTIVITY LOG", "list"))
        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("border: none; border-top: 1px solid rgba(255, 255, 255, 0.04); margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_sec("FILE UPLOAD", "upload"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded")
        self._file_hint.setFont(_font(8))
        self._file_hint.setStyleSheet(f"color: {C.MUTED}; background: transparent; padding: 0 4px;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("border: none; border-top: 1px solid rgba(255, 255, 255, 0.04); margin: 2px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_sec("COMMAND INPUT", "terminal"))
        lay.addLayout(self._build_input_row())

        self._mute_btn = _IconButton("MICROPHONE ACTIVE", "mic",
                                     color=C.GREEN, fixed_h=30)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        screen_btn = _IconButton("SEE SCREEN", "eye", color=C.BLUE, fixed_h=30)
        screen_btn.clicked.connect(self._on_see_screen)
        lay.addWidget(screen_btn)

        fs_btn = _IconButton("FULLSCREEN  [F11]", "maximize", color=C.TEXT_DIM, fixed_h=26)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)

        return w

    def _on_see_screen(self):
        """
        'See Screen' button: HIDE IRA's own window, capture the screen, restore
        IRA, then send that exact shot to the vision module. Because the window
        is provably hidden at the moment of capture, IRA's own interface is
        excluded — it sees only the rest of the desktop.
        """
        try:
            from actions.screen_processor import _capture_screen
        except Exception as e:
            print(f"[UI] Cannot import screen capture: {e}")
            return

        self.hide()
        QApplication.processEvents()
        time.sleep(0.35)                 # let the OS actually remove the window
        QApplication.processEvents()

        image_bytes = mime = None
        try:
            image_bytes, mime = _capture_screen()
        except Exception as e:
            print(f"[UI] Screen capture failed: {e}")

        self.show()
        self.raise_()
        self.activateWindow()

        if image_bytes and self.on_screen_captured:
            threading.Thread(
                target=self.on_screen_captured,
                args=(image_bytes, mime),
                daemon=True,
            ).start()
        elif self.on_text_command:
            self._run_text(
                "Use screen_process to look at my screen and tell me what "
                "you see, in English, addressing me as Yuvan.")

    def _on_interrupt(self):
        self._log.append_log("SYS: Interrupt requested")
        if self.on_interrupt:
            threading.Thread(target=self.on_interrupt, daemon=True).start()

    def _on_live_screen(self):
        from actions.screen_overlay import FloatingCaptureButton

        def on_capture(image_bytes, mime):
            if image_bytes and self.on_live_screen:
                threading.Thread(
                    target=self.on_live_screen,
                    args=(image_bytes, mime),
                    daemon=True,
                ).start()

        FloatingCaptureButton.show_snap_button(on_capture=on_capture)
        self._log.append_log("SYS: Snap button shown — click it to capture full screen")
        self.showMinimized()

    def _on_hand_gesture(self):
        self._gesture_active = not self._gesture_active
        if self._gesture_active:
            self._log.append_log("SYS: Hand gesture control starting…")
        else:
            self._log.append_log("SYS: Hand gesture control stopped")
        if self.on_hand_gesture:
            threading.Thread(target=self.on_hand_gesture, daemon=True).start()

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(6)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(_font(10))
        self._input.setFixedHeight(32)
        ph = self._input.palette()
        ph.setColor(QPalette.ColorRole.PlaceholderText, qcol(C.MUTED, 200))
        self._input.setPalette(ph)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(16, 18, 20, 0.55);
                color: {C.TEXT};
                border: 1px solid rgba(255, 255, 255, 0.07);
                border-radius: 11px;
                padding: 2px 12px;
                selection-background-color: rgba(0, 175, 255, 0.35);
            }}
            QLineEdit:focus {{
                background: rgba(16, 18, 20, 0.78);
                border: 1px solid rgba(0, 175, 255, 0.55);
            }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = _IconButton("", "arrow-right", color=C.TEXT_MED, fixed_h=32, square=True)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_content_panel(self) -> QWidget:
        """
        Collapsible panel below the HUD — shows search results, news, briefings.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: rgba(16, 18, 20, 0.40);
                border: 1px solid rgba(0, 175, 255, 0.10);
                border-radius: 16px;
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(18, 12, 18, 14)
        lay.setSpacing(6)

        hdr = QHBoxLayout(); hdr.setSpacing(8)

        dot = QLabel()
        dot.setPixmap(icon_pm("sparkles", C.BLUE, 14))
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(_font(10, QFont.Weight.DemiBold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(_font(8))
        self._content_ts_lbl.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QLabel()
        dismiss.setPixmap(icon_pm("x", C.MUTED, 12))
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.mousePressEvent = lambda _e: w.hide()
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("border: none; border-top: 1px solid rgba(0, 175, 255, 0.08);")
        lay.addWidget(sep)

        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(_font(9))
        self._content_display.setFixedHeight(140)
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(7, 7, 7, 0.35);
                color: {C.TEXT};
                border: 1px solid rgba(0, 175, 255, 0.06);
                border-radius: 12px;
                padding: 8px 12px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 4px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(0, 175, 255, 0.15); border-radius: 2px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _show_content(self, title: str, text: str):
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        self._content_panel.show()

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(28)
        w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(12)

        def _pill(txt, color=C.MUTED):
            l = QLabel(txt)
            l.setFont(_font(8))
            l.setStyleSheet(f"color: {color}; background: transparent; letter-spacing: 0.5px;")
            return l

        lay.addWidget(_pill("F4  MUTE"))
        lay.addWidget(_pill("F11  FULLSCREEN"))
        lay.addStretch()

        dot = QLabel()
        dot.setPixmap(icon_pm("circle", C.GREEN, 7))
        lay.addWidget(dot)
        lay.addWidget(_pill("ACTIVE", C.GREEN))
        lay.addWidget(_pill("12ms", C.BLUE))
        lay.addStretch()

        ver = QLabel("IRA  V1.0")
        ver.setFont(_font(8))
        ver.setStyleSheet(f"color: {C.MUTED}; background: transparent; letter-spacing: 1px;")
        lay.addWidget(ver)
        return w

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon_name, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        try:
            size = _fmt_size(p.stat().st_size)
        except OSError:
            size = "--"              # file vanished — keep the UI alive, log below
        self._file_hint.setText(f"{icon_name}  {p.name}  ·  {size}  ·  Tell IRA what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            self._run_text(msg)

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_smart_home(self):
        """Left-panel Smart Home button -> dedicated Smart Home overlay."""
        if getattr(self, "_smart_home_overlay", None):
            self._smart_home_overlay._do_close()
        cw = self.centralWidget()
        ow, oh = SmartHomeOverlay._OW, SmartHomeOverlay._OH
        ov = SmartHomeOverlay(parent=cw)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, "_smart_home_overlay", None))
        self._smart_home_overlay = ov
        ov.show()
        ov.raise_()
        self._log.append_log("SYS: Smart Home panel opened")

    def _open_hand_control(self):
        """Left-panel Hand Control button -> embedded barehands air-board."""
        if getattr(self, "_hand_control_overlay", None):
            self._hand_control_overlay._do_close()
        try:
            from core.settings_store import load_profile
            user = (load_profile().get("name") or "default").strip() or "default"
        except Exception:
            user = "default"
        cw = self.centralWidget()
        ow, oh = cw.width() - 48, cw.height() - 48
        ov = HandControlOverlay(user=user, parent=cw)
        ov.setGeometry(
            (cw.width() - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, "_hand_control_overlay", None))
        self._hand_control_overlay = ov
        ov.show()
        ov.raise_()
        self._log.append_log(f"SYS: Hand Control opened for {user}")

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted.")
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active.")

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.set_text("MICROPHONE MUTED")
            self._mute_btn.set_color(C.ERROR)
        else:
            self._mute_btn.set_text("MICROPHONE ACTIVE")
            self._mute_btn.set_color(C.GREEN)

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            self._run_text(txt)

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 420
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        API_FILE.write_text(
            json.dumps({"gemini_api_key": key, "os_system": os_name}, indent=4),
            encoding="utf-8",
        )
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. IRA online.")


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow(face_path)
        self._win.show()
        self.root = _RootShim(self._app)

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_screen_captured(self):
        return self._win.on_screen_captured

    @on_screen_captured.setter
    def on_screen_captured(self, cb):
        self._win.on_screen_captured = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    @property
    def on_live_screen(self):
        return self._win.on_live_screen

    @on_live_screen.setter
    def on_live_screen(self, cb):
        self._win.on_live_screen = cb

    @property
    def on_hand_gesture(self):
        return self._win.on_hand_gesture

    @on_hand_gesture.setter
    def on_hand_gesture(self, cb):
        self._win.on_hand_gesture = cb

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")
