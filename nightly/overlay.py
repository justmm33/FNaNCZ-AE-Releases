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
                          QRectF, QPointF, QUrl, QObject, pyqtSignal, pyqtSlot)
from PyQt6.QtGui import (QPainter, QColor, QFont, QFontMetrics, QPixmap, QPen, QPainterPath, QGuiApplication,
                         QFontDatabase, QMovie, QDesktopServices)

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
_accent_hex = ""   # set by set_accent(); sent along to the XWayland helper so its toasts match


_DARK_DEFAULTS = dict(BG=QColor("#1c1c1c"), STRIPE=QColor(255, 255, 255, 9), TEXT=QColor("white"),
                      ACCENT=QColor("#a97cff"), NAME=QColor("#9b6cff"), AVATAR=QColor("#b18cff"))
TEXT_COLOR = QColor("white")   # the white "is playing" part
_accent_base = None            # QColor chosen in the launcher (None = the built-in purple)
_light = False                 # light mode: light gray toast with dark text
_sunset = False                # "New" launcher theme: black toast, faint white stripes, white accent (always dark)


def _readable(c):
    """Lifts a dark accent so text in that colour stays readable on the toast's dark background."""
    h, s, l, a = c.getHslF()
    if l >= 0.55:
        return QColor(c)
    return QColor.fromHslF(max(h, 0.0), s, 0.55, a)


def _readable_light(c):
    """Darkens a bright accent so text in that colour stays readable on the light toast."""
    h, s, l, a = c.getHslF()
    if l <= 0.42:
        return QColor(c)
    return QColor.fromHslF(max(h, 0.0), s, 0.42, a)


def _apply_colors():
    """Recomputes every toast colour from the accent + light/dark mode."""
    global BG, STRIPE, TEXT_COLOR, ACCENT, NAME_COLOR, GAME_COLOR, AVATAR_COLOR
    d = _DARK_DEFAULTS
    light = _light and not _sunset
    if light:
        BG, STRIPE, TEXT_COLOR = QColor("#e4e4e4"), QColor(0, 0, 0, 12), QColor("#1c1c1c")   # gray -> light gray
    elif _sunset:
        BG, STRIPE, TEXT_COLOR = QColor("#0c0c0c"), QColor(255, 255, 255, 9), QColor("#aaaaaa")
    else:
        BG, STRIPE, TEXT_COLOR = QColor(d["BG"]), QColor(d["STRIPE"]), QColor(d["TEXT"])
    if _sunset:
        base = QColor(_accent_base) if _accent_base is not None else QColor("#fcfcfc")
        ACCENT = QColor(base)
        if base.lightnessF() > 0.85:                 # white accent: white names, grey avatar placeholder
            NAME_COLOR = GAME_COLOR = QColor("#ffffff")
            AVATAR_COLOR = QColor("#4d4d4d")
        else:                                        # an account theme with its own coloured accent
            NAME_COLOR = GAME_COLOR = _readable(base)
            AVATAR_COLOR = _readable(base).lighter(115)
        return
    base = _accent_base
    if base is None and not light:
        ACCENT, NAME_COLOR, GAME_COLOR, AVATAR_COLOR = QColor(d["ACCENT"]), QColor(d["NAME"]), QColor(d["NAME"]), QColor(d["AVATAR"])
        return
    base = base or QColor(d["ACCENT"])
    ACCENT = QColor(base)
    NAME_COLOR = GAME_COLOR = _readable_light(base) if light else _readable(base)
    AVATAR_COLOR = QColor(base) if light else _readable(base).lighter(115)


def set_accent(color):
    """Recolours the toasts (bar, username, game name, fallback avatar) to the launcher's accent colour."""
    global _accent_base, _accent_hex
    c = QColor(color)
    if not c.isValid():
        return
    _accent_base, _accent_hex = c, c.name()
    _apply_colors()


def set_sunset(flag):
    """New theme on/off: black toast with a white accent (overrides light mode)."""
    global _sunset
    _sunset = bool(flag)
    _apply_colors()


def set_light(flag):
    """Light mode on/off: light gray toast with dark text instead of the dark one."""
    global _light
    _light = bool(flag)
    _apply_colors()


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


# ---- Apple emoji drawn as pictures inside the toast text (the OS font would draw its own emoji otherwise)
import re
import urllib.request

_EL = (r"(?:[\U0001F1E6-\U0001F1FF]{2}"                                   # flag
       r"|[0-9#*]\uFE0F?\u20E3"                                           # keycap
       r"|(?:[\U0001F300-\U0001FAFF\u231A\u231B\u23E9-\u23EC\u23F0\u23F3\u25FD\u25FE\u2614\u2615\u2648-\u2653"
       r"\u267F\u2693\u26A1\u26AA\u26AB\u26BD\u26BE\u26C4\u26C5\u26CE\u26D4\u26EA\u26F2\u26F3\u26F5\u26FA\u26FD"
       r"\u2705\u270A\u270B\u2728\u274C\u274E\u2753-\u2755\u2757\u2795-\u2797\u27B0\u27BF\u2B1B\u2B1C\u2B50\u2B55]"
       r"|[\u00A9\u00AE\u203C\u2049\u2122\u2139\u2190-\u21FF\u2300-\u23FF\u24C2\u25A0-\u25FF\u2600-\u27BF"
       r"\u2900-\u297F\u2B00-\u2BFF\u3030\u303D\u3297\u3299]\uFE0F)"
       r"[\U0001F3FB-\U0001F3FF]?\uFE0F?[\U000E0020-\U000E007F]*)")
_EMOJI_TEXT_RE = re.compile(_EL + r"(?:\u200D" + _EL + r")*")


emoji_dir = ""            # folder of the emoji PNG cache (shared with the launcher); set by main.py / the helper's JSON
_emoji_pm = {}
_emoji_busy = set()
_emoji_failed = {}
_APPLE_EMOJI_URLS = (
    "https://cdn.jsdelivr.net/gh/iamcal/emoji-data@master/img-apple-64/{code}.png",
    "https://raw.githubusercontent.com/iamcal/emoji-data/master/img-apple-64/{code}.png",
    "https://cdn.jsdelivr.net/npm/emoji-datasource-apple@15.0.1/img/apple/64/{code}.png",
)
_TWEMOJI_URLS = (
    "https://raw.githubusercontent.com/jdecked/twemoji/v15.1.0/assets/72x72/{code}.png",
    "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/{code}.png",
)


def set_emoji_dir(path):
    global emoji_dir
    if path:
        emoji_dir = str(path)


def _emoji_code(ch):
    cps = [f"{ord(c):x}" for c in ch]
    if "200d" not in cps:
        cps = [c for c in cps if c != "fe0f"]
    return "-".join(cps)


def _emoji_fetch(code, path):
    def work():
        data = None
        parts = code.split("-")
        variants = [code, code + "-fe0f"] + ([parts[0] + "-fe0f-" + "-".join(parts[1:])] if len(parts) > 1 else [])
        try:
            for url in _APPLE_EMOJI_URLS:
                for c in variants:
                    try:
                        with urllib.request.urlopen(urllib.request.Request(
                                url.format(code=c), headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as r:
                            blob = r.read()
                        if blob[:4] == b"\x89PNG":
                            data = blob
                            break
                    except Exception:
                        pass
                if data:
                    break
            if not data:
                for url in _TWEMOJI_URLS:
                    try:
                        with urllib.request.urlopen(urllib.request.Request(
                                url.format(code=code), headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as r:
                            blob = r.read()
                        if blob[:4] == b"\x89PNG":
                            data = blob
                            break
                    except Exception:
                        pass
            if data:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".part", "wb") as f:
                    f.write(data)
                os.replace(path + ".part", path)
            else:
                _emoji_failed[code] = time.time()
        except Exception:
            _emoji_failed[code] = time.time()
        finally:
            _emoji_busy.discard(code)
    _emoji_busy.add(code)
    threading.Thread(target=work, daemon=True).start()


def _emoji_png(ch):
    code = _emoji_code(ch)
    pm = _emoji_pm.get(code)
    if pm is not None:
        return pm
    if not emoji_dir:
        return None
    path = os.path.join(emoji_dir, code + ".png")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        pm = QPixmap(path)
        if not pm.isNull():
            _emoji_pm[code] = pm
            return pm
    elif code not in _emoji_busy and time.time() - _emoji_failed.get(code, 0) > 60:
        _emoji_fetch(code, path)
    return None


def _emoji_w(font):
    return int((font.pixelSize() if font.pixelSize() > 0 else 16) * 1.05)


def _segments(text):
    out, pos = [], 0
    for mt in _EMOJI_TEXT_RE.finditer(text):
        if mt.start() > pos:
            out.append((text[pos:mt.start()], False))
        out.append((mt.group(0), True))
        pos = mt.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out


def _text_w(font, text):
    fm = QFontMetrics(font)
    if not _EMOJI_TEXT_RE.search(text):
        return fm.horizontalAdvance(text)
    ew = _emoji_w(font)
    return sum(ew if em else fm.horizontalAdvance(seg) for seg, em in _segments(text))


def _draw_text(p, rect, text, flags=Qt.AlignmentFlag.AlignVCenter):
    """Left-aligned, vertically centred text where every emoji is its Apple picture."""
    if not _EMOJI_TEXT_RE.search(text):
        p.drawText(rect, flags, text)
        return
    font = p.font()
    fm = QFontMetrics(font)
    ew = _emoji_w(font)
    x = rect.x()
    for seg, em in _segments(text):
        if em:
            r = QRectF(x, rect.y() + (rect.height() - ew) / 2, ew, ew)
            pm = _emoji_png(seg)
            if pm is not None:
                p.drawPixmap(r, pm, QRectF(pm.rect()))
            else:
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, seg)
            x += ew
        else:
            w = fm.horizontalAdvance(seg)
            p.drawText(QRectF(x, rect.y(), w + 2, rect.height()), flags, seg)
            x += w


ICON_D = 46                       # size of the little icon on the right (unscaled toast units)
ICON_PAD = 14                     # gap to the toast's right edge
ICON_RESERVE = ICON_D + ICON_PAD + 6      # text stops this far from the right edge
_icon_cache = {"pm": None, "tried": False}


def _toast_icon():
    """assets/icon.png as a square, cropped to the middle (None when the file isn't there)."""
    if not _icon_cache["tried"]:
        _icon_cache["tried"] = True
        for d in _assets_dirs():
            path = os.path.join(d, "icon.png")
            if os.path.isfile(path):
                pm = QPixmap(path)
                if not pm.isNull():
                    big = pm.scaled(ICON_D * 2, ICON_D * 2, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
                    _icon_cache["pm"] = big.copy((big.width() - ICON_D * 2) // 2, (big.height() - ICON_D * 2) // 2,
                                                 ICON_D * 2, ICON_D * 2)
                    break
    return _icon_cache["pm"]


def _draw_icon(p, cy):
    """Rounded-corner icon at the right edge of the toast, vertically centred on cy."""
    pm = _toast_icon()
    if pm is None:
        return
    r = QRectF(W - ICON_PAD - ICON_D, cy - ICON_D / 2, ICON_D, ICON_D)
    clip = QPainterPath()
    clip.addRoundedRect(r, 11, 11)
    p.save()
    p.setClipPath(clip)
    p.drawPixmap(r, pm, QRectF(pm.rect()))
    p.restore()


def _fit_font(text, px, max_w, bold=True):
    """Largest font (up to px) that fits max_w."""
    while px > 9:
        f = _make_font(px, bold)
        if _text_w(f, text) <= max_w:
            return f
        px -= 1
    return _make_font(9, bold)


class PlayingToast(QWidget):
    def __init__(self, user, game, avatar_path=None, duration_ms=4000, prefix="is playing ", image_path=None,
                 badge_path=None, plain=False):
        super().__init__()
        self.plain = plain          # text only: no avatar circle (used for "Screenshot taken")
        self.user, self.game, self.prefix = user, game, prefix
        self.avatar = QPixmap(avatar_path) if avatar_path else None
        self.badge = QPixmap(badge_path) if badge_path else None   # small round picture on the avatar (group chats: who wrote)
        self.movie, self.image, self.ibox = None, None, None   # picture under the text (png / jpg / gif ...)
        if image_path:
            self._load_image(image_path)
        self.h = H + (self.ibox[1] + 8 if self.ibox else 0)    # total height, unscaled
        self.th = int(self.h * SCALE)
        if self.ibox:
            duration_ms = max(duration_ms, 6000)
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
        self.setFixedSize(SW + MARGIN, self.th)
        self.duration_ms = duration_ms
        self.offset, self.on_close = 0, None   # offset = height of the toasts below this one (stack goes upwards)
        self._off = float(SW + MARGIN)   # how far the toast is pushed right (hidden = SW + MARGIN)
        self._finished = False
        self._raise_timer = None

    def _set_off(self, v):
        self._off = float(v)
        self.update()

    def _load_image(self, path):
        """Loads a picture (animated for .gif) and works out how big it is drawn under the text."""
        pm = None
        if str(path).lower().endswith(".gif"):
            mv = QMovie(str(path))
            if mv.isValid():
                mv.jumpToFrame(0)
                pm = mv.currentPixmap()
                if not pm.isNull():
                    self.movie = mv
        if self.movie is None:
            pm = QPixmap(str(path))
        if pm is None or pm.isNull():
            self.movie = None
            return
        aw, ah = W - BAR_W - 19, 200
        k = min(aw / pm.width(), ah / pm.height(), 1.0)
        bw, bh = max(1, int(pm.width() * k)), max(1, int(pm.height() * k))
        self.ibox = (bw, bh)
        if self.movie is None:
            self.image = pm.scaled(bw * 2, bh * 2, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)

    def set_offset(self, off):
        self.offset = off
        scr = QApplication.primaryScreen().availableGeometry()
        self.move(scr.right() + 1 - (SW + MARGIN), scr.bottom() - self.th - MARGIN - off)

    def set_slot(self, i):                    # kept for compatibility
        self.set_offset(i * (SH + GAP))

    def _done(self):
        if self._finished:                       # may be reached twice (animation end + being dropped from the stack)
            return
        self._finished = True
        if self._raise_timer is not None:
            self._raise_timer.stop()
        self._stop_emoji_timer()
        if self.movie is not None:
            self.movie.stop()
        anim = getattr(self, "_anim", None)
        if anim is not None:
            anim.stop()
        self.close()
        cb, self.on_close = self.on_close, None
        if cb:
            cb()
        self.deleteLater()

    def _stop_emoji_timer(self):
        try:
            t = getattr(self, "_emoji_timer", None)
            if t is not None:
                t.stop()
        except RuntimeError:
            pass

    def dismiss(self):
        """Closes the toast right now (used when the stack is full)."""
        self._done()

    def slide_out(self):
        """Stops the countdown and slides the toast back out the way it came in, then closes it."""
        if self._finished or getattr(self, "_leaving", False):
            return
        self._leaving = True
        anim = getattr(self, "_anim", None)
        if anim is not None:
            anim.stop()
        out = QVariantAnimation(self)
        out.setDuration(SLIDE_MS)
        out.setStartValue(float(self._off))
        out.setEndValue(float(SW + MARGIN))
        out.setEasingCurve(QEasingCurve.Type.InCubic)
        out.valueChanged.connect(self._set_off)
        out.finished.connect(self._done)
        out.start()
        self._anim = out  # keep alive

    def popup(self):
        """Slides in from the right screen edge, waits, then slides back out. The window itself stays
        put (it never moves off the monitor); only the drawing inside it slides."""
        self.set_offset(self.offset)
        hidden = float(SW + MARGIN)
        self.show()
        if self.movie is not None:
            self.movie.frameChanged.connect(lambda _f: self.update())
            self.movie.start()
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
        if _EMOJI_TEXT_RE.search(str(self.user) + str(self.prefix) + str(self.game)):
            self._emoji_timer = QTimer(self)             # repaint while the emoji pictures download
            self._emoji_timer.timeout.connect(self.update)
            self._emoji_timer.start(300)
            QTimer.singleShot(6000, self._stop_emoji_timer)
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
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        h = self.h
        p.translate(self._off, 0)
        p.scale(SCALE, SCALE)
        p.setClipRect(QRectF(0, 0, W, h))
        p.fillRect(0, 0, W, h, BG)

        # diagonal stripes
        p.setPen(QPen(STRIPE, 16))
        for x in range(-h, W + h, 64):
            p.drawLine(x, h, x + h, 0)

        # accent bar
        p.fillRect(0, 0, BAR_W, h, ACCENT)
        _draw_icon(p, H / 2)

        if self.plain:
            tx = BAR_W + 22
            max_w = W - tx - 14 - ICON_RESERVE
            p.setPen(NAME_COLOR)
            p.setFont(_fit_font(self.user, 28, max_w))
            _draw_text(p, QRectF(tx, 0, max_w, H), self.user)
            return

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

        # sender badge: in a group chat the big avatar is the group icon and this is the person who wrote
        if self.badge is not None and not self.badge.isNull():
            bd = 32
            br = QRectF(r.right() - 10 - bd / 2, r.bottom() - 10 - bd / 2, bd, bd)
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(BG)
            p.drawEllipse(br.adjusted(-3, -3, 3, 3))          # ring that separates it from the group icon
            bp = QPainterPath()
            bp.addEllipse(br)
            p.setClipPath(bp)
            bpm = self.badge.scaled(bd * 2, bd * 2, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(br, bpm, QRectF(bpm.rect()))
            p.restore()

        # text: username (purple), "is playing" (white), game (purple)
        tx = int(r.right()) + 10
        max_w = W - tx - 12 - ICON_RESERVE
        p.setPen(NAME_COLOR)
        p.setFont(_fit_font(self.user, 25, max_w))
        _draw_text(p, QRectF(tx, 18, max_w, 36), self.user)

        prefix = self.prefix
        f2 = _fit_font(prefix + self.game, 20, max_w)
        p.setFont(f2)
        px = _text_w(f2, prefix)
        p.setPen(TEXT_COLOR)
        _draw_text(p, QRectF(tx, 54, px, 30), prefix)
        p.setPen(GAME_COLOR)
        _draw_text(p, QRectF(tx + px, 54, max_w - px, 30), self.game)

        # picture sent by the friend (png / jpg / gif ...), under the text
        if self.ibox:
            bw, bh = self.ibox
            rr = QRectF(BAR_W + 7 + ((W - BAR_W - 19) - bw) / 2, H - 4, bw, bh)
            clip = QPainterPath()
            clip.addRoundedRect(rr, 8, 8)
            p.save()
            p.setClipPath(clip)
            src = self.movie.currentPixmap() if self.movie is not None else self.image
            if src is not None and not src.isNull():
                p.drawPixmap(rr, src, QRectF(src.rect()))
            p.restore()


INV_H = 150                      # invite toast height (unscaled): cover on the left, text + two buttons on the right
INV_COVER = (BAR_W + 10, 15, 80, 120)


# main.py sets this to a callable(link) that starts the game first (when it isn't open) and then accepts the invite.
# Without it (e.g. inside the XWayland helper process) the join link is just opened.
invite_accept_hook = None


def _accept_invite(link):
    hook = invite_accept_hook
    if hook is not None:
        try:
            hook(link)
            return
        except Exception:
            pass
    QDesktopServices.openUrl(QUrl(link))


class InviteToast(PlayingToast):
    """'<friend> invited you to play <game>' with the game's cover and clickable Accept / Dismiss buttons.
    Accept opens the Steam join link, exactly like accepting an invite in Steam. Unlike the other toasts this one takes
    mouse clicks (it still never takes keyboard focus away from the game)."""

    SUBTITLE = "invited you to play"
    ACCEPT_TEXT, DISMISS_TEXT = "Accept", "Dismiss"
    COVER_LETTER = ""

    def __init__(self, user, game, link, avatar_path=None, cover_path=None, duration_ms=20000):
        super().__init__(user, game, avatar_path, duration_ms=duration_ms, prefix="invited you to play ")
        self.link = link if str(link).startswith("steam://") else ""
        self.cover = QPixmap(cover_path) if cover_path else None
        self.h = INV_H
        self.th = int(self.h * SCALE)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowTransparentForInput)
        self.setFixedSize(SW + MARGIN, self.th)
        self.setMouseTracking(True)
        tx = INV_COVER[0] + INV_COVER[2] + 12
        self._accept_r = QRectF(tx, 96, 130, 38)
        self._dismiss_r = QRectF(tx + 138, 96, 119, 38)
        self._hover = None

    # ---- clicks (the toast is drawn scaled and slid sideways, so map the mouse back to toast coordinates)
    def _which(self, e):
        pt = QPointF((e.position().x() - self._off) / SCALE, e.position().y() / SCALE)
        if self._accept_r.contains(pt):
            return "accept"
        if self._dismiss_r.contains(pt):
            return "dismiss"
        return None

    def mouseMoveEvent(self, e):
        w = self._which(e)
        if w != self._hover:
            self._hover = w
            self.setCursor(Qt.CursorShape.PointingHandCursor if w else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self._hover = None
        self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if getattr(self, "_leaving", False):
            return
        w = self._which(e)
        if w == "accept":
            if self._do_accept():
                self.slide_out()
        elif w == "dismiss":
            self._do_dismiss()
            self.slide_out()

    def _do_accept(self):
        if not self.link:
            return False
        _accept_invite(self.link)
        return True

    def _do_dismiss(self):
        pass

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        h = self.h
        p.translate(self._off, 0)
        p.scale(SCALE, SCALE)
        p.setClipRect(QRectF(0, 0, W, h))
        p.fillRect(0, 0, W, h, BG)
        p.setPen(QPen(STRIPE, 16))
        for x in range(-h, W + h, 64):
            p.drawLine(x, h, x + h, 0)
        p.fillRect(0, 0, BAR_W, h, ACCENT)
        _draw_icon(p, 12 + ICON_D / 2)

        # cover
        cx, cy, cw, ch = INV_COVER
        r = QRectF(cx, cy, cw, ch)
        clip = QPainterPath()
        clip.addRoundedRect(r, 8, 8)
        p.save()
        p.setClipPath(clip)
        if self.cover is not None and not self.cover.isNull():
            pm = self.cover.scaled(cw * 2, ch * 2, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(r, pm, QRectF((pm.width() - cw * 2) / 2, (pm.height() - ch * 2) / 2, cw * 2, ch * 2))
        else:
            p.fillRect(r, AVATAR_COLOR)
            p.setPen(QColor("white") if AVATAR_COLOR.lightnessF() < 0.6 else QColor("#111111"))
            p.setFont(_make_font(40, True))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, (self.COVER_LETTER or self.game[:1] or "?").upper())
        p.restore()

        # sender: small avatar + name, then "invited you to play", then the game
        tx = cx + cw + 12
        max_w = W - tx - 12 - ICON_RESERVE          # rows next to the icon
        full_w = W - tx - 12                         # the game line sits below it
        d = 26
        ar = QRectF(tx, 12, d, d)
        apath = QPainterPath()
        apath.addEllipse(ar)
        if self.avatar and not self.avatar.isNull():
            p.save()
            p.setClipPath(apath)
            p.drawPixmap(ar.topLeft(), self.avatar.scaled(d * 2, d * 2, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                                          Qt.TransformationMode.SmoothTransformation).scaled(
                d, d, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            p.restore()
        else:
            p.fillPath(apath, AVATAR_COLOR)
        nx = tx + d + 8
        p.setPen(NAME_COLOR)
        p.setFont(_fit_font(self.user, 20, W - nx - 12 - ICON_RESERVE))
        _draw_text(p, QRectF(nx, 10, W - nx - 12 - ICON_RESERVE, 30), self.user)
        p.setPen(TEXT_COLOR)
        p.setFont(_make_font(15, False))
        p.drawText(QRectF(tx, 44, max_w, 22), Qt.AlignmentFlag.AlignVCenter, self.SUBTITLE)
        p.setPen(GAME_COLOR)
        p.setFont(_fit_font(self.game, 21, full_w))
        _draw_text(p, QRectF(tx, 64, full_w, 28), self.game)

        # buttons
        for rect, text, primary, key in ((self._accept_r, self.ACCEPT_TEXT, True, "accept"),
                                         (self._dismiss_r, self.DISMISS_TEXT, False, "dismiss")):
            hot = self._hover == key
            bp = QPainterPath()
            bp.addRoundedRect(rect, 8, 8)
            if primary:
                col = QColor(ACCENT).lighter(115) if hot else QColor(ACCENT)
                p.fillPath(bp, col)
                txt = QColor("white") if col.lightnessF() < 0.6 else QColor("#111111")
            else:
                p.fillPath(bp, QColor(128, 128, 128, 80 if hot else 40))
                txt = TEXT_COLOR
            p.setPen(txt)
            p.setFont(_make_font(16, True))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


# ---- chess challenges: toast with Accept / Decline. main.py sets the two hooks (gid, opp_uid, user). In the XWayland
# helper process there are no hooks, so the click is sent back to the launcher as a tagged line on the helper's stdout.
chess_accept_hook = None
chess_decline_hook = None
_IS_HELPER = False
_ACTION_TAG = "@@ncz-action@@ "


def _emit_action(d):
    try:
        sys.stdout.write(_ACTION_TAG + json.dumps(d) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _chess_action(kind, gid, opp_uid, user):
    hook = chess_accept_hook if kind == "accept" else chess_decline_hook
    if hook is not None:
        try:
            hook(gid, opp_uid, user)
        except Exception:
            pass
        return
    if _IS_HELPER:
        _emit_action({"action": "chess_" + kind, "gid": gid, "opp_uid": opp_uid, "user": user})


class ChessInviteToast(InviteToast):
    """'<friend> challenged you to chess' with the time control and clickable Accept / Decline buttons."""
    SUBTITLE = "challenged you to chess"
    ACCEPT_TEXT, DISMISS_TEXT = "Accept", "Decline"
    COVER_LETTER = "\u265F"

    def __init__(self, user, tc, gid, opp_uid, avatar_path=None, cover_path=None, duration_ms=30000):
        super().__init__(user, tc or "Chess", "", avatar_path, cover_path, duration_ms)
        self.prefix = "challenged you to chess"
        self.gid, self.opp_uid = gid, opp_uid

    def _do_accept(self):
        _chess_action("accept", self.gid, self.opp_uid, self.user)
        return True

    def _do_dismiss(self):
        _chess_action("decline", self.gid, self.opp_uid, self.user)


class _ActionBridge(QObject):
    got = pyqtSignal(dict)

    @pyqtSlot(dict)
    def on_got(self, d):
        a = str(d.get("action", ""))
        if a in ("chess_accept", "chess_decline"):
            _chess_action(a[6:], str(d.get("gid", "")), str(d.get("opp_uid", "")), str(d.get("user", "")))


_action_bridge_obj = None


def _action_bridge():
    """Created on the GUI thread (first call comes from there) so signals emitted by worker threads arrive on it."""
    global _action_bridge_obj
    if _action_bridge_obj is None:
        b = _ActionBridge()
        b.got.connect(b.on_got)
        _action_bridge_obj = b
    return _action_bridge_obj


def _helper_reader(proc, logpath, bridge):
    """Reads the helper's stdout: tagged lines are button clicks, everything else goes to its log file."""
    try:
        with open(logpath, "a", encoding="utf-8", errors="replace") as log:
            for line in proc.stdout:
                if line.startswith(_ACTION_TAG):
                    try:
                        bridge.got.emit(json.loads(line[len(_ACTION_TAG):]))
                    except ValueError:
                        pass
                else:
                    log.write(line)
                    log.flush()
    except (OSError, ValueError):
        pass


def _linux_notify_chess(user, tc, chess, icon=None):
    """Wayland fallback: a libnotify notification with Accept / Decline actions (needs notify-send with --action)."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    body = html.escape(f"challenged you to chess ({tc})" if tc else "challenged you to chess")
    try:
        helptext = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=3)
        has_actions = "--action" in (helptext.stdout + helptext.stderr)
    except Exception:
        has_actions = False
    cmd = [exe, "-a", "NCZ Games Launcher", "-t", "30000", "-i", icon or "applications-games"]
    if has_actions:
        cmd += ["-A", "accept=Accept", "-A", "decline=Decline"]
    cmd += [str(user), body]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE if has_actions else subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return False
    if has_actions:
        bridge = _action_bridge()

        def wait():
            try:
                out = (proc.communicate(timeout=60)[0] or "").strip()
            except Exception:
                return
            if out in ("accept", "decline"):
                bridge.got.emit({"action": "chess_" + out, "gid": chess.get("gid", ""),
                                 "opp_uid": chess.get("opp_uid", ""), "user": str(user)})
        threading.Thread(target=wait, daemon=True).start()
    return True


def _linux_notify(user, game, prefix, icon=None, link=None):
    """Plain desktop notification (libnotify) - the fallback when no positioned toast window is possible,
    e.g. on a Wayland session where the XWayland helper can't start."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    body = html.escape((str(prefix) + str(game)).strip())
    cmd = [exe, "-a", "NCZ Games Launcher", "-t", "5000", "-i", icon or "applications-games", str(user), body]
    if link and str(link).startswith("steam://"):
        try:
            helptext = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=3)
            has_actions = "--action" in (helptext.stdout + helptext.stderr)
        except Exception:
            has_actions = False
        if has_actions:                       # notify-send prints the clicked action's name when it is closed
            cmd = [exe, "-a", "NCZ Games Launcher", "-t", "20000", "-i", icon or "applications-games",
                   "-A", "join=Accept", str(user), body]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError:
                return False

            def wait():
                try:
                    out = proc.communicate(timeout=60)[0] or ""
                except Exception:
                    return
                if out.strip() == "join":
                    if invite_accept_hook is not None:
                        try:
                            QTimer.singleShot(0, lambda l=str(link): invite_accept_hook(l))   # back on the GUI thread
                            return
                        except Exception:
                            pass
                    try:
                        subprocess.Popen(["xdg-open", str(link)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except OSError:
                        pass
            threading.Thread(target=wait, daemon=True).start()
            return True
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        logpath = os.path.join(tempfile.gettempdir(), "ncz_overlay_helper.log")
        log = open(logpath, "w")
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                          env=env, text=True, bufsize=1, start_new_session=True)
        finally:
            log.close()                       # the child keeps its own copy of the handle
        threading.Thread(target=_helper_reader, args=(self._proc, logpath, _action_bridge()), daemon=True).start()
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
        for user, game, avatar, prefix, image, link, badge, plain, chess in pending:
            self._show_local(user, game, avatar, prefix, image, link, badge, plain, chess)

    def _send_to_helper(self, user, game, avatar_path, prefix, image=None, link=None, badge=None, plain=False, chess=None):
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
                                         "prefix": prefix, "image": image or "", "link": link or "",
                                         "badge": badge or "", "plain": bool(plain), "chess": chess or {},
                                         "accent": _accent_hex, "light": _light, "sunset": _sunset,
                                         "emoji_dir": emoji_dir}) + "\n")
            proc.stdin.flush()
            if self._pending is not None:
                self._pending.append((user, game, avatar_path, prefix, image, link, badge, plain, chess))
            return True
        except (OSError, ValueError):
            self._proc = None
            self._helper_failed = True
            return False

    def notify_playing(self, user, game, avatar_path=None, prefix="is playing ", image=None, badge=None):
        """Shows '<user> <prefix><game>' (game part is highlighted purple). image: path of a picture (png / jpg /
        gif ...) to show under the text. badge: path of a small round picture drawn on the avatar's corner (used
        for group chats: avatar = group icon, badge = the person who wrote)."""
        if self.mode == "off":
            return
        if self._wants_helper() and self._send_to_helper(user, game, avatar_path, prefix, image, None, badge):
            return
        self._show_local(user, game, avatar_path, prefix, image, None, badge)

    def notify_text(self, text):
        """A toast with just the text (no avatar circle), e.g. 'Screenshot taken'."""
        if self.mode == "off":
            return
        if self._wants_helper() and self._send_to_helper(text, "", None, "", None, None, None, True):
            return
        self._show_local(text, "", None, "", None, None, None, True)

    def notify_invite(self, user, game, link, avatar_path=None, image=None):
        """'<user> invited you to play <game>' with an Accept button that opens the Steam join link.
        image: path of the game's cover."""
        if self.mode == "off":
            return
        prefix = "invited you to play "
        if self._wants_helper() and self._send_to_helper(user, game, avatar_path, prefix, image, link):
            return
        self._show_local(user, game, avatar_path, prefix, image, link)

    def notify_chess_invite(self, user, tc, gid, opp_uid, avatar_path=None, image=None):
        """'<user> challenged you to chess (<tc>)' with Accept / Decline buttons. image: path of the chess cover."""
        if self.mode == "off":
            return
        chess = {"gid": gid, "opp_uid": opp_uid}
        prefix = "challenged you to chess"
        if self._wants_helper() and self._send_to_helper(user, tc, avatar_path, prefix, image, None, None, False, chess):
            return
        self._show_local(user, tc, avatar_path, prefix, image, None, None, False, chess)

    def _show_local(self, user, game, avatar_path, prefix, image=None, link=None, badge=None, plain=False, chess=None):
        # A native Wayland window can't be placed in the corner or kept above games, so use a normal desktop
        # notification there instead of a misplaced window.
        if sys.platform.startswith("linux") and QGuiApplication.platformName().startswith("wayland"):
            if chess:
                if _linux_notify_chess(user, game, chess, image or avatar_path):
                    return
            elif _linux_notify(user, game, prefix, image, link):
                return
        # "inject" backend is a later phase; use the window backend for now.
        live = [x for x in self._toasts if x.isVisible()]
        while len(live) >= MAX_STACK:          # keep the stack on screen: drop the oldest
            live.pop(0).dismiss()
        if chess:
            t = ChessInviteToast(user, game, chess.get("gid", ""), chess.get("opp_uid", ""), avatar_path, image)
        elif link:
            t = InviteToast(user, game, link, avatar_path, image)
        else:
            t = PlayingToast(user, game, avatar_path, prefix=prefix, image_path=image, badge_path=badge, plain=plain)
        avail = QApplication.primaryScreen().availableGeometry().height() - 2 * MARGIN
        while live and sum(x.th + GAP for x in live) + t.th > avail:    # tall picture toasts: keep the stack on screen
            live.pop(0).dismiss()
        t.offset = sum(x.th + GAP for x in live)    # new toasts stack on top of the ones still showing
        t.on_close = lambda t=t: self._closed(t)
        self._toasts = live + [t]
        t.popup()

    def _closed(self, t):
        self._toasts = [x for x in self._toasts if x is not t]
        self._relayout()

    def _relayout(self):
        off = 0
        for x in self._toasts:
            x.set_offset(off)
            off += x.th + GAP


overlay = OverlayManager()

# ---------------------------------------------------------------- injection backend (Windows, D3D11)
def _game_pids_linux(exe_name):
    """PIDs of the Wine process(es) running <exe_name>, best match first. Under Proton several processes mention the
    exe: `proton run <exe>` (python), the 32-bit `steam.exe` wrapper (the exe's unix path is one of its arguments) and
    the game itself, whose own command line *is* the exe's Windows path. Only the last one gets the 64-bit overlay DLL,
    so processes whose argv[0] is the exe come first."""
    want = exe_name.lower()
    strong, weak = [], []
    try:
        entries = sorted((d for d in os.listdir("/proc") if d.isdigit()), key=int)
    except OSError:
        return []
    for d in entries:
        try:
            if not os.path.basename(os.readlink(f"/proc/{d}/exe")).lower().startswith("wine"):
                continue
            with open(f"/proc/{d}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        base = lambda a: a.decode("utf-8", "ignore").replace("\\", "/").strip().lower().rsplit("/", 1)[-1]
        args = [a for a in raw.split(b"\0") if a.strip()]
        if not args:
            continue
        if base(args[0]) == want:
            strong.append(int(d))
        elif any(base(a) == want for a in args[1:]):
            weak.append(int(d))
    return strong + weak


def _find_pid_linux(exe_name):
    pids = _game_pids_linux(exe_name)
    return pids[0] if pids else None


def _pid_alive(name, pid):
    if sys.platform.startswith("linux"):
        return pid in _game_pids_linux(name)
    return _find_pid(name) == pid


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
    pids = _game_pids_linux(name) if prefix else []
    if not pids:
        return False
    if _injected.get(name) in pids:
        return True
    tmp = _wine_temp_dir(prefix)
    if not tmp:
        return False
    for pid in pids:                       # whichever of the game's processes really has the DLL mapped
        if _dll_loaded(pid):
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
    out.append(f"game pids={_game_pids_linux(name)}")
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
    alive = {n: p for n, p in _injected.items() if _pid_alive(n, p)}
    _injected.clear()
    _injected.update(alive)
    if not alive and sys.platform.startswith("linux"):
        for n in list(_wine_prefixes):     # the DLL may have come up after the launcher's last look: check again now
            _inject_linux(n)
        alive = dict(_injected)
    if not alive:
        return False
    _send_q.append((user, game, prefix))
    if not _send_running:
        _send_running = True
        threading.Thread(target=_send_worker, daemon=True).start()
    return True


def has_injected():
    """True if a game that has the overlay DLL is still running."""
    alive = {n: p for n, p in _injected.items() if _pid_alive(n, p)}
    _injected.clear()
    _injected.update(alive)
    if not alive and sys.platform.startswith("linux"):
        for n in list(_wine_prefixes):
            _inject_linux(n)
        alive = dict(_injected)
    return bool(alive)


# ---------------------------------------------------------------- XWayland helper process (Linux)
class _ServerBridge(QObject):
    got = pyqtSignal(dict)
    done = pyqtSignal()

    @pyqtSlot(dict)
    def on_got(self, d):
        if d.get("emoji_dir"):
            set_emoji_dir(d["emoji_dir"])
        if d.get("accent"):
            set_accent(str(d["accent"]))
        if "sunset" in d:
            set_sunset(bool(d["sunset"]))
        if "light" in d:
            set_light(bool(d["light"]))
        ch = d.get("chess")
        if isinstance(ch, dict) and ch.get("gid"):
            overlay.notify_chess_invite(str(d.get("user", "")), str(d.get("game", "")), str(ch["gid"]),
                                        str(ch.get("opp_uid", "")), d.get("avatar") or None, d.get("image") or None)
            return
        if d.get("link"):
            overlay.notify_invite(str(d.get("user", "")), str(d.get("game", "")), str(d["link"]),
                                  d.get("avatar") or None, d.get("image") or None)
            return
        if d.get("plain"):
            overlay.notify_text(str(d.get("user", "")))
            return
        overlay.notify_playing(str(d.get("user", "")), str(d.get("game", "")), d.get("avatar") or None,
                               str(d.get("prefix", "is playing ")), d.get("image") or None,
                               badge=d.get("badge") or None)


def run_server():
    """Helper process: draws the toasts the launcher sends as JSON lines on stdin, using the X11 (xcb) platform so
    the windows can be placed exactly, stay out of the taskbar and sit above XWayland (Proton) games.
    Exits when the launcher closes the pipe."""
    global _IS_HELPER
    _IS_HELPER = True
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
