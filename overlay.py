"""Overlay toast ("X is playing Y") for the launcher.

Standalone test:   python overlay.py
Launcher usage:    from overlay import overlay
                   overlay.notify_playing("AmrThePigeon", "DELTARUNE", avatar_path=None)
Modes: "window" (default) | "off" | "inject" (not built yet, falls back to window)
"""
import sys
from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import (Qt, QTimer, QVariantAnimation, QSequentialAnimationGroup, QEasingCurve,
                          QRectF)
from PyQt6.QtGui import QPainter, QColor, QFont, QFontMetrics, QPixmap, QPen, QPainterPath

W, H = 400, 110
SCALE = 2 / 3   # overall size; 1.0 = original, lower = smaller
SW, SH = int(W * SCALE), int(H * SCALE)
MARGIN = 20     # gap to the screen edge; the window spans toast + margin so it never leaves the screen
BAR_W = 29
SLIDE_MS = 350
BG = QColor("#1c1c1c")
STRIPE = QColor(255, 255, 255, 9)
ACCENT = QColor("#a97cff")
NAME_COLOR = QColor("#9b6cff")
GAME_COLOR = QColor("#9b6cff")
AVATAR_COLOR = QColor("#b18cff")


def _fit_font(text, px, max_w, bold=True):
    """Largest font (up to px) that fits max_w."""
    while px > 9:
        f = QFont()
        f.setPixelSize(px)
        f.setBold(bold)
        if QFontMetrics(f).horizontalAdvance(text) <= max_w:
            return f
        px -= 1
    f = QFont()
    f.setPixelSize(9)
    f.setBold(bold)
    return f


class PlayingToast(QWidget):
    def __init__(self, user, game, avatar_path=None, duration_ms=4000, prefix="is playing "):
        super().__init__()
        self.user, self.game, self.prefix = user, game, prefix
        self.avatar = QPixmap(avatar_path) if avatar_path else None
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(SW + MARGIN, SH)
        self.duration_ms = duration_ms
        self._off = float(SW + MARGIN)   # how far the toast is pushed right (hidden = SW + MARGIN)

    def _set_off(self, v):
        self._off = float(v)
        self.update()

    def popup(self):
        """Slides in from the right screen edge, waits, then slides back out. The window itself stays
        put (it never moves off the monitor); only the drawing inside it slides."""
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() + 1 - (SW + MARGIN), scr.bottom() - SH - MARGIN)
        hidden = float(SW + MARGIN)
        self.show()
        a_in = QVariantAnimation(self)
        a_in.setDuration(SLIDE_MS)
        a_in.setStartValue(hidden)
        a_in.setEndValue(0.0)
        a_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        a_in.valueChanged.connect(self._set_off)
        a_out = QVariantAnimation(self)
        a_out.setDuration(SLIDE_MS)
        a_out.setStartValue(0.0)
        a_out.setEndValue(hidden)
        a_out.setEasingCurve(QEasingCurve.Type.InCubic)
        a_out.valueChanged.connect(self._set_off)
        grp = QSequentialAnimationGroup(self)
        grp.addAnimation(a_in)
        grp.addPause(self.duration_ms)
        grp.addAnimation(a_out)
        grp.finished.connect(self.close)
        grp.start()
        self._anim = grp  # keep alive

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(self._off, 0)
        p.scale(SCALE, SCALE)
        p.setClipRect(QRectF(0, 0, W, H))
        p.fillRect(0, 0, W, H, BG)

        # diagonal stripes
        p.setPen(QPen(STRIPE, 16))
        for x in range(-H, W + H, 64):
            p.drawLine(x, H, x + H, 0)

        # accent bar
        p.fillRect(0, 0, BAR_W, H, ACCENT)

        # avatar (circle) = profile picture
        d = 78
        r = QRectF(BAR_W + 7, (H - d) / 2, d, d)
        path = QPainterPath()
        path.addEllipse(r)
        if self.avatar and not self.avatar.isNull():
            p.save()
            p.setClipPath(path)
            pm = self.avatar.scaled(d, d, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(r.topLeft(), pm)
            p.restore()
        else:
            p.fillPath(path, AVATAR_COLOR)

        # text: username (purple), "is playing" (white), game (purple)
        tx = int(r.right()) + 10
        max_w = W - tx - 12
        p.setPen(NAME_COLOR)
        p.setFont(_fit_font(self.user, 25, max_w))
        p.drawText(QRectF(tx, 18, max_w, 36), Qt.AlignmentFlag.AlignVCenter, self.user)

        prefix = self.prefix
        f2 = _fit_font(prefix + self.game, 20, max_w)
        p.setFont(f2)
        px = QFontMetrics(f2).horizontalAdvance(prefix)
        p.setPen(QColor("white"))
        p.drawText(QRectF(tx, 54, px, 30), Qt.AlignmentFlag.AlignVCenter, prefix)
        p.setPen(GAME_COLOR)
        p.drawText(QRectF(tx + px, 54, max_w - px, 30), Qt.AlignmentFlag.AlignVCenter, self.game)


class OverlayManager:
    def __init__(self):
        self.mode = "window"   # wire this to launcher settings: off | window | inject
        self._toasts = []

    def set_mode(self, mode):
        self.mode = mode if mode in ("off", "window", "inject") else "window"

    def notify_playing(self, user, game, avatar_path=None, prefix="is playing "):
        """Shows '<user> <prefix><game>' (game part is highlighted purple)."""
        if self.mode == "off":
            return
        # "inject" backend is a later phase; use the window backend for now.
        t = PlayingToast(user, game, avatar_path, prefix=prefix)
        self._toasts = [x for x in self._toasts if x.isVisible()] + [t]
        t.popup()


overlay = OverlayManager()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    overlay.notify_playing("AmrThePigeon", "DELTARUNE")
    QTimer.singleShot(5000, app.quit)
    sys.exit(app.exec())


# ---------------------------------------------------------------- injection backend (Windows, D3D11)
def _find_pid(exe_name):
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=0x08000000).stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) > 1 and parts[0].lower() == exe_name.lower():
                return int(parts[1])
    except Exception:
        pass
    return None


def _inject_dll(pid, dll_path):
    """Classic LoadLibraryW remote-thread injection. DLL bitness must match the game's."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.VirtualAllocEx.restype = ctypes.c_void_p
    k.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    k.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.POINTER(ctypes.c_size_t)]
    k.GetModuleHandleW.restype = wintypes.HANDLE
    k.GetProcAddress.restype = ctypes.c_void_p
    k.GetProcAddress.argtypes = [wintypes.HANDLE, ctypes.c_char_p]
    k.CreateRemoteThread.restype = wintypes.HANDLE
    k.CreateRemoteThread.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                     ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    h = k.OpenProcess(0x1F0FFF, False, pid)
    if not h:
        return False
    try:
        data = (dll_path + "\0").encode("utf-16-le")
        mem = k.VirtualAllocEx(h, None, len(data), 0x3000, 0x04)
        if not mem or not k.WriteProcessMemory(h, mem, data, len(data), None):
            return False
        load = k.GetProcAddress(k.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
        t = k.CreateRemoteThread(h, None, 0, load, mem, 0, None)
        if not t:
            return False
        k.CloseHandle(t)
        return True
    finally:
        k.CloseHandle(h)


_injected = {}   # exe name -> pid of games that already have the overlay DLL


def inject_overlay(exe_path, dll_path):
    """Injects dll_path into the running game (once per process). True if the game has the overlay."""
    import os
    if not sys.platform.startswith("win") or not os.path.exists(dll_path):
        return False
    name = os.path.basename(exe_path)
    pid = _find_pid(name)
    if not pid:
        return False
    if _injected.get(name) == pid:
        return True
    if _inject_dll(pid, dll_path):
        _injected[name] = pid
        return True
    return False


def send_to_injected(user, game, prefix="is playing "):
    """Hands a toast to the injected DLL. True if a game with the overlay is still running."""
    import os, time, tempfile
    alive = {n: p for n, p in _injected.items() if _find_pid(n) == p}
    _injected.clear()
    _injected.update(alive)
    if not alive:
        return False
    try:
        with open(os.path.join(tempfile.gettempdir(), "ncz_overlay.txt"), "w", encoding="utf-8") as f:
            f.write(f"{time.time_ns()}\n{user}\n{prefix}\n{game}\n")   # line 1 changes -> DLL shows a new toast
        return True
    except OSError:
        return False


def has_injected():
    """True if a game that has the overlay DLL is still running."""
    alive = {n: p for n, p in _injected.items() if _find_pid(n) == p}
    _injected.clear()
    _injected.update(alive)
    return bool(alive)
