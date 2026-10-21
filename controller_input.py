"""Controller -> keyboard/mouse translator (Windows, XInput). Like Steam Input's "keyboard + mouse" template.

Default layout (remap it from Settings > Controller Translator > Remap buttons):
  Left stick = WASD      Right stick = mouse      RT = left click     LT = right click
  A Space  B Ctrl  X R  Y E  LB Q  RB F  L3 Shift  Start Esc  Back Tab  D-pad = arrow keys
"""
import ctypes
import json
import math
import os
import sys
import threading
import time
from ctypes import wintypes

AVAILABLE = sys.platform.startswith("win")

STICK_KEY_THRESHOLD = 0.40   # left stick deflection that counts as a press
MOUSE_DEADZONE = 0.18
DEFAULT_SPEED = 12           # mouse pixels per tick at full right-stick deflection (~125 ticks/s)
TRIGGER_THRESHOLD = 40       # 0-255

# Every remappable controller input: (id, label shown in the remap screen, XInput button mask or None)
INPUTS = [
    ("A", "A", 0x1000), ("B", "B", 0x2000), ("X", "X", 0x4000), ("Y", "Y", 0x8000),
    ("LB", "Left Bumper", 0x0100), ("RB", "Right Bumper", 0x0200),
    ("L3", "Left Stick Click", 0x0040), ("R3", "Right Stick Click", 0x0080),
    ("Start", "Start", 0x0010), ("Back", "Back", 0x0020),
    ("DUp", "D-pad Up", 0x0001), ("DDown", "D-pad Down", 0x0002),
    ("DLeft", "D-pad Left", 0x0004), ("DRight", "D-pad Right", 0x0008),
    ("LT", "Left Trigger", None), ("RT", "Right Trigger", None),
    ("LSUp", "Left Stick Up", None), ("LSDown", "Left Stick Down", None),
    ("LSLeft", "Left Stick Left", None), ("LSRight", "Left Stick Right", None),
]
DEFAULTS = {
    "A": "Space", "B": "Ctrl", "X": "R", "Y": "E", "LB": "Q", "RB": "F", "L3": "Shift", "R3": "None",
    "Start": "Esc", "Back": "Tab",
    "DUp": "Up Arrow", "DDown": "Down Arrow", "DLeft": "Left Arrow", "DRight": "Right Arrow",
    "LT": "Right Click", "RT": "Left Click",
    "LSUp": "W", "LSDown": "S", "LSLeft": "A", "LSRight": "D",
}


def _build_actions():
    """name -> ("k", scancode, extended) | ("m", mouse button) | None"""
    acts = {"None": None}
    for n, s in [("Space", 0x39), ("Enter", 0x1C), ("Esc", 0x01), ("Tab", 0x0F), ("Shift", 0x2A),
                 ("Ctrl", 0x1D), ("Alt", 0x38), ("Backspace", 0x0E)]:
        acts[n] = ("k", s, 0)
    for n, s in [("Up Arrow", 0x48), ("Down Arrow", 0x50), ("Left Arrow", 0x4B), ("Right Arrow", 0x4D)]:
        acts[n] = ("k", s, 1)
    acts["Left Click"], acts["Right Click"], acts["Middle Click"] = ("m", "lclick"), ("m", "rclick"), ("m", "mclick")
    for letters, base in [("QWERTYUIOP", 0x10), ("ASDFGHJKL", 0x1E), ("ZXCVBNM", 0x2C)]:
        for i, ch in enumerate(letters):
            acts[ch] = ("k", base + i, 0)
    for i, ch in enumerate("1234567890"):
        acts[ch] = ("k", 0x02 + i, 0)
    for i in range(10):
        acts[f"F{i + 1}"] = ("k", 0x3B + i, 0)
    acts["F11"], acts["F12"] = ("k", 0x57, 0), ("k", 0x58, 0)
    return acts


ACTIONS = _build_actions()


def read_profile(path):
    """-> {input id: action name, "_mouse_speed": int}; missing/invalid entries fall back to defaults."""
    prof = dict(DEFAULTS)
    prof["_mouse_speed"] = DEFAULT_SPEED
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k == "_mouse_speed" and isinstance(v, (int, float)):
                prof[k] = max(2, min(40, int(v)))
            elif k in DEFAULTS and v in ACTIONS:
                prof[k] = v
    except Exception:
        pass
    return prof


def write_profile(path, prof):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prof, f, indent=1)

if AVAILABLE:
    class _GAMEPAD(ctypes.Structure):
        _fields_ = [("wButtons", wintypes.WORD), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                    ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                    ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]

    class _STATE(ctypes.Structure):
        _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", _GAMEPAD)]

    class _KI(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _MI(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", _KI), ("mi", _MI)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _xi = None
    for _n in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            _xi = ctypes.WinDLL(_n)
            break
        except OSError:
            pass
    AVAILABLE = _xi is not None
    if AVAILABLE:
        _xi.XInputGetState.argtypes = [wintypes.DWORD, ctypes.POINTER(_STATE)]

    def _send(items):
        if items:
            arr = (_INPUT * len(items))(*items)
            _user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT))

    def _key(scan, ext, up):
        flags = 0x0008 | (0x0001 if ext else 0) | (0x0002 if up else 0)   # SCANCODE | EXTENDED | KEYUP
        return _INPUT(1, _U(ki=_KI(0, scan, flags, 0, 0)))

    def _mouse(flags, dx=0, dy=0):
        return _INPUT(0, _U(mi=_MI(dx, dy, 0, flags, 0, 0)))

    _MOUSE_BTNS = {"lclick": (0x0002, 0x0004), "rclick": (0x0008, 0x0010), "mclick": (0x0020, 0x0040)}   # (down, up)


class ControllerTranslator(threading.Thread):
    """enabled_fn(): user setting.  active_fn(): True while a launcher-started game is running."""

    def __init__(self, enabled_fn, active_fn, profile_path=None):
        super().__init__(daemon=True)
        self.enabled, self.active = enabled_fn, active_fn
        self.held = set()          # ("k", scan, ext) or ("m", name)
        self.profile_path = profile_path
        self._prof, self._pmtime, self._ptime, self._speed = None, None, 0.0, float(DEFAULT_SPEED)

    def _profile(self):
        """Mapping {input id: action}; re-read when the remap screen saves a new file."""
        now = time.time()
        if self._prof is None or now - self._ptime > 1.0:
            self._ptime = now
            try:
                mt = os.path.getmtime(self.profile_path)
            except (OSError, TypeError):
                mt = 0
            if self._prof is None or mt != self._pmtime:
                self._pmtime = mt
                raw = read_profile(self.profile_path) if self.profile_path else dict(DEFAULTS, _mouse_speed=DEFAULT_SPEED)
                self._prof = {k: ACTIONS.get(v) for k, v in raw.items() if k != "_mouse_speed"}
                self._speed = float(raw.get("_mouse_speed", DEFAULT_SPEED))
        return self._prof

    def _foreground_is_us(self):
        try:
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), ctypes.byref(pid))
            return pid.value == os.getpid()
        except Exception:
            return False

    def _apply(self, want):
        out = []
        for h in self.held - want:
            out.append(_key(h[1], h[2], True) if h[0] == "k" else _mouse(_MOUSE_BTNS[h[1]][1]))
        for h in want - self.held:
            out.append(_key(h[1], h[2], False) if h[0] == "k" else _mouse(_MOUSE_BTNS[h[1]][0]))
        _send(out)
        self.held = set(want)

    def run(self):
        if not AVAILABLE:
            return
        ax = ay = 0.0
        while True:
            time.sleep(0.008)
            try:
                if not (self.enabled() and self.active()) or self._foreground_is_us():
                    if self.held:
                        self._apply(set())
                    time.sleep(0.25)
                    continue
                st = _STATE()
                if _xi.XInputGetState(0, ctypes.byref(st)) != 0:      # no controller connected
                    if self.held:
                        self._apply(set())
                    time.sleep(0.5)
                    continue
                g = st.Gamepad
                prof = self._profile()
                lx, ly = g.sThumbLX / 32768.0, g.sThumbLY / 32768.0
                T = STICK_KEY_THRESHOLD
                analog = {"LT": g.bLeftTrigger > TRIGGER_THRESHOLD, "RT": g.bRightTrigger > TRIGGER_THRESHOLD,
                          "LSUp": ly > T, "LSDown": ly < -T, "LSLeft": lx < -T, "LSRight": lx > T}
                want = set()
                for iid, _label, mask in INPUTS:
                    act = prof.get(iid)
                    if act and ((g.wButtons & mask) if mask is not None else analog[iid]):
                        want.add(act)
                self._apply(want)
                # right stick -> mouse look (curved, with sub-pixel accumulation)
                rx, ry = g.sThumbRX / 32768.0, g.sThumbRY / 32768.0
                mag = math.hypot(rx, ry)
                if mag > MOUSE_DEADZONE:
                    s = ((min(mag, 1.0) - MOUSE_DEADZONE) / (1 - MOUSE_DEADZONE)) ** 2 * self._speed
                    ax += rx / mag * s
                    ay += -ry / mag * s
                    ix, iy = int(ax), int(ay)
                    ax -= ix
                    ay -= iy
                    if ix or iy:
                        _send([_mouse(0x0001, ix, iy)])               # MOUSEEVENTF_MOVE (relative)
            except Exception:
                time.sleep(0.5)
