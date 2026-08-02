"""screen_overlay.py — Floating capture button for live screen analysis.

When IRA is minimized:
1. A small floating crosshair button appears on the desktop
2. Click it to capture full screen
3. IRA analyzes and speaks the answer
4. Button stays visible for multiple uses
5. Auto-hides when IRA window is restored
"""

import io

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False

try:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QColor, QFont, QPainter, QPen
    from PyQt6.QtWidgets import QApplication, QWidget
    _QT = True
except ImportError:
    _QT = False


def _compress(img_bytes: bytes, max_w: int = 640, max_h: int = 360,
              quality: int = 60) -> tuple[bytes, str]:
    """Downscale and JPEG-compress an image for efficient sending to Gemini."""
    if not _PIL:
        return img_bytes, "image/png"
    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((max_w, max_h), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Snap] Compress error: {e}")
        return img_bytes, "image/png"


class FloatingCaptureButton(QWidget):
    """Small floating crosshair button on the desktop.

    Stays on top. Click to capture the screen region around it.
    """

    _INSTANCE = None

    def __init__(self, on_capture=None):
        super().__init__()
        self._on_capture = on_capture
        self._size = 56

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.CrossCursor)

        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            x = geom.right() - self._size - 30
            y = geom.bottom() - self._size - 80
        else:
            x, y = 100, 100
        self.setGeometry(x, y, self._size, self._size)

    def show_button(self):
        FloatingCaptureButton._INSTANCE = self
        self.show()
        self.raise_()
        self.activateWindow()

    def hide_button(self):
        self.hide()
        FloatingCaptureButton._INSTANCE = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w // 2, h // 2

        # Outer ring
        painter.setPen(QPen(QColor(0, 212, 255, 220), 2.5))
        painter.setBrush(QColor(0, 10, 20, 200))
        painter.drawEllipse(2, 2, w - 4, h - 4)

        # Crosshair
        painter.setPen(QPen(QColor(0, 212, 255, 255), 2))
        painter.drawLine(8, cy, w - 8, cy)
        painter.drawLine(cx, 8, cx, h - 8)

        # Center dot
        painter.setBrush(QColor(0, 212, 255, 255))
        painter.drawEllipse(cx - 3, cy - 3, 6, 6)

        # Label
        painter.setPen(QPen(QColor(200, 200, 200, 150), 1))
        painter.setFont(QFont("Courier New", 6))
        painter.drawText(self.rect().adjusted(0, h - 14, 0, 0),
                         Qt.AlignmentFlag.AlignCenter, "SNAP")

    def mousePressEvent(self, event):
        if not self._on_capture:
            return
        image_bytes, mime = self._capture_full_screen()
        if image_bytes:
            self._on_capture(image_bytes, mime)

    def _capture_full_screen(self):
        """Capture the entire primary monitor (compressed for Gemini)."""
        if not _MSS:
            return None, None
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]  # primary monitor
                shot = sct.grab(monitor)
                png = mss.tools.to_png(shot.rgb, shot.size)
            compressed, mime = _compress(png)
            print(f"[Snap] Full screen: {len(png):,}px → {len(compressed):,}px JPEG")
            return compressed, mime
        except Exception as e:
            print(f"[Snap] Full screen capture error: {e}")
            return None, None

    @staticmethod
    def show_snap_button(on_capture):
        if not _QT:
            return
        FloatingCaptureButton._INSTANCE = FloatingCaptureButton(on_capture=on_capture)
        FloatingCaptureButton._INSTANCE.show_button()

    @staticmethod
    def hide_snap_button():
        if FloatingCaptureButton._INSTANCE:
            FloatingCaptureButton._INSTANCE.hide_button()

    @staticmethod
    def instance():
        return FloatingCaptureButton._INSTANCE
