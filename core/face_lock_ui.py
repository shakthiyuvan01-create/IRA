"""Face-scan lock screen shown over IRA's main window right after it opens.

First launch : scans and enrolls the owner's face.
Later launches: scans and unlocks only for the enrolled face.
Small blue dot (bottom-right): opens the password fallback.
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from core import face_lock as fl

_BLUE = QColor("#00AFFF")
_GREEN = QColor("#00FF88")
_RED = QColor("#E05555")
_DIM = "#7A7F87"

_MAX_PASSWORD_TRIES = 5
_LOCKOUT_SECONDS = 30
_VERIFY_STREAK = 3          # consecutive matching frames needed to unlock
_ENROLL_INTERVAL = 0.25     # seconds between enrollment samples
_MIN_FACE_FRACTION = 0.16   # face width / frame width, so it's close enough to read


def _font(px: int, bold: bool = False) -> QFont:
    f = QFont("Segoe UI")
    f.setPixelSize(px)
    f.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
    return f


# ── Camera / recognition worker ──────────────────────────────────────────────

class _ScanWorker(QThread):
    frame = pyqtSignal(QImage)
    status = pyqtSignal(str)
    progress = pyqtSignal(float)        # 0..1 during enrollment
    face_seen = pyqtSignal(bool)
    done = pyqtSignal(str)              # "enrolled" | "matched"
    failed = pyqtSignal(str)

    def __init__(self, enroll: bool, parent=None):
        super().__init__(parent)
        self._enroll = enroll
        self._stop = False

    def stop(self):
        self._stop = True
        self.wait(3000)

    def run(self):
        import cv2

        try:
            self.status.emit("Preparing face scan…")
            engine = fl.FaceEngine(fl.ensure_models(progress=self.status.emit))
        except Exception as e:
            self.failed.emit(str(e))
            return

        enrolled = None
        if not self._enroll:
            enrolled = fl.load_enrollment()
            if enrolled is None or len(enrolled) == 0:
                self._enroll = True

        while not self._stop:
            index, backend = fl.camera_source()
            cap = cv2.VideoCapture(index, backend)
            if not cap.isOpened():
                cap.release()
                self.failed.emit("Camera unavailable. Tap the blue dot to use your password.")
                for _ in range(30):
                    if self._stop:
                        return
                    time.sleep(0.1)
                continue
            try:
                self._loop(cap, engine, enrolled)
            finally:
                cap.release()
            return

    def _loop(self, cap, engine, enrolled):
        import cv2

        samples: list = []
        last_sample = 0.0
        streak = 0
        misses = 0
        self.status.emit("Look at the camera to enroll your face" if self._enroll
                         else "Scanning face…")
        while not self._stop:
            ok, frame = cap.read()
            if not ok or frame is None:
                misses += 1
                if misses > 30:
                    self.failed.emit("Camera stopped sending frames. Tap the blue dot to use your password.")
                    return
                time.sleep(0.03)
                continue
            misses = 0
            frame = cv2.flip(frame, 1)   # mirror, so it feels like a mirror
            self._emit_preview(frame)

            face, count = engine.detect(frame)
            self.face_seen.emit(face is not None)
            if face is None:
                streak = 0
                continue

            if self._enroll:
                if count > 1:
                    self.status.emit("Only you in frame, please")
                    continue
                if face[2] < frame.shape[1] * _MIN_FACE_FRACTION:
                    self.status.emit("Move a little closer")
                    continue
                now = time.monotonic()
                if now - last_sample < _ENROLL_INTERVAL:
                    continue
                last_sample = now
                samples.append(engine.embed(frame, face))
                self.progress.emit(len(samples) / fl.ENROLL_SAMPLES)
                self.status.emit("Hold still… turn your head slightly" if len(samples) < fl.ENROLL_SAMPLES
                                 else "Face recorded")
                if len(samples) >= fl.ENROLL_SAMPLES:
                    fl.save_enrollment(samples)
                    self.done.emit("enrolled")
                    return
            else:
                score = engine.similarity(engine.embed(frame, face), enrolled)
                if score >= fl.MATCH_THRESHOLD:
                    streak += 1
                    self.status.emit("Verifying…")
                    if streak >= _VERIFY_STREAK:
                        self.done.emit("matched")
                        return
                else:
                    streak = 0
                    self.status.emit("Face not recognised")

    def _emit_preview(self, frame):
        import cv2

        h, w = frame.shape[:2]
        side = min(h, w)
        y0, x0 = (h - side) // 2, (w - side) // 2
        sq = cv2.resize(frame[y0:y0 + side, x0:x0 + side], (320, 320))
        rgb = cv2.cvtColor(sq, cv2.COLOR_BGR2RGB)
        img = QImage(rgb.data, 320, 320, 320 * 3, QImage.Format.Format_RGB888).copy()
        self.frame.emit(img)


# ── Circular camera view ─────────────────────────────────────────────────────

class _ScanCircle(QWidget):
    SIZE = 260

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE + 24, self.SIZE + 24)
        self._img: QImage | None = None
        self._color = _BLUE
        self._progress = 0.0
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30)

    def _tick(self):
        self._phase = (self._phase + 4) % 360
        self.update()

    def set_image(self, img: QImage):
        self._img = img

    def set_color(self, c: QColor):
        self._color = c

    def set_progress(self, p: float):
        self._progress = max(0.0, min(1.0, p))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(12, 12, self.SIZE, self.SIZE)

        clip = QPainterPath()
        clip.addEllipse(r)
        p.save()
        p.setClipPath(clip)
        if self._img is not None:
            p.drawImage(r, self._img)
        else:
            p.fillRect(r, QColor(16, 18, 20))
        p.restore()

        # faint full ring + spinning arc (or enrollment progress arc)
        ring = QColor(self._color)
        ring.setAlpha(60)
        p.setPen(QPen(ring, 3))
        p.drawEllipse(r.adjusted(-6, -6, 6, 6))
        p.setPen(QPen(self._color, 4, cap=Qt.PenCapStyle.RoundCap))
        arc_r = r.adjusted(-6, -6, 6, 6)
        if self._progress > 0:
            p.drawArc(arc_r, 90 * 16, int(-360 * 16 * self._progress))
        else:
            p.drawArc(arc_r, int(-self._phase * 16), 70 * 16)
        p.end()


# ── Lock overlay ─────────────────────────────────────────────────────────────

class FaceLockOverlay(QWidget):
    """Covers the whole main window until the owner's face or password is verified."""

    unlocked = pyqtSignal(str)   # "face" | "enrolled" | "password"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("FaceLockOverlay")
        self.setStyleSheet("#FaceLockOverlay { background: rgb(5, 5, 5); }")

        fl.ensure_password_seeded()
        self._enroll = not fl.is_enrolled()
        self._worker: _ScanWorker | None = None
        self._fails = 0
        self._locked_until = 0.0
        self._finished = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addStretch(1)

        self._title = QLabel("FIRST-TIME SETUP · ENROLL YOUR FACE" if self._enroll else "FACE ID")
        self._title.setFont(_font(15, bold=True))
        self._title.setStyleSheet("color: #FFFFFF; letter-spacing: 2px; background: transparent;")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._title)
        lay.addSpacing(18)

        self._circle = _ScanCircle(self)
        lay.addWidget(self._circle, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(16)

        self._status = QLabel("Starting camera…")
        self._status.setFont(_font(13))
        self._status.setStyleSheet(f"color: {_DIM}; background: transparent;")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._status)
        lay.addSpacing(14)

        self._pw = QLineEdit(self)
        self._pw.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw.setPlaceholderText("Password")
        self._pw.setFont(_font(13))
        self._pw.setFixedSize(260, 38)
        self._pw.setStyleSheet("""
            QLineEdit { background: rgba(16,18,20,0.9); color: #FFFFFF;
                        border: 1px solid rgba(0,175,255,0.35); border-radius: 12px;
                        padding: 4px 14px; }
            QLineEdit:focus { border: 1px solid rgba(0,175,255,0.8); }
            QLineEdit:disabled { color: #5E646B; border: 1px solid rgba(255,255,255,0.06); }
        """)
        self._pw.returnPressed.connect(self._submit_password)
        self._pw.hide()
        lay.addWidget(self._pw, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(2)

        # The small blue dot: password fallback
        self._dot = QPushButton(self)
        self._dot.setFixedSize(16, 16)
        self._dot.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dot.setToolTip("Use password")
        self._dot.setStyleSheet("""
            QPushButton { background: #00AFFF; border: none; border-radius: 8px; }
            QPushButton:hover { background: #4CC8FF; }
            QPushButton:pressed { background: #0088BB; }
        """)
        self._dot.clicked.connect(self._toggle_password)

        app = _app()
        if app is not None:
            app.aboutToQuit.connect(self._stop_worker)

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self):
        self.raise_()
        self.show()
        self._start_worker()

    def _start_worker(self):
        self._stop_worker()
        self._circle.set_progress(0.0)
        self._circle.set_color(_BLUE)
        w = _ScanWorker(self._enroll, self)
        w.frame.connect(self._circle.set_image)
        w.status.connect(self._set_status)
        w.progress.connect(self._circle.set_progress)
        w.face_seen.connect(self._on_face_seen)
        w.done.connect(self._on_done)
        w.failed.connect(self._on_failed)
        self._worker = w
        w.start()

    def _stop_worker(self):
        if self._worker is not None:
            self._worker.stop()
            self._worker = None

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._dot.move(self.width() - self._dot.width() - 22, self.height() - self._dot.height() - 22)

    # ── worker signals ───────────────────────────────────────────────────
    def _set_status(self, text: str):
        if not self._finished:
            self._status.setText(text)

    def _on_face_seen(self, seen: bool):
        if self._finished:
            return
        if not seen:
            self._status.setText("No face detected")

    def _on_failed(self, msg: str):
        self._circle.set_color(_RED)
        self._status.setText(msg)

    def _on_done(self, how: str):
        self._circle.set_color(_GREEN)
        self._circle.set_progress(1.0)
        self._status.setStyleSheet("color: #00FF88; background: transparent;")
        self._status.setText("Face recorded. Welcome." if how == "enrolled" else "Welcome back.")
        self._finish("enrolled" if how == "enrolled" else "face", delay_ms=700)

    # ── password fallback ────────────────────────────────────────────────
    def _toggle_password(self):
        if self._finished:
            return
        if self._pw.isVisible():
            self._pw.hide()
            return
        self._pw.show()
        self._pw.setFocus()

    def _submit_password(self):
        now = time.monotonic()
        if now < self._locked_until:
            return
        if fl.check_password(self._pw.text()):
            self._pw.clear()
            self._circle.set_color(_GREEN)
            self._status.setStyleSheet("color: #00FF88; background: transparent;")
            self._status.setText("Password accepted. Welcome.")
            self._finish("password", delay_ms=400)
            return
        self._pw.clear()
        self._fails += 1
        if self._fails >= _MAX_PASSWORD_TRIES:
            self._fails = 0
            self._locked_until = now + _LOCKOUT_SECONDS
            self._pw.setEnabled(False)
            self._status.setText(f"Too many attempts. Try again in {_LOCKOUT_SECONDS} seconds.")
            QTimer.singleShot(_LOCKOUT_SECONDS * 1000, self._end_lockout)
        else:
            self._status.setText("Wrong password")

    def _end_lockout(self):
        self._pw.setEnabled(True)
        if self._pw.isVisible():
            self._pw.setFocus()

    def _finish(self, how: str, delay_ms: int):
        if self._finished:
            return
        self._finished = True
        self._pw.hide()
        self._stop_worker()
        QTimer.singleShot(delay_ms, lambda: self.unlocked.emit(how))


def _app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance()
