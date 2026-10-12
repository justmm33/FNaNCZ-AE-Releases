"""Overlay toast ("X is playing Y") for the launcher.

Standalone test:   python overlay.py
Launcher usage:    from overlay import overlay
                   overlay.notify_playing("AmrThePigeon", "DELTARUNE", avatar_path=None)
Modes: "window" (default) | "off" | "inject" (not built yet, falls back to window)
"""
import os
import sys
import json
import html
import shutil
import subprocess
import tempfile
import threading
import time
from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import (Qt, QTimer, QVariantAnimation, QSequentialAnimationGroup, QEasingCurve,
                          QRectF, QObject, pyqtSignal, pyqtSlot)
from PyQt6.QtGui import (QPainter, QColor, QFont, QFontMetrics, QPixmap, QPen, QPainterPath, QGuiApplication,
                         QFontDatabase)

W, H = 400, 110
SCALE = 2 / 3   # overall size; 1.0 = original, lower = smaller
SW, SH = int(W * SCALE), int(H * SCALE)
GAP = 8         # vertical gap between stacked toasts
MAX_STACK = 8
MARGIN = 20     # gap to the screen edge; the window spans toast + margin so it never leaves the screen
BAR_W = 29
SLIDE_MS = 350
BG = QColor("#1c1c1c")
STRIPE = QColor(255, 255, 255, 9)
ACCENT = QColor("#a97cff")
NAME_COLOR = QColor("#9b6cff")
GAME_COLOR = QColor("#9b6cff")
AVATAR_COLOR = QColor("#b18cff")


_font_family = {"name": None, "tried": False}


def _assets_dirs():
    base = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
    dirs = [os.path.join(base, "assets")] if base else []
    dirs.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"))
    if getattr(sys, "frozen", False):
        dirs.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "assets"))
    return dirs


def _toast_font_family():
    """Registers assets/ubuntu.ttf (plus ubuntu-bold.ttf etc. next to it) once and returns its family name.
    Runs in the helper process too, which is why it doesn't rely on the launcher having loaded it."""
    if _font_family["tried"]:
        return _font_family["name"]
    _font_family["tried"] = True
    for d in _assets_dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        files = [n for n in names if n.lower().startswith("ubuntu") and n.lower().endswith((".ttf", ".otf"))]
        files.sort(key=lambda n: n.lower() != "ubuntu.ttf")      # ubuntu.ttf first: it decides the family name
        for n in files:
            fid = QFontDatabase.addApplicationFont(os.path.join(d, n))
            if fid != -1 and not _font_family["name"]:
                fams = QFontDatabase.applicationFontFamilies(fid)
                if fams:
                    _font_family["name"] = fams[0]
        if _font_family["name"]:
            break
    return _font_family["name"]


def _make_font(px, bold):
    f = QFont()
    fam = _toast_font_family()
    if fam:
        f.setFamily(fam)
    f.setPixelSize(px)
    f.setBold(bold)
    return f


def _fit_font(text, px, max_w, bold=True):
    """Largest font (up to px) that fits max_w."""
    while px > 9:
        f = _make_font(px, bold)
        if QFontMetrics(f).horizontalAdvance(text) <= max_w:
            return f
        px -= 1
    return _make_font(9, bold)


class PlayingToast(QWidget):
    def __init__(self, user, game, avatar_path=None, duration_ms=4000, prefix="is playing "):
        super().__init__()
        self.user, self.game, self.prefix = user, game, prefix
        self.avatar = QPixmap(avatar_path) if avatar_path else None
        flags = (Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.Tool
                 | Qt.WindowType.WindowTransparentForInput
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        if sys.platform.startswith("linux") and QGuiApplication.platformName() == "xcb":
            flags |= Qt.WindowType.X11BypassWindowManagerHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(SW + MARGIN, SH)
        self.duration_ms = duration_ms
        self.slot, self.on_close = 0, None   # slot 0 = bottom; higher slots stack upwards
        self._off = float(SW + MARGIN)   # how far the toast is pushed right (hidden = SW + MARGIN)
        self._finished = False
        self._raise_timer = None

    def _set_off(self, v):
        self._off = float(v)
        self.update()

    def set_slot(self, i):
        self.slot = i
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() + 1 - (SW + MARGIN), scr.bottom() - SH - MARGIN - i * (SH + GAP))

    def _done(self):
        if self._finished:                       # may be reached twice (animation end + being dropped from the stack)
            return
        self._finished = True
        if self._raise_timer is not None:
            self._raise_timer.stop()
        anim = getattr(self, "_anim", None)
        if anim is not None:
            anim.stop()
        self.close()
        cb, self.on_close = self.on_close, None
        if cb:
            cb()
        self.deleteLater()

    def dismiss(self):
        """Closes the toast right now (used when the stack is full)."""
        self._done()

    def popup(self):
        """Slides in from the right screen edge, waits, then slides back out. The window itself stays
        put (it never moves off the monitor); only the drawing inside it slides."""
        self.set_slot(self.slot)
        hidden = float(SW + MARGIN)
        self.show()
        if QGuiApplication.platformName() == "xcb":
            # keep the toast above fullscreen / always-on-top windows (X11 and XWayland)
            self._raise_timer = QTimer(self)
            self._raise_timer.timeout.connect(self.raise_)
            self._raise_timer.start(400)
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
        grp.finished.connect(self._done)
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


def _linux_notify(user, game, prefix):
    """Plain desktop notification (libnotify) - the fallback when no positioned toast window is possible,
    e.g. on a Wayland session where the XWayland helper can't start."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    body = html.escape((str(prefix) + str(game)).strip())
    try:
        subprocess.Popen([exe, "-a", "NCZ Games Launcher", "-t", "5000", "-i", "applications-games",
                          str(user), body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


class OverlayManager:
    def __init__(self):
        self.mode = "window"   # wire this to launcher settings: off | window | inject
        self._toasts = []
        self._proc = None
        self._helper_failed = False
        self._restarts = 0
        self._pending = None   # toasts sent to a freshly started helper, replayed locally if it dies on start-up

    def set_mode(self, mode):
        self.mode = mode if mode in ("off", "window", "inject") else "window"

    # ---- Linux / Wayland: a native Wayland window can't be positioned or hidden from the taskbar, so the toasts
    # are drawn by a small helper process running through XWayland (see run_server below).
    def _wants_helper(self):
        return (sys.platform.startswith("linux") and not self._helper_failed
                and QGuiApplication.platformName().startswith("wayland") and bool(os.environ.get("DISPLAY")))

    def _spawn_helper(self):
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "xcb"
        env.pop("QT_WAYLAND_DISABLE_WINDOWDECORATION", None)
        cmd = ([sys.executable, "--ncz-overlay-server"] if getattr(sys, "frozen", False)
               else [sys.executable, os.path.abspath(__file__), "--server"])
        log = open(os.path.join(tempfile.gettempdir(), "ncz_overlay_helper.log"), "w")
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                          env=env, text=True, bufsize=1, start_new_session=True)
        finally:
            log.close()                       # the child keeps its own copy of the handle
        self._pending = []
        QTimer.singleShot(2500, self._check_helper)

    def _check_helper(self):
        """2.5 s after start-up: if the helper already died (usually the Qt xcb plugin is missing libxcb-cursor),
        show what it was given with the fallback instead and stop using it."""
        pending, self._pending = self._pending or [], None
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return                             # alive: it is drawing the toasts
        self._proc = None
        self._helper_failed = True
        for user, game, avatar, prefix in pending:
            self._show_local(user, game, avatar, prefix)

    def _send_to_helper(self, user, game, avatar_path, prefix):
        try:
            proc = self._proc
            if proc is not None and proc.poll() is not None:     # the helper died after running for a while
                self._proc = proc = None
                self._restarts += 1
                if self._restarts > 2:
                    self._helper_failed = True
                    return False
            if proc is None:
                self._spawn_helper()
                proc = self._proc
            proc.stdin.write(json.dumps({"user": user, "game": game, "avatar": avatar_path or "",
                                         "prefix": prefix}) + "\n")
            proc.stdin.flush()
            if self._pending is not None:
                self._pending.append((user, game, avatar_path, prefix))
            return True
        except (OSError, ValueError):
            self._proc = None
            self._helper_failed = True
            return False

    def notify_playing(self, user, game, avatar_path=None, prefix="is playing "):
        """Shows '<user> <prefix><game>' (game part is highlighted purple)."""
        if self.mode == "off":
            return
        if self._wants_helper() and self._send_to_helper(user, game, avatar_path, prefix):
            return
        self._show_local(user, game, avatar_path, prefix)

    def _show_local(self, user, game, avatar_path, prefix):
        # A native Wayland window can't be placed in the corner or kept above games, so use a normal desktop
        # notification there instead of a misplaced window.
        if sys.platform.startswith("linux") and QGuiApplication.platformName().startswith("wayland"):
            if _linux_notify(user, game, prefix):
                return
        # "inject" backend is a later phase; use the window backend for now.
        live = [x for x in self._toasts if x.isVisible()]
        while len(live) >= MAX_STACK:          # keep the stack on screen: drop the oldest
            live.pop(0).dismiss()
        t = PlayingToast(user, game, avatar_path, prefix=prefix)
        t.slot = len(live)                     # new toasts stack on top of the ones still showing
        t.on_close = lambda t=t: self._closed(t)
        self._toasts = live + [t]
        t.popup()

    def _closed(self, t):
        self._toasts = [x for x in self._toasts if x is not t]
        self._relayout()

    def _relayout(self):
        for i, x in enumerate(self._toasts):
            x.set_slot(i)


overlay = OverlayManager()

# ---------------------------------------------------------------- injection backend (Windows, D3D11)
def _find_pid_linux(exe_name):
    """PID of the Wine process running <exe_name>. Under Wine the game's /proc cmdline holds its Windows path
    (Z:\\...\\game.exe); Proton's own `proton run <exe>` python process also mentions it, so only processes
    that are wine(64)-preloader binaries count."""
    want = exe_name.lower()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for d in entries:
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                raw = f.read()
            if not any(a.decode("utf-8", "ignore").replace("\\", "/").lower().rsplit("/", 1)[-1] == want
                       for a in raw.split(b"\0") if a):
                continue
            if not os.path.basename(os.readlink(f"/proc/{d}/exe")).lower().startswith("wine"):
                continue
        except OSError:
            continue
        return int(d)
    return None


def _find_pid(exe_name):
    if sys.platform.startswith("linux"):
        return _find_pid_linux(exe_name)
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
    if sys.platform.startswith("linux"):
        return _inject_linux(exe_path)
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


# ---------------------------------------------------------------- Wine / Proton (Linux)
# The DLL can't be pushed in with CreateRemoteThread from a native Linux process, so the launcher registers it as an
# AppInit_DLL in the game's prefix (see _overlay_prepare_wine in main.py) and Wine loads it into the game itself.
# This side finds the game, confirms the DLL really got loaded and works out where the DLL's temp files live.
_wine_prefixes = {}   # exe name -> wine prefix the launcher started it in
_wine_temp = {}       # exe name -> that prefix's Windows Temp folder (what GetTempPath returns inside the game)


def register_wine_prefix(exe_path, prefix):
    _wine_prefixes[os.path.basename(exe_path)] = prefix


def _wine_temp_dir(prefix):
    import glob
    cands = [p for p in glob.glob(os.path.join(prefix, "drive_c", "users", "*", "Temp"))
             if os.path.basename(os.path.dirname(p)).lower() != "public"]
    cands.sort(key=lambda p: os.path.basename(os.path.dirname(p)) != "steamuser")   # Proton's user comes first
    return cands[0] if cands else None


def _dll_loaded(pid, dll_name="overlay_hook.dll"):
    try:
        with open(f"/proc/{pid}/maps", encoding="utf-8", errors="ignore") as f:
            return any(dll_name in line.lower() for line in f)
    except OSError:
        return False


def _inject_linux(exe_path):
    """True once the overlay DLL is loaded inside the running Wine game (it gets there via AppInit_DLLs)."""
    name = os.path.basename(exe_path)
    prefix = _wine_prefixes.get(name)
    pid = _find_pid(name) if prefix else None
    if not pid:
        return False
    if _injected.get(name) == pid:
        return True
    tmp = _wine_temp_dir(prefix)
    if tmp and _dll_loaded(pid):
        _wine_temp[name] = tmp
        _injected[name] = pid
        return True
    return False


def diagnose_linux(exe_path):
    """One line explaining why the DLL isn't (yet) in the game: where the lookup stops. Written to the launcher's
    overlay_debug.log."""
    name = os.path.basename(exe_path)
    prefix = _wine_prefixes.get(name)
    out = [f"prefix={prefix}"]
    out.append(f"dll copied={os.path.isfile(os.path.join(prefix, 'drive_c', 'ncz', 'overlay_hook.dll'))}" if prefix
               else "dll copied=?")
    try:
        with open(os.path.join(prefix, "system.reg"), encoding="utf-8", errors="ignore") as f:
            hit = [l.strip() for l in f if "AppInit_DLLs" in l or "LoadAppInit_DLLs" in l]
        out.append(f"system.reg={hit[:3] or 'no AppInit entries (registry file is only saved when wine exits)'}")
    except (OSError, TypeError):
        out.append("system.reg=unreadable")
    out.append(f"game pid={_find_pid(name)}")
    want = name.lower()
    wine_pids, loose, with_dll = [], [], []
    try:
        entries = os.listdir("/proc")
    except OSError:
        entries = []
    for d in entries:
        if not d.isdigit():
            continue
        try:
            exe = os.path.basename(os.readlink(f"/proc/{d}/exe"))
            with open(f"/proc/{d}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
        except OSError:
            continue
        if want in cmd.replace("\\", "/").lower():
            loose.append((int(d), exe, cmd[:90]))
        if exe.lower().startswith("wine"):
            wine_pids.append(int(d))
            if _dll_loaded(int(d)):
                with_dll.append(int(d))
    out.append(f"processes mentioning the exe={loose[:4]}")
    out.append(f"wine processes={len(wine_pids)}, with overlay_hook.dll mapped={with_dll}")
    return "; ".join(out)


def injected_temp_dirs():
    """Folders where the overlay DLL reads/writes its ncz_*.txt files: %TEMP% on Windows, the Temp folder of each
    Wine prefix with an injected game on Linux."""
    import tempfile
    if sys.platform.startswith("linux"):
        dirs = []
        for n in list(_injected):
            d = _wine_temp.get(n)
            if d and d not in dirs:
                dirs.append(d)
        if dirs:
            return dirs
    return [tempfile.gettempdir()]


_send_q = []
_send_running = False


def _send_worker():
    global _send_running
    import os, time, tempfile
    one = lambda s: " ".join(str(s).split())
    while _send_q:
        time.sleep(0.15)                       # notifications that arrive together go out as one batch
        batch = _send_q[:8]
        del _send_q[:8]
        body = "".join(f"{one(u)}\n{p}\n{one(g)}\n" for u, g, p in
                       ((u, g, p) for u, g, p in batch))
        for d in injected_temp_dirs():             # Windows: %TEMP%; Wine/Proton: the game prefix's Temp folder
            path = os.path.join(d, "ncz_overlay.txt")
            try:
                with open(path + ".tmp", "w", encoding="utf-8") as f:
                    f.write(f"{time.time_ns()}\n{body}")      # line 1 changes -> the DLL shows every toast in the file
                os.replace(path + ".tmp", path)
            except OSError:
                pass
        time.sleep(0.7)                        # the DLL polls every 0.5 s: give each batch its own tick
    _send_running = False


def send_to_injected(user, game, prefix="is playing "):
    """Queues a toast for the injected DLL. True if a game with the overlay is still running."""
    global _send_running
    import threading
    alive = {n: p for n, p in _injected.items() if _find_pid(n) == p}
    _injected.clear()
    _injected.update(alive)
    if not alive:
        return False
    _send_q.append((user, game, prefix))
    if not _send_running:
        _send_running = True
        threading.Thread(target=_send_worker, daemon=True).start()
    return True


def has_injected():
    """True if a game that has the overlay DLL is still running."""
    alive = {n: p for n, p in _injected.items() if _find_pid(n) == p}
    _injected.clear()
    _injected.update(alive)
    return bool(alive)


# ---------------------------------------------------------------- XWayland helper process (Linux)
class _ServerBridge(QObject):
    got = pyqtSignal(dict)
    done = pyqtSignal()

    @pyqtSlot(dict)
    def on_got(self, d):
        overlay.notify_playing(str(d.get("user", "")), str(d.get("game", "")), d.get("avatar") or None,
                               str(d.get("prefix", "is playing ")))


def run_server():
    """Helper process: draws the toasts the launcher sends as JSON lines on stdin, using the X11 (xcb) platform so
    the windows can be placed exactly, stay out of the taskbar and sit above XWayland (Proton) games.
    Exits when the launcher closes the pipe."""
    os.environ["QT_QPA_PLATFORM"] = "xcb"
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    bridge = _ServerBridge()
    bridge.got.connect(bridge.on_got)
    bridge.done.connect(app.quit)

    def read():
        for line in sys.stdin:
            try:
                bridge.got.emit(json.loads(line))
            except ValueError:
                pass
        bridge.done.emit()

    threading.Thread(target=read, daemon=True).start()
    sys.exit(app.exec())


if __name__ == "__main__":
    if "--server" in sys.argv:
        run_server()
    else:
        app = QApplication(sys.argv)
        overlay.notify_playing("AmrThePigeon", "DELTARUNE")
        QTimer.singleShot(5000, app.quit)
        sys.exit(app.exec())
