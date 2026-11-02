#!/usr/bin/env python3
"""ncz-gm - command line for the NCZ Games Launcher.

Lives next to main.py. It puts an `ncz-gm` command on your PATH (Linux: ~/.local/bin, Windows: a folder added to
your user PATH) the first time it runs, and again every time the launcher starts, so the command keeps pointing at
the right place.

    ncz-gm help
"""
import getpass
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

CLI_NAME = "ncz-gm"
IS_WIN = sys.platform.startswith("win")
FROZEN = bool(getattr(sys, "frozen", False))
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
PATH_MARK = "# added by ncz-gm"

HELP = """\
NCZ Games Launcher - command line

usage: ncz-gm <command> [arguments]

commands:
  login <email> [password]                sign in (every command except `demo` needs you to be signed in)
  workshop <mod id>                       download a Steam Workshop mod (an item link works too)
  download <game id> [path] [server]      download a Steam game (Steam app id); no path = your Settings install
                                          folder, no server = it asks. server: 1 = SteamRIP, 2 = FitGirl, 3 = AnkerGames
  big-pic                                 open Big Picture mode
  open                                    open the launcher
  open <game name>                        open (launch) a game from your library
  shiftq                                  open the Shift+Q overlay menu
  gmod-extract <path>                     extract Garry's Mod addons (a .gma / .bin file or a folder of them)
  update                                  update the launcher
  demo                                    delete account.json and open demo.py (back to the sign-in window)
                                          (works without being signed in)
  help                                    list all commands

extra:
  install / uninstall                     add / remove the `ncz-gm` command on your PATH

options:
  workshop      --login <steam name>      Steam account for games whose Workshop needs one (default: the launcher's)
  gmod-extract  --to <addons folder>      where to extract (default: garrysmod/addons of your Garry's Mod)
"""

_LAUNCHER = None      # the launcher's main.py as a module (loaded only by commands that need its code)
_QAPP = None


# ============================================================================================== colors
DEFAULT_ACCENT = "#a666ff"
_COLOR = None


def _accent_hex():
    """The launcher's accent colour (Settings > Accent Color), read straight from launcher.json."""
    try:
        with open(os.path.join(settings_dir(), "launcher.json"), encoding="utf-8") as f:
            c = json.load(f).get("accent_color")
        if isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c):
            return c.lower()
    except Exception:
        pass
    return DEFAULT_ACCENT


def _enable_vt():
    """Turns on ANSI colour handling in the Windows console (no-op elsewhere). True if it works."""
    if not IS_WIN:
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        ok = False
        for std in (-11, -12):                       # stdout, stderr
            h = k.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                ok = bool(k.SetConsoleMode(h, mode.value | 0x0004)) or ok     # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return ok
    except Exception:
        return False


def use_color():
    global _COLOR
    if _COLOR is None:
        out = sys.stdout
        _COLOR = bool(out is not None and getattr(out, "isatty", lambda: False)()
                      and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb" and _enable_vt())
    return _COLOR


def _rgb(hex_color=None):
    h = (hex_color or _accent_hex()).lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(rgb, other, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(rgb, other))


def _fg(rgb):
    return "\x1b[38;2;%d;%d;%dm" % rgb


_RESET, _BOLD, _DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"


def paint(text, kind="accent"):
    """accent / light (hover shade) / dark (pressed shade) / dim / bold / ok / warn / bad."""
    if not use_color():
        return text
    base = _rgb()
    if kind == "accent":
        return f"{_fg(base)}{text}{_RESET}"
    if kind == "light":
        return f"{_fg(_mix(base, (255, 255, 255), 0.35))}{text}{_RESET}"
    if kind == "dark":
        return f"{_fg(_mix(base, (0, 0, 0), 0.3))}{text}{_RESET}"
    if kind == "bold":
        return f"{_BOLD}{_fg(base)}{text}{_RESET}"
    if kind == "dim":
        return f"{_DIM}{text}{_RESET}"
    if kind == "ok":
        return f"{_fg((92, 214, 128))}{text}{_RESET}"
    if kind == "warn":
        return f"{_fg((255, 200, 87))}{text}{_RESET}"
    if kind == "bad":
        return f"{_fg((255, 107, 107))}{text}{_RESET}"
    return text


def gradient(text):
    """Text fading from the accent's light shade to its dark shade (banner / headings)."""
    if not use_color() or not text:
        return text
    base = _rgb()
    a, b = _mix(base, (255, 255, 255), 0.35), _mix(base, (0, 0, 0), 0.25)
    n = max(len(text) - 1, 1)
    return "".join(_fg(_mix(a, b, i / n)) + ch for i, ch in enumerate(text)) + _RESET


def _style_help(text):
    """Colours the help text with the accent: headings, command names, options."""
    if not use_color():
        return text
    out = []
    for line in text.splitlines():
        if line.startswith("NCZ Games Launcher"):
            line = _BOLD + gradient(line)
        elif line.startswith("usage:"):
            line = paint("usage:", "bold") + line[6:].replace("ncz-gm", paint("ncz-gm", "light"), 1)
        elif re.fullmatch(r"[a-z]+:", line):
            line = paint(line, "bold")
        else:
            m = re.match(r"^(  )([a-z-]+(?: / [a-z-]+)?)(\s.*)$", line)
            if m and not line.startswith("                "):
                line = m.group(1) + paint(m.group(2), "accent") + m.group(3).replace("<", _DIM + "<").replace(">", ">" + _RESET)
            elif line.startswith("                "):
                line = paint(line, "dim")
        out.append(line)
    return "\n".join(out)


# ============================================================================================== small helpers
def say(text=""):
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode(), flush=True)


def err(text):
    try:
        if use_color() and sys.stderr is sys.stdout:
            text = paint(text, "bad")
        elif use_color() and getattr(sys.stderr, "isatty", lambda: False)():
            text = paint(text, "bad")
        print(text, file=sys.stderr, flush=True)
    except Exception:
        pass


def _attach_console():
    """A windowed Windows build has no console of its own: borrow the one it was started from."""
    if not (IS_WIN and FROZEN):
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
    except Exception:
        pass


def _fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class Progress:
    """One-line progress bar on a terminal, a line every 10% otherwise."""
    def __init__(self):
        self.tty = sys.stdout is not None and sys.stdout.isatty()
        self.last, self.step, self.open = 0.0, -1, False

    def _clear(self):
        if self.open:
            print(flush=True)
            self.open = False

    def bar(self, done, total, speed=0.0):
        now = time.time()
        pct = int(done * 100 / total) if total else -1
        if self.tty:
            if now - self.last < 0.25 and pct < 100:
                return
            self.last = now
            width = 24
            fill = int(width * pct / 100) if pct >= 0 else 0
            if pct >= 0:
                bar = paint("\u2588" * fill, "accent") + paint("\u2591" * (width - fill), "dark") if use_color() \
                    else "[" + "#" * fill + "-" * (width - fill) + "]"
                head = f"{bar} {paint(f'{pct:3d}%', 'bold')}  {_fmt_size(done)} / {_fmt_size(total)}"
            else:
                head = paint(_fmt_size(done), "accent")
            tail = "  " + paint(f"{_fmt_size(speed)}/s", "light") if speed else ""
            sys.stdout.write(f"\r  {head}{tail}      ")
            sys.stdout.flush()
            self.open = True
        elif pct >= 0 and pct // 10 != self.step:
            self.step = pct // 10
            say(f"  {paint(str(pct) + '%', 'accent')}  {_fmt_size(done)} / {_fmt_size(total)}")

    def percent(self, pct):
        self.bar(pct, 100)

    def line(self, text):
        self._clear()
        say(text)

    def done(self):
        self._clear()


# ============================================================================================== paths / launcher
def _profile():
    return re.sub(r"[^A-Za-z0-9_-]", "_", os.environ.get("NCZ_PROFILE", "").strip())


def settings_dir():
    """Same folder as the launcher's launcher.json / account.json."""
    if IS_WIN:
        base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local")), "NCZ_Games_Launcher")
    else:
        base = os.path.expanduser("~/.config/NCZ_Games_Launcher")
    return f"{base}_{_profile()}" if _profile() else base


def instance_name():
    user = re.sub(r"[^A-Za-z0-9]", "", os.environ.get("USERNAME") or os.environ.get("USER") or "user")
    return f"NCZGamesLauncher-{user}-{_profile()}"


def main_py():
    for p in (os.environ.get("NCZ_MAIN", ""), os.path.join(HERE, "main.py"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")):
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    return None


def python_console():
    """The interpreter to run this script with (python.exe, not pythonw.exe, so output shows)."""
    exe = sys.executable
    if IS_WIN and os.path.basename(exe).lower() == "pythonw.exe":
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


_PY_NAME = re.compile(r"^(python|pythonw|pypy|pypyw)[\d.]*(\.exe)?$", re.I)


def _looks_like_python(path):
    return bool(path) and bool(_PY_NAME.match(os.path.basename(path))) and os.path.isfile(path)


def real_python():
    """A genuine Python interpreter (console version) to run this script with, or None.
    sys.executable can be the launcher's own stub exe (an embedded / frozen build); a shim that runs
    `<launcher exe> ncz_gm.py` ignores its arguments and just starts the launcher again - so it's checked."""
    cands = [python_console(), getattr(sys, "_base_executable", None)]
    base = getattr(sys, "_base_executable", None) or sys.executable
    cands.append(os.path.join(os.path.dirname(base), "python.exe" if IS_WIN else "python3"))
    for p in cands:
        if p and IS_WIN and os.path.basename(p).lower() == "pythonw.exe":
            q = os.path.join(os.path.dirname(p), "python.exe")
            p = q if os.path.exists(q) else p
        if _looks_like_python(p):
            return p
    for name in (("python", "py") if IS_WIN else ("python3", "python")):
        found = shutil.which(name)
        if found and not _is_launcher_exe(found):
            return found
    return None


def _is_launcher_exe(path):
    try:
        return os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(sys.executable)) \
            and not _looks_like_python(sys.executable)
    except Exception:
        return False


def launcher_command():
    """(argv, cwd) that starts the launcher the way it is installed here."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return [appimage], os.path.dirname(os.path.abspath(appimage))
    if FROZEN and not main_py():
        return [sys.executable], os.path.dirname(os.path.abspath(sys.executable))
    mp = main_py()
    if not mp:
        raise RuntimeError("main.py not found next to ncz_gm.py (set NCZ_MAIN to its path)")
    exe = sys.executable
    if IS_WIN:
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
    return [exe, mp], os.path.dirname(mp)


def spawn_detached(argv, cwd):
    kw = {}
    if IS_WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    env = dict(os.environ)
    if FROZEN:
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **kw)


def start_launcher(extra=()):
    if _lock_owner_alive():
        raise RuntimeError("The launcher is already running but isn't answering yet - try again in a few seconds.")
    argv, cwd = launcher_command()
    spawn_detached(argv + list(extra), cwd)


def load_launcher():
    """main.py as a module, for the commands that reuse the launcher's own download / extract code."""
    global _LAUNCHER, _QAPP
    if _LAUNCHER is not None:
        return _LAUNCHER
    mp = main_py()
    if not mp:
        raise RuntimeError("main.py not found next to ncz_gm.py (set NCZ_MAIN to its path)")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"          # no window is ever shown by the CLI
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        raise RuntimeError("PyQt6 isn't installed for this Python (pip install PyQt6) - the launcher needs it too")
    _QAPP = QApplication.instance() or QApplication([sys.argv[0]])
    sys.path.insert(0, os.path.dirname(mp))
    spec = importlib.util.spec_from_file_location("ncz_launcher_main", mp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ncz_launcher_main"] = mod
    spec.loader.exec_module(mod)
    _LAUNCHER = mod
    return mod


# ============================================================================================== talking to the launcher
_QCORE = None


def _send_qt(msg):
    """Same QLocalSocket the launcher itself uses. True = a launcher answered; raises if Qt isn't usable."""
    global _QCORE
    from PyQt6.QtCore import QCoreApplication
    from PyQt6.QtNetwork import QLocalSocket
    if QCoreApplication.instance() is None:
        _QCORE = QCoreApplication([])
    sock = QLocalSocket()
    sock.connectToServer(instance_name())
    if not sock.waitForConnected(800):
        return False
    if msg:
        sock.write(msg)
        sock.flush()
        sock.waitForBytesWritten(800)
    sock.disconnectFromServer()
    return True


def _send_plain(msg):
    """No PyQt: speak to Qt's local server directly (unix socket in the temp folder / Windows named pipe)."""
    name = instance_name()
    if IS_WIN:
        try:
            with open("\\\\.\\pipe\\" + name, "r+b", buffering=0) as f:
                if msg:
                    f.write(msg)
            return True
        except OSError:
            return False
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(os.path.join(os.environ.get("TMPDIR") or tempfile.gettempdir(), name))
        if msg:
            s.sendall(msg)
        return True
    except OSError:
        return False
    finally:
        s.close()


def send_to_launcher(msg=b"", wait=0.0):
    """Sends a request to the running launcher (b'' only checks that one is running). True if one answered."""
    deadline = time.time() + wait
    while True:
        try:
            ok = _send_qt(msg)
        except Exception:
            ok = _send_plain(msg)
        if ok:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.4)


def _lock_owner_alive():
    """The launcher's lock file (QLockFile: first line = pid) names a live process. Catches a launcher that is
    still starting up or whose socket we couldn't reach - starting another one would replace it (= a restart)."""
    try:
        with open(os.path.join(tempfile.gettempdir(), instance_name() + ".lock"), encoding="utf-8") as f:
            pid = int(f.readline().strip())
    except Exception:
        return False
    if pid == os.getpid():
        return False
    if IS_WIN:
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return False
    return os.path.exists(f"/proc/{pid}") if os.path.isdir("/proc") else _pid_alive_posix(pid)


def _pid_alive_posix(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def launcher_running():
    return send_to_launcher(b"") or _lock_owner_alive()


# ============================================================================================== putting `ncz-gm` on PATH
def _self_invocation():
    """argv that runs this CLI."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return [appimage, "--cli"]
    script = os.path.abspath(__file__)
    if FROZEN:
        # A stub/packaged exe that runs main.py from disk doesn't understand --cli: when ncz_gm.py is a real file on disk
        # (not inside the exe's own bundle) and a Python is installed, run the script with that Python instead.
        bundle = getattr(sys, "_MEIPASS", "")
        if os.path.isfile(script) and not (bundle and script.startswith(bundle)):
            py = shutil.which("python") or shutil.which("py") or shutil.which("python3")
            if py:
                return [py, script]
        return [sys.executable, "--cli"]
    py = real_python()
    if not py:
        raise RuntimeError("couldn't find a Python interpreter to run ncz-gm with (is Python installed and on PATH?)")
    return [py, script]


def _bin_dir():
    if IS_WIN:
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local")),
                            "NCZ_Games_Launcher", "bin")
    return os.path.expanduser("~/.local/bin")


def _shim_path():
    return os.path.join(_bin_dir(), CLI_NAME + (".cmd" if IS_WIN else ""))


def _shim_text():
    argv = _self_invocation()
    if IS_WIN:
        q = " ".join('"%s"' % a for a in argv)
        if "--cli" in argv:                    # only a windowed exe needs this; a Python script runs in the console itself
            q = "start \"\" /wait " + q        # a windowed exe: wait for it so the prompt comes back after the output
        return "@echo off\r\n%s %%*\r\nexit /b %%errorlevel%%\r\n" % q
    return "#!/bin/sh\nexec %s \"$@\"\n" % " ".join(shlex.quote(a) for a in argv)


def _norm_dir(p):
    return os.path.normcase(os.path.normpath(os.path.expandvars(os.path.expanduser(p)))).rstrip("\\/")


def _on_path_env(d):
    return any(_norm_dir(p) == _norm_dir(d) for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def _add_to_windows_path(d):
    """Adds d to the *user* PATH (registry) and tells running programs. True if it changed anything."""
    import winreg
    import ctypes
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            cur, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            cur, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in cur.split(";") if p]
        if any(_norm_dir(p) == _norm_dir(d) for p in parts):
            return False
        parts.append(d)
        winreg.SetValueEx(key, "Path", 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
                          else winreg.REG_EXPAND_SZ, ";".join(parts))
    try:
        res = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(res))
    except Exception:
        pass
    return True


def _remove_from_windows_path(d):
    import winreg
    import ctypes
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            cur, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return False
        parts = [p for p in cur.split(";") if p]
        keep = [p for p in parts if _norm_dir(p) != _norm_dir(d)]
        if len(keep) == len(parts):
            return False
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(keep))
    try:
        res = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(res))
    except Exception:
        pass
    return True


def _add_to_unix_path():
    """~/.local/bin is on PATH on most distros once it exists, but not all: make sure of it. Returns files edited."""
    home = os.path.expanduser("~")
    line = f'export PATH="$HOME/.local/bin:$PATH"  {PATH_MARK}\n'
    edited = []
    rcs = [os.path.join(home, ".profile")] + [os.path.join(home, n) for n in (".bashrc", ".zshrc")
                                              if os.path.exists(os.path.join(home, n))]
    for rc in rcs:
        try:
            text = open(rc, encoding="utf-8", errors="replace").read() if os.path.exists(rc) else ""
        except OSError:
            continue
        if ".local/bin" in text:
            continue
        try:
            with open(rc, "a", encoding="utf-8") as f:
                f.write(("" if not text or text.endswith("\n") else "\n") + line)
            edited.append(rc)
        except OSError:
            pass
    fish = os.path.join(home, ".config", "fish")
    if os.path.isdir(fish):
        conf = os.path.join(fish, "conf.d", "ncz-gm.fish")
        if not os.path.exists(conf):
            try:
                os.makedirs(os.path.dirname(conf), exist_ok=True)
                with open(conf, "w", encoding="utf-8") as f:
                    f.write(f"fish_add_path -g $HOME/.local/bin  {PATH_MARK}\n")
                edited.append(conf)
            except OSError:
                pass
    return edited


def install_path(quiet=False):
    """Creates the `ncz-gm` command and puts its folder on PATH. Safe to call any number of times.
    Returns True if anything changed. Never raises."""
    changed, notes = False, []
    try:
        d, shim, text = _bin_dir(), _shim_path(), _shim_text()
        os.makedirs(d, exist_ok=True)
        try:
            with open(shim, "r", encoding="utf-8", newline="") as f:
                same = f.read() == text
        except OSError:
            same = False
        if not same and FROZEN and "--cli" in text and os.path.exists(shim):
            try:                         # never replace a working `python ncz_gm.py` shim with the exe form
                with open(shim, encoding="utf-8") as f:
                    if "--cli" not in f.read():
                        same = True
            except OSError:
                pass
        if not same:
            with open(shim, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            if not IS_WIN:
                os.chmod(shim, 0o755)
            changed = True
            notes.append(f"created {shim}")
        if IS_WIN:
            if _add_to_windows_path(d):
                changed = True
                notes.append(f"added {d} to your user PATH")
        elif not _on_path_env(d):
            files = _add_to_unix_path()
            if files:
                changed = True
                notes.append("added ~/.local/bin to PATH in " + ", ".join(files))
    except Exception as e:
        if not quiet:
            err(f"couldn't set up the {CLI_NAME} command: {e}")
        return False
    if changed or not quiet:
        for n in notes:
            say("  " + paint(n, "dim"))
        if changed:
            say(paint(f"\u2714 {CLI_NAME} is installed.", "ok") + " Open a new terminal window to use it.")
        elif not quiet:
            say(paint(f"\u2714 {CLI_NAME} is already installed", "ok") + paint(f" ({_shim_path()})", "dim"))
    return changed


def uninstall_path():
    removed = []
    shim = _shim_path()
    if os.path.exists(shim):
        os.remove(shim)
        removed.append(shim)
    if IS_WIN:
        try:
            if _remove_from_windows_path(_bin_dir()):
                removed.append("user PATH entry")
        except Exception:
            pass
    else:
        for rc in (os.path.expanduser(p) for p in ("~/.profile", "~/.bashrc", "~/.zshrc")):
            try:
                lines = open(rc, encoding="utf-8", errors="replace").read().splitlines(True)
            except OSError:
                continue
            keep = [l for l in lines if PATH_MARK not in l]
            if len(keep) != len(lines):
                with open(rc, "w", encoding="utf-8") as f:
                    f.writelines(keep)
                removed.append(rc)
        fish = os.path.expanduser("~/.config/fish/conf.d/ncz-gm.fish")
        if os.path.exists(fish):
            os.remove(fish)
            removed.append(fish)
    say("removed: " + ", ".join(removed) if removed else f"{CLI_NAME} wasn't installed.")
    return 0


# ============================================================================================== game names
def _norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _known_titles():
    titles = ["Five Nights at NCZ AE", "Five Nights at NCZ 2"]
    for fn in ("steam_library.json", "custom_games.json"):
        try:
            with open(os.path.join(settings_dir(), fn), encoding="utf-8") as f:
                for g in json.load(f):
                    if isinstance(g, dict) and g.get("title"):
                        titles.append(str(g["title"]))
        except Exception:
            pass
    return list(dict.fromkeys(titles))


def resolve_game(query):
    """(title, error). Exact name first, then a name that starts with / contains what was typed."""
    titles = _known_titles()
    want = _norm(query)
    if not want:
        return None, "no game name given"
    for tier in (lambda t: _norm(t) == want, lambda t: _norm(t).startswith(want), lambda t: want in _norm(t)):
        hits = [t for t in titles if tier(t)]
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, "more than one game matches \"%s\":\n  %s" % (query, "\n  ".join(hits))
    return query, None            # not in the lists we can read: let the launcher try the name as typed


# ============================================================================================== sign-in
# Same Firebase project the launcher and demo.py use. Read from main.py when it's next to us so there's one source of truth.
_FB_KEY = "AIzaSyBEphV3IipXeUUIpgP6XYrtPG3RrZ-wPt4"
_FB_DB = "https://ncz-games-launcher-default-rtdb.europe-west1.firebasedatabase.app"
_LOGIN_ERRORS = {
    "INVALID_LOGIN_CREDENTIALS": "Wrong email or password.",
    "INVALID_PASSWORD": "Wrong email or password.",
    "EMAIL_NOT_FOUND": "Wrong email or password.",
    "INVALID_EMAIL": "That doesn't look like a valid email address.",
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many attempts. Please wait a bit and try again.",
    "USER_DISABLED": "This account has been disabled.",
}


def _firebase_config():
    key, db = _FB_KEY, _FB_DB
    mp = main_py()
    if mp:
        try:
            with open(mp, encoding="utf-8", errors="replace") as f:
                head = f.read(400000)
            k = re.search(r'^FIREBASE_API_KEY\s*=\s*"([^"]+)"', head, re.M)
            d = re.search(r'^FIREBASE_DB_URL\s*=\s*"([^"]+)"', head, re.M)
            key, db = (k.group(1) if k else key), (d.group(1) if d else db)
        except OSError:
            pass
    return key, db.rstrip("/")


def _account_file():
    return os.path.join(settings_dir(), "account.json")


def _ban_flag_file():
    return os.path.join(settings_dir(), "launcher_ban.json")


def is_logged_in():
    """Same test the launcher uses: account.json holds a refresh token and a uid (and the account isn't banned)."""
    if os.path.exists(_ban_flag_file()):
        return False
    try:
        with open(_account_file(), encoding="utf-8") as f:
            acct = json.load(f)
        return isinstance(acct, dict) and bool(acct.get("refresh_token")) and bool(acct.get("uid"))
    except Exception:
        return False


def _fb_json(url, payload=None, method=None):
    headers = {"User-Agent": "NCZ-Games-Launcher"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace"))["error"]["message"]
        except Exception:
            msg = ""
        code = str(msg).split(" : ")[0].strip()
        raise RuntimeError(_LOGIN_ERRORS.get(code) or f"Sign-in failed ({code or 'HTTP %s' % e.code}).")
    except (urllib.error.URLError, OSError):
        raise RuntimeError("Couldn't reach the cloud. Check your internet connection.")
    return json.loads(body) if body.strip() else None


def _discord_banned():
    """Best effort, same check demo.py does on sign-in: a banned Discord id blocks it. Needs demo.py + PyQt6 + pypresence;
    when any of those is missing (or Discord is closed) it lets the sign-in through, exactly like demo.py does."""
    demo = os.path.join(os.path.dirname(main_py() or HERE), "demo.py")
    if not os.path.isfile(demo):
        return None
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        spec = importlib.util.spec_from_file_location("ncz_demo_check", demo)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        res = mod.discord_check(False)
        if res.get("status") == "banned":
            mod.write_ban_flag(res.get("user_id"))
            return res.get("user_id")
    except BaseException:
        pass
    return None


def cmd_login(args):
    if not 1 <= len(args) <= 2:
        err("usage: ncz-gm login <email> [password]")
        return 2
    email = args[0].strip()
    if "@" not in email:
        err("That doesn't look like a valid email address.")
        return 2
    if os.path.exists(_ban_flag_file()):
        err("This device can't sign in.")
        return 1
    if len(args) == 2:
        password = args[1]
    else:                                  # leaving it off keeps it out of your shell history
        try:
            password = getpass.getpass("Password: ")
        except (EOFError, KeyboardInterrupt):
            say()
            return 130
    if not password:
        err("No password given.")
        return 2
    key, db = _firebase_config()
    say(paint("\u27f3", "accent") + " Signing in...")
    res = _fb_json("https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key="
                   + urllib.parse.quote(key, safe=""),
                   {"email": email, "password": password, "returnSecureToken": True})
    uid, token = res["localId"], res["idToken"]
    profile = _fb_json(f"{db}/users/{urllib.parse.quote(uid, safe='')}/profile.json?auth={urllib.parse.quote(token, safe='')}")
    if profile is None:
        err("This account isn't set up yet. Sign in once in the launcher window to finish creating it.")
        return 1
    banned = _discord_banned()
    if banned:
        err("This device can't sign in.")
        return 1
    acct = {"email": res.get("email", email), "uid": uid, "refresh_token": res["refreshToken"]}
    path = _account_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(acct, f)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    say(paint("\u2714 Signed in as", "ok") + " " + paint(acct["email"], "bold"))
    return 0


# ============================================================================================== commands
def cmd_open(args):
    if not args:
        if launcher_running():
            send_to_launcher(b"show")
            say(paint("\u2714", "ok") + " Launcher is already running - brought it to the front.")
        else:
            start_launcher()
            say(paint("\u25b6", "accent") + " Opening the launcher...")
        return 0
    title, e = resolve_game(" ".join(args))
    if e:
        err(e)
        return 1
    if launcher_running():
        send_to_launcher(b"launch:" + title.encode("utf-8"))
    else:
        start_launcher(["--launch-game", urllib.parse.quote(title, safe="")])
    say(paint("\u25b6", "accent") + " Opening " + paint(title, "bold") + "...")
    return 0


def cmd_bigpic(_args):
    if launcher_running():
        send_to_launcher(b"bigpicture")
    else:
        start_launcher(["--bigpicture"])
    say(paint("\u25b6", "accent") + " Opening " + paint("Big Picture", "bold") + "...")
    return 0


def cmd_shiftq(_args):
    if not launcher_running():
        say(paint("!", "warn") + " The launcher isn't running - starting it first...")
        start_launcher()
        if not send_to_launcher(b"", wait=90):
            err("The launcher didn't start.")
            return 1
        time.sleep(8)                  # the overlay exists once the window is built
    send_to_launcher(b"friends:cli")
    say(paint("\u2714", "ok") + " Toggled the Shift+Q overlay.")
    return 0


def _take_flag(args, flag):
    """Removes `flag value` from args; returns (value or None, rest)."""
    rest, val, i = [], None, 0
    while i < len(args):
        if args[i] == flag and i + 1 < len(args):
            val = args[i + 1]
            i += 2
        elif args[i].startswith(flag + "="):
            val = args[i].split("=", 1)[1]
            i += 1
        else:
            rest.append(args[i])
            i += 1
    return val, rest


def cmd_workshop(args):
    login, args = _take_flag(args, "--login")
    if len(args) != 1:
        err("usage: ncz-gm workshop <mod id>")
        return 2
    m = re.search(r"[?&]id=(\d+)", args[0]) or re.fullmatch(r"\s*(\d+)\s*", args[0])
    if not m:
        err(f"\"{args[0]}\" isn't a Workshop item id or link.")
        return 2
    item_id = m.group(1)
    mod = load_launcher()
    found, problems = [], []
    lw = mod.WorkshopLinkWorker([item_id], {})
    lw.done.connect(lambda results, errors: (found.extend(results), problems.extend(errors)))
    lw.run()
    if not found:
        err("; ".join(problems) or f"Couldn't look up item {item_id}.")
        return 1
    appid, game, item = found[0]
    say(paint(item['title'], "bold") + "  -  " + paint(f"{game} (app {appid})", "light"))
    prog, result = Progress(), {}
    w = mod.SteamCmdWorker(appid, item_id, login or mod.ws_cfg_get("steam_login", ""))
    w.progress.connect(prog.percent)
    w.status.connect(lambda t: None if "progress:" in t else prog.line("  " + t))
    w.finished_item.connect(lambda ok, msg: result.update(ok=ok, msg=msg))
    try:
        w.run()
    except KeyboardInterrupt:
        w.cancel()
        prog.done()
        err("Cancelled.")
        return 130
    prog.done()
    if not result.get("ok"):
        err("Download failed: " + (result.get("msg") or "unknown error"))
        return 1
    downloads = [d for d in mod.ws_load_downloads() if not (str(d["id"]) == item_id and d["appid"] == appid)]
    downloads.append({"appid": appid, "id": item_id, "title": item["title"], "game": game,
                      "preview": item.get("preview", ""), "author": item.get("author", ""), "time": int(time.time())})
    mod.ws_save_downloads(downloads)
    path = mod.locate_workshop_item(appid, item_id)
    say(paint("\u2714 Downloaded to:", "ok") + f" {path}")
    if int(appid) == 4000:
        say(f"Garry's Mod addon - install it with:  " + paint(f"ncz-gm gmod-extract \"{path}\"", "accent"))
    return 0


def ask_server(mod, title):
    """Terminal version of the launcher's "Choose download server" popup (it also checks which servers have the game)."""
    import threading
    state = {}
    for sid in mod.PW_SERVER_NAMES:
        state[sid] = "checking"
        def probe(sid=sid):
            try:
                state[sid] = mod._pw_probe_server(title, sid)
            except Exception:
                state[sid] = "unknown"
        threading.Thread(target=probe, daemon=True).start()
    end = time.time() + 12
    while "checking" in state.values() and time.time() < end:
        time.sleep(0.2)
    label = {"ok": "available", "404": "not found", "unknown": "couldn't check", "checking": "couldn't check"}
    say(paint("Where do you want to download ", "bold") + paint(title, "accent") + paint(" from?", "bold"))
    for sid, (name, desc) in mod.PW_SERVER_NAMES.items():
        _k = {"ok": "ok", "404": "bad"}.get(state[sid], "warn")
        say(f"  {paint(sid + ')', 'accent')} {paint(name, 'bold')} - {desc}  " + paint(f"[{label[state[sid]]}]", _k))
    while True:
        try:
            pick = input(paint("Server", "accent") + " (1/2/3, empty to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            say()
            return None
        if not pick:
            return None
        if pick in mod.PW_SERVER_NAMES:
            return pick
        say("Please type 1, 2 or 3.")


def cmd_download(args):
    if not 1 <= len(args) <= 3:
        err("usage: ncz-gm download <game id> [path] [server 1/2/3]")
        return 2
    gid = args[0]
    rest = args[1:]
    path_arg = server_arg = None
    if len(rest) == 2:
        path_arg, server_arg = rest
    elif len(rest) == 1:            # `download <id> 2` = a server; anything else = a path
        if rest[0] in ("1", "2", "3") and not os.path.isdir(rest[0]):
            server_arg = rest[0]
        else:
            path_arg = rest[0]
    if not gid.isdigit():
        err(f"\"{gid}\" isn't a Steam game id (the number in the store link, e.g. 1091500).")
        return 2
    if server_arg is not None and server_arg not in ("1", "2", "3"):
        err("server must be 1 (SteamRIP), 2 (FitGirl) or 3 (AnkerGames).")
        return 2
    mod = load_launcher()
    server = server_arg
    appid = int(gid)
    title = None
    try:
        import json as _json
        raw = mod._http_get(f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic", timeout=15)
        title = _json.loads(raw.decode("utf-8", "replace"))[str(appid)]["data"]["name"]
    except Exception:
        title = next((g["title"] for g in mod.load_steam_library() if g["appid"] == appid), None)
    if not title:
        err(f"Couldn't find a Steam game with id {appid}.")
        return 1
    if server is None:
        server = ask_server(mod, title)
        if server is None:
            err("Cancelled.")
            return 1
    if path_arg:
        dest = os.path.abspath(os.path.expanduser(path_arg))
    else:                                   # no path given: the install folder from the launcher's Settings
        mod.apply_install_dir()
        dest = os.path.abspath(mod.GAMES_DIR)
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        err(f"Can't use {dest}: {e}")
        return 1
    name, desc = mod.PW_SERVER_NAMES[server]
    say(paint(title, "bold") + paint(f" (app {appid})", "dim") + f" from server {paint(server, 'accent')} - {name}, {desc}")
    say(paint("Installing into:", "light") + f" {os.path.join(dest, mod.sanitize_folder_name(title))}")

    # This process only does this one download, so the launcher's settings are overridden in memory (nothing is saved):
    # the install folder, and the server (instead of its Settings / "Ask every time" popup).
    mod.GAMES_DIR = dest
    mod.DOWNLOAD_DIR = os.path.join(dest, "")
    if path_arg:                            # an explicit path wins over games already installed elsewhere
        mod.installed_game_dir = lambda game_name: os.path.join(dest, game_name)
    mod.pw_server = lambda: server
    mod._pw_ask_server = lambda worker: server
    try:
        mod._pw_tls.rx_server = server
    except Exception:
        pass

    prog, result = Progress(), {}
    w = mod.FirebaseDownloadWorker(title, mod.pw_game_url(title, server), 0, existing_zip_path=None)
    w.progress.connect(lambda done, total, speed: prog.bar(done, total or 0, speed))
    w.status_update.connect(lambda t: prog.line("  " + t.replace("\n", "\n  ")))
    w.finished.connect(lambda p: result.update(ok=True, path=p))
    w.failed.connect(lambda m: result.update(ok=False, msg=m))
    try:
        w.run()
    except KeyboardInterrupt:
        w.cancel()
        prog.done()
        err("Cancelled.")
        return 130
    prog.done()
    if not result.get("ok"):
        msg = result.get("msg") or "unknown error"
        err("Cancelled." if msg == "CANCELLED" else "Download failed: " + msg)
        return 1

    # make the game show up as installed / in the library the next time the launcher starts
    game_dir = os.path.join(dest, mod.sanitize_folder_name(title))
    try:
        exe = getattr(w, "game_exe", None) or mod.find_best_game_exe(game_dir, title)
        if exe and os.path.isfile(exe):
            mod.record_installed_exe(title, exe)
        games = mod.load_steam_library()
        if not any(g["appid"] == appid for g in games):
            games.append({"appid": appid, "title": title, "cover": ""})
            mod.save_steam_library(games)
    except Exception:
        pass
    say(paint("\u2714 Done:", "ok") + f" {game_dir}")
    archive = result.get("path")
    if archive and os.path.isfile(archive):
        say(f"(the downloaded archive was kept at {archive} - delete it to save space)")
    return 0


def cmd_gmod_extract(args):
    to, args = _take_flag(args, "--to")
    if len(args) != 1:
        err("usage: ncz-gm gmod-extract <path>")
        return 2
    src = os.path.abspath(os.path.expanduser(args[0]))
    if not os.path.exists(src):
        err(f"{src} doesn't exist.")
        return 1
    mod = load_launcher()
    if to:
        addons = os.path.abspath(os.path.expanduser(to))
    else:
        root = mod.find_gmod_root()
        if not root:
            err("Couldn't find Garry's Mod. Install it through the launcher, or pass --to <garrysmod/addons folder>.")
            return 1
        addons = os.path.join(root, "garrysmod", "addons")
    os.makedirs(os.path.join(addons, "disabled"), exist_ok=True)
    say(paint("Extracting into:", "light") + f" {addons}")

    def log(text, kind="info"):
        say(paint("  [!] " + text, "bad" if kind == "error" else "warn") if kind in ("warn", "error") else "  " + text)

    if os.path.isdir(src):
        done, skipped, failed = mod.extract_workshop_folder(src, addons, log, lambda a, b: None)
        return 1 if failed else 0
    stem = os.path.splitext(os.path.basename(src))[0]
    tmp = os.path.join(addons, f".extract_tmp_{os.getpid()}")
    try:
        title = mod.extract_gma(mod.read_workshop_file(src), tmp)
        safe = mod.safe_addon_name(title, stem)
        final = os.path.join(addons, safe)
        if os.path.isdir(final) or os.path.isdir(os.path.join(addons, "disabled", safe + "(Disabled)")):
            log(f'Directory "{safe}" already exists, skipped', "warn")
            shutil.rmtree(tmp, ignore_errors=True)
            return 0
        os.replace(tmp, final)
        log(f'Extracted "{title or safe}"')
        return 0
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        log(f"Failed to extract {os.path.basename(src)}: {e}", "error")
        return 1


def cmd_update(_args):
    mod = load_launcher()
    if FROZEN:
        say("This is a packaged build; it can't update itself. Download the new version from:")
        say("  " + mod._launcher_update_url())
        return 0
    say(paint("\u27f3", "accent") + " Checking for updates...")
    try:
        status = mod.manual_update_check()
    except Exception as e:
        err(f"Update check failed: {e}")
        return 1
    if status == "updated":
        say(paint("\u2714 Updated to the latest version.", "ok") + (" Restart the launcher to apply it." if launcher_running() else ""))
    else:
        say(paint("\u2714", "ok") + " Already up to date.")
    return 0


def cmd_demo(_args):
    demo = os.path.join(os.path.dirname(main_py() or HERE), "demo.py")
    if FROZEN or os.environ.get("APPIMAGE") or not os.path.isfile(demo):
        err("demo.py wasn't found next to main.py (packaged builds don't have it).")
        return 1
    if launcher_running():                      # close it first, or it would write account.json again
        send_to_launcher(b"quit")
        for _ in range(40):
            time.sleep(0.25)
            if not launcher_running():
                break
    acct = os.path.join(settings_dir(), "account.json")
    try:
        os.remove(acct)
        say(paint("\u2714 Deleted", "ok") + f" {acct}")
    except FileNotFoundError:
        say("No account.json (already signed out).")
    except OSError as e:
        err(f"Couldn't delete {acct}: {e}")
        return 1
    exe = sys.executable
    if IS_WIN and os.path.basename(exe).lower() == "python.exe":
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
    spawn_detached([exe, demo], os.path.dirname(demo))
    say(paint("\u25b6", "accent") + " Opening " + paint("demo.py", "bold") + "...")
    return 0


def cmd_install(_args):
    install_path()
    return 0


def cmd_uninstall(_args):
    return uninstall_path()


def cmd_help(_args):
    say(_style_help(HELP))
    return 0


COMMANDS = {
    "login": cmd_login, "workshop": cmd_workshop, "download": cmd_download, "big-pic": cmd_bigpic, "bigpic": cmd_bigpic,
    "big-picture": cmd_bigpic, "open": cmd_open, "shiftq": cmd_shiftq, "gmod-extract": cmd_gmod_extract,
    "update": cmd_update, "demo": cmd_demo, "install": cmd_install, "uninstall": cmd_uninstall,
    "help": cmd_help, "-h": cmd_help, "--help": cmd_help,
}


def main(argv=None, launcher=None):
    """Entry point. `launcher` is the already-loaded main.py module when the launcher itself runs the CLI
    (packaged builds start it as `<exe> --cli ...`)."""
    global _LAUNCHER
    if launcher is not None:
        _LAUNCHER = launcher
    _attach_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not os.environ.get("NCZ_GM_NO_INSTALL") and (not argv or argv[0].lower() not in ("install", "uninstall")):
        install_path(quiet=True)           # first run: add the command to PATH (a no-op once it's there)
    name = argv[0].lower() if argv else ""
    if name not in ("login", "demo") and not is_logged_in():          # only `login` and `demo` work signed out
        err("You're not signed in. Sign in first:  ncz-gm login <email> [password]")
        return 1
    if not argv:
        return cmd_help([])
    cmd = COMMANDS.get(name)
    if cmd is None:
        err(f"unknown command: {argv[0]}\n")
        say(_style_help(HELP))
        return 2
    try:
        return cmd(argv[1:]) or 0
    except KeyboardInterrupt:
        return 130
    except RuntimeError as e:
        err(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
