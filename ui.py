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
    QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QTextEdit,
    QVBoxLayout, QWidget, QProgressBar, QGraphicsDropShadowEffect,
)

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"

_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 190
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    """Ultra-minimal palette — three colors + grayscale."""
    # Backgrounds
    BG        = "#050505"   # deep matte black
    PANEL     = "#0C0C0C"   # subtle panel
    PANEL2    = "#0E0E0E"   # glass base

    # The only two accents
    BLUE      = "#00AFFF"
    GREEN     = "#00FF88"

    # Text — pure grayscale
    TEXT      = "#FFFFFF"
    TEXT_MED  = "#999999"
    TEXT_DIM  = "#555555"

    # Status — desaturated
    SUCCESS   = "#00FF88"
    WARN      = "#CC8800"
    ERROR     = "#CC4444"
    MUTED     = "#CC4444"

    # Chat
    CHAT_AI   = "#00AFFF"
    CHAT_USER = "#FFFFFF"

    # Border — nearly invisible
    BORDER    = "#111111"

    # Shortcuts for legacy compatibility
    PRI       = BLUE
    PRI_DIM   = "#0088AA"
    PRI_GHO   = "#001828"
    SEC       = GREEN
    SEC_DIM   = "#00AA66"
    WHITE     = TEXT
    WARN_     = WARN
    ERROR_    = ERROR
    MUTED_C   = MUTED
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

def _add_glow(widget, color: str = C.BLUE, radius: int = 20):
    """Add a colored glow effect for the glassmorphism floating look."""
    glow = QGraphicsDropShadowEffect()
    glow.setBlurRadius(radius)
    glow.setColor(QColor(color))
    glow.setOffset(0, 0)
    widget.setGraphicsEffect(glow)
    return widget

def _add_glass_shadow(widget, color: str = "#00AFFF", blur: int = 20, alpha: int = 8):
    """Ultra-soft floating shadow — almost imperceptible."""
    glow = QGraphicsDropShadowEffect()
    glow.setBlurRadius(blur)
    c = QColor(color); c.setAlpha(alpha)
    glow.setColor(c)
    glow.setOffset(0, 4)
    widget.setGraphicsEffect(glow)
    return widget


class _AmbientGlow(QWidget):
    """Ultra-subtle ambient lighting + faint film grain + hexagonal pattern."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._noise_seed = random.randint(0, 9999)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        # ── Very faint blue ambient from left ───────────────────────────────
        blue_glow = QRadialGradient(0, H * 0.5, W * 0.55)
        blue_glow.setColorAt(0.0, QColor(0, 175, 255, 12))
        blue_glow.setColorAt(0.5, QColor(0, 175, 255, 4))
        blue_glow.setColorAt(1.0, QColor(0, 175, 255, 0))
        p.setBrush(QBrush(blue_glow))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(self.rect())

        # ── Very faint green ambient from right ─────────────────────────────
        green_glow = QRadialGradient(W, H * 0.5, W * 0.55)
        green_glow.setColorAt(0.0, QColor(0, 255, 136, 10))
        green_glow.setColorAt(0.5, QColor(0, 255, 136, 3))
        green_glow.setColorAt(1.0, QColor(0, 255, 136, 0))
        p.setBrush(QBrush(green_glow))
        p.drawRect(self.rect())

        # ── Extremely faint hexagonal dots ─────────────────────────────────
        p.setPen(QPen(QColor(0, 175, 255, 4), 0.3))
        spacing = 52
        for x in range(0, W, spacing):
            for y in range(0, H, spacing):
                offset = (x // spacing) % 2 * (spacing // 2)
                p.drawPoint(x, y + offset)

        # ── Very subtle film grain ─────────────────────────────────────────
        random.seed(self._noise_seed)
        grain_alpha = 6
        p.setPen(QPen(QColor(255, 255, 255, grain_alpha), 0.3))
        for i in range(min(W * H // 2000, 600)):
            gx = random.randint(0, W - 1)
            gy = random.randint(0, H - 1)
            p.drawPoint(gx, gy)


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
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

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # NVIDIA
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

        # AMD (Linux)
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

            # Intel GPU (Linux)
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

        # macOS — powermetrics (GPU Engine)
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
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    """3D particle sphere AI core — premium green/blue glowing particles with depth."""

    _NUM_PARTICLES = 2000  # slightly denser for premium feel

    def __init__(self, face_path: str, parent=None):
        # face_path is accepted for API compatibility but not used
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
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

        # Particle system
        self._particles = self._init_particles(HudCanvas._NUM_PARTICLES)
        self._glow_cache: dict[int, QPixmap] = {}
        # Energy interpolation (0 = idle, 1 = full speaking)
        self._energy = 0.0
        self._target_energy = 0.0
        self._burst_particles: list[dict] = []

        # Dynamic FPS: 30 at idle, 60 when speaking
        self._idle_mode = True
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(33)  # 30fps idle (low CPU)

    # ── Particle generation ─────────────────────────────────────────────────

    def _init_particles(self, count: int) -> list[dict]:
        """Generate particles — more organic distribution with denser core feel."""
        particles = []
        for _ in range(count):
            theta = random.uniform(0, 2 * math.pi)
            phi = math.acos(2 * random.uniform(0, 1) - 1)

            # Slight clustering toward center for denser core appearance
            r_cluster = 0.92 + random.uniform(-0.06, 0.08) if random.random() < 0.35 else 1.0

            particles.append({
                "theta": theta,
                "phi": phi,
                "r_factor": r_cluster + random.uniform(-0.06, 0.06),
                # More varied sizes: 65% small, 22% medium, 13% highlight
                "size": (
                    random.uniform(4.0, 5.5) if random.random() < 0.13
                    else random.uniform(2.8, 3.8) if random.random() < 0.25
                    else random.uniform(1.6, 2.4)
                ),
                "color_blend": random.uniform(0, 1),
                "use_glow": random.random() < 0.15,  # slightly more glow particles
                # Wave deformation parameters (organic morphing)
                "phase": random.uniform(0, 2 * math.pi),
                "wf1": random.uniform(1.5, 4.0),
                "wf2": random.uniform(2.0, 5.0),
                "wa": random.uniform(0.02, 0.06),
                # Drift parameters
                "da": random.uniform(0.0, 0.15),
                "df": random.uniform(0.3, 1.0),
                "dp": random.uniform(0, 2 * math.pi),
                "dt": random.uniform(0, 2 * math.pi),
                "dphi": math.acos(2 * random.uniform(0, 1) - 1),
            })
        return particles

    # ── 3D rotation ─────────────────────────────────────────────────────────

    def _rotate(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        """Apply 3D rotation around Y-axis then X-axis."""
        # Y-axis rotation
        cos_y, sin_y = math.cos(self._rot_y), math.sin(self._rot_y)
        x, z = x * cos_y + z * sin_y, -x * sin_y + z * cos_y
        # X-axis rotation
        cos_x, sin_x = math.cos(self._rot_x), math.sin(self._rot_x)
        y, z = y * cos_x - z * sin_x, y * sin_x + z * cos_x
        return x, y, z

    # ── Glow sprite cache ───────────────────────────────────────────────────

    def _get_glow_sprite(self, size: int) -> QPixmap:
        """Return a cached radial-gradient glow sprite of given radius."""
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

    # ── Animation step ──────────────────────────────────────────────────────

    def _step(self):
        self._tick += 1

        # ── Energy interpolation — smooth ~250ms transition ────────────────
        self._target_energy = 1.0 if self.speaking else 0.0
        lerp_speed = 4.0
        if self._energy < self._target_energy:
            self._energy = min(self._target_energy, self._energy + lerp_speed * 0.033)
        elif self._energy > self._target_energy:
            self._energy = max(self._target_energy, self._energy - lerp_speed * 0.033)

        e = self._energy

        # ── Dynamic FPS: 30 idle, 60 when speaking ──────────────────────────
        should_be_idle = e < 0.05
        if should_be_idle != self._idle_mode:
            self._idle_mode = should_be_idle
            self._tmr.setInterval(33 if should_be_idle else 16)

        # ── 3D rotation (slower, more elegant) ─────────────────────────────────
        speed = 0.7 + e * 2.0
        self._rot_y += 0.005 * speed
        self._rot_x += 0.002 * speed

        # ── Emit burst particles when speaking ──────────────────────────────
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

        # Update burst particles
        dt = 0.033
        decay = 0.03 + e * 0.04
        self._burst_particles = [
            {**bp,
             "x": bp["x"] + bp["vx"] * dt,
             "y": bp["y"] + bp["vy"] * dt,
             "z": bp["z"] + bp["vz"] * dt,
             "life": bp["life"] - decay}
            for bp in self._burst_particles
            if bp["life"] - decay > 0
        ]

        # ── Status text blink ───────────────────────────────────────────────
        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0

        self.update()

    # ── Rendering ───────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)
        t = self._tick * 0.016  # approximate seconds

        # ── Energy state ─────────────────────────────────────────────────────
        e = self._energy

        # ── Soft ambient glow behind the sphere (very subtle) ──────────────
        glow_r = fw * 0.36
        bg_glow = QRadialGradient(cx, cy, glow_r)
        glow_intensity = 10 + int(e * 14)
        bg_glow.setColorAt(0.0, QColor(0, 175, 255, glow_intensity))
        bg_glow.setColorAt(0.4, QColor(0, 175, 255, max(0, glow_intensity - 6)))
        bg_glow.setColorAt(1.0, QColor(0, 175, 255, 0))
        p.setBrush(QBrush(bg_glow))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

        # ── Breathing / pulsing — slower, more elegant ──────────────────────
        # Calm idle
        breath_idle = 1.0 + 0.02 * math.sin(t * 0.5)
        pulse_idle  = 1.0 + 0.01 * math.sin(t * 1.0 + 1.0)
        # Energetic speaking
        breath_speak = 1.0 + 0.04 * math.sin(t * 0.8)
        pulse_speak  = 1.0 + 0.025 * math.sin(t * 1.4 + 0.5)
        # Muted override — always calm
        if self.muted:
            breath_idle = 1.0 + 0.005 * math.sin(t * 0.3)
            pulse_idle  = 1.0 + 0.003 * math.sin(t * 0.5)
            breath_speak = breath_idle
            pulse_speak  = pulse_idle

        breath = breath_idle + (breath_speak - breath_idle) * e
        pulse  = pulse_idle + (pulse_speak - pulse_idle) * e

        sphere_r = fw * 0.30 * breath * pulse

        # ── Project all particles to 2D ─────────────────────────────────────
        draw_list: list[tuple[float, float, float, float, float, float]] = []

        for pt in self._particles:
            theta = pt["theta"]
            phi   = pt["phi"]

            # Organic wave deformation — strength scales with energy (3× at full speak)
            wave_mul = 1.0 + e * 3.0
            wave = wave_mul * (pt["wa"] * math.sin(theta * pt["wf1"] + t * 0.6 + e * 2.0)
                    + pt["wa"] * 0.7 * math.sin(phi * pt["wf2"] + t * 0.8 + e * 1.5)
                    + pt["wa"] * 0.5 * math.sin((theta + phi) * 2.5 + t * 0.4))

            r = pt["r_factor"] + wave

            # Cartesian coordinates on unit sphere
            x = r * math.sin(phi) * math.cos(theta)
            y = r * math.sin(phi) * math.sin(theta)
            z = r * math.cos(phi)

            # Drift — amplitude and frequency scale with energy (bursts outward)
            drift = pt["da"] * (1.0 + e * 4.0) * math.sin(t * (pt["df"] + e * 1.5) + pt["dp"])
            if abs(drift) > 0.001:
                x += drift * math.sin(pt["dphi"]) * math.cos(pt["dt"])
                y += drift * math.sin(pt["dphi"]) * math.sin(pt["dt"])
                z += drift * math.cos(pt["dphi"])

            # 3D rotation (Y → X)
            x, y, z = self._rotate(x, y, z)

            # Perspective projection
            depth_val = z + 2.5          # shift to positive range
            persp     = 2.5 / depth_val   # focal length 2.5
            px = cx + x * sphere_r * persp
            py = cy - y * sphere_r * persp

            # Depth factor — front particles are bigger & brighter
            depth_fac = max(0.3, min(1.0, (z + 1.5) / 3.0))
            psize = pt["size"] * persp * depth_fac

            # Cull off-screen / too-small particles
            if psize < 0.4 or px < -20 or px > W + 20 or py < -20 or py > H + 20:
                continue

            draw_list.append((z, px, py, psize, depth_fac, pt["color_blend"], pt["use_glow"]))

        # Sort back-to-front
        draw_list.sort(key=lambda x: x[0])

        # ── Render particles ────────────────────────────────────────────────
        glow_boost = 1.0 + e * 0.8
        for _z, px, py, psize, depth_fac, color_blend, use_glow in draw_list:
            # Pre-compute color once per particle
            de = depth_fac
            alpha = int(200 * de * glow_boost)
            g = int((1.0 - color_blend * 0.314) * 255 * de)
            b = int((0.533 + color_blend * 0.467) * 255 * de)

            # Only ~12% of particles get expensive glow sprites
            if use_glow:
                sprite = self._get_glow_sprite(int(psize * (1.3 + e * 0.3)))
                op = de * (0.85 + e * 0.15)
                p.setOpacity(op)
                p.drawPixmap(int(px - sprite.width() * 0.5),
                             int(py - sprite.height() * 0.5), sprite)
                p.setOpacity(1.0)

            # Fast filled circle for ALL particles (core dot)
            core_sz = max(0.4, psize * (0.5 + e * 0.1))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(
                min(255, int((40 + e * 60) * de)),
                min(255, g + int(60 * (1.0 + e * 0.8))),
                min(255, b + int(60 * (1.0 + e * 0.8))),
                alpha,
            ))
            p.drawEllipse(QPointF(px, py), core_sz, core_sz)

        # ── Render burst particles (fly outward when speaking) ──────────────
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

            # Burst glow
            sprite = self._get_glow_sprite(int(bsize * 2.5))
            p.setOpacity(bp["life"] * 0.7)
            p.drawPixmap(int(bpx - sprite.width() / 2),
                         int(bpy - sprite.height() / 2), sprite)
            p.setOpacity(1.0)

            # Burst core
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(
                min(255, int(60 * bdepth)),
                min(255, bg + 80),
                min(255, bb + 80),
                balpha,
            ))
            core_sz_b = max(0.5, bsize * 0.5)
            p.drawEllipse(QPointF(bpx, bpy), core_sz_b, core_sz_b)

        # ── Status text with subtle glass pill ───────────────────────────────
        sy = cy + fw * 0.40
        if self.muted:
            txt, col = "MUTED",     qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "SPEAKING",  qcol(C.ACC)
        elif self.state == "THINKING":
            txt, col = f"THINKING",   qcol(C.ACC2)
        elif self.state == "PROCESSING":
            txt, col = "PROCESSING", qcol(C.ACC2)
        elif self.state == "LISTENING":
            txt, col = f"LISTENING",  qcol(C.GREEN)
        else:
            txt, col = self.state, qcol(C.PRI)

        # Glass pill background for status
        pill_w = len(txt) * 9 + 32
        pill_h = 22
        pill_x = (W - pill_w) / 2
        pill_y = sy
        p.setBrush(QBrush(qcol(C.PANEL2, 160)))
        p.setPen(QPen(qcol(col, 60), 0.5))
        p.drawRoundedRect(QRectF(pill_x, pill_y, pill_w, pill_h), 11, 11)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        p.drawText(QRectF(pill_x, pill_y, pill_w, pill_h), Qt.AlignmentFlag.AlignCenter, txt)

        # ── Waveform ────────────────────────────────────────────────────────
        wy = sy + 30
        N, bw = 36, 8
        wx0 = (W - N * bw) / 2
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(C.MUTED_C)
            elif self.speaking:
                hgt = random.randint(3, 20)
                cl  = qcol(C.PRI) if hgt > 12 else qcol(C.PRI_DIM)
            else:
                hgt = int(3 + 2 * math.sin(self._tick * 0.09 + i * 0.6))
                cl  = qcol(C.BORDER_B)
            p.fillRect(QRectF(wx0 + i * bw, wy + 20 - hgt, bw - 1, hgt), cl)

class MetricBar(QWidget):
    """Premium glass card system metric — icon, value, animated progress bar."""

    _ICONS = {
        "CPU": "C", "MEM": "M", "NET": "N", "GPU": "G",
        "TMP": "T", "MIC": "V", "AI": "A",
    }

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0
        self._text  = "--"
        self._tick  = 0
        self._history: list[float] = []  # sparkline data
        self.setFixedHeight(60)
        self.setMinimumWidth(120)

        self._anim = QTimer(self)
        self._anim.timeout.connect(lambda: (setattr(self, '_tick', self._tick + 1), self.update()))
        self._anim.start(50)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text  = text
        self._history.append(pct)
        if len(self._history) > 30:
            self._history.pop(0)
        if not self._anim.isActive():
            self._anim.start()

    def _bar_color(self) -> QColor:
        """Return color for the progress bar based on value."""
        if self._value > 90:
            return qcol(C.ERROR)
        elif self._value > 75:
            return qcol(C.WARN)
        return qcol(self._color)

    def _border_color(self) -> QColor:
        """Return border color — glows brighter when busy."""
        if self._value > 0:
            return qcol(C.BORDER, 60)
        return qcol(C.BORDER, 20)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        R = 14

        # ── Ultra-subtle background ────────────────────────────────────────────
        p.setBrush(QBrush(qcol(C.PANEL2, 140)))
        p.setPen(QPen(qcol(C.BORDER, 6), 0.3))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), R, R)

        # ── Icon circle ─────────────────────────────────────────────────────
        icon = MetricBar._ICONS.get(self._label, ":")
        icon_r = 8
        cx_icon, cy_icon = 17, 18
        p.setBrush(QBrush(qcol(self._color, 10)))
        p.setPen(QPen(qcol(self._color, 16), 0.3))
        p.drawEllipse(QPointF(cx_icon, cy_icon), icon_r, icon_r)
        p.setFont(QFont("Segoe UI", 6, QFont.Weight.DemiBold))
        p.setPen(QPen(qcol(self._color, 90), 1))
        p.drawText(QRectF(cx_icon - 10, cy_icon - 10, 20, 20),
                   Qt.AlignmentFlag.AlignCenter, icon)

        # ── Label ───────────────────────────────────────────────────────────
        p.setFont(QFont("Segoe UI", 6))
        p.setPen(QPen(qcol(C.TEXT_DIM, 140), 1))
        p.drawText(QRectF(32, 3, W - 48, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        # ── Value (hero number) ────────────────────────────────────────────
        p.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        val_col = self._bar_color()
        p.setPen(QPen(val_col, 1))
        p.drawText(QRectF(0, 5, W - 12, 24),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

        # ── Ultra-thin progress line ────────────────────────────────────────
        bar_h, bar_r = 1.5, 1
        bar_y  = H - bar_h - 8
        bar_w  = W - 20
        bar_x  = 10
        fill_w = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.PANEL, 100)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), bar_r, bar_r)

        if fill_w > 0:
            grad = QLinearGradient(bar_x, 0, bar_x + fill_w, 0)
            grad.setColorAt(0.0, qcol(self._color, 200))
            grad.setColorAt(1.0, val_col)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), bar_r, bar_r)

        # ── Sparkline (very subtle line only) ──────────────────────────────
        if len(self._history) > 2:
            sp_x, sp_y = 10, 30
            sp_w = W - 20
            sp_h = 10
            p.save()
            p.setClipRect(sp_x, sp_y, sp_w, sp_h)
            path = QPainterPath()
            n = len(self._history)
            for i, v in enumerate(self._history):
                px = sp_x + (i / max(n - 1, 1)) * sp_w
                py = sp_y + sp_h - (v / 100.0) * sp_h
                if i == 0:
                    path.moveTo(px, py)
                else:
                    path.lineTo(px, py)
            p.setPen(QPen(qcol(self._color, 25), 0.8))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            p.restore()

        # ── Tiny pulse dot ───────────────────────────────────────────────
        pulse_alpha = max(30, int(100 + 60 * math.sin(self._tick * 0.08)))
        alive_col = qcol(C.SUCCESS if self._value > 0 else C.TEXT_DIM, pulse_alpha)
        p.setBrush(QBrush(alive_col))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(W - 11, H - 8), 1.5, 1.5)

class LogWidget(QTextEdit):
    """Premium chat log with glass bubbles, refined timestamps, and smooth scrolling."""
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Segoe UI", 9))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {C.TEXT};
                border: none;
                padding: 6px 4px;
                selection-background-color: rgba(0, 24, 40, 0.5);
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 1px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(255, 255, 255, 0.06);
                border-radius: 1px;
                min-height: 20px;
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
        self._current_bubble_html = ""
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

        # Insert the bubble structure immediately (empty text, fills via typewriter)
        self._insert_bubble()
        self._tmr.start(14)

    def _insert_bubble(self):
        """Insert a chat bubble HTML with empty text to be filled by typewriter."""
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M")

        if self._tag == "you":
            bubble_bg  = "rgba(255, 255, 255, 0.04)"
            border_c   = "rgba(255, 255, 255, 0.06)"
            text_c     = "#FFFFFF"
            align      = "right"
        elif self._tag == "ai":
            bubble_bg  = "rgba(0, 175, 255, 0.05)"
            border_c   = "rgba(0, 175, 255, 0.10)"
            text_c     = "#B5D5FF"
            align      = "left"
        else:
            # System-style message — outline text icons, no emoji
            c_map = {"file": "#00AFFF", "err": "#FF6B6B", "sys": "#7A7A7A"}
            i_map = {"file": "[F] ", "err": "[!] ", "sys": "[·] "}
            cc = c_map.get(self._tag, "#7A7A7A")
            ii = i_map.get(self._tag, "[·] ")
            clean = self._escape(self._text)
            html = f'<div style="color:{cc};font-size:7px;padding:2px 8px;text-align:center;letter-spacing:0.3px;">{ii}{clean}</div>'
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertHtml(html + "<br>")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            # Skip typewriter for system messages
            self._pos = len(self._text)
            self._tmr.stop()
            QTimer.singleShot(10, self._next)
            return

        clean = self._escape(self._text)
        for pfx in ["You: ", "IRA: ", "you: ", "ira: "]:
            if clean.startswith(pfx):
                clean = clean[len(pfx):]
                break

        # Build bubble HTML with a placeholder span for typewriter
        self._current_bubble_html = f'''
        <div style="margin:3px 4px;text-align:{align};">
            <div style="display:inline-block;max-width:86%;
                        background:{bubble_bg};border:0.5px solid {border_c};
                        border-radius:12px;padding:5px 10px;text-align:left;">
                <span style="color:{text_c};font-size:8px;line-height:1.5;letter-spacing:0.1px;"><!--TW--></span>
                <div style="color:rgba(85,85,85,0.5);font-size:5.5px;text-align:right;margin-top:4px;letter-spacing:0.3px;">{ts}</div>
            </div>
        </div>'''

        # Insert the empty bubble
        cur = self.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        # Use a marker that we'll replace character by character
        cur.insertHtml(self._current_bubble_html.replace("<!--TW-->", ""))
        self.setTextCursor(cur)
        self.ensureCursorVisible()

        # Find the position of the span we just inserted to track where to append
        doc = self.document()
        end_pos = doc.characterCount() - 1
        self._type_pos = end_pos

    def _escape(self, text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _step(self):
        if self._pos < len(self._text):
            ch = self._text[self._pos]
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)

            col_map = {
                "you": qcol(C.CHAT_USER),
                "ai":  qcol(C.CHAT_AI),
            }
            fmt = cur.charFormat()
            fmt.setForeground(QBrush(col_map.get(self._tag, qcol(C.TEXT))))
            cur.insertText(ch, fmt)

            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("[IMG]", "#00d4ff"), "video":   ("[VID]", "#ff6b00"),
    "audio":   ("[AUD]", "#cc44ff"), "pdf":     ("[PDF]", "#ff4444"),
    "word":    ("[DOC]", "#4488ff"), "excel":   ("[XLS]", "#44bb44"),
    "code":    ("[COD]", "#ffcc00"), "archive": ("[ARC]", "#ff8844"),
    "pptx":    ("[PPT]", "#ff6622"), "text":    ("[TXT]", "#aaaaaa"),
    "data":    ("[DAT]", "#88ddff"), "unknown": ("[FIL]", "#888888"),
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
        self.setFixedHeight(90)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.8) % 20
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
        R    = 14
        pad  = 4
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        # Glass background
        if z._drag_over:
            bg_col = qcol(C.BLUE, 8)
        elif z._hovering:
            bg_col = qcol(C.PANEL2, 120)
        else:
            bg_col = qcol(C.PANEL2, 100)
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, R, R)

        # Border — ultra-thin
        if z._current_file:   border_col = qcol(C.GREEN, 60)
        elif z._drag_over:    border_col = qcol(C.BLUE, 80)
        elif z._hovering:     border_col = qcol(C.BLUE, 40)
        else:                 border_col = qcol(C.BORDER, 12)

        pen = QPen(border_col, 0.8, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, R, R)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.TEXT_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 1.2)); p.setBrush(Qt.BrushStyle.NoBrush)
        # Upload icon (simple arrow) — smaller, cleaner
        arrow_s = 8
        p.drawLine(QPointF(cx, cy - arrow_s), QPointF(cx, cy + 3))
        p.drawLine(QPointF(cx - 5, cy - 3), QPointF(cx, cy - arrow_s))
        p.drawLine(QPointF(cx + 5, cy - 3), QPointF(cx, cy - arrow_s))
        p.drawLine(QPointF(cx - 9, cy + 3), QPointF(cx + 9, cy + 3))
        p.setFont(QFont("Segoe UI", 7))
        p.setPen(QPen(qcol(C.TEXT_MED if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 10, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Drop file or click to browse")
        p.setFont(QFont("Segoe UI", 6))
        p.setPen(QPen(qcol(C.TEXT_DIM, 120), 1))
        p.drawText(QRectF(0, cy + 26, W, 12), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 30, W, 36), Qt.AlignmentFlag.AlignCenter, "DROP")
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 50
        p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        p.setPen(QPen(qcol(icon_col, 180), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 4
        tw = W - tx - 34

        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        p.setPen(QPen(qcol(C.WHITE, 200), 1))
        name = path.name if len(path.name) <= 30 else path.name[:27] + "..."
        p.drawText(QRectF(tx, H * 0.16, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Segoe UI", 6))
        p.setPen(QPen(qcol(C.TEXT_DIM, 160), 1))
        p.drawText(QRectF(tx, H * 0.16 + 16, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        # Remove button
        p.setFont(QFont("Segoe UI", 9))
        p.setPen(QPen(qcol(C.RED, 120), 1))
        p.drawText(QRectF(W - 30, 0, 24, H), Qt.AlignmentFlag.AlignCenter, "[x]")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(7, 7, 7, 230);
                border: 0.5px solid rgba(0, 175, 255, 0.12);
                border-radius: 20px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(10)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Segoe UI", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
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
        self._key_input.setFont(QFont("Segoe UI", 10))
        self._key_input.setFixedHeight(36)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(16, 16, 16, 0.8);
                color: {C.TEXT};
                border: 1px solid rgba(0, 175, 255, 0.15);
                border-radius: 10px;
                padding: 4px 12px;
            }}
            QLineEdit:focus {{ border: 1px solid rgba(0, 175, 255, 0.4); }}
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
            btn.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            btn.setFixedHeight(34)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(0, 175, 255, 0.06);
                color: {C.PRI};
                border: 0.5px solid rgba(0, 175, 255, 0.12);
                border-radius: 10px;
                letter-spacing: 0.5px;
            }}
            QPushButton:hover {{
                background: rgba(0, 175, 255, 0.10);
                border: 0.5px solid rgba(0, 175, 255, 0.25);
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"rgba(0,175,255,0.15)"),"mac":(C.SEC,"rgba(0,255,136,0.15)"),"linux":(C.SEC,"rgba(0,255,136,0.15)")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {bg}; color: {fg};
                        border: 1px solid {fg}; border-radius: 8px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(16, 16, 16, 0.5);
                        color: {C.TEXT_DIM};
                        border: 1px solid rgba(0, 175, 255, 0.08);
                        border-radius: 8px;
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


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()

    _OW, _OH = 400, 465

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(7, 7, 7, 230);
                border: 0.5px solid rgba(0, 175, 255, 0.12);
                border-radius: 20px;
            }}
        """)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(6)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Segoe UI", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
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
        self._key_lbl.setFont(QFont("Segoe UI", 28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.PRI};
            background: rgba(18, 18, 18, 0.4);
            border: 0.5px solid rgba(0, 175, 255, 0.10);
            border-radius: 12px;
            padding: 6px 4px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Segoe UI", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(30)
        new_btn.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(18, 18, 18, 0.4);
                color: {C.PRI};
                border: 0.5px solid rgba(255, 255, 255, 0.04);
                border-radius: 8px;
                letter-spacing: 0.5px;
            }}
            QPushButton:hover {{
                background: rgba(0, 175, 255, 0.06);
                border: 0.5px solid rgba(0, 175, 255, 0.15);
            }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 0.5px solid rgba(255, 255, 255, 0.04); border-radius: 8px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 0.5px solid rgba(0, 175, 255, 0.12); }}
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
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
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
                    background: rgba(18, 18, 18, 0.4);
                    border: 0.5px solid rgba(0, 175, 255, 0.15);
                    border-radius: 12px;
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


class MainWindow(QMainWindow):
    _log_sig     = pyqtSignal(str)
    _state_sig   = pyqtSignal(str)
    _content_sig = pyqtSignal(str, str)   # (title, text) — thread-safe content display

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
        self._gesture_active  = False

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        # Ambient glow overlay (behind all panels)
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

        # Center column: HUD on top + content panel below
        _center = QWidget()
        _center.setStyleSheet(f"background: {C.BG};")
        _center_lay = QVBoxLayout(_center)
        _center_lay.setContentsMargins(0, 0, 0, 0)
        _center_lay.setSpacing(0)
        self.hud = HudCanvas(face_path)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        _center_lay.addWidget(self.hud, stretch=1)
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

        # Metrik güncelleme timer'ı
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
            ow, oh = 460, 390
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

    def _update_metrics(self):
        snap = _metrics.snapshot()

        # CPU
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")

        # MEM
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)  # 10 MB/s = %100
        self._bar_net.set_value(net_pct, net_str)

        # GPU
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")

        # TMP
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")

        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("PROC  --")


    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(48)
        w.setStyleSheet(f"background: rgba(5, 5, 5, 0.9);")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(0)

        # ── Left: IRA brand ─────────────────────────────────────────────────
        brand = QHBoxLayout(); brand.setSpacing(6)

        title = QLabel("IRA")
        title.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.BLUE}; background: transparent; letter-spacing: 1px;")
        brand.addWidget(title)

        ver = QLabel("v1")
        ver.setFont(QFont("Segoe UI", 5))
        ver.setStyleSheet(f"color: rgba(85, 85, 85, 0.6); background: transparent; padding-top: 3px;")
        brand.addWidget(ver)

        status = QLabel("ONLINE")
        status.setFont(QFont("Segoe UI", 6))
        status.setStyleSheet(f"color: rgba(0, 255, 136, 0.7); background: transparent;")
        brand.addWidget(status)

        lay.addLayout(brand)
        lay.addSpacing(20)

        # ── Center: model info pills ────────────────────────────────────────
        def _pill(txt, color=C.TEXT_MED):
            l = QLabel(txt)
            l.setFont(QFont("Segoe UI", 5))
            l.setStyleSheet(f"color: {color}; background: transparent; border: none; padding: 1px 6px; letter-spacing: 0.3px;")
            return l

        info_row = QHBoxLayout(); info_row.setSpacing(6)
        info_row.addStretch()
        info_row.addWidget(_pill("Gemini 2.5 Flash", C.PRI))
        info_row.addWidget(_pill("12ms", C.TEXT_MED))
        info_row.addWidget(_pill("2.1GB", C.TEXT_MED))
        info_row.addWidget(_pill("Iris", C.TEXT_MED))
        info_row.addStretch()
        lay.addLayout(info_row)
        lay.addStretch()

        # ── Right: clock + date ─────────────────────────────────────────────
        right_col = QVBoxLayout(); right_col.setSpacing(-1)
        self._clock_lbl = QLabel("00:00")
        self._clock_lbl.setFont(QFont("Segoe UI", 14, QFont.Weight.DemiBold))
        self._clock_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent; letter-spacing: 0.5px;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Segoe UI", 5))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        lay.addLayout(right_col)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M"))
        self._date_lbl.setText(time.strftime("%a %d %b"))

    def _make_feature_btn(self, text: str, command: str,
                          color: str = None, bg: str = None) -> QPushButton:
        """Ultra-minimal button — invisible glass until hovered."""
        if color is None: color = C.BLUE
        btn = QPushButton(text)
        btn.setFixedHeight(22)
        btn.setFont(QFont("Segoe UI", 6))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {C.TEXT_DIM};
                border: none;
                border-radius: 6px;
                padding: 0 6px;
                text-align: left;
            }}
            QPushButton:hover {{
                background: rgba(0, 175, 255, 0.06);
                color: {C.BLUE};
            }}
            QPushButton:pressed {{
                background: rgba(0, 175, 255, 0.10);
                color: {C.BLUE};
            }}
        """)
        btn.clicked.connect(lambda: self._on_feature_command(command))
        return btn

    def _on_feature_command(self, command: str):
        """Send a feature button command to IRA via text command or special handler."""
        self._log.append_log(f"CMD: {command}")

        # Handle special control commands
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

        # Regular text command
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(command,), daemon=True).start()

    def _add_btn_group(self, layout, title: str, color: str, buttons: list):
        """Add a group header + buttons. Clean minimal headers."""
        hdr = QLabel(f"{title}")
        hdr.setFont(QFont("Segoe UI", 5))
        hdr.setStyleSheet(f"color: rgba(85, 85, 85, 0.7); background: transparent; padding: 6px 4px 1px 4px; letter-spacing: 0.6px;")
        layout.addWidget(hdr)
        for text, command in buttons:
            layout.addWidget(self._make_feature_btn(text, command, color))
        layout.addSpacing(2)

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: transparent; border-right: 0.5px solid rgba(255, 255, 255, 0.02);")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(8, 8, 8, 0)
        outer.setSpacing(6)

        # ── Top: glass system monitor ──────────────────────────────────────────
        top = QWidget()
        top.setStyleSheet("background: transparent;")
        top_lay = QVBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(6)

        self._bar_cpu = MetricBar("CPU", C.BLUE)
        self._bar_mem = MetricBar("MEM", C.GREEN)
        self._bar_net = MetricBar("NET", C.GREEN)
        self._bar_gpu = MetricBar("GPU", C.BLUE)
        self._bar_tmp = MetricBar("TMP", C.WARN)
        for bar in [self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp]:
            top_lay.addWidget(bar)

        # ── Info card (uptime, procs, OS) ──────────────────────────────────────
        ip = QWidget()
        ip.setStyleSheet(f"background: rgba(14, 14, 14, 0.4); border-radius: 12px;")
        ip_ly = QVBoxLayout(ip)
        ip_ly.setContentsMargins(10, 8, 10, 8)
        ip_ly.setSpacing(2)
        self._uptime_lbl = QLabel("UP  --:--")
        self._uptime_lbl.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none; letter-spacing: 0.3px;")
        ip_ly.addWidget(self._uptime_lbl)
        self._proc_lbl = QLabel("PROC  --")
        self._proc_lbl.setFont(QFont("Segoe UI", 6))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        ip_ly.addWidget(self._proc_lbl)
        os_name = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS  {os_name}")
        os_lbl.setFont(QFont("Segoe UI", 6))
        os_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        ip_ly.addWidget(os_lbl)
        top_lay.addWidget(ip)
        outer.addWidget(top)

        # ── Middle: scrollable feature buttons ────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: transparent; width: 2px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(0, 175, 255, 0.12); border-radius: 1px; min-height: 12px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)

        btn_container = QWidget()
        btn_container.setStyleSheet("background: transparent;")
        btn_lay = QVBoxLayout(btn_container)
        btn_lay.setContentsMargins(2, 4, 2, 4)
        btn_lay.setSpacing(0)

        # ── Feature button groups ─────────────────────────────────────────────
        self._add_btn_group(btn_lay, "CONTROLS", C.ERROR, [
            ("STOP", "[STOP]"),
            ("MUTE", "[TOGGLE_MUTE]"),
            ("SEE SCREEN", "Use screen_process to look at my screen and tell me what you see"),
            ("LIVE SCREEN", "[LIVE_SCREEN]"),
            ("GESTURES", "[TOGGLE_GESTURE]"),
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
            ("Smart Home", "Control smart home"),
            ("Self Check", "Run a self evaluation"),
            ("Provider Status", "Check AI provider status"),
        ])

        btn_lay.addStretch()
        scroll.setWidget(btn_container)
        outer.addWidget(scroll, stretch=1)

        # ── Bottom: minimal STOP button ──────────────────────────────────────────
        bottom = QWidget()
        bottom.setStyleSheet("background: transparent;")
        btm_lay = QVBoxLayout(bottom)
        btm_lay.setContentsMargins(0, 4, 0, 8)
        btm_lay.setSpacing(0)

        self._interrupt_btn = QPushButton("STOP")
        self._interrupt_btn.setFixedHeight(26)
        self._interrupt_btn.setFont(QFont("Segoe UI", 6))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: rgba(204, 68, 68, 0.5);
                border: none;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                background: rgba(204, 68, 68, 0.08);
                color: {C.ERROR};
            }}
            QPushButton:pressed {{
                background: rgba(204, 68, 68, 0.15);
            }}
        """)
        self._interrupt_btn.clicked.connect(self._on_interrupt)
        btm_lay.addWidget(self._interrupt_btn)

        outer.addWidget(bottom)
        return w
    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"background: transparent; border-left: 0.5px solid rgba(255, 255, 255, 0.02);")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        def _sec(txt):
            l = QLabel(f"{txt}")
            l.setFont(QFont("Segoe UI", 5))
            l.setStyleSheet(f"color: rgba(85, 85, 85, 0.7); background: transparent; letter-spacing: 0.6px; padding: 0 2px;")
            return l

        lay.addWidget(_sec("ACTIVITY LOG"))
        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        # ── Minimal separator ──────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("border: none; border-top: 0.5px solid rgba(255, 255, 255, 0.03); margin: 0;")
        lay.addWidget(sep)

        lay.addWidget(_sec("FILE UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded")
        self._file_hint.setFont(QFont("Segoe UI", 6))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; padding: 0 2px;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("border: none; border-top: 0.5px solid rgba(255, 255, 255, 0.03); margin: 0;")
        lay.addWidget(sep2)

        lay.addWidget(_sec("COMMAND INPUT"))
        lay.addLayout(self._build_input_row())

        # ── Minimal action buttons ─────────────────────────────────────────
        self._mute_btn = QPushButton("MICROPHONE ACTIVE")
        self._mute_btn.setFixedHeight(28)
        self._mute_btn.setFont(QFont("Segoe UI", 6))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        screen_btn = QPushButton("SEE SCREEN")
        screen_btn.setFixedHeight(28)
        screen_btn.setFont(QFont("Segoe UI", 6))
        screen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        screen_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: none; border-radius: 8px;
            }}
            QPushButton:hover {{
                background: rgba(0, 175, 255, 0.06); color: {C.BLUE};
            }}
            QPushButton:pressed {{
                background: rgba(0, 175, 255, 0.10);
            }}
        """)
        screen_btn.clicked.connect(self._on_see_screen)
        lay.addWidget(screen_btn)

        remote_btn = QPushButton("REMOTE CONTROL")
        remote_btn.setFixedHeight(28)
        remote_btn.setFont(QFont("Segoe UI", 6))
        remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: none; border-radius: 8px;
            }}
            QPushButton:hover {{
                background: rgba(0, 175, 255, 0.06); color: {C.BLUE};
            }}
            QPushButton:pressed {{
                background: rgba(0, 175, 255, 0.10);
            }}
        """)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)

        fs_btn = QPushButton("FULLSCREEN [F11]")
        fs_btn.setFixedHeight(22)
        fs_btn.setFont(QFont("Segoe UI", 5))
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: rgba(85, 85, 85, 0.5);
                border: none; border-radius: 6px;
            }}
            QPushButton:hover {{
                color: {C.TEXT_MED};
            }}
        """)
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
        import time
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

        # Bring IRA back exactly as it was (preserves fullscreen/maximized).
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
            # Fallback if no direct handler is wired.
            threading.Thread(
                target=self.on_text_command,
                args=("Use screen_process to look at my screen and tell me what "
                      "you see, in English, addressing me as Yuvan.",),
                daemon=True,
            ).start()

    def _on_interrupt(self):
        """Interrupt IRA's current speech."""
        self._log.append_log("SYS: Interrupt requested")
        if self.on_interrupt:
            threading.Thread(target=self.on_interrupt, daemon=True).start()

    def _on_live_screen(self):
        """Live screen: minimize IRA, show floating capture button on desktop."""
        from actions.screen_overlay import FloatingCaptureButton

        def on_capture(image_bytes, mime):
            """Called when floating button is clicked - sends full screen capture to IRA."""
            if image_bytes and self.on_live_screen:
                threading.Thread(
                    target=self.on_live_screen,
                    args=(image_bytes, mime),
                    daemon=True,
                ).start()

        # Show floating button on the desktop
        FloatingCaptureButton.show_snap_button(on_capture=on_capture)
        self._log.append_log("SYS: Snap button shown — click it to capture full screen")

        # Minimize IRA
        self.showMinimized()

    def _on_hand_gesture(self):
        """Toggle hand gesture control on/off."""
        self._gesture_active = not self._gesture_active
        if self._gesture_active:
            self._log.append_log("SYS: Hand gesture control starting…")
        else:
            self._log.append_log("SYS: Hand gesture control stopped")
        if self.on_hand_gesture:
            threading.Thread(target=self.on_hand_gesture, daemon=True).start()

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(4)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(QFont("Segoe UI", 7))
        self._input.setFixedHeight(28)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(14, 14, 14, 0.5);
                color: {C.TEXT};
                border: none;
                border-radius: 8px;
                padding: 2px 8px;
            }}
            QLineEdit:focus {{
                background: rgba(14, 14, 14, 0.7);
            }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("→")
        send.setFixedSize(28, 28)
        send.setFont(QFont("Segoe UI", 11, QFont.Weight.Light))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {C.TEXT_DIM};
                border: none; border-radius: 8px;
            }}
            QPushButton:hover {{
                background: rgba(0, 175, 255, 0.08);
                color: {C.BLUE};
            }}
            QPushButton:pressed {{
                background: rgba(0, 175, 255, 0.15);
            }}
        """)
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
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: rgba(12, 12, 12, 0.4);
                border-top: 0.5px solid rgba(0, 175, 255, 0.05);
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 10, 16, 12)
        lay.setSpacing(6)

        # ── header row ───────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(6)

        dot = QLabel(":")
        dot.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.BLUE}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 0.8px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont("Segoe UI", 6))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("x")
        dismiss.setFont(QFont("Segoe UI", 7))
        dismiss.setFixedHeight(18)
        dismiss.setFixedWidth(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 0.5px solid rgba(255, 255, 255, 0.04); border-radius: 9px;
            }}
            QPushButton:hover {{ color: {C.TEXT_MED}; border-color: rgba(0, 175, 255, 0.15); }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("border: none; border-top: 0.5px solid rgba(0, 175, 255, 0.06);")
        lay.addWidget(sep)

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(QFont("Segoe UI", 8))
        self._content_display.setFixedHeight(140)
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(7, 7, 7, 0.3);
                color: {C.TEXT};
                border: 0.5px solid rgba(0, 175, 255, 0.05);
                border-radius: 10px;
                padding: 8px 10px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 4px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(0, 175, 255, 0.12); border-radius: 2px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        # Scroll to top
        cur = self._content_display.textCursor()
        cur.moveToStart() if hasattr(cur, "moveToStart") else None
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        self._content_panel.show()

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(20)
        w.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(w); lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(8)

        def _pill(txt, color=None):
            if color is None: color = C.TEXT_DIM
            l = QLabel(txt)
            l.setFont(QFont("Segoe UI", 5))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_pill("F4 Mute", C.TEXT_DIM))
        lay.addWidget(_pill("F11 Fullscreen", C.TEXT_DIM))
        lay.addStretch()
        lay.addWidget(_pill("Active", C.GREEN))
        lay.addWidget(_pill("12ms", C.BLUE))
        lay.addStretch()
        ver = QLabel("IRA v1")
        ver.setFont(QFont("Segoe UI", 5))
        ver.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addWidget(ver)
        return w

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell IRA what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

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
            self._mute_btn.setText("MICROPHONE MUTED")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: rgba(204, 68, 68, 0.5);
                    border: none; border-radius: 8px;
                }}
                QPushButton:hover {{
                    background: rgba(204, 68, 68, 0.08);
                    color: {C.ERROR};
                }}
            """)
        else:
            self._mute_btn.setText("MICROPHONE ACTIVE")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: rgba(0, 255, 136, 0.5);
                    border: none; border-radius: 8px;
                }}
                QPushButton:hover {{
                    background: rgba(0, 255, 136, 0.06);
                    color: {C.GREEN};
                }}
            """)

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

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
        ow, oh = 460, 390
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