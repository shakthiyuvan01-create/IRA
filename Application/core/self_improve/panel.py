"""The Self Improve chat panel: it sits beside the Activity Log and looks like it.

The person types (or says) "improve ..." or "add a ... skill". IRA plans the
change, shows the diff here, and writes nothing until Apply is pressed. Every
applied change is journaled and can be undone from this panel ("undo").

Panel commands: apply / discard, undo [id], history, restart.
While this tab is open, the window's COMMAND INPUT box types into it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QFont, QTextBlockFormat, QTextCharFormat
from PyQt6.QtWidgets import QHBoxLayout, QTextEdit, QVBoxLayout, QWidget

from . import agent, journal

_DIFF_LINES_SHOWN = 260
_LS = chr(0x2028)       # line break that stays inside one text block
_ACTIVE: "SelfImprovePanel | None" = None


def submit(text: str) -> bool:
    """Send a request to the open Self Improve panel from any thread (voice,
    the main command box, a tool). Returns False if no panel exists."""
    panel = _ACTIVE
    if panel is None:
        return False
    panel._external_sig.emit(text)
    return True


def _restart_ira() -> None:
    """Start a fresh IRA once this one has exited, then close this window."""
    from PyQt6.QtWidgets import QApplication
    relaunch = ("import time, subprocess, sys; time.sleep(3); "
                "subprocess.Popen(sys.argv[1:], cwd=%r)" % os.getcwd())
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([sys.executable, "-c", relaunch, sys.executable, *sys.argv],
                     creationflags=flags, close_fds=True)
    for w in QApplication.topLevelWidgets():
        if w.isWindow() and w.isVisible():
            w.close()
    QApplication.quit()


class _ChatView(QTextEdit):
    """Same glass, bubbles and timestamps as the Activity Log, minus the
    typewriter: diffs are long and must appear at once."""

    def __init__(self, ui):
        super().__init__()
        self._ui = ui
        self.setReadOnly(True)
        self.setFont(ui._font(10))
        self.document().setMaximumBlockCount(4000)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(16, 18, 20, 0.42);
                color: {ui.C.TEXT};
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 16px;
                padding: 8px 6px;
                selection-background-color: rgba(0, 175, 255, 0.30);
            }}
            QScrollBar:vertical {{ background: transparent; width: 4px; border: none; }}
            QScrollBar::handle:vertical {{
                background: rgba(255, 255, 255, 0.12); border-radius: 2px; min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)
        mono = QFont("Cascadia Mono")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPixelSize(9)
        self._mono = mono

    def _block(self, bg, align=Qt.AlignmentFlag.AlignLeft, top=7.0, bottom=3.0, side=8.0):
        cur = self.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        if not self.document().isEmpty():
            cur.insertBlock()
        bf = QTextBlockFormat()
        if bg is not None:
            bf.setBackground(QBrush(bg))
        bf.setAlignment(align)
        bf.setTopMargin(top); bf.setBottomMargin(bottom)
        bf.setLeftMargin(side); bf.setRightMargin(side)
        cur.setBlockFormat(bf)
        return cur

    def _fmt(self, color: str, font: QFont, alpha: int = 255) -> QTextCharFormat:
        f = QTextCharFormat()
        f.setForeground(QBrush(self._ui.qcol(color, alpha)))
        f.setFont(font)
        return f

    def _finish(self, cur):
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def message(self, role: str, text: str):
        """role: you | ira | sys | err | ok"""
        ui = self._ui
        if role in ("sys", "err", "ok"):
            color = {"sys": "#8A8F96", "err": ui.C.ERROR, "ok": ui.C.GREEN}[role]
            f = QFont(self.font()); f.setPixelSize(9)
            cur = self._block(None, Qt.AlignmentFlag.AlignCenter, 4.0, 2.0, 10.0)
            cur.insertText(text, self._fmt(color, f))
            return self._finish(cur)
        mine = role == "you"
        bg = ui.qcol("#FFFFFF", 13) if mine else ui.qcol("#00AFFF", 18)
        cur = self._block(bg, Qt.AlignmentFlag.AlignRight if mine else Qt.AlignmentFlag.AlignLeft)
        # U+2028 keeps a multi-line message inside one bubble instead of
        # splitting it into separately tinted blocks.
        body = _LS.join(text.splitlines())
        cur.insertText(body, self._fmt("#EAF0F6" if mine else "#B9DCFF", QFont(self.font())))
        ts = QFont(self.font()); ts.setPixelSize(7)
        cur.insertText(_LS + datetime.now().strftime("%H:%M"), self._fmt("#9AA0A8", ts, 175))
        self._finish(cur)

    def diff(self, text: str):
        lines = text.splitlines()
        shown = lines[:_DIFF_LINES_SHOWN]
        bg = self._ui.qcol("#000000", 90)
        for i, line in enumerate(shown):
            if line.startswith(("+++", "---")):
                color = "#E6EAF0"
            elif line.startswith("@@"):
                color = "#00AFFF"
            elif line.startswith("+"):
                color = "#3DDC97"
            elif line.startswith("-"):
                color = "#FF6B6B"
            else:
                color = "#8A8F96"
            cur = self._block(bg, top=6.0 if i == 0 else 0.0, bottom=0.0)
            cur.insertText(line or " ", self._fmt(color, self._mono))
        if len(lines) > len(shown):
            cur = self._block(bg, top=0.0, bottom=0.0)
            cur.insertText(f"… {len(lines) - len(shown)} more lines (full diff is saved "
                           f"with the change)", self._fmt("#8A8F96", self._mono))
        self._finish(cur)


class SelfImprovePanel(QWidget):
    _external_sig = pyqtSignal(str)       # request from voice / command box
    _progress_sig = pyqtSignal(str)
    _done_sig = pyqtSignal(object)        # Proposal | Exception
    show_requested = pyqtSignal()         # ask the host to bring this tab forward

    def __init__(self, parent=None):
        super().__init__(parent)
        from core import ui as _ui        # shared design tokens, imported lazily
        self._ui = _ui
        self._pending: agent.Proposal | None = None
        self._busy = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._view = _ChatView(_ui)
        lay.addWidget(self._view, stretch=1)

        # Apply / Discard: visible only while a diff waits for a decision.
        self._actions = QWidget()
        self._actions.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        a = QHBoxLayout(self._actions); a.setContentsMargins(0, 0, 0, 0); a.setSpacing(6)
        self._apply_btn = _ui._IconButton("APPLY CHANGE", "check", color=_ui.C.GREEN, fixed_h=30)
        self._discard_btn = _ui._IconButton("DISCARD", "x", color=_ui.C.TEXT_MED, fixed_h=30)
        self._apply_btn.clicked.connect(self._apply)
        self._discard_btn.clicked.connect(self._discard)
        a.addWidget(self._apply_btn, stretch=3); a.addWidget(self._discard_btn, stretch=2)
        self._actions.hide()
        lay.addWidget(self._actions)

        self._restart_btn = _ui._IconButton("RESTART IRA", "zap",
                                            color=_ui.C.BLUE, fixed_h=30)
        self._restart_btn.clicked.connect(_restart_ira)
        self._restart_btn.hide()
        lay.addWidget(self._restart_btn)


        self._external_sig.connect(self._on_external)
        self._progress_sig.connect(lambda t: self._view.message("sys", t))
        self._done_sig.connect(self._on_planned)

        self._view.message("ira", "Tell me what to improve, or which skill to add. "
                                  "I'll show you the exact code change first, and nothing "
                                  "is written until you press Apply. Type in Command Input "
                                  "below, or just say it.")
        global _ACTIVE
        _ACTIVE = self

    # ── input ────────────────────────────────────────────────────────────────
    # The window's COMMAND INPUT box types into this tab while it is open
    # (see MainWindow._send), so the panel needs no input row of its own.
    def _host_input(self):
        return getattr(self.window(), "_input", None)

    def set_placeholder(self, active: bool):
        box = self._host_input()
        if box is None:
            return
        if not active:
            box.setPlaceholderText("Type a command or question…")
        else:
            box.setPlaceholderText("Working on it…" if self._busy
                                   else "Improve…  or  add a … skill")

    def _on_external(self, text: str):
        self.show_requested.emit()
        self.handle(text)

    def handle(self, text: str):
        self._view.message("you", text)
        low = text.lower().strip(" .!")
        if self._pending and low in ("apply", "yes", "apply it", "do it", "approve"):
            return self._apply()
        if self._pending and low in ("discard", "no", "cancel", "reject"):
            return self._discard()
        if low == "history":
            return self._history()
        if low == "restart":
            return _restart_ira()
        if low.startswith("undo"):
            return self._undo(text[4:].strip() or None)
        if self._busy:
            return self._view.message("sys", "Still working on the last request…")
        if self._pending:
            self._view.message("sys", "Replacing the change that was waiting.")
            self._set_pending(None)
        self._plan(text)

    # ── planning ─────────────────────────────────────────────────────────────
    def _plan(self, request: str):
        self._busy = True
        self.set_placeholder(self.isVisible())

        def run():
            try:
                result = agent.plan(request, progress=self._progress_sig.emit)
            except Exception as exc:          # PlanError or a provider failure
                result = exc
            self._done_sig.emit(result)

        threading.Thread(target=run, daemon=True, name="self-improve-plan").start()

    def _on_planned(self, result):
        self._busy = False
        self.set_placeholder(self.isVisible())
        if isinstance(result, Exception):
            self._view.message("err", "Could not build a safe change.")
            self._view.message("ira", f"{result}\n\nTry rephrasing, or name the file or "
                                      f"feature to change.")
            return
        p: agent.Proposal = result
        added, removed = p.stats
        files = "\n".join(f"  {'new  ' if c.is_new else 'edit '} {c.path}" for c in p.changes)
        when = ("It loads straight away." if p.hot_loadable
                else "IRA needs a restart to pick it up.")
        self._view.message("ira", f"{p.summary}\n\n{files}\n+{added} / -{removed} lines. "
                                  f"{when} Review the diff, then Apply or Discard.")
        self._view.diff(p.diff)
        self._set_pending(p)

    def _set_pending(self, proposal):
        self._pending = proposal
        self._actions.setVisible(proposal is not None)

    # ── decisions ────────────────────────────────────────────────────────────
    def _apply(self):
        p = self._pending
        if p is None:
            return
        self._set_pending(None)
        try:
            folder = journal.apply(p)
        except Exception as exc:
            self._view.message("err", f"Apply failed, nothing was changed: {exc}")
            return
        self._view.message("ok", f"Applied {p.id}. Logged in Data/self_improve.")
        if p.hot_loadable:
            loaded, errors = journal.hot_load([c.path for c in p.changes])
            if errors:
                self._view.message("err", "Loading failed: " + "; ".join(
                    f"{k}: {v}" for k, v in errors.items()))
                self._view.message("ira", "Say \"undo\" to roll it back, or describe the fix.")
            else:
                names = ", ".join(loaded) or "the skill"
                self._view.message("ira", f"Done. {names} is live: ask me to use it in chat. "
                                          f"Voice picks it up after a restart.")
                self._restart_btn.show()
        else:
            self._view.message("ira", "Done. Restart IRA to run the new code. "
                                      "Say \"undo\" here if anything breaks.")
            self._restart_btn.show()
        print(f"[SelfImprove] applied {p.id} -> {folder}")

    def _discard(self):
        if self._pending:
            self._view.message("sys", "Discarded. Nothing was written.")
        self._set_pending(None)

    def _undo(self, change_id):
        try:
            m = journal.undo(change_id)
        except Exception as exc:
            return self._view.message("err", str(exc))
        files = ", ".join(m["created"] + m["modified"])
        self._view.message("ok", f"Undid {m['id']} ({files}).")
        if any(f.startswith("actions/ingested/") for f in m["created"] + m["modified"]):
            try:
                journal.hot_load([])
            except Exception:
                pass
        self._view.message("ira", "Restored. Restart IRA so the running copy matches.")
        self._restart_btn.show()

    def _history(self):
        items = journal.history(8)
        if not items:
            return self._view.message("ira", "No self-improvements yet.")
        lines = [f"{h['id']}  [{h['status']}]  {h['request'][:60]}" for h in items]
        self._view.message("ira", "Recent changes (say \"undo <id>\" to revert one):\n"
                                  + "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
#  Tab header: ACTIVITY LOG | SELF IMPROVE
# ─────────────────────────────────────────────────────────────────────────────

class _Tab(QWidget):
    """A section-label tab: muted when idle, bright with a blue underline when
    selected. Same type scale and icon size as the panel's other headers."""
    clicked = pyqtSignal()

    def __init__(self, text: str, icon: str, ui):
        super().__init__()
        self._ui, self._text, self._icon = ui, text, icon
        self._active = self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName(text.title())
        self.setFixedHeight(22)
        f = ui._font(8, QFont.Weight.DemiBold, 1.2)
        from PyQt6.QtGui import QFontMetrics
        fm = QFontMetrics(f)
        self._f = f
        self.setFixedWidth(16 + fm.horizontalAdvance(text) + 8)

    def set_active(self, on: bool):
        self._active = on
        self.update()

    def enterEvent(self, e):
        self._hover = True; self.update(); super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False; self.update(); super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _):
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QPainter
        ui = self._ui
        color = ui.C.TEXT if self._active else (ui.C.SECONDARY if self._hover else ui.C.MUTED)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(2, (self.height() - 10) // 2 - 1, ui.icon_pm(self._icon, color, 10))
        p.setPen(ui._pen(color, 1))
        p.setFont(self._f)
        p.drawText(QRectF(16, 0, self.width() - 16, self.height() - 2),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)
        if self._active or self.hasFocus():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(ui._brush(ui.C.BLUE, 220 if self._active else 110))
            p.drawRoundedRect(QRectF(16, self.height() - 2, self.width() - 22, 2), 1, 1)
        p.end()


def build_log_tabs(log_widget: QWidget):
    """Return (header, stack, panel): the tab row, the stacked Activity Log /
    Self Improve views, and the Self Improve panel itself."""
    from PyQt6.QtWidgets import QStackedWidget
    from core import ui as _ui

    panel = SelfImprovePanel()
    stack = QStackedWidget()
    stack.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    stack.addWidget(log_widget)
    stack.addWidget(panel)

    header = QWidget()
    header.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    row = QHBoxLayout(header)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(14)
    tabs = [_Tab("ACTIVITY LOG", "list", _ui), _Tab("SELF IMPROVE", "sparkles", _ui)]

    def select(i: int):
        stack.setCurrentIndex(i)
        for j, t in enumerate(tabs):
            t.set_active(i == j)
        panel.set_placeholder(i == 1)
        box = panel._host_input()
        if i == 1 and box is not None:
            box.setFocus()

    for i, t in enumerate(tabs):
        t.clicked.connect(lambda i=i: select(i))
        row.addWidget(t)
    row.addStretch()
    panel.show_requested.connect(lambda: select(1))
    select(0)
    return header, stack, panel
