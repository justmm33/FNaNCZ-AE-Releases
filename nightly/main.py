import os
import sys
import re
import time
import json
import shutil
import shlex
import zipfile
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import platform
import base64
import threading
import html as html_lib
from concurrent.futures import ThreadPoolExecutor
import webbrowser
from string import Template
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QFrame, QMenu, QButtonGroup, QStackedWidget,
    QDialog, QProgressBar, QFormLayout, QLineEdit, QScrollArea, QComboBox, QMessageBox,
    QFileDialog, QInputDialog, QTextEdit, QSizePolicy, QLayout, QListWidget, QListWidgetItem,
    QAbstractItemView
)
from PyQt6.QtCore import QMimeData, QSize, Qt, QRectF, QRect, QThread, QUrl, QBuffer, QIODevice, QObject, QTimer, QPoint, pyqtSignal
from PyQt6.QtGui import QTextCursor, QTextImageFormat, QTextCharFormat, QTextDocument, QPixmap, QPainter, QPainterPath, QPalette, QColor, QIcon, QFont, QFontDatabase, QDesktopServices, QImage, QPen, QMovie, QKeySequence

if "--ncz-overlay-server" in sys.argv:      # XWayland overlay helper (see overlay.py), frozen builds
    from overlay import run_server
    run_server()
    sys.exit(0)

if "--ncz-rp-helper" in sys.argv:           # Steamworks helper: prints the running game's Steam rich presence "connect" text
    import ctypes
    _i = sys.argv.index("--ncz-rp-helper")
    try:
        _appid, _lib, _sid = sys.argv[_i + 1:_i + 4]
        os.environ["SteamAppId"] = os.environ["SteamGameId"] = _appid
        os.chdir(os.path.dirname(_lib))
        _api = ctypes.CDLL(_lib)
        _api.SteamAPI_Init.restype = ctypes.c_bool
        if _api.SteamAPI_Init():
            _fr = None
            for _v in range(20, 7, -1):     # the accessor's name carries the ISteamFriends version of that steam_api library
                _fn = getattr(_api, "SteamAPI_SteamFriends_v%03d" % _v, None)
                if _fn is not None:
                    _fn.restype = ctypes.c_void_p
                    _fr = _fn()
                    if _fr:
                        break
            if _fr:
                _get = _api.SteamAPI_ISteamFriends_GetFriendRichPresence
                _get.restype = ctypes.c_char_p
                _get.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_char_p]
                _req = _api.SteamAPI_ISteamFriends_RequestFriendRichPresence
                _req.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
                _req(_fr, int(_sid))
                for _n in range(40):
                    _api.SteamAPI_RunCallbacks()
                    _val = _get(_fr, int(_sid), b"connect")
                    if _val:
                        print(_val.decode("utf-8", "replace"))
                        break
                    time.sleep(0.1)
            _api.SteamAPI_Shutdown()
    except Exception:
        pass
    sys.exit(0)

def _bundled_dir():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

def _writable_dir():
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return os.path.dirname(os.path.abspath(appimage))
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

SCRIPT_DIR = _writable_dir()
ASSETS_DIR = os.path.join(_bundled_dir(), "assets")
DEFAULT_GAMES_DIR = os.path.join(SCRIPT_DIR, "games")
GAMES_DIR = DEFAULT_GAMES_DIR            # where NEW installs go (changed in Settings -> Game Library)
DOWNLOAD_DIR = os.path.join(GAMES_DIR, "")
LIBRARY_FOLDER_NAME = "NCZLibrary"       # looked for on every partition
AE_GAME_NAME = "Five Nights at NCZ AE"
NCZ2_GAME_NAME = "Five Nights at NCZ 2"
NCZFRONT_URL = "https://occultist-tattling-elevation.ngrok-free.dev/"

FIREBASE_API_KEY = "AIzaSyBEphV3IipXeUUIpgP6XYrtPG3RrZ-wPt4"
FIREBASE_DB_URL = "https://ncz-games-launcher-default-rtdb.europe-west1.firebasedatabase.app"

def asset_path(filename):
    return os.path.join(ASSETS_DIR, filename)

# ---------------------------------------------------------------- game library (multiple partitions)
_SKIP_MOUNT_PREFIXES = ("/boot", "/snap", "/var/lib/snapd", "/var/lib/docker", "/sys", "/proc", "/dev", "/run/user",
                        "/run/snapd", "/tmp", "/usr", "/etc", "/opt/pw-browsers")
_SKIP_FSTYPES = {"squashfs", "iso9660", "udf", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "cgroup2",
                 "autofs", "efivarfs", "fuse.portal", "fuse.gvfsd-fuse"}

def mounted_partitions():
    """Mount points of the PC's partitions / drives (internal and external)."""
    out = []
    if sys.platform.startswith("win"):
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            if os.path.exists(c + ":\\"):
                out.append(c + ":\\")
        return out
    best = {}   # device -> shortest mount point (btrfs subvolumes mount the same device many times)
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dev, mp, fstype = parts[0], parts[1], parts[2]
                mp = mp.replace("\\040", " ").replace("\\011", "\t")
                if not dev.startswith("/dev/") or dev.startswith("/dev/loop") or fstype in _SKIP_FSTYPES:
                    continue
                if mp != "/" and mp.startswith(_SKIP_MOUNT_PREFIXES):
                    continue
                if dev not in best or len(mp) < len(best[dev]):
                    best[dev] = mp
    except OSError:
        pass
    for mp in best.values():
        out.append(mp)
    return sorted(out)

def partition_library_path(mount_point):
    return os.path.join(mount_point, LIBRARY_FOLDER_NAME)

_roots_cache = {"time": 0.0, "roots": []}

def invalidate_library_roots():
    _roots_cache["time"] = 0.0

def library_roots():
    """Every folder that can hold installed games: the active install folder, the default one, folders
    added by the user, and any NCZLibrary folder found on any partition. Cached for a few seconds."""
    if time.time() - _roots_cache["time"] < 5 and _roots_cache["roots"]:
        return _roots_cache["roots"]
    roots = []
    def add(path):
        if path:
            path = os.path.abspath(path)
            if path not in roots:
                roots.append(path)
    add(GAMES_DIR)
    add(DEFAULT_GAMES_DIR)
    try:
        for extra in load_launcher_settings().get("library_dirs", []):
            add(extra)
    except Exception:
        pass
    for mp in mounted_partitions():
        lib = partition_library_path(mp)
        try:
            if os.path.isdir(lib):
                add(lib)
        except OSError:
            pass
    _roots_cache.update(time=time.time(), roots=roots)
    return roots

def apply_install_dir():
    """Points GAMES_DIR / DOWNLOAD_DIR at the install folder chosen in Settings (default: ./games)."""
    global GAMES_DIR, DOWNLOAD_DIR
    chosen = ""
    try:
        chosen = load_launcher_settings().get("install_dir", "")
    except Exception:
        pass
    target = DEFAULT_GAMES_DIR
    if chosen:
        try:
            os.makedirs(chosen, exist_ok=True)
            if os.access(chosen, os.W_OK):
                target = chosen
        except OSError:
            pass   # drive unplugged / not writable: fall back to the default folder
    GAMES_DIR = target
    DOWNLOAD_DIR = os.path.join(GAMES_DIR, "")
    invalidate_library_roots()

def _find_in_library(name):
    for root in library_roots():
        path = os.path.join(root, name)
        try:
            if os.path.exists(path):
                return path
        except OSError:
            pass
    return None

def installed_game_dir(game_name):
    # An already-installed game is found on any partition; a new install goes to the active install folder.
    return _find_in_library(game_name) or os.path.join(GAMES_DIR, game_name)

# ---------------------------------------------------------------- wine (Linux)
GAME_INFO = {
    "ae": dict(
        name=AE_GAME_NAME,
        exe="FNaNCZ AE.exe",
        zip_prefixes=("five nights at ncz ae", "fnancz_ae", "fnanczae"),
        version_url="https://raw.githubusercontent.com/justmm33/FNaNCZ-AE-Releases/refs/heads/main/version.txt",
        linux_cmd="rm -f fnancz-installer2.sh >/dev/null 2>&1 && wget https://raw.githubusercontent.com/AmrThePigeon/FNANCZAE1_Script_Builder/refs/heads/main/fnancz-installer2.sh --no-cache && chmod +x fnancz-installer2.sh && ./fnancz-installer2.sh",
    ),
    "ncz2": dict(
        name=NCZ2_GAME_NAME,
        exe="FNANCZ 2.exe",
        zip_prefixes=("five nights at ncz 2", "fnancz_2", "fnancz 2", "fnancz2"),
        version_url="https://raw.githubusercontent.com/justmm33/FNaNCZ-AE-Releases/refs/heads/main/version-2.txt",
        linux_cmd="rm -f fnancz2-installer1.sh >/dev/null 2>&1 && wget https://raw.githubusercontent.com/AmrThePigeon/FNANCZAE2_Script_Builder/refs/heads/main/fnancz2-installer1.sh --no-cache && chmod +x fnancz2-installer1.sh && ./fnancz2-installer1.sh",
    ),
}

def wine_game_dir(game_name):
    # Separate from the native (converted) install so the two never overwrite each other.
    name = f"{game_name} (Wine)"
    return _find_in_library(name) or os.path.join(GAMES_DIR, name)

def system_env():
    # A frozen build can point LD_LIBRARY_PATH at its own bundle, which breaks system programs like wine.
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        orig = env.get("LD_LIBRARY_PATH_ORIG")
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env

# ---------------------------------------------------------------- Direct3D renderer (Wine / Proton)
WINE_D3D_MODES = {
    "default": "Default",
    "wined3d": "WineD3D (OpenGL)",
    "dxvk": "DXVK (Vulkan, D3D 8-11)",
    "dxvk_vkd3d": "DXVK + VKD3D (Vulkan, D3D 8-12)",
}
_D3D_DLLS = "d3d8,d3d9,d3d10core,d3d11,dxgi"
_D3D12_DLLS = "d3d12,d3d12core"

_d3d_override = {"mode": ""}   # set by _launch_exe while starting a game that has its own renderer

def get_wine_d3d_mode():
    mode = _d3d_override["mode"] or load_launcher_settings().get("wine_d3d", "default")
    return mode if mode in WINE_D3D_MODES else "default"

def _wine_prefix():
    return os.environ.get("WINEPREFIX") or os.path.expanduser("~/.wine")

def wine_game_env():
    """system_env() plus the DLL overrides for the chosen Direct3D renderer."""
    env = system_env()
    mode = get_wine_d3d_mode()
    if mode == "default":
        return env
    if mode == "wined3d":
        override = f"{_D3D_DLLS}=b"                      # always use Wine's own Direct3D
    else:
        override = f"{_D3D_DLLS}=n,b"                    # DXVK if it's in the prefix, else Wine's
        if mode == "dxvk_vkd3d":
            override += f";{_D3D12_DLLS}=n,b"
    old = env.get("WINEDLLOVERRIDES", "")
    env["WINEDLLOVERRIDES"] = (old + ";" if old else "") + override
    return env

def apply_proton_d3d(env):
    """Proton already uses DXVK/VKD3D by default, so only WineD3D needs asking for."""
    if get_wine_d3d_mode() == "wined3d":
        env["PROTON_USE_WINED3D"] = "1"
    return env

def _d3d_components_missing(mode):
    want = ["dxvk"] + (["vkd3d"] if mode == "dxvk_vkd3d" else [])
    try:
        with open(os.path.join(_wine_prefix(), ".ncz_d3d_installed"), encoding="utf-8") as f:
            have = set(f.read().split())
    except OSError:
        have = set()
    return [w for w in want if w not in have]

def launch_wine(wine_path, exe, cwd, args=None):
    """Starts a Windows exe under wine with the chosen Direct3D renderer.
    For DXVK/VKD3D, the first launch installs them into the prefix with winetricks (if it's installed)."""
    args = list(args or [])
    env = wine_game_env()
    mode = get_wine_d3d_mode()
    tricks = shutil.which("winetricks")
    missing = _d3d_components_missing(mode) if mode.startswith("dxvk") else []
    if missing and tricks:
        def work():
            try:
                e = system_env()
                e["WINE"] = wine_path
                r = subprocess.run([tricks, "-q"] + missing, env=e, timeout=1800, capture_output=True)
                if r.returncode == 0:
                    prefix = _wine_prefix()
                    os.makedirs(prefix, exist_ok=True)
                    with open(os.path.join(prefix, ".ncz_d3d_installed"), "a", encoding="utf-8") as f:
                        f.write(" ".join(missing) + "\n")
            except Exception:
                pass
            try:
                subprocess.Popen([wine_path, exe] + args, cwd=cwd, env=env)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()
        return
    subprocess.Popen([wine_path, exe] + args, cwd=cwd, env=env)

def find_wine():
    """Returns (path, version) for the first working wine found, or None. Linux only."""
    if not sys.platform.startswith("linux"):
        return None
    candidates = []
    for name in ("wine", "wine64"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates += [
        "/opt/wine-stable/bin/wine", "/opt/wine-staging/bin/wine", "/opt/wine-devel/bin/wine",
        "/usr/lib/wine/wine64", "/usr/lib/wine/wine",
    ]
    seen = set()
    for path in candidates:
        if path in seen or not os.path.isfile(path) or not os.access(path, os.X_OK):
            continue
        seen.add(path)
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=5, env=system_env()).stdout.strip()
        except Exception:
            continue
        if out:
            return path, out.split()[0]
    return None

# ---------------------------------------------------------------- Wine builds + compatibility tools (Linux)
def wine_builds_dir():
    # Wine builds downloaded in the Wine tab live here, one folder per build.
    return os.path.expanduser("~/.local/share/NCZ_Games_Launcher/wine")

def _wine_bin_in(build_dir):
    for name in ("wine", "wine64"):
        path = os.path.join(build_dir, "bin", name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None

def wine_installed_builds():
    """{build name: path of its wine binary} for the builds installed from the Wine tab."""
    found = {}
    if not sys.platform.startswith("linux"):
        return found
    base = wine_builds_dir()
    try:
        for name in sorted(os.listdir(base)):
            if name.startswith("."):
                continue
            binary = _wine_bin_in(os.path.join(base, name))
            if binary:
                found[name] = binary
    except OSError:
        pass
    return found

def compat_tool_kind(path):
    """'proton' for a Proton tool (its `proton` script), 'wine' for a wine binary, None if unset or missing."""
    if not path or not os.path.exists(path):
        return None
    return "proton" if os.path.basename(path) == "proton" else "wine"

def default_wine_path(app=None):
    """The wine used when a game has no tool picked: system wine, else the newest build from the Wine tab."""
    system = getattr(app, "wine", None)
    if system:
        return system[0]
    builds = wine_installed_builds()
    if builds:
        return max(builds.values(), key=lambda p: os.path.getmtime(p))
    return None

def find_game_exe(root, exe_name):
    if not os.path.isdir(root):
        return None
    direct = os.path.join(root, exe_name)
    if os.path.isfile(direct):
        return direct
    target = exe_name.lower()
    for folder, _dirs, files in os.walk(root):
        for f in files:
            if f.lower() == target:
                return os.path.join(folder, f)
    return None

def find_existing_zip(prefixes):
    if not os.path.isdir(DOWNLOAD_DIR):
        return None
    for f in sorted(os.listdir(DOWNLOAD_DIR)):
        if f.lower().endswith(".zip") and f.lower().startswith(prefixes):
            return os.path.join(DOWNLOAD_DIR, f)
    return None

def _fallback_path(candidates):
    for path in candidates:
        if os.path.isdir(os.path.dirname(path)):
            return path
    return candidates[0]

def get_ae_save_path():
    if sys.platform.startswith("win"):
        appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        candidates = [
            os.path.join(appdata, "Five_Nights_at_NCZ_AE", "fnancz", "save.json"),
            os.path.join(appdata, "Five_Nights_at_NCZ_AE", "save.json"),
            os.path.join(appdata, "FNANCZ_AE", "fnancz", "save.json"),
            os.path.join(appdata, "FNANCZ_AE", "save.json")
        ]
    else:
        candidates = [
            os.path.expanduser("~/.config/Five_Nights_at_NCZ_AE/fnancz/save.json"),
            os.path.expanduser("~/.config/Five_Nights_at_NCZ_AE/save.json"),
            os.path.expanduser("~/.config/FNANCZ_AE/fnancz/save.json"),
            os.path.expanduser("~/.config/FNANCZ_AE/save.json")
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return _fallback_path(candidates)

def get_ae_settings_path():
    if sys.platform.startswith("win"):
        appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        candidates = [
            os.path.join(appdata, "Five_Nights_at_NCZ_AE", "fnancz", "settings.json"),
            os.path.join(appdata, "Five_Nights_at_NCZ_AE", "settings.json"),
            os.path.join(appdata, "FNANCZ_AE", "fnancz", "settings.json"),
            os.path.join(appdata, "FNANCZ_AE", "settings.json")
        ]
    else:
        candidates = [
            os.path.expanduser("~/.config/Five_Nights_at_NCZ_AE/fnancz/settings.json"),
            os.path.expanduser("~/.config/Five_Nights_at_NCZ_AE/settings.json"),
            os.path.expanduser("~/.config/FNANCZ_AE/fnancz/settings.json"),
            os.path.expanduser("~/.config/FNANCZ_AE/settings.json")
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return _fallback_path(candidates)

def get_ncz2_save_path():
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", os.path.expanduser("~/AppData/Roaming"))
        candidates = [
            os.path.join(appdata, "FNANCZ_2", "save.json"),
            os.path.join(appdata, "FNANCZ_2", "fnancz", "save.json"),
            os.path.join(appdata, "Five_Nights_at_NCZ_2", "save.json"),
            os.path.join(appdata, "Five_Nights_at_NCZ_2", "fnancz", "save.json")
        ]
    else:
        candidates = [
            os.path.expanduser("~/.config/FNANCZ_2/save.json"),
            os.path.expanduser("~/.config/FNANCZ_2/fnancz/save.json"),
            os.path.expanduser("~/.config/Five_Nights_at_NCZ_2/save.json"),
            os.path.expanduser("~/.config/Five_Nights_at_NCZ_2/fnancz/save.json")
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return _fallback_path(candidates)

def get_ncz2_settings_path():
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", os.environ.get("APPDATA", os.path.expanduser("~/AppData/Roaming")))
        candidates = [
            os.path.join(appdata, "FNANCZ_2", "settings.json"),
            os.path.join(appdata, "FNANCZ_2", "fnancz", "settings.json"),
            os.path.join(appdata, "Five_Nights_at_NCZ_2", "settings.json"),
            os.path.join(appdata, "Five_Nights_at_NCZ_2", "fnancz", "settings.json")
        ]
    else:
        candidates = [
            os.path.expanduser("~/.config/FNANCZ_2/settings.json"),
            os.path.expanduser("~/.config/FNANCZ_2/fnancz/settings.json"),
            os.path.expanduser("~/.config/Five_Nights_at_NCZ_2/settings.json"),
            os.path.expanduser("~/.config/Five_Nights_at_NCZ_2/fnancz/settings.json")
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return _fallback_path(candidates)

def flatten_dict(d, parent_key=''):
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}.{k}" if parent_key else str(k)
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key).items())
        else:
            items.append((new_key, v))
    return dict(items)

def unflatten_dict(d):
    result = {}
    for key, value in d.items():
        parts = key.split('.')
        target = result
        for part in parts[:-1]:
            if part not in target or not isinstance(target[part], dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value
    return result

def load_game_json(file_path):
    with open(file_path, 'rb') as f:
        raw = f.read()
    trailing_null = raw.endswith(b'\x00')
    text = raw.replace(b'\x00', b'').decode('utf-8-sig', errors='ignore').strip()
    if not text:
        return {}, trailing_null
    loaded = json.loads(text)
    if isinstance(loaded, dict):
        return flatten_dict(loaded), trailing_null
    return {}, trailing_null

def get_launcher_settings_path():
    if sys.platform.startswith("win"):
        appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        return os.path.join(appdata, "NCZ_Games_Launcher", "launcher.json")
    return os.path.expanduser("~/.config/NCZ_Games_Launcher/launcher.json")

def load_launcher_settings():
    settings = {"dark_mode": "system", "steamgriddb_api_key": "", "currency": "EGP", "wine_d3d": "default",
                "library_sort": "custom", "discord_rpc": True, "install_dir": "", "library_dirs": [],
                "start_on_boot": True, "auto_update": True, "xwayland_mode": True,
                "start_minimized": True, "close_to_tray": True, "accent_color": "#a666ff"}
    path = get_launcher_settings_path()
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                mode = str(loaded.get("dark_mode", "system")).lower()
                if mode in ("off", "on", "system"):
                    settings["dark_mode"] = mode
                if loaded.get("wine_d3d") in ("default", "wined3d", "dxvk", "dxvk_vkd3d"):
                    settings["wine_d3d"] = loaded["wine_d3d"]
                if loaded.get("currency") in ("EGP", "QAR", "USD", "SAR", "AED", "EUR"):
                    settings["currency"] = loaded["currency"]
                if loaded.get("library_sort") in ("custom", "az"):
                    settings["library_sort"] = loaded["library_sort"]
                if isinstance(loaded.get("install_dir"), str):
                    settings["install_dir"] = loaded["install_dir"].strip()
                if isinstance(loaded.get("library_dirs"), list):
                    settings["library_dirs"] = [d for d in loaded["library_dirs"] if isinstance(d, str) and d.strip()]
                if isinstance(loaded.get("discord_rpc"), bool):
                    settings["discord_rpc"] = loaded["discord_rpc"]
                if isinstance(loaded.get("start_on_boot"), bool):
                    settings["start_on_boot"] = loaded["start_on_boot"]
                if isinstance(loaded.get("auto_update"), bool):
                    settings["auto_update"] = loaded["auto_update"]
                if isinstance(loaded.get("xwayland_mode"), bool):
                    settings["xwayland_mode"] = loaded["xwayland_mode"]
                for _k in ("start_minimized", "close_to_tray"):
                    if isinstance(loaded.get(_k), bool):
                        settings[_k] = loaded[_k]
                if isinstance(loaded.get("accent_color"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", loaded["accent_color"]):
                    settings["accent_color"] = loaded["accent_color"].lower()
                key = loaded.get("steamgriddb_api_key", "")
                if isinstance(key, str):
                    settings["steamgriddb_api_key"] = key.strip()
                opts = loaded.get("launch_options", {})
                if isinstance(opts, dict):
                    settings["launch_options"] = {str(k): v for k, v in opts.items()
                                                  if isinstance(v, str) and v.strip()}
        except Exception:
            pass
    settings.setdefault("launch_options", {})
    return settings

def save_launcher_settings(settings):
    path = get_launcher_settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=4)
    invalidate_library_roots()

apply_install_dir()

# ---------------------------------------------------------------- launch options (per game)
# Keys match the compatibility-tool keys: "ae", "ncz2", a Steam appid, "custom-<id>", "fan-<id>".
def get_launch_options(key):
    return load_launcher_settings().get("launch_options", {}).get(str(key), "")

def set_launch_options(key, text):
    settings = load_launcher_settings()
    options = settings.setdefault("launch_options", {})
    text = (text or "").strip()
    if text:
        options[str(key)] = text
    else:
        options.pop(str(key), None)
    save_launcher_settings(settings)

def parse_launch_options(text):
    text = (text or "").strip()
    if not text:
        return []
    try:
        if sys.platform.startswith("win"):
            return [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t for t in shlex.split(text, posix=False)]
        return shlex.split(text)
    except ValueError:
        return text.split()

def launch_args_for(key):
    """The saved launch options for one game, as a list of arguments."""
    return parse_launch_options(get_launch_options(key))

CLOUD_TIMEOUT = 20
FIREBASE_AUTH_URL = "https://identitytoolkit.googleapis.com/v1/accounts:{action}?key={key}"
FIREBASE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token?key={key}"

# ---------------------------------------------------------------- game versions
VERSION_KEYS = ("version", "latest_version", "current_version", "game_version")
_versions_lock = threading.Lock()

def parse_remote_version(content):
    """Reads the version out of the same text file the download link comes from."""
    fallback = ""
    for line in content.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'").strip()
        if not value:
            continue
        if key in VERSION_KEYS:
            return value
        if not fallback and "version" in key and "download" not in key and "link" not in key:
            fallback = value
    return fallback

def fetch_remote_version(version_url):
    req = urllib.request.Request(version_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return parse_remote_version(resp.read().decode('utf-8', errors='replace'))

def get_installed_versions_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "installed_versions.json")

def _load_installed_versions():
    try:
        with open(get_installed_versions_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_installed_versions(data):
    path = get_installed_versions_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.replace(tmp, path)

def get_installed_version(game_name):
    with _versions_lock:
        value = _load_installed_versions().get(game_name)
    return value if isinstance(value, str) else ""

def record_installed_version(game_name, version):
    if not version:
        return
    with _versions_lock:
        data = _load_installed_versions()
        data[game_name] = version
        _save_installed_versions(data)

def forget_installed_version(game_name):
    with _versions_lock:
        data = _load_installed_versions()
        if game_name in data:
            del data[game_name]
            _save_installed_versions(data)

def is_newer_version(remote, local):
    if not remote or not local or remote == local:
        return False
    remote_nums = tuple(int(n) for n in re.findall(r"\d+", remote))
    local_nums = tuple(int(n) for n in re.findall(r"\d+", local))
    if remote_nums and local_nums:
        return remote_nums > local_nums
    return True

class CloudError(Exception):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code

CLOUD_ERROR_MESSAGES = {
    "EMAIL_EXISTS": "An account with this email already exists. Try logging in instead.",
    "INVALID_LOGIN_CREDENTIALS": "Wrong email or password.",
    "INVALID_PASSWORD": "Wrong email or password.",
    "EMAIL_NOT_FOUND": "Wrong email or password.",
    "INVALID_EMAIL": "That doesn't look like a valid email address.",
    "MISSING_EMAIL": "Please enter your email address.",
    "MISSING_PASSWORD": "Please enter your password.",
    "WEAK_PASSWORD": "Password must be at least 6 characters.",
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many attempts. Please wait a bit and try again.",
    "USER_DISABLED": "This account has been disabled.",
    "OPERATION_NOT_ALLOWED": "Email/password sign-in isn't enabled for this cloud project yet.",
    "TOKEN_EXPIRED": "Your session expired. Please log in again.",
    "INVALID_REFRESH_TOKEN": "Your session expired. Please log in again.",
    "USER_NOT_FOUND": "Your session expired. Please log in again.",
    "CREDENTIAL_TOO_OLD_LOGIN_AGAIN": "Your session expired. Please log in again.",
    "Permission denied": "The cloud refused the request (check the database rules).",
}
SESSION_DEAD_CODES = ("TOKEN_EXPIRED", "INVALID_REFRESH_TOKEN", "USER_NOT_FOUND",
                      "USER_DISABLED", "CREDENTIAL_TOO_OLD_LOGIN_AGAIN")

GAME_SYNC = {
    "ae": (AE_GAME_NAME, get_ae_save_path, get_ae_settings_path),
    "ncz2": (NCZ2_GAME_NAME, get_ncz2_save_path, get_ncz2_settings_path),
}

_token_lock = threading.Lock()
_token_cache = {"uid": None, "id_token": None, "expires_at": 0.0}

def cloud_configured():
    return bool(FIREBASE_API_KEY.strip() and FIREBASE_DB_URL.strip())

def _require_cloud_config():
    if not cloud_configured():
        raise CloudError("Cloud sync isn't set up in this build of the launcher yet.", "NOT_CONFIGURED")

def _api_key():
    return urllib.parse.quote(FIREBASE_API_KEY.strip(), safe="")

def _cloud_error_from(raw_message, http_code=0):
    code = str(raw_message).split(" : ")[0].strip()
    if code in CLOUD_ERROR_MESSAGES:
        return CloudError(CLOUD_ERROR_MESSAGES[code], code)
    if code.lower().startswith("api key not valid"):
        return CloudError("The cloud API key in this launcher is invalid.", "API_KEY_INVALID")
    if http_code in (401, 403):
        return CloudError("The cloud refused the request (check the database rules).", code)
    return CloudError(f"Cloud error: {code or 'HTTP ' + str(http_code)}", code)

def _http_json(url, method="GET", payload=None, form=None, timeout=None):
    headers = {"User-Agent": "NCZ-Games-Launcher"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout or CLOUD_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            err = json.loads(raw).get("error")
            message = err.get("message", "") if isinstance(err, dict) else str(err or "")
        except Exception:
            message = raw[:120]
        raise _cloud_error_from(message, e.code)
    except ValueError:
        raise CloudError("The cloud address in this launcher isn't valid.", "BAD_URL")
    except (urllib.error.URLError, OSError):
        raise CloudError("Couldn't reach the cloud. Check your internet connection.", "NETWORK")
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except ValueError:
        raise CloudError("The cloud sent back something unexpected.", "BAD_REPLY")

def get_account_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "account.json")

def load_account():
    try:
        with open(get_account_path(), encoding='utf-8') as f:
            acct = json.load(f)
        if isinstance(acct, dict) and acct.get("refresh_token") and acct.get("uid"):
            return acct
    except Exception:
        pass
    return None

def save_account(acct):
    path = get_account_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(acct, f)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

def clear_account():
    with _token_lock:
        _token_cache.update(uid=None, id_token=None, expires_at=0.0)
    for path in (get_account_path(), get_profile_cache_path(), get_avatar_cache_path()):
        try:
            os.remove(path)
        except OSError:
            pass

def cloud_sign_in(email, password, create=False):
    _require_cloud_config()
    res = _http_json(
        FIREBASE_AUTH_URL.format(action="signUp" if create else "signInWithPassword", key=_api_key()),
        "POST", {"email": email, "password": password, "returnSecureToken": True})
    uid = res["localId"]
    with _token_lock:
        _token_cache.update(uid=uid, id_token=res["idToken"],
                            expires_at=time.time() + int(res.get("expiresIn", 3600)))
    save_account({"email": res.get("email", email), "uid": uid, "refresh_token": res["refreshToken"]})
    return res.get("email", email)

def cloud_send_password_reset(email):
    _require_cloud_config()
    try:
        _http_json(FIREBASE_AUTH_URL.format(action="sendOobCode", key=_api_key()),
                   "POST", {"requestType": "PASSWORD_RESET", "email": email})
    except CloudError as e:
        if e.code != "EMAIL_NOT_FOUND":
            raise

def get_id_token():
    _require_cloud_config()
    acct = load_account()
    if not acct:
        raise CloudError("You're not logged in.", "NOT_LOGGED_IN")
    with _token_lock:
        if (_token_cache["uid"] == acct["uid"] and _token_cache["id_token"]
                and _token_cache["expires_at"] - 60 > time.time()):
            return _token_cache["id_token"], acct["uid"]
    try:
        res = _http_json(FIREBASE_TOKEN_URL.format(key=_api_key()), "POST",
                         form={"grant_type": "refresh_token", "refresh_token": acct["refresh_token"]})
    except CloudError as e:
        if e.code in SESSION_DEAD_CODES:
            clear_account()
        raise
    uid = res.get("user_id", acct["uid"])
    with _token_lock:
        _token_cache.update(uid=uid, id_token=res["id_token"],
                            expires_at=time.time() + int(res.get("expires_in", 3600)))
    acct["uid"] = uid
    acct["refresh_token"] = res.get("refresh_token", acct["refresh_token"])
    save_account(acct)
    return res["id_token"], uid

def read_game_text(path):
    with open(path, 'rb') as f:
        raw = f.read()
    had_null = raw.endswith(b'\x00')
    text = raw.replace(b'\x00', b'').decode('utf-8-sig', errors='ignore').strip()
    if text:
        try:
            ok = isinstance(json.loads(text), dict)
        except ValueError:
            ok = False
        if not ok:
            raise CloudError(f"{os.path.basename(path)} isn't valid game data, so it wasn't uploaded.", "BAD_LOCAL_FILE")
    return text, had_null

def write_game_text(path, text, default_null=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    use_null = default_null
    if os.path.exists(path):
        try:
            with open(path, 'rb') as f:
                existing = f.read()
            if existing:
                use_null = existing.endswith(b'\x00')
        except OSError:
            pass
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    payload = text.encode('utf-8') + (b'\x00' if use_null else b'')
    tmp = path + ".tmp"
    with open(tmp, 'wb') as f:
        f.write(payload)
    os.replace(tmp, path)

def _game_url(uid, game_id, token):
    base = FIREBASE_DB_URL.strip().rstrip("/")
    return (f"{base}/users/{urllib.parse.quote(uid, safe='')}/games/"
            f"{urllib.parse.quote(game_id, safe='')}.json?auth={urllib.parse.quote(token, safe='')}")

def cloud_get_game(game_id):
    token, uid = get_id_token()
    data = _http_json(_game_url(uid, game_id, token))
    return data if isinstance(data, dict) else None

def cloud_upload_game(game_id, save_path, settings_path):
    payload = {"updated": {".sv": "timestamp"}, "device": platform.system() or "Unknown"}
    count = 0
    for key, path in (("save", save_path), ("settings", settings_path)):
        if path and os.path.exists(path):
            text, had_null = read_game_text(path)
            if text:
                payload[f"{key}_text"] = text
                payload[f"{key}_null"] = had_null
                count += 1
    if not count:
        raise CloudError("Nothing to upload yet - play the game once so it creates its save/settings files.", "NOTHING")
    token, uid = get_id_token()
    _http_json(_game_url(uid, game_id, token), "PATCH", payload)
    return count

def apply_cloud_game(record, save_path, settings_path):
    written = []
    for key, path in (("save", save_path), ("settings", settings_path)):
        text = record.get(f"{key}_text")
        if not isinstance(text, str) or not text.strip():
            continue
        json.loads(text)
        write_game_text(path, text, bool(record.get(f"{key}_null")))
        written.append(key)
    return written

# ---------------------------------------------------------------- profile (username + picture)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,20}$")
AVATAR_SIZE = 128
MAX_PHOTO_CHARS = 400000

def get_profile_cache_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "profile.json")

def get_avatar_cache_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "avatar.png")

def validate_username(name):
    if not USERNAME_RE.match(name or ""):
        return "Username must be 3-20 characters: letters, numbers, _ . or -"
    return ""

def prepare_avatar_png(path):
    """Centre-crops the image to a square and shrinks it to a small PNG for the cloud."""
    img = QImage(path)
    if img.isNull():
        raise CloudError("Couldn't read that image.", "BAD_IMAGE")
    side = min(img.width(), img.height())
    img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
    img = img.scaled(AVATAR_SIZE, AVATAR_SIZE, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())

def _decode_photo(photo):
    if not isinstance(photo, str) or not photo or len(photo) > MAX_PHOTO_CHARS:
        return None
    try:
        raw = base64.b64decode(photo, validate=True)
    except Exception:
        return None
    return raw if QImage().loadFromData(raw) else None

def load_profile_cache():
    """Returns (username, avatar_path or None) for the logged-in account, from the local cache."""
    acct = load_account()
    if not acct:
        return "", None
    try:
        with open(get_profile_cache_path(), encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("uid") == acct["uid"]:
            avatar = get_avatar_cache_path()
            has_photo = bool(data.get("has_photo")) and os.path.exists(avatar)
            return str(data.get("username") or ""), (avatar if has_photo else None)
    except Exception:
        pass
    return "", None

def save_profile_cache(uid, username, png_bytes):
    folder = os.path.dirname(get_profile_cache_path())
    os.makedirs(folder, exist_ok=True)
    avatar = get_avatar_cache_path()
    if png_bytes:
        tmp = avatar + ".tmp"
        with open(tmp, 'wb') as f:
            f.write(png_bytes)
        os.replace(tmp, avatar)
    else:
        try:
            os.remove(avatar)
        except OSError:
            pass
    with open(get_profile_cache_path(), 'w', encoding='utf-8') as f:
        json.dump({"uid": uid, "username": username or "", "has_photo": bool(png_bytes)}, f)

def _profile_url(uid, token):
    base = FIREBASE_DB_URL.strip().rstrip("/")
    return (f"{base}/users/{urllib.parse.quote(uid, safe='')}/profile.json"
            f"?auth={urllib.parse.quote(token, safe='')}")

def cloud_pull_profile():
    """Downloads the profile from the cloud and refreshes the local cache."""
    token, uid = get_id_token()
    data = _http_json(_profile_url(uid, token))
    data = data if isinstance(data, dict) else {}
    username = data.get("username") if isinstance(data.get("username"), str) else ""
    png = _decode_photo(data.get("photo"))
    save_profile_cache(uid, username, png)
    return username

def cloud_save_profile(username=None, png=None, remove_photo=False):
    payload = {"updated": {".sv": "timestamp"}}
    if username is not None:
        payload["username"] = username
    if png is not None:
        payload["photo"] = base64.b64encode(png).decode("ascii")
    elif remove_photo:
        payload["photo"] = None
    token, uid = get_id_token()
    _http_json(_profile_url(uid, token), "PATCH", payload)
    return cloud_pull_profile()

def describe_save(text):
    try:
        night = json.loads(text).get("night")
        if isinstance(night, (int, float)) and not isinstance(night, bool):
            return f"Night {int(night) if float(night).is_integer() else night}"
    except Exception:
        pass
    return ""

def format_cloud_time(ms):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ms) / 1000.0))
    except Exception:
        return "an unknown time"

# ---------------------------------------------------------------- desktop shortcut
def get_launch_command():
    """(target, args, working_dir) that starts this launcher the way it is currently being run."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return appimage, [], os.path.dirname(os.path.abspath(appimage))
    if getattr(sys, "frozen", False):
        return sys.executable, [], os.path.dirname(os.path.abspath(sys.executable))
    script = os.path.abspath(__file__)
    exe = sys.executable
    if sys.platform.startswith("win"):
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pythonw):
            exe = pythonw  # no console window
    return exe, [script], os.path.dirname(script)

def prepare_shortcut_icon():
    """Copies/converts assets/icon.png to a stable location the shortcut can point at. None if unavailable."""
    src = asset_path("icon.png")
    if not os.path.exists(src):
        return None
    folder = os.path.dirname(get_launcher_settings_path())
    os.makedirs(folder, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            dest = os.path.join(folder, "launcher.ico")
            img = QImage(src)
            if img.isNull():
                return None
            size = 256
            scaled = img.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
            canvas = QImage(size, size, QImage.Format.Format_ARGB32)
            canvas.fill(Qt.GlobalColor.transparent)
            painter = QPainter(canvas)
            painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
            painter.end()
            return dest if canvas.save(dest, "ICO") else None
        dest = os.path.join(folder, "launcher-icon.png")
        shutil.copyfile(src, dest)
        return dest
    except Exception:
        return None

def _ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"

def _create_windows_shortcut(target, args, workdir, icon):
    script = (
        "$desktop=[Environment]::GetFolderPath('Desktop');"
        "$lnk=Join-Path $desktop 'NCZ Games Launcher.lnk';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($lnk);"
        f"$s.TargetPath={_ps_quote(target)};"
        f"$s.Arguments={_ps_quote(subprocess.list2cmdline(args))};"
        f"$s.WorkingDirectory={_ps_quote(workdir)};"
        + (f"$s.IconLocation={_ps_quote(icon)};" if icon else "")
        + "$s.Description='NCZ Games Launcher';"
        "$s.Save();Write-Output $lnk"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=30, creationflags=0x08000000)
    except Exception as e:
        raise RuntimeError(f"Couldn't run PowerShell: {e}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(detail[0] if detail else "PowerShell couldn't create the shortcut.")
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "your Desktop"

def _linux_desktop_dir():
    try:
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True,
                             timeout=5, env=system_env()).stdout.strip()
        if out and os.path.isdir(out):
            return out
    except Exception:
        pass
    fallback = os.path.expanduser("~/Desktop")
    return fallback if os.path.isdir(fallback) else None

def _create_linux_shortcut(target, args, workdir, icon):
    desktop = _linux_desktop_dir()
    if not desktop:
        raise RuntimeError("Couldn't find your Desktop folder.")

    def quote(arg):
        return '"' + str(arg).replace("%", "%%") + '"'

    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        "Name=NCZ Games Launcher",
        "Comment=Install and launch NCZ games",
        "Exec=" + " ".join(quote(a) for a in [target] + list(args)),
        f"Path={workdir}",
    ]
    if icon:
        lines.append(f"Icon={icon}")
    lines += ["Terminal=false", "Categories=Game;", ""]
    path = os.path.join(desktop, "NCZ Games Launcher.desktop")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.chmod(path, 0o755)
    if shutil.which("gio"):  # lets GNOME launch it without the "Allow Launching" step
        try:
            subprocess.run(["gio", "set", path, "metadata::trusted", "true"],
                           capture_output=True, timeout=5, env=system_env())
        except Exception:
            pass
    return path

def create_desktop_shortcut():
    """Creates (or refreshes) a desktop shortcut and returns its path. Raises RuntimeError with a readable message."""
    target, args, workdir = get_launch_command()
    icon = prepare_shortcut_icon()
    if sys.platform.startswith("win"):
        return _create_windows_shortcut(target, args, workdir, icon)
    if sys.platform.startswith("linux"):
        return _create_linux_shortcut(target, args, workdir, icon)
    raise RuntimeError("Desktop shortcuts aren't supported on this platform yet.")

THEMES = {
    # Black + red, flat and rounded (Epic Games feel) with Steam's top navigation bar.
    "dark": dict(
        bg="#0d0d0f", sidebar="#050506", panel="#151518", input="#0a0a0c", border="#26262b",
        text="#f2f2f4", subtext="#8d8d96", accent="#a666ff", accent_hover="#bb85ff", accent_press="#8444e0",
        button="#222226", button_hover="#2f2f35", disabled_bg="#18181b", disabled_text="#55555c",
        handle="#34343a", hover="#1c1c20",
        red="#a666ff", red_hover="#bb85ff", red_press="#8444e0",
        nav_text="#a0a0a8", nav_active="#ffffff",
        brand="#ffffff", sidebar_border="#1e1e22", nav_fill="rgba(255,255,255,0.05)", nav_fill_hover="rgba(255,255,255,0.10)",
    ),
    "light": dict(
        bg="#f2f2f4", sidebar="#e4e4e8", panel="#ffffff", input="#ffffff", border="#d9d9de",
        text="#141416", subtext="#6a6a72", accent="#a666ff", accent_hover="#bb85ff", accent_press="#8444e0",
        button="#e4e4e8", button_hover="#d6d6db", disabled_bg="#ececef", disabled_text="#a4a4ab",
        handle="#c4c4cb", hover="#e9e9ed",
        red="#a666ff", red_hover="#bb85ff", red_press="#8444e0",
        nav_text="#5c5c64", nav_active="#141416", brand="#141416",
        sidebar_border="#d2d2d8", nav_fill="rgba(0,0,0,0.06)", nav_fill_hover="rgba(0,0,0,0.11)",
    ),
}

# Colour the window-caption buttons (minimize / maximize / close) follow, so they stay readable on the bar.
_theme_state = {"nav_text": "#a0a0a8", "nav_active": "#ffffff"}
_theme_listeners = []

STYLE_TEMPLATE = Template("""
QMainWindow, QWidget#Content, QWidget#GridContainer { background: $bg; }
QDialog, QMessageBox { background: $panel; }
QLabel { color: $text; background: transparent; }
QToolTip { background: #ffffff; color: #0a0a0c; border: none; padding: 5px 8px; }

QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget#qt_scrollarea_viewport { background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QWidget#SettingsContent { background: transparent; }

/* ---- top navigation bar ---- */
QFrame#Sidebar { background: $sidebar; border-bottom: 1px solid $sidebar_border; }
QLabel#Brand { font-size: 15px; font-weight: bold; color: $brand; }
QPushButton#NavButton {
    background: transparent; color: $nav_text; border: none; border-bottom: 3px solid transparent;
    border-radius: 0; text-align: center; padding: 0px 15px; font-size: 12px; font-weight: bold;
}
QPushButton#NavButton:hover { background: $nav_fill; color: $nav_active; }
QPushButton#NavButton:checked { background: transparent; color: $nav_active; border-bottom: 3px solid $red; }
QPushButton#ProfileButton { background: $nav_fill; border: 1px solid transparent; border-radius: 22px; padding: 0; text-align: left; }
QPushButton#ProfileButton:hover { background: $nav_fill_hover; }
QPushButton#MinButton, QPushButton#ExitButton { color: $nav_text; }
QPushButton#MinButton:hover, QPushButton#MinButton:pressed { color: $nav_active; }
QPushButton#ProfileButton:checked { background: rgba(166,102,255,0.18); border: 1px solid $red; }
QPushButton#ProfileButton QLabel { color: $nav_active; }
QLabel#Avatar { background: $accent; color: #ffffff; border-radius: 18px; font-size: 15px; font-weight: bold; }
QLabel#AvatarLarge { background: $accent; color: #ffffff; border-radius: 40px; font-size: 32px; font-weight: bold; }
QLabel#ProfileName { font-size: 13px; font-weight: 600; }
QLabel#ProfileSub { color: $nav_text; font-size: 11px; }

/* ---- library page ---- */
QLabel#PageTitle { font-size: 26px; font-weight: bold; }
QLabel#PageCount { color: $subtext; font-size: 13px; padding-left: 12px; padding-bottom: 5px; }
QLabel#EmptyState { color: $subtext; font-size: 14px; padding: 40px; }
QLineEdit#Search { background: $panel; border: 1px solid $border; border-radius: 18px; padding: 0px 16px; font-size: 13px; }
QLineEdit#Search:focus { border: 1px solid $accent; }

/* ---- settings / credits ---- */
QFrame#Panel { background: $panel; border: 1px solid $border; border-radius: 12px; }
QLabel#RowTitle { font-size: 14px; font-weight: 600; }
QLabel#RowDesc { color: $subtext; font-size: 12px; }

/* ---- game cards ---- */
QFrame#GameCard { background: transparent; border: 1px solid transparent; border-radius: 12px; }
QFrame#GameCard:hover { background: $panel; border: 1px solid $accent; }
QLabel#CardTitle { font-size: 14px; font-weight: 600; padding-left: 2px; }
QLabel#CardStatus, QLabel#CardPlaytime { color: $subtext; font-size: 12px; padding-left: 2px; }
QLabel#CoverPlaceholder { background: $panel; border: 1px dashed $border; border-radius: 8px; color: $subtext; }

/* ---- buttons ---- */
QPushButton {
    background: $button; color: $text; border: none; border-radius: 6px;
    padding: 9px 20px; font-weight: 600;
}
QPushButton:hover, QPushButton:pressed { background: $button_hover; color: #ffffff; }
QPushButton:disabled { background: $disabled_bg; color: $disabled_text; }
QPushButton:flat { background: transparent; color: $accent_hover; padding: 4px; }
QPushButton:flat:hover { background: transparent; color: #ffffff; }
QPushButton#Primary { background: $accent; color: #ffffff; font-weight: bold; }
QPushButton#Primary:hover { background: $accent_hover; }
QPushButton#Primary:pressed { background: $accent_press; }
QPushButton#Primary:disabled { background: $disabled_bg; color: $disabled_text; }
QPushButton#Danger, QPushButton#Uninstall { background: transparent; color: $red_hover; border: 1px solid $red; }
QPushButton#Danger:hover, QPushButton#Uninstall:hover { background: $red; color: #ffffff; }
QPushButton#MoreButton { padding: 0; font-size: 11px; }
QPushButton#MoreButton::menu-indicator { image: none; width: 0px; }

/* ---- inputs ---- */
QLineEdit {
    background: $input; color: $text; border: 1px solid $border; border-radius: 6px;
    padding: 8px 10px; selection-background-color: $accent; selection-color: #ffffff;
}
QLineEdit:focus { border: 1px solid $accent; }
QTextEdit#ChatInput {
    background: $input; color: $text; border: 1px solid $border; border-radius: 6px;
    padding: 8px 10px; selection-background-color: $accent; selection-color: #ffffff;
}
QTextEdit#ChatInput:focus { border: 1px solid $accent; }
QComboBox { background: $input; color: $text; border: 1px solid $border; border-radius: 6px; padding: 6px 10px; }
QComboBox:hover { border: 1px solid $accent; }
QComboBox QAbstractItemView {
    background: $panel; color: $text; border: 1px solid $border; outline: none;
    selection-background-color: $accent; selection-color: #ffffff;
}

/* ---- menus ---- */
QMenu { background: $panel; color: $text; border: 1px solid $border; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 9px 30px 9px 14px; border-radius: 5px; background: transparent; }
QMenu::item:selected { background: $accent; color: #ffffff; }
QMenu::item:disabled { color: $disabled_text; }
QMenu::separator { height: 1px; background: $border; margin: 6px 8px; }

/* ---- progress + scrollbars ---- */
QProgressBar { background: $button; border: none; border-radius: 3px; min-height: 6px; max-height: 6px; }
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 $accent_hover, stop:1 $accent_press); border-radius: 3px;
}
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: $handle; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: $accent; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: $handle; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: $accent; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

QMessageBox QPushButton { min-width: 80px; }
""")

def build_palette(t):
    palette = QPalette()
    text = QColor(t["text"])
    disabled = QColor(t["disabled_text"])
    palette.setColor(QPalette.ColorRole.Window, QColor(t["bg"]))
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, QColor(t["input"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(t["panel"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(t["panel"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, QColor(t["button"]))
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 80, 80))
    palette.setColor(QPalette.ColorRole.Link, QColor(t["accent"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(t["accent"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(t["subtext"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)
    return palette

_app_font_family = {"name": None}

def load_app_font():
    """Registers assets/ubuntu.ttf with Qt and returns its family name (None if it can't be loaded)."""
    if _app_font_family["name"]:
        return _app_font_family["name"]
    # ubuntu.ttf first (it decides the family name), then any other weights next to it
    # (ubuntu-bold.ttf, ubuntu-medium.ttf, ...) so bold text uses the real bold, not a smeared fake one.
    paths = [asset_path("ubuntu.ttf")]
    try:
        for f in sorted(os.listdir(ASSETS_DIR)):
            if f.lower().startswith("ubuntu") and f.lower().endswith((".ttf", ".otf")) and f.lower() != "ubuntu.ttf":
                paths.append(os.path.join(ASSETS_DIR, f))
    except OSError:
        pass
    for path in paths:
        if os.path.isfile(path):
            font_id = QFontDatabase.addApplicationFont(path)
            if font_id != -1 and not _app_font_family["name"]:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    _app_font_family["name"] = families[0]
    return _app_font_family["name"]

def build_app_font():
    font = QFont()
    family = load_app_font()
    if family:
        font.setFamily(family)
    else:
        font.setFamilies(["Segoe UI", "Noto Sans", "Helvetica Neue", "Arial"])
    font.setPointSize(10)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)  # no pixel-snapping, so thin strokes aren't "eaten"
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font

def system_prefers_dark():
    app = QApplication.instance()
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        window_color = app.palette().color(QPalette.ColorRole.Window)
        return window_color.lightness() < 128

def set_accent(color):
    """Recolors the launcher's accent (buttons, highlights, links) in both themes."""
    c = QColor(color)
    if not c.isValid():
        c = QColor("#a666ff")
    base, hover, press = c.name(), c.lighter(120).name(), c.darker(125).name()
    for t in THEMES.values():
        t.update(accent=base, accent_hover=hover, accent_press=press, red=base, red_hover=hover, red_press=press)
    try:                                     # in-game toasts follow the accent too
        import overlay as _overlay_mod
        _overlay_mod.set_accent(base)
    except Exception:
        pass

def apply_accent_to_icon(color):
    """Recolors assets/icon.png to match the accent. The original is kept once as assets/icon_default.png
    and every recolor starts from it, so Reset restores the exact original. Returns True if icon.png changed."""
    import colorsys
    try:
        icon, orig = asset_path("icon.png"), asset_path("icon_default.png")
        if not os.path.exists(orig):
            if not os.path.exists(icon):
                return False
            shutil.copyfile(icon, orig)
        if QColor(color).name() == "#a666ff":
            shutil.copyfile(orig, icon)
            return True
        img = QImage(orig)
        if img.isNull():
            return False
        if max(img.width(), img.height()) > 512:
            img = img.scaled(512, 512, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
        base, target = QColor("#a666ff"), QColor(color)
        shift = (max(target.hslHue(), 0) - base.hslHue()) / 360.0
        bs = base.hslSaturation() / 255.0
        ratio = (target.hslSaturation() / 255.0) / bs if bs else 1.0
        ptr = img.bits()
        ptr.setsize(img.sizeInBytes())
        data, cache = bytearray(ptr), {}
        for i in range(0, len(data), 4):                      # bytes are B, G, R, A
            key = (data[i], data[i + 1], data[i + 2])
            new = cache.get(key)
            if new is None:
                h, l, sat = colorsys.rgb_to_hls(key[2] / 255.0, key[1] / 255.0, key[0] / 255.0)
                r, g, b = colorsys.hls_to_rgb((h + shift) % 1.0, l, min(1.0, sat * ratio))
                new = cache[key] = (round(b * 255), round(g * 255), round(r * 255))
            data[i], data[i + 1], data[i + 2] = new
        out = QImage(bytes(data), img.width(), img.height(), img.bytesPerLine(), QImage.Format.Format_ARGB32).copy()
        return out.save(icon, "PNG")
    except Exception:
        return False

def apply_dark_mode(mode):
    app = QApplication.instance()
    if app is None:
        return
    app.setStyle("Fusion")
    use_dark = mode == "on" or (mode == "system" and system_prefers_dark())
    theme = THEMES["dark" if use_dark else "light"]
    try:                                     # in-game toasts follow light / dark mode
        import overlay as _overlay_mod
        _overlay_mod.set_light(not use_dark)
    except Exception:
        pass
    app.setPalette(build_palette(theme))
    app.setFont(build_app_font())
    family = load_app_font()
    force_font = f'* {{ font-family: "{family}"; }}\n' if family else ""
    app.setStyleSheet(force_font + STYLE_TEMPLATE.substitute(theme))
    _theme_state["nav_text"] = theme["nav_text"]
    _theme_state["nav_active"] = theme["nav_active"]
    for listener in list(_theme_listeners):
        try:
            listener()
        except Exception:
            pass

class JsonEditorDialog(QDialog):
    def __init__(self, file_path, title="Edit Save File", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedSize(380, 460)
        self.setModal(True)
        self.file_path = file_path
        self.trailing_null = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        self.data = {}
        if os.path.exists(file_path):
            try:
                self.data, self.trailing_null = load_game_json(file_path)
            except Exception:
                self.data = {}
                self.trailing_null = False

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content_widget = QWidget()
        form_layout = QFormLayout(content_widget)
        form_layout.setSpacing(12)

        self.inputs = {}
        if isinstance(self.data, dict) and self.data:
            for key, val in self.data.items():
                key_str = str(key)
                formatted_key = key_str.replace('_', ' ')
                if formatted_key:
                    formatted_key = formatted_key[0].upper() + formatted_key[1:]

                line_edit = QLineEdit(str(val))
                form_layout.addRow(QLabel(formatted_key), line_edit)
                self.inputs[key] = (line_edit, type(val))
        else:
            no_data_label = QLabel("No editable keys found in JSON file.")
            no_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            form_layout.addRow(no_data_label)

        scroll.setWidget(content_widget)
        layout.addWidget(scroll)

        btn_save = QPushButton("Save and Exit")
        btn_save.setObjectName("Primary")
        btn_save.setFixedHeight(38)
        btn_save.clicked.connect(self.save_and_exit)
        layout.addWidget(btn_save)

    def save_and_exit(self):
        if not self.inputs:
            self.accept()
            return

        new_data = {}
        for key, (line_edit, orig_type) in self.inputs.items():
            text_val = line_edit.text().strip()
            if orig_type is bool:
                val = text_val.lower() in ('true', '1', 'yes')
            elif orig_type is int:
                try:
                    val = int(text_val)
                except ValueError:
                    val = text_val
            elif orig_type is float:
                try:
                    val = float(text_val)
                except ValueError:
                    val = text_val
            else:
                val = text_val
            new_data[key] = val

        try:
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            structured = unflatten_dict(new_data)
            payload = json.dumps(structured, indent=4).encode('utf-8')
            if self.trailing_null:
                payload += b'\x00'
            with open(self.file_path, 'wb') as f:
                f.write(payload)
        except Exception:
            pass

        self.accept()

class ExistingFileDialog(QDialog):
    def __init__(self, filename, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Existing File Found")
        self.setFixedSize(400, 150)
        self.setModal(True)
        self.choice = None

        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)

        label = QLabel(f"An existing '{filename}' was found.")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        btn_use = QPushButton("Use Existing")
        btn_use.setObjectName("Primary")
        btn_overwrite = QPushButton("Overwrite")

        btn_use.clicked.connect(self.use_existing)
        btn_overwrite.clicked.connect(self.overwrite)

        btn_layout.addWidget(btn_use)
        btn_layout.addWidget(btn_overwrite)

        layout.addLayout(btn_layout)

    def use_existing(self):
        self.choice = "use"
        self.accept()

    def overwrite(self):
        self.choice = "overwrite"
        self.accept()

NET_TIMEOUT = 30  # seconds without data before a download request gives up

def stream_download(response, dest_path, emit_progress, is_cancelled=lambda: False):
    """Writes response to dest_path, reporting progress at most ~10 times a second
    (one signal per 8 KB block flooded the GUI thread on large games)."""
    total = int(response.headers.get('Content-Length') or 0)
    downloaded, start, last_emit = 0, time.time(), 0.0
    with open(dest_path, 'wb') as f:
        while not is_cancelled():
            buf = response.read(1 << 16)
            if not buf:
                break
            f.write(buf)
            downloaded += len(buf)
            now = time.time()
            if now - last_emit >= 0.1:
                last_emit = now
                emit_progress(downloaded, total, downloaded / max(now - start, 1e-6))
    elapsed = max(time.time() - start, 1e-6)
    emit_progress(downloaded, total, downloaded / elapsed)

# ---- resumable downloads: a dropped connection or an expired link keeps what's already on disk ----
DL_MAX_FAILS = 6                          # failed attempts in a row (with no new data) before giving up
DL_DEAD_CODES = (400, 401, 403, 404, 410) # the link itself is dead: ask for a new one right away

class _DLBadLink(Exception):
    """The link answered, but not with the file we were downloading (error page, wrong offset...)."""

class _DLRestart(Exception):
    """The new link serves a different file than the partial one: start again from byte 0."""

class _DLFatal(Exception):
    """Retrying can't help (e.g. the disk is full)."""

def _dl_cancelled(w):
    return bool(getattr(w, "_is_cancelled", False))

def _dl_backoff(fails):
    return min(2 ** fails, 20)

def _dl_wait(w, seconds):
    """Sleeps; returns True if the download was cancelled meanwhile."""
    end = time.time() + seconds
    while time.time() < end:
        if _dl_cancelled(w):
            return True
        time.sleep(0.2)
    return _dl_cancelled(w)

def resumable_download(w, get_link, pick_name, expected_total=0, first=None):
    """Downloads a file into DOWNLOAD_DIR and returns its path.

    get_link()  -> (url, headers): finds a download link; called again whenever a fresh one is needed.
    pick_name(response, url) -> file name, used once, on the first connection.
    first       -> an already-found (url, headers), so the first link isn't looked up twice.

    If the connection drops or the link dies mid-download, the bytes already on disk are kept, the downloader
    reconnects (a new link when the old one is dead, or didn't work on a retry) and continues with a Range
    request from the same byte. If a server ignores Range, it skips ahead to the same byte instead.
    Progress never goes backwards. Returns early (file left in place) when w is cancelled; callers clean up."""
    url, headers = first if first else get_link()
    dest_path, done, server_total = None, 0, 0
    fails, retry_same, need_new = 0, 1, False
    while True:
        if _dl_cancelled(w):
            if dest_path:
                return dest_path
            raise RuntimeError("CANCELLED")
        if need_new:
            w.status_update.emit("Getting a new download link...")
            try:
                url, headers = get_link()
                need_new = False
                try:
                    w.download_url = url        # the card/worker always points at the link in use
                except Exception:
                    pass
            except Exception as e:
                if _dl_cancelled(w) or str(e) == "CANCELLED":
                    return dest_path
                fails += 1
                if fails > DL_MAX_FAILS:
                    raise RuntimeError(f"Couldn't get a new download link: {e}") from e
                if _dl_wait(w, _dl_backoff(fails)):
                    return dest_path
                continue
        before = done
        try:
            if done > 0 and (not os.path.exists(dest_path) or os.path.getsize(dest_path) < done):
                done = before = 0               # the partial file went missing: nothing to resume
            hdrs = dict(headers)
            if done > 0:
                hdrs["Range"] = f"bytes={done}-"
                w.status_update.emit("Reconnecting...")
            else:
                w.status_update.emit("Connecting...")
            try:
                response = urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=NET_TIMEOUT)
            except urllib.error.HTTPError as he:
                if he.code == 416 and done > 0:   # "range not satisfiable": everything had already arrived, or the file changed
                    m = re.search(r"/(\d+)\s*$", he.headers.get("Content-Range") or "")
                    if m and int(m.group(1)) == done:
                        return dest_path
                    raise _DLRestart()
                raise
            with response:
                w._response = response
                if _dl_cancelled(w):
                    raise RuntimeError("CANCELLED")
                code = getattr(response, "status", 200)
                ctype = (response.headers.get("Content-Type") or "").lower()
                clen = int(response.headers.get("Content-Length") or 0)
                skip = 0
                if code == 206 and done > 0:
                    m = re.match(r"\s*bytes\s+(\d+)-\d+/(\d+|\*)", response.headers.get("Content-Range") or "")
                    if not m or int(m.group(1)) != done:
                        raise _DLBadLink("The server resumed at the wrong position.")
                    if m.group(2) != "*":
                        total = int(m.group(2))
                        if server_total and total != server_total:
                            raise _DLRestart()
                        server_total = total
                else:                           # a whole-file answer: first connection, or this server ignores Range
                    if done > 0:
                        if ctype.startswith("text/html"):
                            raise _DLBadLink("The link returned a web page instead of the file.")
                        if (server_total and clen and clen != server_total) or (clen and clen <= done):
                            raise _DLRestart()
                        skip = done
                    if clen:
                        server_total = clen
                shown_total = int(expected_total or 0) or server_total
                if dest_path is None:
                    dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, pick_name(response, url)))
                    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                with open(dest_path, "r+b" if done > 0 else "wb") as f:
                    if done > 0:
                        f.truncate(done)
                        f.seek(done)
                    w.progress.emit(done, shown_total, 0.0)    # the bar keeps its place on a reconnect
                    if skip:
                        w.status_update.emit("This server can't resume, skipping ahead to where it stopped...")
                        left = skip
                        while left > 0:
                            if _dl_cancelled(w):
                                return dest_path
                            chunk = response.read(min(1 << 16, left))
                            if not chunk:
                                raise ConnectionError("Connection closed while skipping ahead")
                            left -= len(chunk)
                    sess_t0, sess_base, last_emit = time.time(), done, 0.0
                    while not _dl_cancelled(w):
                        buf = response.read(1 << 16)
                        if not buf:
                            break
                        try:
                            f.write(buf)
                        except OSError as de:
                            raise _DLFatal(f"Couldn't write the file: {de}")
                        done += len(buf)
                        now = time.time()
                        if now - last_emit >= 0.1:
                            last_emit = now
                            w.progress.emit(done, shown_total, (done - sess_base) / max(now - sess_t0, 1e-6))
                if _dl_cancelled(w):
                    return dest_path
                if server_total and done < server_total:
                    raise ConnectionError(f"Connection closed early ({done} of {server_total} bytes)")
                w.progress.emit(done, shown_total, (done - sess_base) / max(time.time() - sess_t0, 1e-6))
                return dest_path
        except Exception as e:
            if _dl_cancelled(w) or str(e) == "CANCELLED":
                if dest_path:
                    return dest_path
                raise RuntimeError("CANCELLED")
            if isinstance(e, _DLFatal):
                raise RuntimeError(str(e)) from e
            if dest_path is None:
                raise                           # never got going: report it like before
            if isinstance(e, _DLRestart):
                done, server_total = 0, 0
            made_progress = done > before
            if made_progress:
                fails, retry_same = 0, 1
            else:
                fails += 1
            if fails > DL_MAX_FAILS:
                raise RuntimeError(f"Download failed after {DL_MAX_FAILS} retries: {e}") from e
            dead = isinstance(e, _DLBadLink) or (isinstance(e, urllib.error.HTTPError) and e.code in DL_DEAD_CODES)
            if dead or retry_same <= 0:
                need_new = True                 # a retry on the same link already failed (or it's plainly dead)
            else:
                retry_same -= 1                 # first hiccup: the same link usually still works
            wait = _dl_backoff(fails)
            w.status_update.emit(f"Connection lost, retrying in {wait}s...")
            if _dl_wait(w, wait):
                return dest_path

def _resolve_version_link(w):
    """Reads the game's version file and returns its download_link_windows (MediaFire pages are resolved to the
    direct link). Called again for a brand-new link whenever a download needs one."""
    w.status_update.emit("Checking version...")
    req = urllib.request.Request(w.version_url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
        content = resp.read().decode('utf-8')
    w.remote_version = parse_remote_version(content)
    download_url = None
    for line in content.splitlines():
        if 'download_link_windows' in line:
            parts = line.split('=', 1)
            if len(parts) == 2:
                download_url = parts[1].strip().strip('"').strip("'")
                break
    if not download_url:
        raise ValueError("Could not find download_link_windows in version file")
    if "mediafire.com" in download_url:
        w.status_update.emit("Resolving MediaFire link...")
        mf_req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(mf_req, timeout=NET_TIMEOUT) as mf_resp:
            mf_html = mf_resp.read().decode('utf-8')
        match = re.search(r'href="(https?://download[^"]+)"', mf_html)
        if match:
            download_url = match.group(1)
    return download_url, {'User-Agent': 'Mozilla/5.0'}

def linux_install_terminal(linux_cmd, project_root, dest_path):
    """Starts the Linux conversion script in a terminal window and returns the process.
    Paths are shell-quoted and no outer shell is used, so a server-supplied file name can't run commands."""
    script_name = linux_cmd.rsplit("./", 1)[-1].strip() if "./" in linux_cmd else "installer.sh"
    full_cmd = linux_cmd.replace(f"./{script_name}", f"echo {shlex.quote(GAMES_DIR)} | bash ./{script_name}")
    inner_cmd = (f"cd {shlex.quote(project_root)} && {full_cmd}; "
                 f"rm -f {shlex.quote(dest_path)}; "
                 f'echo "Press ENTER to exit..."; read')
    bash = ["bash", "-c", inner_cmd]
    for term, prefix in (("kitty", []), ("alacritty", ["-e"]), ("foot", []), ("gnome-terminal", ["--"]),
                         ("konsole", ["-e"]), ("xfce4-terminal", ["-x"]), ("tilix", None),
                         ("xterm", ["-e"]), ("x-terminal-emulator", ["-e"])):
        if shutil.which(term):
            argv = [term, "-e", shlex.join(bash)] if prefix is None else [term, *prefix, *bash]  # tilix -e wants one string
            return _RealPopen(argv, cwd=project_root)
    return _RealPopen(bash, cwd=project_root)

class DownloadWorker(QThread):
    progress = pyqtSignal(object, object, float)
    status_update = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, version_url, game_name, linux_cmd, existing_zip_path=None, wine_extract_dir=None, download_only=False):
        super().__init__()
        self.version_url = version_url
        self.game_name = game_name
        self.linux_cmd = linux_cmd
        self.existing_zip_path = existing_zip_path
        self.wine_extract_dir = wine_extract_dir
        self.download_only = download_only
        self.remote_version = ""

    def run(self):
        try:
            downloaded_here = False
            if self.existing_zip_path and os.path.exists(self.existing_zip_path):
                dest_path = self.existing_zip_path
            else:
                downloaded_here = True
                dest_path = resumable_download(
                    self, lambda: _resolve_version_link(self),
                    # The server picks the name; strip path separators so it can't land outside DOWNLOAD_DIR.
                    lambda response, url: safe_filename_from_headers(response, self.game_name))

            if self.download_only:
                self.finished.emit(dest_path)
                return

            if self.wine_extract_dir:
                # Wine mode: plain extraction of the Windows build, no Linux conversion script.
                self.status_update.emit("Extracting game files...")
                part_dir = self.wine_extract_dir + ".part"
                shutil.rmtree(part_dir, ignore_errors=True)
                os.makedirs(part_dir, exist_ok=True)
                with zipfile.ZipFile(dest_path, 'r') as zip_ref:
                    zip_ref.extractall(part_dir)
                shutil.rmtree(self.wine_extract_dir, ignore_errors=True)
                os.rename(part_dir, self.wine_extract_dir)
                if downloaded_here:
                    try:
                        os.remove(dest_path)
                    except OSError:
                        pass
            elif sys.platform.startswith("linux"):
                self.status_update.emit("Linux Conversion Script Running...\nPlease complete installation in the new terminal window.")

                os.makedirs(GAMES_DIR, exist_ok=True)
                linux_install_terminal(self.linux_cmd, os.path.abspath(DOWNLOAD_DIR), dest_path).wait()
            else:
                self.status_update.emit("Extracting game files...")
                extract_dir = installed_game_dir(self.game_name)
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(dest_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)

            if downloaded_here and self.remote_version and not self.wine_extract_dir:
                record_installed_version(self.game_name, self.remote_version)
            self.finished.emit(dest_path)
        except Exception as e:
            self.failed.emit(str(e))

class PingWorker(QThread):
    result = pyqtSignal(bool)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            req = urllib.request.Request(self.url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if 200 <= resp.status < 400:
                    self.result.emit(True)
                    return
        except Exception:
            pass
        self.result.emit(False)

# ---------------------------------------------------------------- common redistributables
REDIST_DIRNAME = "_CommonRedist"
REDIST_DOWNLOADS = [
    # (name, url, file name)
    ("Visual C++ 2015-2022 (x64)", "https://aka.ms/vs/17/release/vc_redist.x64.exe", "vc_redist.x64.exe"),
    ("Visual C++ 2015-2022 (x86)", "https://aka.ms/vs/17/release/vc_redist.x86.exe", "vc_redist.x86.exe"),
    ("DirectX End-User Runtime", "https://download.microsoft.com/download/1/7/1/1718CCC4-6315-4D8E-9543-8E28A4E18C4C/dxwebsetup.exe", "dxwebsetup.exe"),
]
_REDIST_OK_CODES = {0, 1638, 1641, 3010, 5100}   # ok / newer already installed / reboot needed

def _redist_silent_args(filename):
    """Silent switches for a known redist installer, or None if we don't know how to run it quietly."""
    n = filename.lower()
    if n.endswith(".msi"):
        return "msi"
    if "unins" in n:
        return None
    if "vc_redist" in n or "vcredist" in n:
        return ["/q", "/norestart"]
    if "dxwebsetup" in n:
        return ["/Q"]
    if "dxsetup" in n:
        return ["/silent"]
    if "dotnet" in n or "ndp" in n:
        return ["/q", "/norestart"]
    if "oalinst" in n:
        return ["/s"]
    return None

def collect_redist_installers(redist_dir):
    """[(file name, path)] of the installers inside one _CommonRedist folder, one per file name."""
    seen, items = set(), []
    for base, _dirs, files in os.walk(redist_dir):
        for f in sorted(files):
            if not f.lower().endswith((".exe", ".msi")):
                continue
            key = f.lower()
            if key in seen or _redist_silent_args(f) is None:
                continue
            seen.add(key)
            items.append((f, os.path.join(base, f)))
    return items

def download_common_redist(dest_dir, report=None):
    """Downloads the standard redistributables into dest_dir (normally <game folder>/_CommonRedist).
    report(text, value, maximum) is called with progress (maximum 0 = unknown)."""
    os.makedirs(dest_dir, exist_ok=True)
    downloads = [d for d in REDIST_DOWNLOADS if sys.platform.startswith("win") or not _is_dx_setup(d[2])]
    total = len(downloads)
    for i, (name, url, fname) in enumerate(downloads, 1):
        dest = os.path.join(dest_dir, fname)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            continue
        if report:
            report(f"Downloading {name} ({i} of {total})", 0, 0)
        req = urllib.request.Request(url, headers={"User-Agent": "NCZ-Games-Launcher"})
        tmp = dest + ".part"
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
            size = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if report and size:
                    report(f"Downloading {name} ({i} of {total})", done, size)
        os.replace(tmp, dest)

def _is_dx_setup(filename):
    n = filename.lower()
    return "dxwebsetup" in n or "dxsetup" in n

_WINETRICKS_DX = ["d3dx9", "d3dx10", "d3dx11_43", "d3dcompiler_47"]

def _proton_wine_bin(tool):
    base = os.path.dirname(tool)
    for sub in ("files/bin/wine64", "files/bin/wine", "dist/bin/wine64", "dist/bin/wine"):
        p = os.path.join(base, sub)
        if os.path.isfile(p):
            return p
    return None

def _proton_redist_env(proton):
    env = _clean_proton_env()
    os.makedirs(proton[1], exist_ok=True)
    env["STEAM_COMPAT_DATA_PATH"] = proton[1]
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = os.path.expanduser("~/.local/share/Steam")
    return env

_redist_last = {"detail": ""}

def _redist_check(r, ok_codes=None):
    """True if the finished process succeeded; otherwise remembers a short reason for the results list."""
    ok_codes = _REDIST_OK_CODES if ok_codes is None else ok_codes
    if r.returncode in ok_codes:
        _redist_last["detail"] = ""
        return True
    text = ((r.stderr or b"") + b"\n" + (r.stdout or b"")).decode("utf-8", "replace")
    lines = [l.strip() for l in text.splitlines() if l.strip()
             and "unit test" not in l and "ProtonFixes" not in l and not l.startswith("fixme:")]
    _redist_last["detail"] = f"exit code {r.returncode}" + (f": {lines[-1][:160]}" if lines else "")
    return False

def _run_redist_installer(path, wine=None, proton=None):
    """Runs one installer silently. proton = (proton script, compat data folder) installs into that Proton prefix.
    Returns True on success (on failure _redist_last['detail'] says why)."""
    _redist_last["detail"] = ""
    if proton:
        tool, compat = proton
        env = _proton_redist_env(proton)
        if path == "winetricks":
            tricks, wbin = shutil.which("winetricks"), _proton_wine_bin(tool)
            if not tricks or not wbin:
                _redist_last["detail"] = "winetricks or Proton's wine not found"
                return False
            env["WINE"] = wbin
            env["WINEPREFIX"] = os.path.join(compat, "pfx")
            r = subprocess.run([tricks, "-q"] + _WINETRICKS_DX, env=env, capture_output=True, timeout=3600)
            return _redist_check(r, {0})
        a = _redist_silent_args(os.path.basename(path))
        cmd = [tool, "run", "msiexec", "/i", path, "/qn", "/norestart"] if a == "msi" else [tool, "run", path] + a
        return _redist_check(subprocess.run(cmd, env=env, capture_output=True, timeout=3600))
    if path == "winetricks":     # Linux stand-in for the DirectX installers (they don't work under wine)
        tricks = shutil.which("winetricks")
        if not tricks:
            _redist_last["detail"] = "winetricks not found"
            return False
        env = wine_game_env()
        if wine:
            env["WINE"] = wine
        r = subprocess.run([tricks, "-q"] + _WINETRICKS_DX, env=env, capture_output=True, timeout=3600)
        return _redist_check(r, {0})
    args = _redist_silent_args(os.path.basename(path))
    if sys.platform.startswith("win"):
        if args == "msi":
            exe, arg_str = "msiexec", f'/i "{path}" /qn /norestart'
        else:
            exe, arg_str = path, " ".join(args)
        q = lambda t: t.replace("'", "''")
        ps = (f"$p = Start-Process -FilePath '{q(exe)}' -ArgumentList '{q(arg_str)}' -Verb RunAs -Wait -PassThru; "
              "exit $p.ExitCode")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                           timeout=1800, creationflags=0x08000000)
        return _redist_check(r)
    if not wine:
        _redist_last["detail"] = "wine not found"
        return False
    cmd = [wine, "msiexec", "/i", path, "/qn", "/norestart"] if args == "msi" else [wine, path] + args
    r = subprocess.run(cmd, env=wine_game_env(), capture_output=True, timeout=1800)
    return _redist_check(r)

def install_game_redist(game_dir, wine=None, report=None, item_done=None, proton=None):
    """Installs the redists in <game_dir>/_CommonRedist; downloads them into that folder if it's missing/empty.
    Windows: runs them directly (asks for admin). Linux: runs them with wine.
    report(text, value, maximum) = progress; item_done(name, ok) = one installer finished. Returns a summary."""
    report = report or (lambda *a: None)
    item_done = item_done or (lambda *a: None)
    if not game_dir or not os.path.isdir(game_dir):
        raise RuntimeError("This game isn't installed.")
    if not sys.platform.startswith("win") and not wine and not proton:
        raise RuntimeError("Wine isn't installed, so the Windows redistributables can't be installed.")
    redist_dir = os.path.join(game_dir, REDIST_DIRNAME)
    report(f"Looking in {REDIST_DIRNAME}...", 0, 0)
    items = collect_redist_installers(redist_dir) if os.path.isdir(redist_dir) else []
    source = f"the game's {REDIST_DIRNAME} folder"
    if not items:
        download_common_redist(redist_dir, report)
        items = collect_redist_installers(redist_dir)
        source = f"a download into {REDIST_DIRNAME}"
    if not sys.platform.startswith("win"):
        # wine has DirectX built in and Microsoft's DirectX installers fail under it; winetricks adds the d3dx DLLs instead
        had_dx = any(_is_dx_setup(n) for n, _p in items)
        items = [x for x in items if not _is_dx_setup(x[0])]
        if had_dx and shutil.which("winetricks") and (not proton or _proton_wine_bin(proton[0])):
            items.append(("DirectX (d3dx9/10/11 via winetricks)", "winetricks"))
    if not items:
        raise RuntimeError("Nothing to install here. Wine already includes DirectX; "
                           "install winetricks if you want the d3dx9/10/11 DLLs added too.")
    ok, failed = 0, []
    for i, (name, path) in enumerate(items):
        report(f"Installing {name} ({i + 1} of {len(items)})", i, len(items))
        try:
            good = _run_redist_installer(path, wine, proton)
            detail = _redist_last["detail"]
        except Exception as e:
            good, detail = False, str(e)[:160]
        item_done(name, good, "" if good else detail)
        if good:
            ok += 1
        else:
            failed.append(name)
    report("Done", len(items), len(items))
    text = f"Installed {ok} of {len(items)} redistributables from {source}."
    if failed:
        text += f" {len(failed)} couldn't be installed."
    return text

class _RedistWorker(QThread):
    progress = pyqtSignal(str, int, int)
    item = pyqtSignal(str, bool, str)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, game_dir, wine, proton=None):
        super().__init__()
        self.game_dir, self.wine, self.proton = game_dir, wine, proton

    def run(self):
        try:
            text = install_game_redist(self.game_dir, self.wine,
                                       report=lambda t, v, m: self.progress.emit(t, int(v), int(m)),
                                       item_done=lambda n, ok, d="": self.item.emit(n, bool(ok), d),
                                       proton=self.proton)
        except Exception as e:
            self.failed.emit(str(e))
        else:
            self.done.emit(text)

class RedistDialog(QDialog):
    """Progress window for installing a game's common redistributables."""
    def __init__(self, title, game_dir, wine, parent=None, proton=None):
        super().__init__(parent)
        self.setWindowTitle("Common Redistributables")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self._finished = False
        self.result_text = ""
        self.error = ""
        self._lines = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(6)

        heading = QLabel("Installing redistributables")
        heading.setObjectName("RowTitle")
        sub = QLabel(title + (" (Proton prefix)" if proton else ""))
        sub.setObjectName("RowDesc")
        sub.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(sub)
        layout.addSpacing(14)

        self.status = QLabel("Starting...")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addSpacing(2)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        layout.addWidget(self.bar)

        self.results = QLabel("")
        self.results.setTextFormat(Qt.TextFormat.RichText)
        self.results.setWordWrap(True)
        self.results.setVisible(False)
        layout.addSpacing(10)
        layout.addWidget(self.results)

        hint = "If Windows asks for permission, approve it so the installers can run." \
            if sys.platform.startswith("win") else (
            "Installers run inside the game's Proton prefix and may take a minute." if proton
            else "Installers run through wine and may take a minute.")
        self.hint = QLabel(hint)
        self.hint.setObjectName("RowDesc")
        self.hint.setWordWrap(True)
        layout.addSpacing(8)
        layout.addWidget(self.hint)

        row = QHBoxLayout()
        row.addStretch(1)
        self.close_btn = QPushButton("Close")
        self.close_btn.setFixedWidth(110)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        row.addWidget(self.close_btn)
        layout.addSpacing(10)
        layout.addLayout(row)

        self.worker = _RedistWorker(game_dir, wine, proton)
        self.worker.progress.connect(self._on_progress)
        self.worker.item.connect(self._on_item)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)

    def _on_progress(self, text, value, maximum):
        self.status.setText(text)
        self.bar.setRange(0, maximum)
        self.bar.setValue(value)

    def _on_item(self, name, ok, detail=""):
        mark = "&#10003;" if ok else "&#10007;"
        color = "#10eb73" if ok else "#ff5c5c"
        line = f'<span style="color:{color}">{mark}</span>&nbsp; {html_lib.escape(name)}'
        if detail:
            line += f'<br><span style="color:#888">&nbsp;&nbsp;&nbsp;&nbsp;{html_lib.escape(detail)}</span>'
        self._lines.append(line)
        self.results.setText("<br>".join(self._lines))
        self.results.setVisible(True)

    def _finish(self):
        self._finished = True
        self.hint.setVisible(False)
        self.close_btn.setEnabled(True)
        self.close_btn.setFocus()

    def _on_done(self, text):
        self.result_text = text
        self.status.setText(text)
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self._finish()

    def _on_failed(self, message):
        self.error = message
        self.status.setText(f"Couldn't install: {message}")
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self._finish()

    def reject(self):
        if self._finished:
            super().reject()

    def closeEvent(self, event):
        if self._finished:
            event.accept()
        else:
            event.ignore()

    def run(self):
        self.worker.start()
        self.exec()
        self.worker.wait()

def install_redist_for_game(app, game_dir_func, title, launch_func=None):
    """Menu action: installs the common redistributables for one game (resolved at click time).
    launch_func() -> (exe, compat tool): if the game runs on a Proton tool, installs into its Proton prefix."""
    game_dir = game_dir_func()
    wine = None if sys.platform.startswith("win") else default_wine_path(app)
    proton = None
    if sys.platform.startswith("linux") and launch_func:
        exe, tool = launch_func()
        if exe and compat_tool_kind(tool) == "proton":
            proton = (tool, compat_data_dir_for(exe))
    RedistDialog(title, game_dir, wine, app, proton).run()

def _ae_launch_info(game_id, game_name):
    cfg = GAME_INFO.get(game_id)
    exe = find_game_exe(_ae_redist_dir(game_name), cfg["exe"]) if cfg else None
    return exe, load_compat_config().get(game_id, "")

def _ae_redist_dir(game_name):
    if sys.platform.startswith("linux"):
        wd = wine_game_dir(game_name)
        if os.path.isdir(wd):
            return wd
    return installed_game_dir(game_name)

# ---------------------------------------------------------------- start with the system
STARTUP_NAME = "NCZGamesLauncher"
_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

def _startup_command():
    target, args, _workdir = get_launch_command()
    return [target] + list(args) + ["--startup"]

def _linux_autostart_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", "ncz-games-launcher.desktop")

def set_startup_registered(enabled):
    """Adds / removes the launcher from the system's startup apps (Windows: Run key, Linux: ~/.config/autostart)."""
    if sys.platform.startswith("win"):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, STARTUP_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(_startup_command()))
            else:
                try:
                    winreg.DeleteValue(key, STARTUP_NAME)
                except FileNotFoundError:
                    pass
    elif sys.platform.startswith("linux"):
        path = _linux_autostart_path()
        if not enabled:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return
        quote = lambda a: '"' + str(a).replace('"', '\\"').replace("%", "%%") + '"'
        lines = ["[Desktop Entry]", "Type=Application", "Name=NCZ Games Launcher",
                 "Exec=" + " ".join(quote(a) for a in _startup_command()),
                 f"Path={get_launch_command()[2]}"]
        icon = prepare_shortcut_icon()
        if icon:
            lines.append(f"Icon={icon}")
        lines += ["Terminal=false", "X-GNOME-Autostart-enabled=true", ""]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

def apply_startup_setting():
    """On by default; refreshed every launch so the startup entry keeps pointing at the right place."""
    if _ncz_profile:     # a separate-data profile instance never registers itself
        return
    try:
        if load_launcher_settings().get("start_on_boot", True):
            set_startup_registered(True)
    except Exception:
        pass

# ---------------------------------------------------------------- only one launcher at a time
def _instance_name():
    user = re.sub(r"[^A-Za-z0-9]", "", os.environ.get("USERNAME") or os.environ.get("USER") or "user")
    return f"NCZGamesLauncher-{user}-{_ncz_profile}"

def _another_instance_running(wake=True):
    """True if a launcher is already running (it's asked to come to the front unless wake=False)."""
    try:
        from PyQt6.QtNetwork import QLocalSocket
    except ImportError:
        return False
    sock = QLocalSocket()
    sock.connectToServer(_instance_name())
    if sock.waitForConnected(400):
        if wake:
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(400)
        sock.disconnectFromServer()
        return True
    return False

def _prefer_xwayland():
    """On a Wayland session, run the launcher through XWayland (Qt's xcb plugin). Native Wayland windows can't be
    placed over a game and global hotkeys (Shift+Q) can't be heard there; XWayland also hosts Proton games.
    Turn it off in Settings (Linux) or by setting QT_QPA_PLATFORM yourself."""
    if not sys.platform.startswith("linux") or os.environ.get("QT_QPA_PLATFORM"):
        return
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland"):
        return
    if not os.environ.get("DISPLAY") or not load_launcher_settings().get("xwayland_mode", True):
        return
    try:
        import ctypes.util
        if not ctypes.util.find_library("xcb-cursor"):      # Qt's xcb plugin refuses to load without it
            return
    except Exception:
        return
    os.environ["QT_QPA_PLATFORM"] = "xcb"

def _other_launcher_pids():
    """PIDs of other launcher processes started from this same file/exe with the same NCZ_PROFILE (Linux only)."""
    me = os.getpid()
    frozen = getattr(sys, "frozen", False)
    target = os.path.realpath(sys.executable if frozen else os.path.abspath(__file__))
    pids = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for d in entries:
        if not d.isdigit() or int(d) == me:
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                args = [a.decode(errors="ignore") for a in f.read().split(b"\0") if a]
            if not args or "--ncz-overlay-server" in args:
                continue
            cwd = os.readlink(f"/proc/{d}/cwd")
            with open(f"/proc/{d}/environ", "rb") as f:
                env = dict(e.split(b"=", 1) for e in f.read().split(b"\0") if b"=" in e)
        except OSError:
            continue
        prof = re.sub(r"[^A-Za-z0-9_-]", "_", env.get(b"NCZ_PROFILE", b"").decode(errors="ignore").strip())
        if prof != _ncz_profile:
            continue
        cands = args[:1] if frozen else args[1:4]
        if any(os.path.realpath(os.path.join(cwd, a)) == target for a in cands):
            pids.append(int(d))
    return pids

_instance_lock_ref = None

def _acquire_instance_lock():
    """Atomic 'one launcher per profile' lock (a lock file in the temp folder). Unlike the local-socket check it
    also holds while the launcher is still starting up, so two quick starts - e.g. the login autostart plus a
    double click - can't both get through. A lock left by a crashed launcher is recognised and taken over."""
    global _instance_lock_ref
    if _instance_lock_ref is not None:
        return True
    try:
        from PyQt6.QtCore import QLockFile
    except ImportError:
        return True
    import tempfile
    lock = QLockFile(os.path.join(tempfile.gettempdir(), _instance_name() + ".lock"))
    lock.setStaleLockTime(0)           # a dead owner is still detected by its process id; never steal by age
    if lock.tryLock(0):
        _instance_lock_ref = lock
        return True
    return False

def _kill_other_launchers():
    """Fallback for an old instance that doesn't understand the 'quit' request, or that is still starting up."""
    if sys.platform.startswith("linux"):
        import signal
        pids = _other_launcher_pids()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            for _ in range(15):
                if not any(os.path.exists(f"/proc/{p}") for p in pids):
                    return
                time.sleep(0.2)
    elif sys.platform.startswith("win") and not _ncz_profile:
        names = [os.path.basename(sys.executable)] if getattr(sys, "frozen", False) else []
        script = os.path.abspath(__file__).replace("'", "''")
        cond = f"$_.CommandLine -like '*{script}*'"
        if names:
            cond = f"$_.Name -eq '{names[0]}'"
        ps = (f"Get-CimInstance Win32_Process | Where-Object {{ $_.ProcessId -ne {os.getpid()} -and "
              f"{cond} }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=15, creationflags=0x08000000)
        except Exception:
            pass

def _replace_running_instance():
    """Closes the launcher that's already running so this one can take over. True once we hold the lock."""
    try:
        from PyQt6.QtNetwork import QLocalSocket
        sock = QLocalSocket()
        sock.connectToServer(_instance_name())
        if sock.waitForConnected(400):
            sock.write(b"quit")
            sock.flush()
            sock.waitForBytesWritten(400)
            sock.disconnectFromServer()
    except Exception:
        pass
    for _ in range(25):                              # up to ~5 s for it to close by itself
        if _acquire_instance_lock() and not _another_instance_running(wake=False):
            return True
        time.sleep(0.2)
    _kill_other_launchers()
    for _ in range(15):
        if _acquire_instance_lock() and not _another_instance_running(wake=False):
            return True
        time.sleep(0.2)
    return False

def _start_instance_server(app, window=None):
    window_arg = window
    try:
        from PyQt6.QtNetwork import QLocalServer
    except ImportError:
        return
    name = _instance_name()
    QLocalServer.removeServer(name)      # clears a stale socket left by a crash
    server = QLocalServer(app)
    if not server.listen(name):
        return
    def on_connection():
        window = app._ncz_main_window if getattr(app, "_ncz_main_window", None) is not None else window_arg
        woke = False
        quit_req = False
        friends_req = False
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            conn.waitForReadyRead(200)
            data = bytes(conn.readAll())
            woke = woke or data == b"show"
            quit_req = quit_req or data == b"quit"
            friends_req = friends_req or data == b"friends"
            conn.disconnectFromServer()
        if friends_req:                  # `main.py --friends` from a desktop keyboard shortcut
            fo = getattr(window, "_friends_overlay", None)
            if fo is not None:
                fo._verbose = True
                fo.toggle_req.emit()
        if quit_req:                     # a newer launcher is replacing this one
            server.close()
            QApplication.quit()
            QTimer.singleShot(2500, lambda: os._exit(0))   # don't hang on a stuck background thread
            return
        if woke and window is None:      # still starting up: show it as soon as it exists
            app._ncz_wake_pending = True
        elif woke:
            window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
            window.show()
            window.raise_()
            window.activateWindow()
    server.newConnection.connect(on_connection)
    app._ncz_instance_server = server    # keep it alive

class SettingsPage(QWidget):
    steamgriddb_key_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._key_worker = None
        QApplication.instance().aboutToQuit.connect(self._wait_for_key_worker)
        self.setObjectName("Content")
        # Everything lives in a scroll area so the panels keep their full height instead of being squashed
        # (other code adds rows through self.layout(), so layout() returns the inner content layout).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content = QWidget()
        self._content.setObjectName("SettingsContent")
        self._scroll.setWidget(self._content)
        outer.addWidget(self._scroll)
        layout = QVBoxLayout(self._content)
        self._content_layout = layout
        layout.setContentsMargins(36, 28, 36, 28)
        layout.setSpacing(0)

        title = QLabel("Settings")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        layout.addSpacing(22)

        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 18, 22, 18)

        row = QHBoxLayout()
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name = QLabel("Dark Mode")
        name.setObjectName("RowTitle")
        desc = QLabel("System: Follows your device's theme")
        desc.setObjectName("RowDesc")
        text_col.addWidget(name)
        text_col.addWidget(desc)

        self.dark_mode_combo = QComboBox()
        self.dark_mode_combo.setFixedWidth(160)
        self.dark_mode_combo.addItem("Off", "off")
        self.dark_mode_combo.addItem("On", "on")
        self.dark_mode_combo.addItem("System", "system")

        current_mode = load_launcher_settings().get("dark_mode", "system")
        index = self.dark_mode_combo.findData(current_mode)
        self.dark_mode_combo.setCurrentIndex(index if index >= 0 else self.dark_mode_combo.findData("system"))
        self.dark_mode_combo.currentIndexChanged.connect(self.on_dark_mode_changed)

        row.addLayout(text_col, 1)
        row.addWidget(self.dark_mode_combo)
        panel_layout.addLayout(row)

        acc_row = QHBoxLayout()
        acc_text = QVBoxLayout()
        acc_text.setSpacing(2)
        acc_name = QLabel("Accent Color")
        acc_name.setObjectName("RowTitle")
        acc_desc = QLabel("Pick the highlight color used across the launcher")
        acc_desc.setObjectName("RowDesc")
        acc_text.addWidget(acc_name)
        acc_text.addWidget(acc_desc)
        self.accent_btn = QPushButton("Choose...")
        self.accent_btn.setFixedWidth(100)
        self.accent_btn.clicked.connect(self.on_accent_pick)
        accent_reset = QPushButton("Reset")
        accent_reset.setFixedWidth(70)
        accent_reset.clicked.connect(lambda: self._set_accent_setting("#a666ff"))
        acc_row.addLayout(acc_text, 1)
        acc_row.addWidget(self.accent_btn)
        acc_row.addWidget(accent_reset)
        panel_layout.addSpacing(10)
        panel_layout.addLayout(acc_row)
        layout.addWidget(panel)
        layout.addSpacing(12)

        shortcut_panel = QFrame()
        shortcut_panel.setObjectName("Panel")
        shortcut_panel_layout = QVBoxLayout(shortcut_panel)
        shortcut_panel_layout.setContentsMargins(22, 18, 22, 18)
        shortcut_row = QHBoxLayout()
        shortcut_text = QVBoxLayout()
        shortcut_text.setSpacing(2)
        shortcut_name = QLabel("Desktop Shortcut")
        shortcut_name.setObjectName("RowTitle")
        shortcut_desc = QLabel("Add NCZ Games Launcher to your desktop")
        shortcut_desc.setObjectName("RowDesc")
        shortcut_text.addWidget(shortcut_name)
        shortcut_text.addWidget(shortcut_desc)
        self.shortcut_btn = QPushButton("Create Shortcut")
        self.shortcut_btn.setFixedWidth(160)
        self.shortcut_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.shortcut_btn.clicked.connect(self.on_create_shortcut)
        shortcut_row.addLayout(shortcut_text, 1)
        shortcut_row.addWidget(self.shortcut_btn)
        shortcut_panel_layout.addLayout(shortcut_row)
        layout.addWidget(shortcut_panel)
        layout.addSpacing(12)

        startup_panel = QFrame()
        startup_panel.setObjectName("Panel")
        startup_panel_layout = QVBoxLayout(startup_panel)
        startup_panel_layout.setContentsMargins(22, 18, 22, 18)
        startup_row = QHBoxLayout()
        startup_text = QVBoxLayout()
        startup_text.setSpacing(2)
        startup_name = QLabel("Start with System")
        startup_name.setObjectName("RowTitle")
        startup_desc = QLabel("Start the launcher in the background when you log in")
        startup_desc.setObjectName("RowDesc")
        startup_text.addWidget(startup_name)
        startup_text.addWidget(startup_desc)
        self.startup_btn = QPushButton()
        self.startup_btn.setCheckable(True)
        self.startup_btn.setChecked(load_launcher_settings().get("start_on_boot", True))
        self.startup_btn.setFixedWidth(160)
        self.startup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.startup_btn.clicked.connect(self.on_startup_toggled)
        self._refresh_startup_btn()
        startup_row.addLayout(startup_text, 1)
        startup_row.addWidget(self.startup_btn)
        startup_panel_layout.addLayout(startup_row)
        layout.addWidget(startup_panel)
        layout.addSpacing(12)

        autoupdate_panel = QFrame()
        autoupdate_panel.setObjectName("Panel")
        autoupdate_panel_layout = QVBoxLayout(autoupdate_panel)
        autoupdate_panel_layout.setContentsMargins(22, 18, 22, 18)
        autoupdate_row = QHBoxLayout()
        autoupdate_text = QVBoxLayout()
        autoupdate_text.setSpacing(2)
        autoupdate_name = QLabel("Automatic Updates")
        autoupdate_name.setObjectName("RowTitle")
        autoupdate_desc = QLabel("Download new launcher versions automatically when it starts. "
                                 "When disabled, the launcher stays on the version you have now.")
        autoupdate_desc.setObjectName("RowDesc")
        autoupdate_desc.setWordWrap(True)
        autoupdate_text.addWidget(autoupdate_name)
        autoupdate_text.addWidget(autoupdate_desc)
        self.autoupdate_btn = QPushButton()
        self.autoupdate_btn.setCheckable(True)
        self.autoupdate_btn.setChecked(load_launcher_settings().get("auto_update", True))
        self.autoupdate_btn.setFixedWidth(160)
        self.autoupdate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.autoupdate_btn.clicked.connect(self.on_autoupdate_toggled)
        self._refresh_autoupdate_btn()
        autoupdate_row.addLayout(autoupdate_text, 1)
        autoupdate_row.addWidget(self.autoupdate_btn)
        autoupdate_panel_layout.addLayout(autoupdate_row)
        layout.addWidget(autoupdate_panel)
        layout.addSpacing(12)

        for _key, _title, _desc in (
                ("close_to_tray", "Close button minimizes to tray",
                 "Closing the launcher keeps it running in the system tray (use Quit in the tray menu to exit). "
                 "When disabled, closing exits the launcher. Needs a system tray."),
                ("start_minimized", "Start in the tray",
                 "When the launcher starts with your computer it stays hidden in the tray instead of opening "
                 "its window.")):
            layout.addWidget(self._toggle_panel(_key, _title, _desc))
            layout.addSpacing(12)

        if sys.platform.startswith("linux"):
            xw_panel = QFrame()
            xw_panel.setObjectName("Panel")
            xw_panel_layout = QVBoxLayout(xw_panel)
            xw_panel_layout.setContentsMargins(22, 18, 22, 18)
            xw_row = QHBoxLayout()
            xw_text = QVBoxLayout()
            xw_text.setSpacing(2)
            xw_name = QLabel("Use XWayland on Wayland")
            xw_name.setObjectName("RowTitle")
            xw_desc = QLabel("Needed for the Shift+Q friends overlay and for overlay windows to show over games on a "
                             "Wayland desktop. Turn it off if the launcher looks blurry on a scaled display. "
                             "Restart the launcher to apply.")
            xw_desc.setObjectName("RowDesc")
            xw_desc.setWordWrap(True)
            xw_text.addWidget(xw_name)
            xw_text.addWidget(xw_desc)
            self.xwayland_btn = QPushButton()
            self.xwayland_btn.setCheckable(True)
            self.xwayland_btn.setChecked(load_launcher_settings().get("xwayland_mode", True))
            self.xwayland_btn.setFixedWidth(160)
            self.xwayland_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.xwayland_btn.clicked.connect(self.on_xwayland_toggled)
            self._refresh_xwayland_btn()
            xw_row.addLayout(xw_text, 1)
            xw_row.addWidget(self.xwayland_btn)
            xw_panel_layout.addLayout(xw_row)
            layout.addWidget(xw_panel)
            layout.addSpacing(12)

        sgdb_panel = QFrame()
        sgdb_panel.setObjectName("Panel")
        sgdb_layout = QVBoxLayout(sgdb_panel)
        sgdb_layout.setContentsMargins(22, 18, 22, 18)
        sgdb_layout.setSpacing(8)
        sgdb_name = QLabel("SteamGridDB API Key")
        sgdb_name.setObjectName("RowTitle")
        sgdb_desc = QLabel("Used to load covers in the Store")
        sgdb_desc.setObjectName("RowDesc")
        sgdb_layout.addWidget(sgdb_name)
        sgdb_layout.addWidget(sgdb_desc)

        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        self.sgdb_key_edit = QLineEdit()
        self.sgdb_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.sgdb_key_edit.setPlaceholderText("Paste your API key")
        self.sgdb_key_edit.setText(get_steamgriddb_key())
        self.sgdb_key_edit.returnPressed.connect(self.on_save_sgdb_key)
        self.sgdb_save_btn = QPushButton("Save")
        self.sgdb_save_btn.setFixedWidth(100)
        self.sgdb_save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sgdb_save_btn.clicked.connect(self.on_save_sgdb_key)
        key_row.addWidget(self.sgdb_key_edit, 1)
        key_row.addWidget(self.sgdb_save_btn)
        sgdb_layout.addLayout(key_row)

        self.sgdb_status = QLabel("")
        self.sgdb_status.setObjectName("RowDesc")
        self.sgdb_status.setWordWrap(True)
        sgdb_layout.addWidget(self.sgdb_status)

        get_key_btn = QPushButton("Get a free API key")
        get_key_btn.setFlat(True)
        get_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        get_key_btn.clicked.connect(lambda: webbrowser.open("https://www.steamgriddb.com/profile/preferences/api"))
        sgdb_layout.addWidget(get_key_btn, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(sgdb_panel)
        layout.addSpacing(12)

        cur_panel = QFrame()
        cur_panel.setObjectName("Panel")
        cur_panel_layout = QVBoxLayout(cur_panel)
        cur_panel_layout.setContentsMargins(22, 18, 22, 18)
        cur_row = QHBoxLayout()
        cur_text = QVBoxLayout()
        cur_text.setSpacing(2)
        cur_name = QLabel("Currency")
        cur_name.setObjectName("RowTitle")
        cur_desc = QLabel("Store prices are converted from USD, so they're approximate")
        cur_desc.setObjectName("RowDesc")
        cur_text.addWidget(cur_name)
        cur_text.addWidget(cur_desc)
        self.currency_combo = QComboBox()
        self.currency_combo.setFixedWidth(200)
        for code, label in CURRENCIES.items():
            self.currency_combo.addItem(label, code)
        idx = self.currency_combo.findData(load_launcher_settings().get("currency", "EGP"))
        self.currency_combo.setCurrentIndex(max(idx, 0))
        self.currency_combo.currentIndexChanged.connect(self.on_currency_changed)
        cur_row.addLayout(cur_text, 1)
        cur_row.addWidget(self.currency_combo)
        cur_panel_layout.addLayout(cur_row)
        layout.addWidget(cur_panel)
        layout.addSpacing(12)
        layout.addWidget(self._build_library_panel())

        if sys.platform.startswith("linux"):
            layout.addSpacing(12)
            d3d_panel = QFrame()
            d3d_panel.setObjectName("Panel")
            d3d_panel_layout = QVBoxLayout(d3d_panel)
            d3d_panel_layout.setContentsMargins(22, 18, 22, 18)
            d3d_row = QHBoxLayout()
            d3d_text = QVBoxLayout()
            d3d_text.setSpacing(2)
            d3d_name = QLabel("Direct3D Renderer")
            d3d_name.setObjectName("RowTitle")
            d3d_desc = QLabel("Used for Windows games run with Wine or Proton. DXVK/VKD3D are installed "
                              "into your Wine prefix on first launch (needs winetricks).")
            d3d_desc.setObjectName("RowDesc")
            d3d_desc.setWordWrap(True)
            d3d_text.addWidget(d3d_name)
            d3d_text.addWidget(d3d_desc)
            self.d3d_combo = QComboBox()
            self.d3d_combo.setFixedWidth(240)
            for key, label in WINE_D3D_MODES.items():
                self.d3d_combo.addItem(label, key)
            self.d3d_combo.setCurrentIndex(max(self.d3d_combo.findData(get_wine_d3d_mode()), 0))
            self.d3d_combo.currentIndexChanged.connect(self.on_wine_d3d_changed)
            d3d_row.addLayout(d3d_text, 1)
            d3d_row.addWidget(self.d3d_combo)
            d3d_panel_layout.addLayout(d3d_row)
            layout.addWidget(d3d_panel)
        layout.addStretch()

    def layout(self):
        return getattr(self, "_content_layout", None) or super().layout()

    # ---- game library locations (NCZLibrary on any partition)
    def _build_library_panel(self):
        panel = QFrame()
        panel.setObjectName("Panel")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(22, 18, 22, 18)
        pl.setSpacing(8)
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(2)
        name = QLabel("Game Library")
        name.setObjectName("RowTitle")
        desc = QLabel(f"Where new games are installed. Games inside a \"{LIBRARY_FOLDER_NAME}\" folder on any "
                      "partition are found automatically.")
        desc.setObjectName("RowDesc")
        desc.setWordWrap(True)
        text.addWidget(name)
        text.addWidget(desc)
        self.install_combo = QComboBox()
        self.install_combo.setFixedWidth(320)
        self.install_combo.activated.connect(self._on_install_location_chosen)
        row.addLayout(text, 1)
        row.addWidget(self.install_combo)
        pl.addLayout(row)
        self.library_status = QLabel("")
        self.library_status.setObjectName("RowDesc")
        self.library_status.setWordWrap(True)
        pl.addWidget(self.library_status)
        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add existing library folder...")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self._on_add_library_folder)
        rescan_btn = QPushButton("Rescan partitions")
        rescan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rescan_btn.clicked.connect(self._refresh_library_panel)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(rescan_btn)
        btn_row.addStretch()
        pl.addLayout(btn_row)
        self._refresh_library_panel()
        return panel

    def _refresh_library_panel(self):
        invalidate_library_roots()
        combo = self.install_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(f"Launcher folder (default)", DEFAULT_GAMES_DIR)
        known = {os.path.abspath(DEFAULT_GAMES_DIR)}
        for mp in mounted_partitions():
            path = os.path.abspath(partition_library_path(mp))
            try:
                free = shutil.disk_usage(mp).free / 1024 ** 3
                if not (os.access(mp, os.W_OK) or os.path.isdir(path)):
                    continue   # e.g. "/" owned by root: can't create NCZLibrary there
            except OSError:
                continue
            combo.addItem(f"{mp}  \u2022  {free:,.0f} GB free", path)
            known.add(path)
        for extra in load_launcher_settings().get("library_dirs", []):
            if os.path.abspath(extra) not in known:
                combo.addItem(extra, extra)
                known.add(os.path.abspath(extra))
        if os.path.abspath(GAMES_DIR) not in known:
            combo.addItem(GAMES_DIR, GAMES_DIR)
        combo.addItem("Choose another folder...", "")
        idx = combo.findData(GAMES_DIR)
        if idx < 0:
            idx = max(0, min([i for i in range(combo.count()) if combo.itemData(i)
                              and os.path.abspath(combo.itemData(i)) == os.path.abspath(GAMES_DIR)] or [0]))
        combo.setCurrentIndex(idx)
        combo.blockSignals(False)
        found = [r for r in library_roots() if r != os.path.abspath(GAMES_DIR)]
        self.library_status.setText(
            f"New games install to: {GAMES_DIR}" + (("\nAlso scanning: " + ", ".join(found)) if found else ""))

    def _on_install_location_chosen(self, _index):
        path = self.install_combo.currentData()
        if path == "":
            path = QFileDialog.getExistingDirectory(self, "Choose where to install games", os.path.expanduser("~"))
            if not path:
                self._refresh_library_panel()
                return
            if os.path.basename(path.rstrip("/\\")) != LIBRARY_FOLDER_NAME:
                path = os.path.join(path, LIBRARY_FOLDER_NAME)
        try:
            os.makedirs(path, exist_ok=True)
            if not os.access(path, os.W_OK):
                raise PermissionError(path)
        except OSError as e:
            QMessageBox.warning(self, "Game Library",
                                f"Can't use that folder:\n{path}\n\n{e}\n\n"
                                "If it's a partition that isn't yours to write to, mount it with your user's permissions.")
            self._refresh_library_panel()
            return
        settings = load_launcher_settings()
        settings["install_dir"] = "" if os.path.abspath(path) == os.path.abspath(DEFAULT_GAMES_DIR) else path
        extras = settings.setdefault("library_dirs", [])
        if settings["install_dir"] and path not in extras:
            extras.append(path)
        save_launcher_settings(settings)
        apply_install_dir()
        self._refresh_library_panel()

    def _on_add_library_folder(self):
        path = QFileDialog.getExistingDirectory(self, f"Choose a folder that contains installed games",
                                                os.path.expanduser("~"))
        if not path:
            return
        settings = load_launcher_settings()
        extras = settings.setdefault("library_dirs", [])
        if path not in extras:
            extras.append(path)
        save_launcher_settings(settings)
        self._refresh_library_panel()

    def on_save_sgdb_key(self):
        key = self.sgdb_key_edit.text().strip()
        settings = load_launcher_settings()
        settings["steamgriddb_api_key"] = key
        save_launcher_settings(settings)
        self.steamgriddb_key_changed.emit()
        if not key:
            self.sgdb_status.setText("Key removed. The Store will use Steam's own art.")
            return
        self.sgdb_status.setText("Checking key...")
        self.sgdb_save_btn.setEnabled(False)
        worker = TaskWorker(lambda: check_steamgriddb_key(key))
        worker.done.connect(self._on_sgdb_key_checked)
        worker.failed.connect(self._on_sgdb_key_check_failed)
        self._key_worker = worker
        worker.start()

    def _on_sgdb_key_checked(self, result):
        self.sgdb_save_btn.setEnabled(True)
        if result == "ok":
            self.sgdb_status.setText("Key saved and working.")
        else:
            self.sgdb_status.setText("SteamGridDB rejected this key. It was saved, but double-check it.")

    def _on_sgdb_key_check_failed(self, _message):
        self.sgdb_save_btn.setEnabled(True)
        self.sgdb_status.setText("Key saved, but it couldn't be checked right now (no connection?).")

    def _wait_for_key_worker(self):
        worker = self._key_worker
        if worker is not None and worker.isRunning():
            worker.wait(3000)

    def on_create_shortcut(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            path = create_desktop_shortcut()
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Desktop Shortcut", f"Couldn't create the shortcut:\n\n{e}")
            return
        QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "Desktop Shortcut", f"Shortcut created:\n{path}")

    def _refresh_startup_btn(self):
        self.startup_btn.setText("Enabled" if self.startup_btn.isChecked() else "Disabled")

    def on_startup_toggled(self):
        want = self.startup_btn.isChecked()
        try:
            set_startup_registered(want)
        except Exception as e:
            self.startup_btn.setChecked(not want)
            QMessageBox.warning(self, "Start with System", f"Couldn't change it:\n\n{e}")
        else:
            settings = load_launcher_settings()
            settings["start_on_boot"] = want
            save_launcher_settings(settings)
        self._refresh_startup_btn()

    def _toggle_panel(self, key, title, desc):
        panel = QFrame()
        panel.setObjectName("Panel")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(22, 18, 22, 18)
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(2)
        name = QLabel(title)
        name.setObjectName("RowTitle")
        info = QLabel(desc)
        info.setObjectName("RowDesc")
        info.setWordWrap(True)
        text.addWidget(name)
        text.addWidget(info)
        btn = QPushButton()
        btn.setCheckable(True)
        btn.setChecked(load_launcher_settings().get(key, True))
        btn.setFixedWidth(160)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def refresh():
            btn.setText("Enabled" if btn.isChecked() else "Disabled")

        def toggled():
            settings = load_launcher_settings()
            settings[key] = btn.isChecked()
            save_launcher_settings(settings)
            refresh()

        btn.clicked.connect(toggled)
        refresh()
        row.addLayout(text, 1)
        row.addWidget(btn)
        pl.addLayout(row)
        return panel

    def _refresh_xwayland_btn(self):
        self.xwayland_btn.setText("Enabled" if self.xwayland_btn.isChecked() else "Disabled")

    def on_xwayland_toggled(self):
        settings = load_launcher_settings()
        settings["xwayland_mode"] = self.xwayland_btn.isChecked()
        save_launcher_settings(settings)
        self._refresh_xwayland_btn()
        QMessageBox.information(self, "XWayland", "Restart the launcher for this to take effect.")

    def _refresh_autoupdate_btn(self):
        self.autoupdate_btn.setText("Enabled" if self.autoupdate_btn.isChecked() else "Disabled")

    def on_autoupdate_toggled(self):
        settings = load_launcher_settings()
        settings["auto_update"] = self.autoupdate_btn.isChecked()
        save_launcher_settings(settings)
        self._refresh_autoupdate_btn()

    def on_wine_d3d_changed(self):
        settings = load_launcher_settings()
        settings["wine_d3d"] = self.d3d_combo.currentData()
        save_launcher_settings(settings)

    def on_currency_changed(self):
        settings = load_launcher_settings()
        settings["currency"] = self.currency_combo.currentData()
        save_launcher_settings(settings)

    def on_accent_pick(self):
        from PyQt6.QtWidgets import QColorDialog
        current = QColor(load_launcher_settings().get("accent_color", "#a666ff"))
        chosen = QColorDialog.getColor(current, self, "Pick an accent color")
        if chosen.isValid():
            self._set_accent_setting(chosen.name())

    def _set_accent_setting(self, color):
        settings = load_launcher_settings()
        settings["accent_color"] = color
        save_launcher_settings(settings)
        set_accent(color)
        apply_dark_mode(settings.get("dark_mode", "system"))
        if apply_accent_to_icon(color):
            new_icon = QIcon(asset_path("icon.png"))
            QApplication.instance().setWindowIcon(new_icon)
            for w in QApplication.topLevelWidgets():
                w.setWindowIcon(new_icon)
            main_win = self.window()
            if hasattr(main_win, "reload_brand_logo"):
                main_win.reload_brand_logo()         # top-left logo
            prepare_shortcut_icon()                  # refreshes launcher.ico used by shortcuts / the taskbar menu

    def on_dark_mode_changed(self):
        mode = self.dark_mode_combo.currentData()
        settings = load_launcher_settings()
        settings["dark_mode"] = mode
        save_launcher_settings(settings)
        apply_dark_mode(mode)

class CreditsPage(QWidget):
    CREDITS = (
        ("jsmm33", "FNaNCZ AE, FNaNCZ 2, and the Launcher itself", "jsmm33.png"),
        ("mr.fancypigeon", "Linux Script for FNaNCZ 1 and 2, Garry's Mod Addon Manager, and ideas", "pigeon.png"),
        ("notacape", "NCZFront", "notacape.png"),
    )

    @staticmethod
    def circular_pixmap(image_path, size):
        src = QPixmap(image_path)
        if src.isNull():
            return None
        scaled = src.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation
        )
        out = QPixmap(size, size)
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        path.addEllipse(0, 0, size, size)
        painter.setClipPath(path)
        painter.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
        painter.end()
        return out

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Content")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 28, 36, 0)
        layout.setSpacing(0)

        title = QLabel("Credits")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        layout.addSpacing(22)

        for name, role, image in self.CREDITS:
            panel = QFrame()
            panel.setObjectName("Panel")
            row = QHBoxLayout(panel)
            row.setContentsMargins(22, 16, 22, 16)
            row.setSpacing(14)

            avatar = QLabel(name[:1].upper())
            avatar.setObjectName("Avatar")
            avatar.setFixedSize(36, 36)
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pic = self.circular_pixmap(asset_path(image), 72)
            if pic:
                pic.setDevicePixelRatio(2.0)
                avatar.setText("")
                avatar.setPixmap(pic)
                avatar.setStyleSheet("background: transparent;")

            text_col = QVBoxLayout()
            text_col.setSpacing(2)
            name_label = QLabel(name)
            name_label.setObjectName("RowTitle")
            role_label = QLabel(role)
            role_label.setObjectName("RowDesc")
            text_col.addWidget(name_label)
            text_col.addWidget(role_label)

            row.addWidget(avatar)
            row.addLayout(text_col, 1)
            layout.addWidget(panel)
            layout.addSpacing(12)
        layout.addStretch()

class TaskWorker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, func):
        super().__init__()
        self.func = func

    def run(self):
        try:
            result = self.func()
        except CloudError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"Something went wrong: {e}")
        else:
            self.done.emit(result)

class BusyDialog(QDialog):
    def __init__(self, message, func, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Please wait")
        self.setFixedSize(300, 90)
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self._finished = False
        self.ok = False
        self.value = None
        self.error = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 15, 20, 15)
        layout.setSpacing(10)
        label = QLabel(message)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        layout.addWidget(bar)

        self.worker = TaskWorker(func)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)

    def _on_done(self, value):
        self.ok = True
        self.value = value
        self._finished = True
        self.accept()

    def _on_failed(self, message):
        self.ok = False
        self.error = message
        self._finished = True
        self.accept()

    def reject(self):
        if self._finished:
            super().reject()

    def closeEvent(self, event):
        if self._finished:
            event.accept()
        else:
            event.ignore()

    def run(self):
        self.worker.start()
        self.exec()
        self.worker.wait()
        return self.ok, self.value, self.error

def apply_avatar(label, image_path, letter, size):
    """Shows a circular picture on the label, or the letter badge when there is no picture."""
    pic = CreditsPage.circular_pixmap(image_path, size * 2) if image_path else None
    if pic:
        pic.setDevicePixelRatio(2.0)
        label.setText("")
        label.setPixmap(pic)
        label.setStyleSheet("background: transparent;")
    else:
        label.clear()
        label.setStyleSheet("")
        label.setText(letter)

class AccountPage(QWidget):
    account_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Content")
        self.signup_mode = False
        self._profile_worker = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 28, 36, 0)
        outer.setSpacing(0)

        page_title = QLabel("Account")
        page_title.setObjectName("PageTitle")
        outer.addWidget(page_title)
        outer.addSpacing(22)

        panel = QFrame()
        panel.setObjectName("Panel")
        panel.setFixedWidth(420)
        self.panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(10)

        self.avatar_big = QLabel("?")
        self.avatar_big.setObjectName("AvatarLarge")
        self.avatar_big.setFixedSize(80, 80)
        self.avatar_big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.avatar_big, 0, Qt.AlignmentFlag.AlignHCenter)

        self.title_label = QLabel()
        title_font = self.title_label.font()
        title_font.setPointSize(12)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)

        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.info_label)

        self.form_widget = QWidget()
        form = QFormLayout(self.form_widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("you@example.com")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_label = QLabel("Confirm")
        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.username_label = QLabel("Username")
        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("3-20 letters, numbers, _ . -")
        self.username_edit.setMaxLength(20)
        form.addRow(QLabel("Email"), self.email_edit)
        form.addRow(self.username_label, self.username_edit)
        form.addRow(QLabel("Password"), self.password_edit)
        form.addRow(self.confirm_label, self.confirm_edit)
        layout.addWidget(self.form_widget)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #d9534f;")
        layout.addWidget(self.status_label)

        self.primary_btn = QPushButton()
        self.primary_btn.setObjectName("Primary")
        self.primary_btn.setFixedHeight(38)
        self.primary_btn.clicked.connect(self.submit)
        layout.addWidget(self.primary_btn)

        self.toggle_btn = QPushButton()
        self.toggle_btn.setFlat(True)
        self.toggle_btn.clicked.connect(self.toggle_mode)
        layout.addWidget(self.toggle_btn)

        self.forgot_btn = QPushButton("Forgot password?")
        self.forgot_btn.setFlat(True)
        self.forgot_btn.clicked.connect(self.forgot_password)
        layout.addWidget(self.forgot_btn)

        self.profile_actions = QWidget()
        pa = QHBoxLayout(self.profile_actions)
        pa.setContentsMargins(0, 0, 0, 0)
        pa.setSpacing(8)
        self.photo_btn = QPushButton("Change Picture")
        self.photo_btn.setFixedHeight(36)
        self.photo_btn.clicked.connect(self.change_photo)
        self.name_btn = QPushButton("Edit Username")
        self.name_btn.setFixedHeight(36)
        self.name_btn.clicked.connect(self.edit_username)
        pa.addWidget(self.photo_btn, 1)
        pa.addWidget(self.name_btn, 1)
        layout.addWidget(self.profile_actions)

        self.remove_photo_btn = QPushButton("Remove Picture")
        self.remove_photo_btn.setFlat(True)
        self.remove_photo_btn.clicked.connect(self.remove_photo)
        layout.addWidget(self.remove_photo_btn)

        self.logout_btn = QPushButton("Log Out")
        self.logout_btn.setFixedHeight(38)
        self.logout_btn.clicked.connect(self.log_out)
        layout.addWidget(self.logout_btn)

        outer.addWidget(panel, 0, Qt.AlignmentFlag.AlignLeft)
        outer.addStretch()

        self.password_edit.returnPressed.connect(self.submit)
        self.confirm_edit.returnPressed.connect(self.submit)
        QApplication.instance().aboutToQuit.connect(self._wait_for_profile_worker)
        self.refresh_view()

    TEXT_WIDTH = 372  # panel width minus its padding

    def set_status(self, text):
        self.status_label.setText(text)
        self.status_label.setMinimumHeight(self.status_label.heightForWidth(self.TEXT_WIDTH) if text else 0)

    def showEvent(self, event):
        super().showEvent(event)
        # The session may have expired since this page was last shown.
        self.refresh_view()
        self.account_changed.emit()
        self.sync_profile_async()

    def refresh_view(self):
        self.set_status("")
        acct = load_account() if cloud_configured() else None
        configured = cloud_configured()
        logged_in = acct is not None

        show_form = configured and not logged_in
        for w in (self.form_widget, self.primary_btn, self.toggle_btn):
            w.setVisible(show_form)
        self.forgot_btn.setVisible(show_form and not self.signup_mode)
        self.confirm_label.setVisible(show_form and self.signup_mode)
        self.confirm_edit.setVisible(show_form and self.signup_mode)
        self.logout_btn.setVisible(logged_in)
        self.status_label.setVisible(show_form)

        username, avatar = load_profile_cache() if logged_in else ("", None)
        self.username_label.setVisible(show_form and self.signup_mode)
        self.username_edit.setVisible(show_form and self.signup_mode)
        self.avatar_big.setVisible(logged_in)
        self.profile_actions.setVisible(logged_in)
        self.remove_photo_btn.setVisible(logged_in and bool(avatar))
        self.name_btn.setText("Edit Username" if username else "Set Username")

        if not configured:
            self.title_label.setText("Cloud Sync")
            self.info_label.setText("Cloud sync isn't set up in this build of the launcher yet.")
        elif logged_in:
            self.title_label.setText(username or "Signed in")
            shown_name = username or acct.get("email", "") or "?"
            apply_avatar(self.avatar_big, avatar, shown_name[:1].upper(), 80)
            self.info_label.setText(
                f"{acct.get('email', 'your account')}\n\n"
                "Use Save Data and Sync Data in each game's ••• menu to move your progress between devices.")
        elif self.signup_mode:
            self.title_label.setText("Create Account")
            self.info_label.setText("Create an account to sync your saves and settings across devices.")
            self.primary_btn.setText("Create Account")
            self.toggle_btn.setText("Already have an account? Log in")
        else:
            self.title_label.setText("Log In")
            self.info_label.setText("Log in to sync your saves and settings across devices.")
            self.primary_btn.setText("Log In")
            self.toggle_btn.setText("Need an account? Create one")
        self.info_label.setMinimumHeight(self.info_label.heightForWidth(self.TEXT_WIDTH))
        self.panel.layout().activate()

    def toggle_mode(self):
        self.signup_mode = not self.signup_mode
        self.refresh_view()

    def submit(self):
        email = self.email_edit.text().strip()
        password = self.password_edit.text()
        if not email or "@" not in email:
            self.set_status("Enter a valid email address.")
            return
        if len(password) < 6:
            self.set_status("Password must be at least 6 characters.")
            return
        if self.signup_mode and password != self.confirm_edit.text():
            self.set_status("Passwords don't match.")
            return
        username = self.username_edit.text().strip()
        if self.signup_mode:
            name_error = validate_username(username)
            if name_error:
                self.set_status(name_error)
                return

        create = self.signup_mode

        def sign_in_and_sync():
            cloud_sign_in(email, password, create=create)
            try:
                if create:
                    cloud_save_profile(username=username)
                else:
                    cloud_pull_profile()
            except CloudError as e:
                return str(e)
            return ""

        busy = BusyDialog("Creating your account..." if create else "Logging in...",
                          sign_in_and_sync, self)
        ok, profile_warning, error = busy.run()
        if ok:
            self.password_edit.clear()
            self.confirm_edit.clear()
            self.username_edit.clear()
            self.signup_mode = False
            self.refresh_view()
            self.account_changed.emit()
            if profile_warning:
                QMessageBox.warning(self, "Profile",
                                    f"You're signed in, but your profile couldn't be synced:\n\n{profile_warning}")
        else:
            self.set_status(error)

    def forgot_password(self):
        email = self.email_edit.text().strip()
        if not email or "@" not in email:
            self.set_status("Type your email above first, then click Forgot password.")
            return
        busy = BusyDialog("Sending reset email...", lambda: cloud_send_password_reset(email), self)
        ok, _value, error = busy.run()
        if ok:
            QMessageBox.information(self, "Password Reset",
                                    "If an account exists for that email, a reset link is on its way.")
        else:
            self.set_status(error)

    def _profile_updated(self, ok, error, what):
        if ok:
            self.refresh_view()
            self.account_changed.emit()
        else:
            QMessageBox.warning(self, "Profile", error or f"Couldn't update your {what}.")

    def change_photo(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose a profile picture", os.path.expanduser("~"),
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)")
        if not path:
            return
        try:
            png = prepare_avatar_png(path)
        except CloudError as e:
            QMessageBox.warning(self, "Profile", str(e))
            return
        busy = BusyDialog("Uploading your picture...", lambda: cloud_save_profile(png=png), self)
        ok, _value, error = busy.run()
        self._profile_updated(ok, error, "picture")

    def remove_photo(self):
        busy = BusyDialog("Removing your picture...", lambda: cloud_save_profile(remove_photo=True), self)
        ok, _value, error = busy.run()
        self._profile_updated(ok, error, "picture")

    def edit_username(self):
        current, _avatar = load_profile_cache()
        text = current
        while True:
            text, accepted = QInputDialog.getText(
                self, "Username", "Choose a username (3-20 letters, numbers, _ . or -):",
                QLineEdit.EchoMode.Normal, text)
            if not accepted:
                return
            text = text.strip()
            name_error = validate_username(text)
            if not name_error:
                break
            QMessageBox.warning(self, "Username", name_error)
        if text == current:
            return
        busy = BusyDialog("Saving your username...", lambda: cloud_save_profile(username=text), self)
        ok, _value, error = busy.run()
        self._profile_updated(ok, error, "username")

    def sync_profile_async(self):
        """Quietly refreshes the username/picture from the cloud (e.g. after changing them on another device)."""
        if not cloud_configured() or load_account() is None:
            return
        if self._profile_worker is not None and self._profile_worker.isRunning():
            return
        worker = TaskWorker(cloud_pull_profile)
        worker.done.connect(self._on_profile_synced)
        worker.failed.connect(self._on_profile_synced)
        self._profile_worker = worker
        worker.start()

    def _on_profile_synced(self, _result=None):
        self.refresh_view()
        self.account_changed.emit()

    def _wait_for_profile_worker(self):
        worker = self._profile_worker
        if worker is not None and worker.isRunning():
            worker.wait(5000)

    def log_out(self):
        clear_account()
        self.signup_mode = False
        self.refresh_view()
        self.account_changed.emit()

class DownloadDialog(QDialog):
    def __init__(self, version_url, game_name, linux_cmd, parent=None, existing_zip_path=None, wine_extract_dir=None, download_only=False):
        super().__init__(parent)
        self.wine_mode = bool(wine_extract_dir)
        self.zip_path = None
        self.setWindowTitle("Downloading Game")
        self.setFixedSize(420, 160)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 20, 20, 20)

        self.status_label = QLabel("Connecting...")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)

        self.info_label = QLabel("Speed: 0 KB/s | Downloaded: 0 MB")
        layout.addWidget(self.info_label)

        self.worker = DownloadWorker(version_url, game_name, linux_cmd, existing_zip_path, wine_extract_dir, download_only)
        self.worker.progress.connect(self.on_progress)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_status_update(self, message):
        self.status_label.setText(message)
        self.progress_bar.setRange(0, 0)
        self.info_label.setText("Please wait..." if self.wine_mode else "Please check terminal / wait...")

    def on_progress(self, downloaded, total, speed):
        dl_mb = downloaded / (1024 * 1024)
        speed_str = f"{speed / 1024:.1f} KB/s" if speed < 1024 * 1024 else f"{speed / (1024 * 1024):.2f} MB/s"

        if total > 0:
            percent = int((downloaded / total) * 100)
            self.progress_bar.setValue(percent)
            total_mb = total / (1024 * 1024)
            self.status_label.setText(f"Downloading... {percent}%")
            self.info_label.setText(f"Speed: {speed_str} | Progress: {_fmt_size(downloaded)} / {_fmt_size(total)}")
        else:
            self.progress_bar.setRange(0, 0)
            self.status_label.setText("Downloading...")
            self.info_label.setText(f"Speed: {speed_str} | Downloaded: {_fmt_size(downloaded)}")

    def on_finished(self, zip_path):
        self.zip_path = zip_path
        self.accept()

    def on_failed(self, error_msg):
        self.status_label.setText("Download failed!")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet("QProgressBar::chunk { background-color: red; }")
        self.info_label.setText("Error details copied to clipboard.")
        QApplication.clipboard().setText(error_msg)

# ---------------------------------------------------------------- store (Steam games + SteamGridDB art)
SGDB_API = "https://www.steamgriddb.com/api/v2"
STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"
STORE_PAGE_SIZE = 50
STORE_COVER_W, STORE_COVER_H = 150, 225
STORE_CARD_W = STORE_COVER_W + 18
STORE_GRID_GAP = 18
NO_ART_RETRY_SECONDS = 14 * 24 * 3600

def get_steamgriddb_key():
    return load_launcher_settings().get("steamgriddb_api_key", "").strip()

def get_store_cache_dir():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "store_cache", "covers")

def _http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

_ROW_RE = re.compile(r'<a\b[^>]*?\bdata-ds-appid="(\d+)"[^>]*>(.*?)</a>', re.S)
_TITLE_RE = re.compile(r'<span class="title">(.*?)</span>', re.S)
_IMG_RE = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"', re.S)

def parse_steam_results(results_html):
    """Turns the store search HTML into [(appid, title, image_base_url)]."""
    games, seen = [], set()
    for appid, body in _ROW_RE.findall(results_html or ""):
        title_match = _TITLE_RE.search(body)
        if not title_match or appid in seen:
            continue
        seen.add(appid)
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
        img = _IMG_RE.search(body)
        base = html_lib.unescape(img.group(1)).split("?")[0].rsplit("/", 1)[0] if img else ""
        games.append((int(appid), title, base))
    return games

def fetch_steam_games(term, start, count=STORE_PAGE_SIZE, tags=None, sort_by="_ASC"):
    """One page of Steam's own game list (games only, no DLC/software). Returns (games, total_count).
    tags: optional list of Steam tag ids, a game must have all of them. sort_by: "_ASC" (relevance) or "Reviews_DESC"."""
    params = {"query": "", "term": term, "start": start, "count": count, "dynamic_data": "",
              "sort_by": sort_by, "category1": 998, "l": "english", "infinite": 1}
    if tags:
        params["tags"] = ",".join(str(int(t)) for t in tags)
    url = STEAM_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    data = json.loads(_http_get(url).decode("utf-8", errors="replace"))
    return parse_steam_results(data.get("results_html", "")), int(data.get("total_count") or 0)

# ---- Store tags: the tag list, a game's own tags, and "more like this"
# Used if Steam's tag list can't be downloaded (offline). Name -> tag id.
_TAG_FALLBACK = (
    ("Action", 19), ("Adventure", 21), ("RPG", 122), ("Strategy", 9), ("Simulation", 599), ("Indie", 492),
    ("Casual", 597), ("Puzzle", 1664), ("Horror", 1667), ("Multiplayer", 3859), ("Co-op", 1685),
    ("Singleplayer", 4182), ("FPS", 1663), ("Shooter", 1774), ("Open World", 1695), ("Roguelike", 1716),
    ("Survival", 1662), ("Platformer", 1625), ("Sandbox", 3810), ("Racing", 699), ("Sports", 701),
    ("Story Rich", 1742), ("Atmospheric", 4166), ("Pixel Graphics", 3964), ("Souls-like", 29482),
    ("Metroidvania", 1628), ("Turn-Based", 1677), ("Fantasy", 1684), ("Sci-fi", 3942), ("Anime", 4085),
    ("Free to Play", 113), ("Early Access", 493), ("Difficult", 4026), ("Funny", 4136),
)
# Tags nearly every game has: skipped when picking what "more like this" should match on.
_GENERIC_TAGS = {"singleplayer", "multiplayer", "indie", "action", "adventure", "casual", "early access",
                 "free to play", "great soundtrack", "atmospheric", "2d", "3d", "colorful", "cute", "funny",
                 "controller", "violent", "gore", "nudity", "sexual content", "mature", "co-op",
                 "online co-op", "local co-op"}
_tags_cache = []
_STORE_WORKERS = set()      # keeps short-lived background workers alive until they finish

def _keep_worker(worker):
    _STORE_WORKERS.add(worker)
    worker.finished.connect(lambda w=worker: _STORE_WORKERS.discard(w))

def get_steam_tags():
    """[(name, tag_id)] sorted by name. Steam's list is cached for a week; a built-in list is the offline fallback."""
    global _tags_cache
    if _tags_cache:
        return _tags_cache
    path = os.path.join(os.path.dirname(get_launcher_settings_path()), "store_cache", "tags.json")
    tags = []
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 86400:
            with open(path, encoding="utf-8") as f:
                tags = [(str(n), int(i)) for n, i in json.load(f)]
    except Exception:
        tags = []
    if not tags:
        try:
            raw = json.loads(_http_get("https://store.steampowered.com/tagdata/populartags/english",
                                       timeout=10).decode("utf-8", errors="replace"))
            tags = [(html_lib.unescape(str(t["name"])).strip(), int(t["tagid"]))
                    for t in raw if t.get("name") and t.get("tagid")]
            if tags:
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(tags, f)
                except OSError:
                    pass
        except Exception:
            tags = []
    if not tags:
        return sorted(_TAG_FALLBACK, key=lambda t: t[0].lower())     # not cached, so we retry next time
    tags.sort(key=lambda t: t[0].lower())
    _tags_cache = tags
    return tags

_APP_TAG_JSON_RE = re.compile(r"InitAppTagModal\(\s*\d+\s*,\s*(\[.*?\])\s*,", re.S)
_APP_TAG_LINK_RE = re.compile(r'<a\b[^>]*class="app_tag"[^>]*>(.*?)</a>', re.S)

def fetch_app_tags(appid, limit=12):
    """A game's Steam user tags, most voted first, as [(tag_id, name)]."""
    headers = {"Accept-Language": "en-US,en;q=0.9",
               "Cookie": "birthtime=568022401; lastagecheckage=1-0-1988; wants_mature_content=1"}
    page = _http_get(f"https://store.steampowered.com/app/{appid}/?l=english", headers=headers,
                     timeout=15).decode("utf-8", errors="replace")
    tags = []
    match = _APP_TAG_JSON_RE.search(page)
    if match:
        try:
            for t in json.loads(match.group(1)):
                if t.get("tagid") and t.get("name") and t.get("browseable", True):
                    tags.append((int(t["tagid"]), html_lib.unescape(str(t["name"])).strip()))
        except (ValueError, TypeError):
            tags = []
    if not tags:    # older page layout: only the names are in the markup, so look the ids up by name
        by_name = {name.lower(): tid for name, tid in get_steam_tags()}
        for raw in _APP_TAG_LINK_RE.findall(page):
            name = html_lib.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
            if name.lower() in by_name and by_name[name.lower()] not in [t for t, _ in tags]:
                tags.append((by_name[name.lower()], name))
    return tags[:limit]

def fetch_similar_games(appid, tags, count=24):
    """Games sharing this game's most distinctive tags, best reviewed first. Tries 3 tags, then 2, then 1."""
    specific = [t for t, name in tags if name.lower() not in _GENERIC_TAGS]
    generic = [t for t, name in tags if name.lower() in _GENERIC_TAGS]
    ordered = specific + generic
    best, tried = [], []
    for chosen in (ordered[:3], ordered[:2], ordered[:1]):
        if not chosen or chosen in tried:
            continue
        tried.append(chosen)
        games, _ = fetch_steam_games("", 0, count + 1, tags=chosen, sort_by="Reviews_DESC")
        games = [g for g in games if g[0] != appid][:count]
        if len(games) > len(best):
            best = games
        if len(games) >= 12:
            break
    return best

SGDB_PORTRAIT_SIZES = "600x900,342x482,660x930"

def _sgdb_get_json(path, key):
    """GET a SteamGridDB endpoint; None when it answers 404 (nothing there)."""
    try:
        body = _http_get(SGDB_API + path, headers={"Authorization": f"Bearer {key}"})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return json.loads(body.decode("utf-8", errors="replace"))

def _first_grid_image(payload):
    for item in (payload or {}).get("data") or []:
        if isinstance(item, dict):
            link = item.get("thumb") or item.get("url")
            if link:
                return _http_get(link)
    return None

def fetch_sgdb_cover(appid, key):
    """Best-scored static portrait community cover for a Steam app id, as image bytes (None if there isn't one)."""
    return _first_grid_image(_sgdb_get_json(
        f"/grids/steam/{appid}?dimensions={SGDB_PORTRAIT_SIZES}&types=static", key))

def _norm_title(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())

def fetch_sgdb_cover_by_name(title, key):
    """Last resort: find the game on SteamGridDB by exact (normalised) name, then take its best portrait cover."""
    found = _sgdb_get_json("/search/autocomplete/" + urllib.parse.quote(title, safe=""), key)
    wanted = _norm_title(title)
    for game in (found or {}).get("data") or []:
        if isinstance(game, dict) and game.get("id") and _norm_title(game.get("name")) == wanted:
            return _first_grid_image(_sgdb_get_json(
                f"/grids/game/{game['id']}?dimensions={SGDB_PORTRAIT_SIZES}&types=static", key))
    return None

def steam_cover_urls(appid, img_base):
    urls = []
    if img_base:
        urls += [img_base + "/library_600x900.jpg", img_base + "/header.jpg"]
    urls += [f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg",
             f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"]
    return urls

def check_steamgriddb_key(key):
    """'ok' or 'invalid'. Raises on network problems."""
    try:
        _http_get(f"{SGDB_API}/games/steam/220", headers={"Authorization": f"Bearer {key}"}, timeout=10)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "invalid"
        if e.code == 404:
            return "ok"  # authenticated, that game just isn't there
        raise
    return "ok"

def fetch_steam_description(appid):
    """Short description for a Steam app (empty string if unavailable)."""
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english"
    data = json.loads(_http_get(url).decode("utf-8", errors="replace"))
    entry = data.get(str(appid)) or {}
    info = entry.get("data") or {}
    text = info.get("short_description") or info.get("about_the_game") or ""
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", text)).strip()

# Steam only sells in a few currencies (no EGP), so prices are fetched in USD and converted.
CURRENCIES = {"EGP": "Egyptian Pound (EGP)", "QAR": "Qatari Riyal (QAR)",
              "USD": "US Dollar (USD)", "SAR": "Saudi Riyal (SAR)",
              "AED": "Emirati Dirham (AED)", "EUR": "Euro (EUR)"}
FALLBACK_RATES = {"USD": 1.0, "EGP": 48.0, "QAR": 3.64, "SAR": 3.75, "AED": 3.6725, "EUR": 0.92}
_rates_cache = {"time": 0, "rates": dict(FALLBACK_RATES)}

def get_usd_rates():
    """Live USD exchange rates (cached for an hour); built-in approximate rates if offline."""
    if time.time() - _rates_cache["time"] < 3600:
        return _rates_cache["rates"]
    try:
        data = json.loads(_http_get("https://open.er-api.com/v6/latest/USD", timeout=8).decode("utf-8"))
        live = data.get("rates") or {}
        rates = {c: float(live.get(c) or FALLBACK_RATES[c]) for c in CURRENCIES}
        _rates_cache.update(time=time.time(), rates=rates)
    except Exception:
        pass
    return _rates_cache["rates"]

CURRENCY_NAMES = {   # code -> (singular, plural)
    "EGP": ("Egyptian Pound", "Egyptian Pounds"), "QAR": ("Qatari Riyal", "Qatari Riyals"),
    "USD": ("US Dollar", "US Dollars"), "SAR": ("Saudi Riyal", "Saudi Riyals"),
    "AED": ("Emirati Dirham", "Emirati Dirhams"), "EUR": ("Euro", "Euros"),
}

def format_price(usd, currency):
    """'250 Qatari Riyals' - the exact converted amount with the full currency name."""
    amount = usd if currency == "USD" else usd * get_usd_rates().get(currency, FALLBACK_RATES[currency])
    text = f"{amount:,.0f}" if currency == "EGP" else f"{amount:,.2f}"
    if "." in text and text.endswith(".00"):
        text = text[:-3]
    singular, plural = CURRENCY_NAMES.get(currency, (currency, currency))
    return f"{text} {singular if text == '1' else plural}"

FLAG_WIKI_FILES = {   # currency -> Wikipedia / Wikimedia Commons flag file
    "EGP": "Flag_of_Egypt.svg", "QAR": "Flag_of_Qatar.svg", "USD": "Flag_of_the_United_States.svg",
    "SAR": "Flag_of_Saudi_Arabia.svg", "AED": "Flag_of_the_United_Arab_Emirates.svg", "EUR": "Flag_of_Europe.svg",
}

def _flag_cache_path(code):
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "flags", f"{code}.png")

def get_flag_file(code):
    """Official flag from Wikimedia Commons (rendered as a PNG), downloaded once and cached. '' if unavailable."""
    name = FLAG_WIKI_FILES.get(code)
    if not name:
        return ""
    path = _flag_cache_path(code)
    if os.path.exists(path) and os.path.getsize(path) > 200:
        return path
    try:
        raw = _http_get(f"https://commons.wikimedia.org/wiki/Special:FilePath/{name}?width=240",
                        headers={"User-Agent": "NCZ-Games-Launcher/1.0 (flag icons)"}, timeout=10)
        if not QImage.fromData(raw).isNull():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(raw)
            return path
    except Exception:
        pass
    return ""

def round_flag_pixmap(path, height=22):
    """Whole flag (3:2) with rounded corners, crisp on HiDPI."""
    src = QPixmap(path)
    if src.isNull():
        return None
    dpr = 2
    h = height * dpr
    w = int(round(h * 1.5))
    scaled = src.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    scaled = scaled.copy((scaled.width() - w) // 2, (scaled.height() - h) // 2, w, h)
    out = QPixmap(w, h)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    radius = 5 * dpr
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), radius, radius)
    p.setClipPath(clip)
    p.drawPixmap(0, 0, scaled)
    p.setClipping(False)
    p.setPen(QPen(QColor(0, 0, 0, 60), 1.5))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(0.75, 0.75, w - 1.5, h - 1.5), radius, radius)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out

def fetch_steam_details(appid, currency="EGP"):
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english&cc=us"
    data = json.loads(_http_get(url).decode("utf-8", errors="replace"))
    info = (data.get(str(appid)) or {}).get("data") or {}
    text = info.get("short_description") or info.get("about_the_game") or ""
    if info.get("is_free"):
        price = "Free to Play"
    else:
        cents = (info.get("price_overview") or {}).get("final")
        price = format_price(cents / 100, currency) if cents else ""
    shots = []
    for shot in (info.get("screenshots") or [])[:5]:
        try:
            raw = _http_get(shot.get("path_thumbnail"), timeout=10)
            img = QImage()
            if img.loadFromData(raw):
                scaled = img.scaledToHeight(100, Qt.TransformationMode.SmoothTransformation)
                buf = QBuffer()
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                scaled.save(buf, "JPG", 50)
                shots.append(bytes(buf.data()))
            else:
                shots.append(raw)
        except Exception:
            continue
    platforms = info.get("platforms") or {}
    reqs_src = info.get("pc_requirements")
    if sys.platform.startswith("linux") and platforms.get("linux"):
        lin = info.get("linux_requirements")
        if isinstance(lin, dict) and (lin.get("minimum") or lin.get("recommended")):
            reqs_src = lin
    reqs = {}
    if isinstance(reqs_src, dict):
        reqs = {"minimum": parse_steam_requirements(reqs_src.get("minimum")),
                "recommended": parse_steam_requirements(reqs_src.get("recommended"))}
    return dict(
        reqs=reqs,
        platforms=platforms,
        description=html_lib.unescape(re.sub(r"<[^>]+>", " ", text)).strip(),
        developers=", ".join(info.get("developers") or []),
        release=(info.get("release_date") or {}).get("date", ""),
        genres=[g.get("description", "") for g in (info.get("genres") or [])][:5],
        price=price,
        price_flag=(get_flag_file(currency) if price and not info.get("is_free") else ""),
        shots=shots,
    )

# ---------------------------------------------------------------- system requirements + "can I run it"
def _req_clean(html):
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()

def parse_steam_requirements(html):
    """Steam's requirements HTML -> [(label, value)] e.g. [('OS', 'Windows 10'), ('Memory', '8 GB RAM')]."""
    if not html or not isinstance(html, str):
        return []
    items = []
    for li in re.findall(r"<li>(.*?)</li>", html, re.S | re.I):
        m = re.match(r"\s*<strong>(.*?)</strong>\s*(.*)", li, re.S | re.I)
        if m:
            label = _req_clean(m.group(1)).rstrip(":").replace("*", "").strip()
            value = _req_clean(m.group(2))
        else:
            label, value = "", _req_clean(li)
        if label or value:
            items.append((label, value))
    if items:
        return items
    # no list: split the free text into lines, dropping the "Minimum:" / "Recommended:" heading
    text = re.sub(r"<br\s*/?>|</li>|</p>|</ul>", "\n", html, flags=re.I)
    for line in text.split("\n"):
        line = _req_clean(line)
        if line and line.lower().rstrip(":") not in ("minimum", "recommended"):
            items.append(("", line))
    return items

_sys_specs_cache = {}

def _run_quiet(cmd, timeout=6):
    kw = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return out.stdout.strip()
    except Exception:
        return ""

def get_system_specs():
    """This PC's CPU, graphics card(s), RAM and free disk space (best effort, cached). Safe to call in a thread."""
    if _sys_specs_cache:
        return _sys_specs_cache
    cpu, gpu, ram_gb = "", "", 0.0
    try:
        import psutil
        ram_gb = psutil.virtual_memory().total / 1024 ** 3
    except Exception:
        pass
    try:
        if sys.platform.startswith("win"):
            if not ram_gb:
                import ctypes

                class _MEM(ctypes.Structure):
                    _fields_ = [("l", ctypes.c_ulong), ("p", ctypes.c_ulong), ("tp", ctypes.c_ulonglong),
                                ("ap", ctypes.c_ulonglong), ("tf", ctypes.c_ulonglong), ("af", ctypes.c_ulonglong),
                                ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
                m = _MEM()
                m.l = ctypes.sizeof(_MEM)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
                ram_gb = m.tp / 1024 ** 3
            out = _run_quiet(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name; "
                              "(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) -join ' | '"])
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            cpu = lines[0] if lines else ""
            gpu = lines[1] if len(lines) > 1 else ""
        elif sys.platform.startswith("linux"):
            try:
                with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.lower().startswith("model name"):
                            cpu = line.split(":", 1)[1].strip()
                            break
            except OSError:
                pass
            if not ram_gb:
                try:
                    with open("/proc/meminfo", encoding="utf-8") as f:
                        for line in f:
                            if line.startswith("MemTotal:"):
                                ram_gb = int(line.split()[1]) / 1024 ** 2
                                break
                except OSError:
                    pass
            gpus = []
            for line in _run_quiet(["lspci"]).splitlines():
                if re.search(r"VGA compatible|3D controller|Display controller", line, re.I):
                    name = re.sub(r"\s*\(rev [0-9a-f]+\)$", "", line.split(": ", 1)[-1])
                    inner = [b for b in re.findall(r"\[([^\]]+)\]", name) if b != "AMD/ATI"]
                    if inner:       # "NVIDIA Corporation GA107M [GeForce RTX 3050 Ti Mobile]" -> the marketing name
                        name = inner[-1]
                    else:
                        name = re.sub(r"\b(Corporation|Inc\.?|Advanced Micro Devices,?)\s*", "", name).strip()
                    if name not in gpus:
                        gpus.append(name)
            gpu = " | ".join(gpus)
    except Exception:
        pass
    probe = GAMES_DIR
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        free_gb = shutil.disk_usage(probe or os.path.expanduser("~")).free / 1024 ** 3
    except Exception:
        free_gb = 0.0
    _sys_specs_cache.update(cpu=re.sub(r"\s+", " ", cpu), gpu=re.sub(r"\s+", " ", gpu), ram_gb=ram_gb, free_gb=free_gb)
    return _sys_specs_cache

def _gb_in(text):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(TB|GB|MB)\b", text or "", re.I)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    return n * 1024 if unit == "TB" else n if unit == "GB" else n / 1024

_REQ_KEYS = {"ram": r"\b(ram|memory)\b",
             "disk": r"\b(storage|hard drive|hard disk|disk space|hdd|drive space|space)\b"}

def _req_gb(items, kind):
    for label, value in items:
        # a line with no label (free text like "512MB RAM, 200 MB hard drive space") is checked piece by piece
        parts = [(label, value)] if label else [("", seg) for seg in re.split(r"[,;/]|\band\b", value)]
        for lab, val in parts:
            probe = (lab or val).lower()
            if re.search(r"video|graphics|vram|gpu|card", probe):
                continue
            if re.search(_REQ_KEYS[kind], probe):
                gb = _gb_in(val) or _gb_in(lab)
                if gb:
                    return gb
    return None

def _fmt_gb(gb):
    return f"{gb:.0f}" if abs(gb - round(gb)) < 0.05 else f"{gb:.1f}"

def assess_requirements(reqs, platforms, system):
    """(verdict, colour, notes) from RAM and free disk space only. Never a hard yes/no."""
    mn, rc = (reqs or {}).get("minimum") or [], (reqs or {}).get("recommended") or []
    ram, free = system.get("ram_gb") or 0, system.get("free_gb") or 0
    ram_min = _req_gb(mn, "ram") or _req_gb(rc, "ram")
    ram_rec = _req_gb(rc, "ram")
    disk = _req_gb(mn, "disk") or _req_gb(rc, "disk")
    meets = lambda have, need: have >= need * 0.92       # RAM is reported a bit under its label (8 GB -> ~7.7)
    problems = []
    if ram and ram_min and not meets(ram, ram_min):
        problems.append(f"your {_fmt_gb(ram)} GB of RAM is under the {_fmt_gb(ram_min)} GB minimum")
    if free and disk and free < disk:
        problems.append(f"it needs {_fmt_gb(disk)} GB of free space and you have {_fmt_gb(free)} GB")
    if problems:
        verdict, color = "May struggle: " + "; ".join(problems), RED
    elif ram and ram_rec and not meets(ram, ram_rec):
        verdict, color = "Should run: meets the minimum RAM, below the recommended", ORANGE
    elif ram_min or disk:
        verdict, color = "Looks OK for RAM and storage", GREEN
    else:
        verdict, color = "Couldn't check: no RAM or storage numbers in the listed requirements", None
    notes = ["Only RAM and free disk space are checked. Compare the processor and graphics card yourself."]
    if sys.platform.startswith("linux") and platforms and not platforms.get("linux"):
        notes.insert(0, "This is a Windows game, so on Linux it needs Wine or Proton.")
    return verdict, color, notes

def _reqs_html(title, items):
    if not items:
        return ""
    rows = [f"<b>{html_lib.escape(l)}:</b> {html_lib.escape(v)}" if l else html_lib.escape(v) for l, v in items]
    return f"<p style='margin:0 0 6px 0; font-size:14px;'><b>{title}</b></p>" + "<br>".join(rows)


def rounded_cover_pixmap(path, width, height, radius):
    src = QPixmap(path)
    if src.isNull():
        return None
    scale = 2
    w, h = width * scale, height * scale
    scaled = src.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(w, h)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, w, h), radius * scale, radius * scale)
    painter.setClipPath(clip)
    painter.drawPixmap((w - scaled.width()) // 2, (h - scaled.height()) // 2, scaled)
    painter.end()
    out.setDevicePixelRatio(scale)
    return out

class CoverLoader(QObject):
    """Downloads/caches store covers on a few background threads. SteamGridDB first, Steam's own art as fallback."""
    loaded = pyqtSignal(int, str)   # appid, cached image path ("" if nothing was found)
    auth_failed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._futures = {}
        self._lock = threading.Lock()
        self._key = ""
        self._rejected = False

    def set_key(self, key):
        key = (key or "").strip()
        if key != self._key:
            self._key = key
            self._rejected = False

    def request(self, appid, img_base, title=""):
        with self._lock:
            if appid in self._futures:
                return
            self._futures[appid] = self._pool.submit(self._work, appid, img_base, self._key, title)

    def cancel_pending(self):
        with self._lock:
            for appid, future in list(self._futures.items()):
                if future.cancel():
                    del self._futures[appid]

    def shutdown(self):
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._pool.shutdown(wait=False)

    def _work(self, appid, img_base, key, title):
        try:
            path = self._fetch(appid, img_base, key, title)
        except Exception:
            path = ""
        with self._lock:
            self._futures.pop(appid, None)
        try:
            self.loaded.emit(appid, path)
        except RuntimeError:
            pass  # window already closed

    def _reject_key(self):
        with self._lock:
            first = not self._rejected
            self._rejected = True
        if first:
            try:
                self.auth_failed.emit()
            except RuntimeError:
                pass

    def _try_sgdb(self, lookup, marker):
        """Runs a SteamGridDB lookup. Leaves a marker when SteamGridDB has nothing, so we don't keep asking."""
        try:
            data = lookup()
            if not data:
                open(marker, "w").close()
            return data
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._reject_key()
        except Exception:
            pass  # rate limit / network hiccup: try again next time
        return None

    def _fetch(self, appid, img_base, key, title):
        folder = get_store_cache_dir()
        os.makedirs(folder, exist_ok=True)
        sgdb_path = os.path.join(folder, f"{appid}-sgdb.img")
        steam_path = os.path.join(folder, f"{appid}-steam.img")
        id_marker = os.path.join(folder, f"{appid}-nosgdb2")      # nothing on SteamGridDB for this Steam id
        name_marker = os.path.join(folder, f"{appid}-nosearch")   # nothing found by searching its name either
        if os.path.exists(sgdb_path):
            return sgdb_path

        def fresh(marker):
            return os.path.exists(marker) and time.time() - os.path.getmtime(marker) < NO_ART_RETRY_SECONDS

        def can_use_sgdb():
            return bool(key) and not self._rejected

        # 1) community cover for this exact Steam game
        if can_use_sgdb() and not fresh(id_marker):
            data = self._try_sgdb(lambda: fetch_sgdb_cover(appid, key), id_marker)
            if data:
                with open(sgdb_path, "wb") as f:
                    f.write(data)
                return sgdb_path

        # 2) Steam's own art
        if os.path.exists(steam_path):
            return steam_path
        for url in steam_cover_urls(appid, img_base):
            try:
                data = _http_get(url, timeout=12)
            except Exception:
                continue
            if data:
                with open(steam_path, "wb") as f:
                    f.write(data)
                return steam_path

        # 3) still nothing: look the game up on SteamGridDB by name and use its community cover
        if can_use_sgdb() and title and not fresh(name_marker):
            data = self._try_sgdb(lambda: fetch_sgdb_cover_by_name(title, key), name_marker)
            if data:
                with open(sgdb_path, "wb") as f:
                    f.write(data)
                return sgdb_path
        return ""

def get_steam_library_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "steam_library.json")

def get_library_cover_dir():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "library_covers")

def load_steam_library():
    """Store games the user added: [{'appid': int, 'title': str, 'cover': path}]."""
    try:
        with open(get_steam_library_path(), encoding="utf-8") as f:
            data = json.load(f)
        return [g for g in data if isinstance(g, dict) and isinstance(g.get("appid"), int) and g.get("title")]
    except Exception:
        return []

def save_steam_library(games):
    path = get_steam_library_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(games, f, indent=4)

class StoreCard(QFrame):
    clicked = pyqtSignal(int, str, str)  # appid, title, cover path

    def __init__(self, appid, title, parent=None):
        super().__init__(parent)
        self.appid = appid
        self.title = title
        self.cover_path = ""
        self.setObjectName("GameCard")
        self.setFixedWidth(STORE_CARD_W)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{title}\nClick for details")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 10)
        layout.setSpacing(0)

        self.cover = QLabel("...")
        self.cover.setObjectName("CoverPlaceholder")
        self.cover.setFixedSize(STORE_COVER_W, STORE_COVER_H)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.cover)
        layout.addSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setFixedHeight(38)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.title_label)

    def set_cover(self, path):
        self.cover_path = path or ""
        pix = rounded_cover_pixmap(path, STORE_COVER_W, STORE_COVER_H, 8) if path else None
        if pix:
            self.cover.setText("")
            self.cover.setPixmap(pix)
            self.cover.setStyleSheet("background: transparent; border: none;")
        else:
            self.cover.setText("No\ncover")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.appid, self.title, self.cover_path)
        super().mousePressEvent(event)

class FlowLayout(QLayout):
    """Left-to-right layout that wraps onto new lines (used for tag chips)."""
    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self._items = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and line_h > 0:
                x = area.x()
                y += line_h + self._gap
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._gap
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()

TAG_CHIP_STYLE = ("QPushButton { background: rgba(128,128,128,0.22); border: none; border-radius: 12px; "
                  "padding: 4px 14px; font-size: 12px; }"
                  "QPushButton:hover { background: rgba(128,128,128,0.42); }")

def make_tag_chip(text):
    chip = QPushButton(text)
    chip.setCursor(Qt.CursorShape.PointingHandCursor)
    chip.setStyleSheet(TAG_CHIP_STYLE)
    chip.setFixedHeight(26)
    return chip

class TagPickerDialog(QDialog):
    """Pick the Steam tags to filter the Store by. Games have to match all the picked tags."""
    def __init__(self, selected_ids, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filter by tags")
        self.resize(380, 520)
        self._selected = set(selected_ids)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        hint = QLabel("Pick one or more tags. Games have to match all of them.")
        hint.setObjectName("RowDesc")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search tags")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.status = QLabel("Loading tags...")
        self.status.setObjectName("RowDesc")
        layout.addWidget(self.status)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.list.clearSelection)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        apply_btn = QPushButton("Apply")
        apply_btn.setObjectName("Primary")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self.accept)
        row.addWidget(clear_btn)
        row.addStretch()
        row.addWidget(cancel_btn)
        row.addWidget(apply_btn)
        layout.addLayout(row)
        worker = TaskWorker(get_steam_tags)
        worker.done.connect(self._on_tags)
        _keep_worker(worker)
        worker.start()

    def _on_tags(self, tags):
        self.status.hide()
        for name, tag_id in tags:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, tag_id)
            self.list.addItem(item)
            if tag_id in self._selected:
                item.setSelected(True)
        self._filter(self.search.text())

    def _filter(self, text):
        needle = text.strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def result_tags(self):
        return [(item.data(Qt.ItemDataRole.UserRole), item.text()) for item in self.list.selectedItems()]

class StoreDetailPage(QWidget):
    back_requested = pyqtSignal()
    add_requested = pyqtSignal(int, str, str)   # appid, title, cover path
    game_selected = pyqtSignal(int, str, str)   # a "more like this" card was clicked
    tag_requested = pyqtSignal(int, str)        # a tag chip was clicked: tag id, name
    DETAIL_W, DETAIL_H = 300, 450
    CHIP_STYLE = ("background: rgba(128,128,128,0.22); border-radius: 11px; "
                  "padding: 3px 12px; font-size: 12px;")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Content")
        self._appid = 0
        self._title = ""
        self._cover_path = ""
        self._in_library = False
        self._workers = []
        self._history = []              # games we came from when jumping through "more like this"
        self._nav_internal = False
        self._sim_cards = {}
        self.loader = CoverLoader()
        self.loader.loaded.connect(self._on_sim_cover)
        QApplication.instance().aboutToQuit.connect(self.loader.shutdown)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = self.scroll_area = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("GridContainer")
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(36, 28, 36, 28)
        layout.setSpacing(0)

        self.back_btn = QPushButton("< Back to Store")
        self.back_btn.setFlat(True)
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self._on_back)
        layout.addWidget(self.back_btn, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(16)

        row = QHBoxLayout()
        row.setSpacing(28)
        self.cover = QLabel()
        self.cover.setObjectName("CoverPlaceholder")
        self.cover.setFixedSize(self.DETAIL_W, self.DETAIL_H)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(10)
        self.title_label = QLabel()
        self.title_label.setObjectName("PageTitle")
        self.title_label.setWordWrap(True)
        self.meta_label = QLabel()
        self.meta_label.setObjectName("RowDesc")
        self.meta_label.setWordWrap(True)
        self.chips_row = QHBoxLayout()
        self.chips_row.setSpacing(8)
        self.price_label = QLabel()
        self.price_label.setStyleSheet(f"color: {GREEN}; font-size: 20px; font-weight: bold;")
        self.price_flag = QLabel()
        self.price_flag.hide()
        self.desc_label = QLabel()
        self.desc_label.setWordWrap(True)
        self.desc_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.desc_label.setStyleSheet("font-size: 14px;")
        self.steam_btn = QPushButton("Open on Steam")
        self.steam_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.steam_btn.setFixedHeight(38)
        self.steam_btn.clicked.connect(
            lambda: webbrowser.open(f"https://store.steampowered.com/app/{self._appid}/"))
        col.addWidget(self.title_label)
        col.addWidget(self.meta_label)
        col.addLayout(self.chips_row)
        col.addSpacing(4)
        price_row = QHBoxLayout()
        price_row.setSpacing(10)
        price_row.addWidget(self.price_label)
        price_row.addWidget(self.price_flag)
        price_row.addStretch()
        col.addLayout(price_row)
        col.addWidget(self.desc_label)
        col.addSpacing(6)
        self.lib_btn = QPushButton("Add to Library")
        self.lib_btn.setObjectName("Primary")
        self.lib_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lib_btn.setFixedHeight(38)
        self.lib_btn.clicked.connect(self._on_library_clicked)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addWidget(self.lib_btn)
        btn_row.addWidget(self.steam_btn)
        btn_row.addStretch()
        col.addLayout(btn_row)
        col.addStretch()
        row.addLayout(col, 1)
        layout.addLayout(row)
        layout.addSpacing(24)

        # tags: click one to browse the Store by it
        self.tags_wrap = QWidget()
        tags_lay = QVBoxLayout(self.tags_wrap)
        tags_lay.setContentsMargins(0, 0, 0, 24)
        tags_lay.setSpacing(6)
        tags_title = QLabel("Tags")
        tags_title.setStyleSheet("font-size: 16px; font-weight: bold;")
        tags_hint = QLabel("Click a tag to find more games with it")
        tags_hint.setObjectName("RowDesc")
        self.tags_host = QWidget()
        self.tags_flow = FlowLayout(self.tags_host, spacing=8)
        tags_lay.addWidget(tags_title)
        tags_lay.addWidget(tags_hint)
        tags_lay.addSpacing(4)
        tags_lay.addWidget(self.tags_host)
        self.tags_wrap.hide()
        layout.addWidget(self.tags_wrap)

        # system requirements + "can I run it" (only filled in for Steam games)
        self.reqs_wrap = QWidget()
        wrap_lay = QVBoxLayout(self.reqs_wrap)
        wrap_lay.setContentsMargins(0, 0, 0, 24)
        panel = QFrame()
        panel.setObjectName("Panel")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(22, 18, 22, 18)
        pl.setSpacing(10)
        reqs_title = QLabel("System Requirements")
        reqs_title.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.verdict_label = QLabel()
        self.verdict_label.setWordWrap(False)       # a wrapping label gets squashed when it is left-aligned
        self.verdict_label.setMinimumHeight(36)
        self.verdict_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.verdict_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.notes_label = QLabel()
        self.notes_label.setObjectName("RowDesc")
        self.notes_label.setWordWrap(True)
        self.pc_label = QLabel()
        self.pc_label.setObjectName("RowDesc")
        self.pc_label.setWordWrap(True)
        cols = QHBoxLayout()
        cols.setSpacing(28)
        self.min_label = QLabel()
        self.rec_label = QLabel()
        for lab in (self.min_label, self.rec_label):
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.TextFormat.RichText)
            lab.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            cols.addWidget(lab, 1)
        self.notes_label.hide()
        pl.addWidget(reqs_title)
        pl.addWidget(self.verdict_label, 0, Qt.AlignmentFlag.AlignLeft)    # the green / red badge
        pl.addWidget(self.pc_label)
        pl.addLayout(cols)
        wrap_lay.addWidget(panel)
        self.reqs_wrap.hide()
        layout.addWidget(self.reqs_wrap)

        self.shots_title = QLabel("Screenshots")
        self.shots_title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(self.shots_title)
        layout.addSpacing(8)
        self.shots_scroll = QScrollArea()
        self.shots_scroll.setWidgetResizable(True)
        self.shots_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.shots_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.shots_scroll.setFixedHeight(190)
        shots_body = QWidget()
        shots_body.setObjectName("GridContainer")
        self.shots_row = QHBoxLayout(shots_body)
        self.shots_row.setContentsMargins(0, 0, 0, 0)
        self.shots_row.setSpacing(12)
        self.shots_scroll.setWidget(shots_body)
        layout.addWidget(self.shots_scroll)

        # more like this: games sharing this game's tags
        self.similar_wrap = QWidget()
        sim_lay = QVBoxLayout(self.similar_wrap)
        sim_lay.setContentsMargins(0, 24, 0, 0)
        sim_lay.setSpacing(8)
        similar_title = QLabel("More like this")
        similar_title.setStyleSheet("font-size: 16px; font-weight: bold;")
        sim_lay.addWidget(similar_title)
        self.similar_scroll = QScrollArea()
        self.similar_scroll.setWidgetResizable(True)
        self.similar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.similar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.similar_scroll.setFixedHeight(STORE_COVER_H + 90)
        sim_body = QWidget()
        sim_body.setObjectName("GridContainer")
        self.sim_row = QHBoxLayout(sim_body)
        self.sim_row.setContentsMargins(0, 0, 0, 0)
        self.sim_row.setSpacing(STORE_GRID_GAP)
        self.similar_scroll.setWidget(sim_body)
        sim_lay.addWidget(self.similar_scroll)
        self.similar_wrap.hide()
        layout.addWidget(self.similar_wrap)
        layout.addStretch()
        self._set_extras_visible(False)

    def set_in_library(self, in_library):
        self._in_library = in_library
        self.lib_btn.setText("In Library" if in_library else "Add to Library")
        self.lib_btn.setEnabled(not in_library)

    def _on_library_clicked(self):
        if not self._in_library:
            self.add_requested.emit(self._appid, self._title, self._cover_path)

    def _set_extras_visible(self, visible):
        for w in (self.shots_title, self.shots_scroll):
            w.setVisible(visible)

    @staticmethod
    def _clear(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _on_back(self):
        if self._history:
            appid, title, cover = self._history.pop()
            self._nav_internal = True
            self.game_selected.emit(appid, title, cover)
        else:
            self.back_requested.emit()

    def _on_similar_clicked(self, appid, title, cover_path):
        self._history.append((self._appid, self._title, self._cover_path))
        self._nav_internal = True
        self.game_selected.emit(appid, title, cover_path)

    def _on_sim_cover(self, appid, path):
        card = self._sim_cards.get(appid)
        if card is not None:
            card.set_cover(path)

    def _load_related(self, appid):
        def work():
            tags, games = [], []
            try:
                tags = fetch_app_tags(appid)
            except Exception:
                pass
            if tags:
                try:
                    games = fetch_similar_games(appid, tags)
                except Exception:
                    pass
            return (appid, tags, games)

        worker = TaskWorker(work)
        worker.done.connect(self._on_related)
        _keep_worker(worker)
        worker.start()

    def _on_related(self, result):
        appid, tags, games = result
        if appid != self._appid:
            return  # user already opened another game
        for tag_id, name in tags:
            chip = make_tag_chip(name)
            chip.clicked.connect(lambda _c=False, t=tag_id, n=name: self.tag_requested.emit(t, n))
            self.tags_flow.addWidget(chip)
        self.tags_wrap.setVisible(bool(tags))
        for sim_id, sim_title, img_base in games:
            card = StoreCard(sim_id, sim_title)
            card.clicked.connect(self._on_similar_clicked)
            self._sim_cards[sim_id] = card
            self.sim_row.addWidget(card)
            self.loader.request(sim_id, img_base, sim_title)
        self.sim_row.addStretch()
        self.similar_wrap.setVisible(bool(games))

    def _reset_related(self):
        self.loader.cancel_pending()
        self.loader.set_key(get_steamgriddb_key())
        while self.tags_flow.count():
            item = self.tags_flow.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._clear(self.sim_row)
        self._sim_cards = {}
        self.tags_wrap.hide()
        self.similar_wrap.hide()

    def show_game(self, appid, title, cover_path):
        if self._nav_internal:
            self._nav_internal = False      # came from "more like this" or Back: keep the trail
        else:
            self._history.clear()           # opened from the Store or Library: start a new trail
        self.back_btn.setText("< Back" if self._history else "< Back to Store")
        self.scroll_area.verticalScrollBar().setValue(0)
        self._appid = appid
        self._title = title
        self._cover_path = cover_path or ""
        self.title_label.setText(title)
        self.meta_label.setText("")
        self.price_label.setText("")
        self.price_flag.hide()
        self.desc_label.setText("Loading details...")
        self._clear(self.chips_row)
        self._clear(self.shots_row)
        self._set_extras_visible(False)
        self.reqs_wrap.hide()
        self._reset_related()
        pix = rounded_cover_pixmap(cover_path, self.DETAIL_W, self.DETAIL_H, 12) if cover_path else None
        if pix:
            self.cover.setText("")
            self.cover.setPixmap(pix)
        else:
            self.cover.setPixmap(QPixmap())
            self.cover.setText("No\ncover")

        currency = load_launcher_settings().get("currency", "EGP")

        def work():
            try:
                d = fetch_steam_details(appid, currency)
                d["system"] = get_system_specs()
                return (appid, d)
            except Exception:
                return (appid, None)

        worker = TaskWorker(work)
        worker.done.connect(self._on_details)
        self._workers = [w for w in self._workers if w.isRunning()] + [worker]
        worker.start()
        self._load_related(appid)

    def _on_details(self, result):
        appid, d = result
        if appid != self._appid:
            return  # user already opened another game
        if d is None:
            self.desc_label.setText("Couldn't load the details. Check your connection.")
            return
        self.desc_label.setText(d["description"] or "No description available.")
        meta = [p for p in (d["developers"], d["release"]) if p]
        self.meta_label.setText("  \u2022  ".join(meta))
        self.price_label.setText(d["price"])
        flag = round_flag_pixmap(d["price_flag"]) if d.get("price_flag") else None
        if flag:
            self.price_flag.setPixmap(flag)
            self.price_flag.show()
        else:
            self.price_flag.hide()
        for genre in d["genres"]:
            chip = QLabel(genre)
            chip.setStyleSheet(self.CHIP_STYLE)
            self.chips_row.addWidget(chip)
        self.chips_row.addStretch()
        for raw in d["shots"]:
            pm = QPixmap()
            if pm.loadFromData(raw):
                shot = QLabel()
                shot.setPixmap(pm.scaledToHeight(160, Qt.TransformationMode.SmoothTransformation))
                self.shots_row.addWidget(shot)
        self.shots_row.addStretch()
        self._set_extras_visible(bool(d["shots"]))
        self._show_requirements(d)

    def _show_requirements(self, d):
        reqs, system = d.get("reqs") or {}, d.get("system") or {}
        mn, rc = reqs.get("minimum") or [], reqs.get("recommended") or []
        pc = [p for p in (system.get("cpu"), system.get("gpu")) if p]
        if system.get("ram_gb"):
            pc.append(f"{_fmt_gb(system['ram_gb'])} GB RAM")
        if system.get("free_gb"):
            pc.append(f"{_fmt_gb(system['free_gb'])} GB free")
        self.pc_label.setText("Your PC: " + "  \u2022  ".join(pc) if pc else "")
        self.pc_label.setVisible(bool(pc))
        if not (mn or rc):
            self.reqs_wrap.hide()           # nothing to show for this game
            return
        verdict, color, _notes = assess_requirements(reqs, d.get("platforms"), system)
        if color is None:
            self.verdict_label.hide()       # couldn't check: just show the lists
        else:
            ok = color in (GREEN, ORANGE)
            self.verdict_label.setText("This game can run on your pc" if ok else "This game may struggle on your pc")
            self.verdict_label.setStyleSheet(
                f"background: {GREEN if ok else RED}; color: #ffffff; border-radius: 12px; "
                "padding: 6px 16px; font-size: 14px; font-weight: bold;")
            self.verdict_label.show()
        self.min_label.setText(_reqs_html("Minimum", mn))
        self.rec_label.setText(_reqs_html("Recommended", rc))
        self.min_label.setVisible(bool(mn))
        self.rec_label.setVisible(bool(rc))
        self.reqs_wrap.show()

class StorePage(QWidget):
    game_selected = pyqtSignal(int, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Content")
        self._request_id = 0
        self._term = ""
        self._next_start = 0
        self._total = 0
        self._loading = False
        self._exhausted = False
        self._loaded_once = False
        self._key_rejected = False
        self._cards = []
        self._card_by_id = {}
        self._workers = []
        self._cols = 0
        self._tag_filter = []       # [(tag_id, name)] the Store is filtered by

        self.loader = CoverLoader()
        self.loader.loaded.connect(self._on_cover_loaded)
        self.loader.auth_failed.connect(self._on_auth_failed)
        QApplication.instance().aboutToQuit.connect(self._shutdown)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(450)
        self._search_timer.timeout.connect(self._start_search)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 28, 24, 0)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(0)
        title = QLabel("Store")
        title.setObjectName("PageTitle")
        self.count_label = QLabel()
        self.count_label.setObjectName("PageCount")
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("Search")
        self.search_edit.setPlaceholderText("Search Steam games")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedSize(260, 36)
        self.search_edit.textChanged.connect(lambda _text: self._search_timer.start())
        self.search_edit.returnPressed.connect(self._start_search)
        header.addWidget(title)
        header.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBottom)
        header.addStretch()
        self.tags_btn = QPushButton("Tags")
        self.tags_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tags_btn.setFixedHeight(36)
        self.tags_btn.setToolTip("Filter games by Steam tags")
        self.tags_btn.clicked.connect(self._open_tag_picker)
        header.addWidget(self.tags_btn)
        header.addSpacing(8)
        header.addWidget(self.search_edit)
        layout.addLayout(header)
        layout.addSpacing(10)

        self.tag_bar = QWidget()                    # chips for the tags in use (click one to drop it)
        FlowLayout(self.tag_bar, spacing=8).setContentsMargins(0, 0, 0, 10)
        self.tag_bar.hide()
        layout.addWidget(self.tag_bar)

        self.banner = QLabel()
        self.banner.setObjectName("RowDesc")
        self.banner.setWordWrap(True)
        self.banner.hide()
        layout.addWidget(self.banner)
        layout.addSpacing(10)

        self.status_label = QLabel()
        self.status_label.setObjectName("EmptyState")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.hide()
        layout.addWidget(self.status_label)
        self.retry_btn = QPushButton("Retry")
        self.retry_btn.setFlat(True)
        self.retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry_btn.clicked.connect(self._retry)
        self.retry_btn.hide()
        layout.addWidget(self.retry_btn, 0, Qt.AlignmentFlag.AlignHCenter)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        container.setObjectName("GridContainer")
        self.grid = QGridLayout(container)
        self.grid.setContentsMargins(0, 0, 12, 24)
        self.grid.setHorizontalSpacing(STORE_GRID_GAP)
        self.grid.setVerticalSpacing(STORE_GRID_GAP)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(container)
        self.scroll.verticalScrollBar().valueChanged.connect(lambda _value: self._maybe_load_more())
        layout.addWidget(self.scroll, 1)

    # ------------------------------------------------------------ lifecycle
    def showEvent(self, event):
        super().showEvent(event)
        self.loader.set_key(get_steamgriddb_key())
        self._update_banner()
        if not self._loaded_once:
            self._loaded_once = True
            self._start_search()
        else:
            QTimer.singleShot(100, self._maybe_load_more)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._cards and self._columns() != self._cols:
            self._relayout()

    def _shutdown(self):
        self.loader.shutdown()
        for worker in self._workers:
            if worker.isRunning():
                worker.wait(3000)

    def on_key_changed(self):
        self._key_rejected = False
        self.loader.set_key(get_steamgriddb_key())
        self._update_banner()
        if self._loaded_once:
            self._start_search()

    # ------------------------------------------------------------ banner / status
    def _update_banner(self):
        if self._key_rejected:
            text = "SteamGridDB rejected your API key, so Steam's own art is shown instead. Check it in Settings."
        elif not get_steamgriddb_key():
            text = "Showing Steam's own art. Add your SteamGridDB API key in Settings to use community covers."
        else:
            text = ""
        self.banner.setText(text)
        self.banner.setVisible(bool(text))

    def _on_auth_failed(self):
        self._key_rejected = True
        self._update_banner()

    def _set_status(self, text):
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    # ------------------------------------------------------------ loading
    def _columns(self):
        available = self.width() - 36 - 24 - 12
        return max(1, (available + STORE_GRID_GAP) // (STORE_CARD_W + STORE_GRID_GAP))

    def _clear_cards(self):
        for card in self._cards:
            self.grid.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards = []
        self._card_by_id = {}

    def _start_search(self):
        self._search_timer.stop()
        self._request_id += 1
        self.loader.cancel_pending()
        self._term = self.search_edit.text().strip()
        self._clear_cards()
        self._next_start = 0
        self._total = 0
        self._exhausted = False
        self._loading = False
        self.retry_btn.hide()
        self.count_label.setText("")
        self._set_status("Loading games...")
        self._load_page()

    def _retry(self):
        if not self._cards:
            self._start_search()
            return
        self.retry_btn.hide()
        self._set_status("")
        self._load_page()

    def _load_page(self):
        if self._loading or self._exhausted:
            return
        self._loading = True
        request_id, term, start = self._request_id, self._term, self._next_start
        tag_ids = [t for t, _ in self._tag_filter]

        def work():
            try:
                games, total = fetch_steam_games(term, start, tags=tag_ids)
                return (request_id, start, games, total, "")
            except Exception as e:
                return (request_id, start, [], 0, str(e) or e.__class__.__name__)

        worker = TaskWorker(work)
        worker.done.connect(self._on_page)
        self._workers = [w for w in self._workers if w.isRunning()] + [worker]
        worker.start()

    def _on_page(self, result):
        request_id, start, games, total, error = result
        if request_id != self._request_id:
            return  # an older search; ignore
        self._loading = False
        if error:
            self._set_status("Couldn't load games from Steam. Check your connection and try again."
                             if not self._cards else "Couldn't load more games.")
            self.retry_btn.show()
            return

        self.retry_btn.hide()
        self._total = total
        if not games and (start == 0 and total > 0):
            self._exhausted = True
            self._set_status("Couldn't read Steam's game list (the store page format may have changed).")
            return

        for appid, title, img_base in games:
            if appid in self._card_by_id:
                continue
            card = StoreCard(appid, title)
            card.clicked.connect(self.game_selected.emit)
            index = len(self._cards)
            cols = self._columns()
            self._cols = cols
            self._cards.append(card)
            self._card_by_id[appid] = card
            self.grid.addWidget(card, index // cols, index % cols)
            self.loader.request(appid, img_base, title)

        self._next_start = start + STORE_PAGE_SIZE
        if not games or self._next_start >= total:
            self._exhausted = True
        self._set_status("" if self._cards else "No games found.")
        self.count_label.setText(f"{total:,} games" if total else "")
        QTimer.singleShot(50, self._maybe_load_more)

    def _maybe_load_more(self):
        if self._loading or self._exhausted or not self._cards or not self.isVisible():
            return
        bar = self.scroll.verticalScrollBar()
        if bar.maximum() == 0 or bar.value() >= bar.maximum() - 400:
            self._load_page()

    def _relayout(self):
        cols = self._columns()
        self._cols = cols
        while self.grid.count():
            self.grid.takeAt(0)
        for index, card in enumerate(self._cards):
            self.grid.addWidget(card, index // cols, index % cols)

    def _on_cover_loaded(self, appid, path):
        card = self._card_by_id.get(appid)
        if card is not None:
            card.set_cover(path)

    # ------------------------------------------------------------ tag filter
    def _open_tag_picker(self):
        dlg = TagPickerDialog([t for t, _ in self._tag_filter], self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.set_tag_filter(dlg.result_tags())

    def _remove_tag(self, tag_id):
        self.set_tag_filter([(t, n) for t, n in self._tag_filter if t != tag_id])

    def set_tag_filter(self, tags, clear_search=False):
        """Filters the Store by these [(tag_id, name)] and reloads it."""
        self._tag_filter = list(tags)
        if clear_search:
            self.search_edit.blockSignals(True)
            self.search_edit.clear()
            self.search_edit.blockSignals(False)
        self._rebuild_tag_bar()
        self._loaded_once = True
        self._start_search()

    def _rebuild_tag_bar(self):
        flow = self.tag_bar.layout()
        while flow.count():
            item = flow.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for tag_id, name in self._tag_filter:
            chip = make_tag_chip(f"{name}   x")
            chip.setToolTip("Click to remove this tag")
            chip.clicked.connect(lambda _c=False, t=tag_id: self._remove_tag(t))
            flow.addWidget(chip)
        self.tag_bar.setVisible(bool(self._tag_filter))
        self.tags_btn.setText(f"Tags ({len(self._tag_filter)})" if self._tag_filter else "Tags")
        self.tags_btn.setObjectName("Primary" if self._tag_filter else "")
        self.tags_btn.style().polish(self.tags_btn)


COVER_W, COVER_H = 200, 300
GRID_COLUMNS = 3
GREEN = "#22c55e"
ORANGE = "#f59e0b"
RED = "#f44336"

class AdaptiveApp(QMainWindow):
    def __init__(self):
        super().__init__()

        self.open_location_buttons = {}
        self.wine = find_wine()
        self.wine_uninstall_buttons = {}
        self.update_buttons = {}
        self.latest_versions = {}
        self._version_workers = []
        self.btn_ae_action = None
        self.btn_ae_uninstall = None
        self.btn_ae_save = None
        self.btn_ae_settings = None

        self.btn_ncz2_action = None
        self.btn_ncz2_uninstall = None
        self.btn_ncz2_save = None
        self.btn_ncz2_settings = None

        self.sync_buttons = {}
        self.cards = []
        self.card_status = {}

        self._grid_cols = 0
        self.resize(1220, 720)
        self.setMinimumSize(QSize(1000, 640))
        self.setWindowTitle("NCZ Games Launcher")

        icon_path = asset_path("icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.center_on_screen()

        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self.build_sidebar())
        self.pages = QStackedWidget()
        self.pages.addWidget(self.build_library_page())
        self.settings_page = SettingsPage()
        self.pages.addWidget(self.settings_page)
        self.pages.addWidget(CreditsPage())
        self.account_page = AccountPage()
        self.account_page.account_changed.connect(self.refresh_profile)
        self.pages.addWidget(self.account_page)
        self.store_page = StorePage()
        self.pages.addWidget(self.store_page)
        self.settings_page.steamgriddb_key_changed.connect(self.store_page.on_key_changed)
        self.store_detail_page = StoreDetailPage()
        self.pages.addWidget(self.store_detail_page)  # index 5
        self.store_page.game_selected.connect(self.open_store_game)
        self.store_detail_page.add_requested.connect(self.add_steam_game)
        self.store_detail_page.back_requested.connect(lambda: self.pages.setCurrentIndex(4))
        self.store_detail_page.game_selected.connect(self.open_store_game)
        self.store_detail_page.tag_requested.connect(self.open_store_tag)
        root_layout.addWidget(self.pages, 1)

        self.apply_filter()
        self.refresh_profile()
        self.account_page.sync_profile_async()
        self.check_nczfront_status()
        QApplication.instance().aboutToQuit.connect(self._wait_for_version_workers)
        self.check_for_updates()

    # ------------------------------------------------------------ sidebar
    def make_nav_button(self, text, checkable=False):
        short = {"Garry's Mod Addon Manager": "GMod Addons"}.get(text, text)
        btn = QPushButton(short.upper())
        btn.setToolTip(text)
        btn.setObjectName("NavButton")
        btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        btn.setCheckable(checkable)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    def build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedHeight(56)
        layout = QHBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(0)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(18, 0, 18, 0)
        brand_row.setSpacing(10)
        self.brand_logo = QLabel()
        self.brand_logo.setFixedSize(32, 32)
        brand_row.addWidget(self.brand_logo)
        self.reload_brand_logo()
        brand = QLabel("NCZ Games")
        brand.setObjectName("Brand")
        brand_row.addWidget(brand)
        brand_row.addStretch()
        layout.addLayout(brand_row)
        layout.addSpacing(14)

        self.nav_group = QButtonGroup(self)
        for index, label in ((0, "Library"), (4, "Store"), (1, "Settings"), (2, "Credits")):
            btn = self.make_nav_button(label, checkable=True)
            self.nav_group.addButton(btn, index)
            layout.addWidget(btn)
        self.nav_group.button(0).setChecked(True)
        self.nav_group.idClicked.connect(self.show_page)
        layout.addStretch()

        profile_wrap = QHBoxLayout()
        profile_wrap.setContentsMargins(8, 0, 0, 0)
        profile_wrap.addWidget(self.build_profile_button())
        layout.addLayout(profile_wrap)
        return sidebar

    def build_profile_button(self):
        btn = QPushButton()
        btn.setObjectName("ProfileButton")
        btn.setFixedSize(176, 44)
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.nav_group.addButton(btn, 3)

        row = QHBoxLayout(btn)
        row.setContentsMargins(10, 0, 10, 0)
        row.setSpacing(10)

        self.avatar_label = QLabel("?")
        self.avatar_label.setObjectName("Avatar")
        self.avatar_label.setFixedSize(36, 36)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        text_col.setContentsMargins(0, 0, 0, 0)
        self.profile_name = QLabel()
        self.profile_name.setObjectName("ProfileName")
        self.profile_sub = QLabel()
        self.profile_sub.setObjectName("ProfileSub")
        text_col.addStretch()
        text_col.addWidget(self.profile_name)
        text_col.addWidget(self.profile_sub)
        text_col.addStretch()

        row.addWidget(self.avatar_label)
        row.addLayout(text_col, 1)

        # Let clicks fall through the labels to the button underneath.
        for w in (self.avatar_label, self.profile_name, self.profile_sub):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        return btn

    def refresh_profile(self):
        acct = load_account() if cloud_configured() else None
        if acct:
            username, avatar = load_profile_cache()
            name = username or (acct.get("email", "") or "").split("@")[0] or "Account"
            apply_avatar(self.avatar_label, avatar, name[:1].upper(), 36)
            self.profile_name.setText(self.profile_name.fontMetrics().elidedText(
                name, Qt.TextElideMode.ElideRight, 120))
            self.profile_sub.setVisible(False)
        elif cloud_configured():
            self.profile_sub.setVisible(True)
            apply_avatar(self.avatar_label, None, "?", 36)
            self.profile_name.setText("Sign in")
            self.profile_sub.setText("Sync your saves")
        else:
            self.profile_sub.setVisible(True)
            apply_avatar(self.avatar_label, None, "?", 36)
            self.profile_name.setText("Account")
            self.profile_sub.setText("Cloud sync unavailable")

    # ------------------------------------------------------------ library page
    def build_library_page(self):
        page = QWidget()
        page.setObjectName("Content")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(36, 28, 24, 0)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(0)
        title = QLabel("Library")
        title.setObjectName("PageTitle")
        self.count_label = QLabel()
        self.count_label.setObjectName("PageCount")
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("Search")
        self.search_edit.setPlaceholderText("Search library")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedSize(240, 36)
        self.search_edit.textChanged.connect(self.apply_filter)
        header.addWidget(title)
        header.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBottom)
        header.addStretch()
        header.addWidget(self.search_edit)
        layout.addLayout(header)
        layout.addSpacing(22)

        self.empty_label = QLabel("No games match your search.")
        self.empty_label.setObjectName("EmptyState")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()
        layout.addWidget(self.empty_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid_container = QWidget()
        self.grid_container.setObjectName("GridContainer")
        self.grid = QGridLayout(self.grid_container)
        self.grid.setContentsMargins(0, 0, 12, 24)
        self.grid.setHorizontalSpacing(24)
        self.grid.setVerticalSpacing(24)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        scroll.setWidget(self.grid_container)
        layout.addWidget(scroll, 1)

        self.create_game_card("Five Nights at NCZ AE", asset_path("fnanczaecover.png"), "ae")
        self.create_game_card("Five Nights at NCZ 2", asset_path("fnancz2cover.png"), "ncz2")
        self.create_game_card("NCZFront", asset_path("nczfront-cover.png"), "nczfront")
        self.steam_cards = {}
        for game in load_steam_library():
            self.create_steam_card(game["appid"], game["title"], game.get("cover", ""))
        return page

    def grid_columns(self):
        # window width minus the library page / grid margins (36 + 24 + 12); the nav bar is on top now
        available = self.width() - 36 - 24 - 12
        card_w, gap = COVER_W + 18, 24
        return max(1, (available + gap) // (card_w + gap))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "search_edit") and self.grid_columns() != self._grid_cols:
            self.apply_filter()

    def apply_filter(self, text=""):
        query = (text if isinstance(text, str) else self.search_edit.text()).strip().lower()
        cols = self.grid_columns()
        self._grid_cols = cols
        while self.grid.count():
            self.grid.takeAt(0)
        shown = 0
        for card, title in self.cards:
            if query in title.lower():
                self.grid.addWidget(card, shown // cols, shown % cols)
                card.show()
                shown += 1
            else:
                card.hide()
        self.count_label.setText(f"{shown} game{'s' if shown != 1 else ''}")
        self.empty_label.setVisible(shown == 0)

    def set_card_status(self, game_id, text, color=None):
        label = self.card_status.get(game_id)
        if label is None:
            return
        label.setText(text)
        label.setStyleSheet(f"color: {color};" if color else "")

    def check_nczfront_status(self):
        if getattr(self, "ping_thread", None) is not None and self.ping_thread.isRunning():
            return
        self.set_card_status("nczfront", "Checking...")
        self.ping_thread = PingWorker(NCZFRONT_URL)
        self.ping_thread.result.connect(self.on_nczfront_ping_result)
        self.ping_thread.start()

    def on_nczfront_ping_result(self, is_hosted):
        if is_hosted:
            self.set_card_status("nczfront", "● Hosted", GREEN)
        else:
            self.set_card_status("nczfront", "● Not Hosted", RED)

    def open_account(self):
        self.nav_group.button(3).setChecked(True)
        self.show_page(3)

    def show_page(self, index):
        self.pages.setCurrentIndex(index)

    def open_store_game(self, appid, title, cover_path):
        self.store_detail_page.show_game(appid, title, cover_path)
        self.store_detail_page.set_in_library(appid in self.steam_cards)
        self.pages.setCurrentWidget(self.store_detail_page)

    def open_store_tag(self, tag_id, tag_name):
        """A tag chip on a game's page was clicked: show the Store filtered by that tag."""
        self.nav_group.button(4).setChecked(True)
        self.pages.setCurrentWidget(self.store_page)
        self.store_page.set_tag_filter([(tag_id, tag_name)], clear_search=True)

    def update_ae_button_state(self):
        if not self.btn_ae_action:
            return
        installed = os.path.exists(installed_game_dir(AE_GAME_NAME))

        try:
            self.btn_ae_action.clicked.disconnect()
        except Exception:
            pass

        if installed:
            self.btn_ae_action.setText("Launch")
            self.btn_ae_action.clicked.connect(self.launch_ae_game)
        else:
            self.btn_ae_action.setText("Install")
            self.btn_ae_action.clicked.connect(self.start_ae_install)
        if self.btn_ae_uninstall:
            self.btn_ae_uninstall.setEnabled(installed)
        self.set_open_location_enabled("ae", installed)
        self.refresh_wine_uninstall("ae")
        if installed:
            self.set_card_status("ae", "● Installed", GREEN)
        else:
            self.set_card_status("ae", "Not installed")

        if self.btn_ae_save:
            self.btn_ae_save.setEnabled(os.path.exists(get_ae_save_path()))
        if self.btn_ae_settings:
            self.btn_ae_settings.setEnabled(os.path.exists(get_ae_settings_path()))

        self.refresh_sync_buttons("ae")
        self.refresh_update_state("ae")

    def launch_ae_game(self):
        game_dir = installed_game_dir(AE_GAME_NAME)
        if sys.platform.startswith("win"):
            exe_path = os.path.join(game_dir, "FNaNCZ AE.exe")
            if os.path.exists(exe_path):
                subprocess.Popen([exe_path] + launch_args_for("ae"), cwd=game_dir)
        elif sys.platform.startswith("linux"):
            sh_path = os.path.join(game_dir, "run.sh")
            if os.path.exists(sh_path):
                os.chmod(sh_path, 0o755)
                subprocess.Popen(["bash", sh_path] + launch_args_for("ae"), cwd=game_dir)

    def switch_compat_tool(self, key):
        config = load_compat_config()
        picked = prompt_compat_tool(self, config.get(key, ""), _d3d_for(key))
        if picked is None:
            return
        chosen, d3d = picked
        if chosen:
            config[key] = chosen
        else:
            config.pop(key, None)
        save_compat_config(config)
        save_game_d3d(key, d3d)
        QMessageBox.information(self, "Compatibility Tool", "Compatibility settings updated for this game.")

    def launch_with_wine(self, game_id):
        tool = load_compat_config().get(game_id, "")
        if not compat_tool_kind(tool) and not default_wine_path(self):
            QMessageBox.warning(self, "Wine Required",
                                "Wine wasn't found. Install it, or download a build in the Wine tab.")
            return
        cfg = GAME_INFO[game_id]
        wine_dir = wine_game_dir(cfg["name"])
        exe_path = find_game_exe(wine_dir, cfg["exe"])

        if not exe_path:
            # Extract the Windows zip (using a local copy if there is one, otherwise downloading it).
            existing_zip = find_existing_zip(cfg["zip_prefixes"])
            dialog = DownloadDialog(cfg["version_url"], cfg["name"], "", self,
                                    existing_zip_path=existing_zip, wine_extract_dir=wine_dir)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            self.refresh_wine_uninstall(game_id)
            if not accepted:
                return
            exe_path = find_game_exe(wine_dir, cfg["exe"])
            if not exe_path:
                QMessageBox.warning(self, "Launch using wine",
                                    f"Couldn't find {cfg['exe']} in the extracted files.")
                return

        _launch_exe(self, exe_path, load_compat_config().get(game_id, ""), _d3d_for(game_id), launch_args_for(game_id))

    def refresh_wine_uninstall(self, game_id):
        action = self.wine_uninstall_buttons.get(game_id)
        if action:
            action.setEnabled(os.path.isdir(wine_game_dir(GAME_INFO[game_id]["name"])))

    def uninstall_wine_version(self, game_id):
        wine_dir = wine_game_dir(GAME_INFO[game_id]["name"])
        shutil.rmtree(wine_dir, ignore_errors=True)
        shutil.rmtree(wine_dir + ".part", ignore_errors=True)
        self.refresh_wine_uninstall(game_id)

    def check_for_updates(self):
        # Reads the same version file the download link comes from, once per launch, in the background.
        for game_id, info in GAME_INFO.items():
            worker = TaskWorker(lambda g=game_id, u=info["version_url"]: (g, fetch_remote_version(u)))
            worker.done.connect(self.on_version_result)
            self._version_workers.append(worker)
            worker.start()

    def on_version_result(self, result):
        game_id, version = result
        if not version:
            return
        self.latest_versions[game_id] = version
        name = GAME_INFO[game_id]["name"]
        if os.path.exists(installed_game_dir(name)) and not get_installed_version(name):
            # First check for a game installed before versions were tracked: treat it as current.
            record_installed_version(name, version)
        self.refresh_update_state(game_id)

    def _wait_for_version_workers(self):
        for worker in self._version_workers:
            if worker.isRunning():
                worker.wait(3000)

    def refresh_update_state(self, game_id):
        btn = self.update_buttons.get(game_id)
        if btn is None:
            return
        name = GAME_INFO[game_id]["name"]
        installed = os.path.exists(installed_game_dir(name))
        available = installed and is_newer_version(self.latest_versions.get(game_id, ""),
                                                   get_installed_version(name))
        btn.setVisible(available)
        launch_btn = getattr(self, f"btn_{game_id}_action", None)
        if launch_btn is not None:
            launch_btn.setStyleSheet("padding: 8px 6px;" if available else "")
        if available:
            self.set_card_status(game_id, "● Update available", ORANGE)

    def update_game(self, game_id):
        info = GAME_INFO[game_id]
        name = info["name"]
        game_dir = installed_game_dir(name)

        # 1) Download the new version first, so a failed download never touches the current install.
        download = DownloadDialog(info["version_url"], name, info["linux_cmd"], self, download_only=True)
        if download.exec() != QDialog.DialogCode.Accepted:
            return
        zip_path = download.zip_path
        version = download.worker.remote_version or self.latest_versions.get(game_id, "")
        if not zip_path or not os.path.exists(zip_path):
            QMessageBox.warning(self, "Update", "The download didn't finish. Please try again.")
            return

        # 2) Move the current install aside (this also fails if the game is still running on Windows).
        old_dir = game_dir + ".old"
        shutil.rmtree(old_dir, ignore_errors=True)
        if os.path.isdir(game_dir):
            try:
                os.rename(game_dir, old_dir)
            except OSError:
                QMessageBox.warning(self, "Update",
                                    f"Couldn't replace the current install. Close {name} and try again.")
                return

        # 3) Install the downloaded zip (Linux conversion script / extraction), then drop the old copy.
        install = DownloadDialog(info["version_url"], name, info["linux_cmd"], self, existing_zip_path=zip_path)
        if install.exec() == QDialog.DialogCode.Accepted:
            shutil.rmtree(old_dir, ignore_errors=True)
            record_installed_version(name, version)
        elif os.path.isdir(old_dir):
            shutil.rmtree(game_dir, ignore_errors=True)
            os.rename(old_dir, game_dir)
        getattr(self, f"update_{game_id}_button_state")()

    def set_open_location_enabled(self, game_id, enabled):
        action = self.open_location_buttons.get(game_id)
        if action:
            action.setEnabled(enabled)

    def open_game_location(self, game_name):
        game_dir = installed_game_dir(game_name)
        if not os.path.isdir(game_dir):
            QMessageBox.information(self, "Open Game Location", f"{game_name} isn't installed.")
            return
        opened = False
        try:
            if sys.platform.startswith("win"):
                os.startfile(game_dir)
                opened = True
            else:
                opened = QDesktopServices.openUrl(QUrl.fromLocalFile(game_dir))
                if not opened and sys.platform.startswith("linux") and shutil.which("xdg-open"):
                    subprocess.Popen(["xdg-open", game_dir])
                    opened = True
        except Exception:
            opened = False
        if not opened:
            QMessageBox.warning(self, "Open Game Location", f"Couldn't open the folder:\n{game_dir}")

    def uninstall_ae_game(self):
        game_dir = installed_game_dir(AE_GAME_NAME)
        if os.path.exists(game_dir):
            shutil.rmtree(game_dir, ignore_errors=True)
        forget_installed_version(AE_GAME_NAME)
        self.update_ae_button_state()

    def open_ae_save_editor(self):
        save_path = get_ae_save_path()
        if os.path.exists(save_path):
            dialog = JsonEditorDialog(save_path, "Edit Save File", self)
            dialog.exec()

    def open_ae_settings_editor(self):
        settings_path = get_ae_settings_path()
        if os.path.exists(settings_path):
            dialog = JsonEditorDialog(settings_path, "Edit Settings File", self)
            dialog.exec()

    def start_ae_install(self):
        existing_zips = [
            f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
            if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz ae", "fnancz_ae", "fnanczae"))
        ]
        version_url = GAME_INFO["ae"]["version_url"]
        linux_cmd = GAME_INFO["ae"]["linux_cmd"]

        if existing_zips:
            existing_zip_path = os.path.join(DOWNLOAD_DIR, existing_zips[0])
            existing_dialog = ExistingFileDialog(existing_zips[0], self)
            if existing_dialog.exec() == QDialog.DialogCode.Accepted:
                if existing_dialog.choice == "use":
                    dialog = DownloadDialog(version_url, AE_GAME_NAME, linux_cmd, self, existing_zip_path=existing_zip_path)
                    if dialog.exec() == QDialog.DialogCode.Accepted:
                        self.update_ae_button_state()
                    return
                elif existing_dialog.choice != "overwrite":
                    return
            else:
                return

        dialog = DownloadDialog(version_url, AE_GAME_NAME, linux_cmd, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.update_ae_button_state()

    def update_ncz2_button_state(self):
        if not self.btn_ncz2_action:
            return
        installed = os.path.exists(installed_game_dir(NCZ2_GAME_NAME))

        try:
            self.btn_ncz2_action.clicked.disconnect()
        except Exception:
            pass

        if installed:
            self.btn_ncz2_action.setText("Launch")
            self.btn_ncz2_action.clicked.connect(self.launch_ncz2_game)
        else:
            self.btn_ncz2_action.setText("Install")
            self.btn_ncz2_action.clicked.connect(self.start_ncz2_install)
        if self.btn_ncz2_uninstall:
            self.btn_ncz2_uninstall.setEnabled(installed)
        self.set_open_location_enabled("ncz2", installed)
        self.refresh_wine_uninstall("ncz2")
        if installed:
            self.set_card_status("ncz2", "● Installed", GREEN)
        else:
            self.set_card_status("ncz2", "Not installed")

        if self.btn_ncz2_save:
            self.btn_ncz2_save.setEnabled(os.path.exists(get_ncz2_save_path()))
        if self.btn_ncz2_settings:
            self.btn_ncz2_settings.setEnabled(os.path.exists(get_ncz2_settings_path()))

        self.refresh_sync_buttons("ncz2")
        self.refresh_update_state("ncz2")

    def launch_ncz2_game(self):
        game_dir = installed_game_dir(NCZ2_GAME_NAME)
        if sys.platform.startswith("win"):
            exe_path = os.path.join(game_dir, "FNANCZ 2.exe")
            if os.path.exists(exe_path):
                subprocess.Popen([exe_path] + launch_args_for("ncz2"), cwd=game_dir)
        elif sys.platform.startswith("linux"):
            sh_path = os.path.join(game_dir, "run.sh")
            if os.path.exists(sh_path):
                os.chmod(sh_path, 0o755)
                subprocess.Popen(["bash", sh_path] + launch_args_for("ncz2"), cwd=game_dir)

    def uninstall_ncz2_game(self):
        game_dir = installed_game_dir(NCZ2_GAME_NAME)
        if os.path.exists(game_dir):
            shutil.rmtree(game_dir, ignore_errors=True)
        forget_installed_version(NCZ2_GAME_NAME)
        self.update_ncz2_button_state()

    def open_ncz2_save_editor(self):
        save_path = get_ncz2_save_path()
        if os.path.exists(save_path):
            dialog = JsonEditorDialog(save_path, "Edit Save File", self)
            dialog.exec()

    def open_ncz2_settings_editor(self):
        settings_path = get_ncz2_settings_path()
        if os.path.exists(settings_path):
            dialog = JsonEditorDialog(settings_path, "Edit Settings File", self)
            dialog.exec()

    def start_ncz2_install(self):
        existing_zips = [
            f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
            if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz 2", "fnancz_2", "fnancz 2", "fnancz2"))
        ]
        version_url = GAME_INFO["ncz2"]["version_url"]
        linux_cmd = GAME_INFO["ncz2"]["linux_cmd"]

        if existing_zips:
            existing_zip_path = os.path.join(DOWNLOAD_DIR, existing_zips[0])
            existing_dialog = ExistingFileDialog(existing_zips[0], self)
            if existing_dialog.exec() == QDialog.DialogCode.Accepted:
                if existing_dialog.choice == "use":
                    dialog = DownloadDialog(version_url, NCZ2_GAME_NAME, linux_cmd, self, existing_zip_path=existing_zip_path)
                    if dialog.exec() == QDialog.DialogCode.Accepted:
                        self.update_ncz2_button_state()
                    return
                elif existing_dialog.choice != "overwrite":
                    return
            else:
                return

        dialog = DownloadDialog(version_url, NCZ2_GAME_NAME, linux_cmd, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.update_ncz2_button_state()

    def refresh_sync_buttons(self, game_id):
        pair = self.sync_buttons.get(game_id)
        if not pair or game_id not in GAME_SYNC:
            return
        btn_up, btn_down = pair
        _name, save_fn, settings_fn = GAME_SYNC[game_id]
        btn_up.setEnabled(os.path.exists(save_fn()) or os.path.exists(settings_fn()))
        btn_down.setEnabled(True)

    def ensure_logged_in(self):
        if not cloud_configured():
            QMessageBox.information(self, "Cloud Sync", "Cloud sync isn't set up in this build of the launcher yet.")
            return False
        if load_account() is None:
            QMessageBox.information(self, "Cloud Sync", "Log in to your account first, then try again.")
            self.open_account()
            return False
        return True

    def cloud_save_data(self, game_id):
        name, save_fn, settings_fn = GAME_SYNC[game_id]
        save_path, settings_path = save_fn(), settings_fn()
        if not (os.path.exists(save_path) or os.path.exists(settings_path)):
            QMessageBox.information(self, "Save Data", f"No save or settings files found for {name} on this device yet.")
            return
        if not self.ensure_logged_in():
            return

        ok, cloud, error = BusyDialog("Checking the cloud...", lambda: cloud_get_game(game_id), self).run()
        if not ok:
            QMessageBox.warning(self, "Cloud Sync", error)
            return

        if cloud and (cloud.get("save_text") or cloud.get("settings_text")):
            lines = [f"The cloud already has data for {name}, saved {format_cloud_time(cloud.get('updated'))} "
                     f"from {cloud.get('device', 'another device')}."]
            cloud_desc = describe_save(cloud.get("save_text") or "")
            try:
                local_desc = describe_save(read_game_text(save_path)[0]) if os.path.exists(save_path) else ""
            except CloudError:
                local_desc = ""
            if cloud_desc and local_desc:
                lines.append(f"Cloud: {cloud_desc}   |   This device: {local_desc}")
            lines.append("Overwrite the cloud copy with this device's data?")
            answer = QMessageBox.question(self, "Save Data", "\n\n".join(lines),
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        ok, _count, error = BusyDialog("Uploading to the cloud...",
                                       lambda: cloud_upload_game(game_id, save_path, settings_path), self).run()
        if ok:
            QMessageBox.information(self, "Save Data", f"Your {name} data is saved to the cloud.")
        else:
            QMessageBox.warning(self, "Cloud Sync", error)

    def cloud_sync_data(self, game_id):
        name, save_fn, settings_fn = GAME_SYNC[game_id]
        if not self.ensure_logged_in():
            return

        ok, cloud, error = BusyDialog("Fetching your cloud data...", lambda: cloud_get_game(game_id), self).run()
        if not ok:
            QMessageBox.warning(self, "Cloud Sync", error)
            return
        if not cloud or not (cloud.get("save_text") or cloud.get("settings_text")):
            QMessageBox.information(self, "Sync Data",
                                    f"There's nothing in the cloud for {name} yet.\n\n"
                                    "Use Save Data on the device that has your progress first.")
            return

        save_path, settings_path = save_fn(), settings_fn()
        lines = [f"Replace this device's {name} data with the cloud copy from "
                 f"{format_cloud_time(cloud.get('updated'))} ({cloud.get('device', 'another device')})?"]
        cloud_desc = describe_save(cloud.get("save_text") or "")
        try:
            local_desc = describe_save(read_game_text(save_path)[0]) if os.path.exists(save_path) else ""
        except CloudError:
            local_desc = ""
        if cloud_desc and local_desc:
            lines.append(f"Cloud: {cloud_desc}   |   This device: {local_desc}")
        lines.append("Close the game first. Your current files are kept as .bak copies next to them.")
        answer = QMessageBox.question(self, "Sync Data", "\n\n".join(lines),
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            apply_cloud_game(cloud, save_path, settings_path)
        except Exception as e:
            QMessageBox.warning(self, "Sync Data", f"Couldn't write the files: {e}")
            return
        getattr(self, f"update_{game_id}_button_state")()
        QMessageBox.information(self, "Sync Data", f"Your {name} data is now up to date on this device.")

    def get_rounded_pixmap(self, image_filename, width, height, radius):
        src_pixmap = QPixmap(image_filename)
        if src_pixmap.isNull():
            return None

        scaled_pixmap = src_pixmap.scaled(
            width, height,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation
        )

        rounded_pixmap = QPixmap(width, height)
        rounded_pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(rounded_pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
        painter.setClipPath(path)

        painter.drawPixmap(0, 0, scaled_pixmap)
        painter.end()

        return rounded_pixmap

    def create_game_card(self, title_text, image_filename, game_id):
        card = QFrame(self.grid_container)
        card.setObjectName("GameCard")
        card.setFixedWidth(COVER_W + 18)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 8, 8, 10)
        layout.setSpacing(0)

        cover = QLabel()
        cover.setFixedSize(COVER_W, COVER_H)
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rounded_cover = None
        if os.path.exists(image_filename):
            rounded_cover = self.get_rounded_pixmap(image_filename, COVER_W, COVER_H, 8)
        if rounded_cover:
            cover.setPixmap(rounded_cover)
        else:
            cover.setObjectName("CoverPlaceholder")
            cover.setText("Missing\nCover" if not os.path.exists(image_filename) else "Error\nLoading")
        layout.addWidget(cover)
        layout.addSpacing(10)

        title = QLabel(title_text)
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        title.setFixedHeight(38)
        title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(title)

        status = QLabel()
        status.setObjectName("CardStatus")
        layout.addWidget(status)
        self.card_status[game_id] = status
        layout.addSpacing(12)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        btn_action = QPushButton("Install")
        btn_action.setObjectName("Primary")
        btn_action.setFixedHeight(38)
        btn_action.setCursor(Qt.CursorShape.PointingHandCursor)

        more_btn = QPushButton("•••")
        more_btn.setObjectName("MoreButton")
        more_btn.setFixedSize(42, 38)
        more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        menu = QMenu(more_btn)
        menu.setToolTipsVisible(True)
        more_btn.setMenu(menu)

        actions.addWidget(btn_action, 1)
        actions.addWidget(more_btn)
        layout.addLayout(actions)

        if game_id == "nczfront":
            btn_action.setText("Play")
            btn_action.clicked.connect(lambda: webbrowser.open(NCZFRONT_URL))
            menu.addAction("Refresh status").triggered.connect(self.check_nczfront_status)
        else:
            btn_save = menu.addAction("Edit Current Save")
            btn_settings = menu.addAction("Edit Current Settings")
            menu.addSeparator()
            btn_cloud_save = menu.addAction("Save Data")
            btn_cloud_sync = menu.addAction("Sync Data")
            btn_cloud_save.setToolTip("Upload this device's save and settings to your account")
            btn_cloud_sync.setToolTip("Download your account's save and settings to this device")
            menu.addSeparator()
            game_name = AE_GAME_NAME if game_id == "ae" else NCZ2_GAME_NAME
            if self.wine or wine_installed_builds():
                btn_wine = menu.addAction(f"Launch using wine ({self.wine[1]})" if self.wine else "Launch using wine")
                btn_wine.setToolTip("Extract the Windows build (no Linux conversion) and run it with wine")
                btn_wine.triggered.connect(lambda _checked=False, g=game_id: self.launch_with_wine(g))
            if sys.platform.startswith("linux"):
                btn_tool = menu.addAction("Switch Compatibility Tool")
                btn_tool.setToolTip("Which Wine / Proton the Windows build runs with (used by \"Launch using wine\")")
                btn_tool.triggered.connect(lambda _checked=False, g=game_id: self.switch_compat_tool(g))
            btn_open_location = menu.addAction("Open Game Location")
            btn_open_location.triggered.connect(lambda _checked=False, n=game_name: self.open_game_location(n))
            btn_redist = menu.addAction("Install Common Redist")
            btn_redist.setToolTip("Install Visual C++ / DirectX from the game's _CommonRedist folder (downloads them if missing)")
            btn_redist.triggered.connect(lambda _checked=False, n=game_name, g=game_id: install_redist_for_game(
                self, lambda: _ae_redist_dir(n), n, lambda: _ae_launch_info(g, n)))
            self.open_location_buttons[game_id] = btn_open_location
            btn_uninstall = menu.addAction("Uninstall")
            if sys.platform.startswith("linux"):
                btn_wine_uninstall = menu.addAction("Uninstall wine version")
                btn_wine_uninstall.triggered.connect(lambda _checked=False, g=game_id: self.uninstall_wine_version(g))
                self.wine_uninstall_buttons[game_id] = btn_wine_uninstall
            btn_update = QPushButton("Update")
            btn_update.setFixedHeight(38)
            btn_update.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_update.setStyleSheet("padding: 8px 6px;")
            btn_update.setToolTip("Download and install the latest version")
            btn_update.hide()
            btn_update.clicked.connect(lambda _checked=False, g=game_id: self.update_game(g))
            actions.insertWidget(1, btn_update, 1)
            self.update_buttons[game_id] = btn_update
            btn_cloud_save.triggered.connect(lambda _checked=False, g=game_id: self.cloud_save_data(g))
            btn_cloud_sync.triggered.connect(lambda _checked=False, g=game_id: self.cloud_sync_data(g))
            self.sync_buttons[game_id] = (btn_cloud_save, btn_cloud_sync)

            if game_id == "ae":
                self.btn_ae_action = btn_action
                self.btn_ae_uninstall = btn_uninstall
                self.btn_ae_save = btn_save
                self.btn_ae_settings = btn_settings
                btn_uninstall.triggered.connect(self.uninstall_ae_game)
                btn_save.triggered.connect(self.open_ae_save_editor)
                btn_settings.triggered.connect(self.open_ae_settings_editor)
                self.update_ae_button_state()
            elif game_id == "ncz2":
                self.btn_ncz2_action = btn_action
                self.btn_ncz2_uninstall = btn_uninstall
                self.btn_ncz2_save = btn_save
                self.btn_ncz2_settings = btn_settings
                btn_uninstall.triggered.connect(self.uninstall_ncz2_game)
                btn_save.triggered.connect(self.open_ncz2_save_editor)
                btn_settings.triggered.connect(self.open_ncz2_settings_editor)
                self.update_ncz2_button_state()

        self.cards.append((card, title_text))

    # ------------------------------------------------------------ Steam games in the library
    def create_steam_card(self, appid, title_text, cover_path):
        card = QFrame(self.grid_container)
        card.setObjectName("GameCard")
        card.setFixedWidth(COVER_W + 18)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 8, 8, 10)
        layout.setSpacing(0)

        cover = QLabel()
        cover.setFixedSize(COVER_W, COVER_H)
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pix = self.get_rounded_pixmap(cover_path, COVER_W, COVER_H, 8) if cover_path and os.path.exists(cover_path) else None
        if pix:
            cover.setPixmap(pix)
        else:
            cover.setObjectName("CoverPlaceholder")
            cover.setText("No\nCover")
        layout.addWidget(cover)
        layout.addSpacing(10)

        title = QLabel(title_text)
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        title.setFixedHeight(38)
        title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(title)

        status = QLabel("Steam game")
        status.setObjectName("CardStatus")
        layout.addWidget(status)
        layout.addSpacing(12)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        btn_play = QPushButton("Install")
        btn_play.setObjectName("Primary")
        btn_play.setFixedHeight(38)
        btn_play.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_play.setToolTip("Install through Steam (Steam must be installed)")
        btn_play.clicked.connect(lambda _c=False, a=appid: QDesktopServices.openUrl(QUrl(f"steam://install/{a}")))
        more_btn = QPushButton("\u2022\u2022\u2022")
        more_btn.setObjectName("MoreButton")
        more_btn.setFixedSize(42, 38)
        more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        menu = QMenu(more_btn)
        more_btn.setMenu(menu)
        menu.addAction("View in Store").triggered.connect(
            lambda _c=False, a=appid, t=title_text, p=cover_path: self.view_steam_game_in_store(a, t, p))
        menu.addSeparator()
        menu.addAction("Remove from Library").triggered.connect(
            lambda _c=False, a=appid: self.remove_steam_game(a))
        actions.addWidget(btn_play, 1)
        actions.addWidget(more_btn)
        layout.addLayout(actions)

        self.steam_cards[appid] = card
        self.cards.append((card, title_text))

    def view_steam_game_in_store(self, appid, title, cover_path):
        self.nav_group.button(4).setChecked(True)
        self.open_store_game(appid, title, cover_path)

    def add_steam_game(self, appid, title, cover_path):
        if appid in self.steam_cards:
            return
        saved_cover = ""
        if cover_path and os.path.exists(cover_path):
            try:
                os.makedirs(get_library_cover_dir(), exist_ok=True)
                saved_cover = os.path.join(get_library_cover_dir(), f"{appid}.img")
                shutil.copyfile(cover_path, saved_cover)
            except Exception:
                saved_cover = cover_path
        games = [g for g in load_steam_library() if g["appid"] != appid]
        games.append({"appid": appid, "title": title, "cover": saved_cover})
        save_steam_library(games)
        self.create_steam_card(appid, title, saved_cover)
        self.apply_filter()
        self.store_detail_page.set_in_library(True)

    def remove_steam_game(self, appid):
        card = self.steam_cards.pop(appid, None)
        if card is not None:
            self.cards = [(c, t) for c, t in self.cards if c is not card]
            card.hide()
            card.setParent(None)
            card.deleteLater()
        save_steam_library([g for g in load_steam_library() if g["appid"] != appid])
        try:
            os.remove(os.path.join(get_library_cover_dir(), f"{appid}.img"))
        except OSError:
            pass
        self.apply_filter()
        if self.store_detail_page._appid == appid:
            self.store_detail_page.set_in_library(False)

    def reload_brand_logo(self):
        """(Re)loads the top-left logo from assets/icon.png, e.g. after the accent recolors it."""
        logo_pix = CreditsPage.circular_pixmap(asset_path("icon.png"), 64)
        self.brand_logo.setVisible(bool(logo_pix))
        if logo_pix:
            logo_pix.setDevicePixelRatio(2.0)
            self.brand_logo.setPixmap(logo_pix)

    def center_on_screen(self):
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

def set_windows_taskbar_identity(window):
    """Windows only: makes the taskbar right-click menu show "NCZ Games Launcher" + its icon instead of "Python".
    Needs pywin32 (pip install pywin32); silently does nothing if it's missing."""
    if not sys.platform.startswith("win"):
        return
    try:
        from win32com.propsys import propsys, pscon
        import pythoncom
        target, args, _workdir = get_launch_command()
        command = subprocess.list2cmdline([target] + list(args))
        icon = prepare_shortcut_icon()
        store = propsys.SHGetPropertyStoreForWindow(int(window.winId()), propsys.IID_IPropertyStore)

        def put(key, text):
            store.SetValue(key, propsys.PROPVARIANTType(text, pythoncom.VT_LPWSTR))

        put(pscon.PKEY_AppUserModel_ID, "NCZ.GamesLauncher")                 # same ID as in main()
        put(pscon.PKEY_AppUserModel_RelaunchCommand, command)                # required, or the two below are ignored
        put(pscon.PKEY_AppUserModel_RelaunchDisplayNameResource, "NCZ Games Launcher")
        if icon:
            put(pscon.PKEY_AppUserModel_RelaunchIconResource, f"{icon},0")
        store.Commit()
    except Exception:
        pass

def main():
    import traceback
    sys.excepthook = lambda *a: traceback.print_exception(*a)  # PyQt6 aborts on unhandled slot errors otherwise
    if sys.platform.startswith("win"):
        # Without its own AppUserModelID, Windows groups the window under python.exe and shows Python's icon.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NCZ.GamesLauncher")
        except Exception:
            pass
    _prefer_xwayland()
    app = QApplication(sys.argv)
    app.setApplicationName("NCZ Games Launcher")
    if "--friends" in sys.argv:          # toggles the friends panel of the running launcher, then exits
        try:
            from PyQt6.QtNetwork import QLocalSocket
            sock = QLocalSocket()
            sock.connectToServer(_instance_name())
            if sock.waitForConnected(800):
                sock.write(b"friends")
                sock.flush()
                sock.waitForBytesWritten(800)
                sock.disconnectFromServer()
        except Exception:
            pass
        sys.exit(0)
    got_lock = _acquire_instance_lock()
    if got_lock and _another_instance_running(wake=False):
        got_lock = False             # an older launcher (one that doesn't use the lock) is already running
    if not got_lock:
        if "--startup" in sys.argv:
            sys.exit(0)  # started at login while one is already up (or starting): leave it alone
        if not _replace_running_instance():     # close the old one and carry on starting this one
            _another_instance_running(wake=True)
            sys.exit(0)  # it wouldn't go away: bring it to the front instead
    _start_instance_server(app)                 # listen right away, not only once the window is built

    icon_path = asset_path("icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    set_accent(load_launcher_settings().get("accent_color", "#a666ff"))
    apply_dark_mode(load_launcher_settings().get("dark_mode", "system"))

    def on_system_color_scheme_changed(_scheme=None):
        if load_launcher_settings().get("dark_mode") == "system":
            apply_dark_mode("system")

    try:
        app.styleHints().colorSchemeChanged.connect(on_system_color_scheme_changed)
    except Exception:
        pass

    window = AdaptiveApp()
    app._ncz_main_window = window
    apply_startup_setting()
    set_windows_taskbar_identity(window)
    tray = getattr(window, "_tray", None)
    if "--startup" in sys.argv and load_launcher_settings().get("start_minimized", True):
        if tray is None:                                # no tray to hide in
            window.showMinimized()
        else:
            window.hide()                               # start quietly in the tray
        if getattr(app, "_ncz_wake_pending", False):    # someone started the launcher again meanwhile
            window.showNormal()
    else:
        window.showMaximized()
    sys.exit(app.exec())
# --- Firebase Steam Link & Downloads Tab Integration ---

class DownloadsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Content")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 28, 36, 0)
        layout.setSpacing(0)

        title = QLabel("Downloads")
        title.setObjectName("PageTitle")
        head_row = QHBoxLayout()
        head_row.addWidget(title, 1)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setFixedHeight(32)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setToolTip("Remove finished and failed downloads (running and paused ones stay)")
        self.clear_btn.clicked.connect(self.clear_finished)
        head_row.addWidget(self.clear_btn)
        layout.addLayout(head_row)
        layout.addSpacing(22)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        self.container = QWidget()
        self.container.setObjectName("GridContainer")
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(0, 0, 12, 24)
        self.container_layout.setSpacing(12)
        self.container_layout.addStretch()

        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self.empty_label = QLabel("No active or recent downloads.")
        self.empty_label.setObjectName("EmptyState")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label)
        self.empty_label.hide()

    def clear_finished(self):
        for i in reversed(range(self.container_layout.count())):
            w = self.container_layout.itemAt(i).widget()
            if isinstance(w, DownloadCard) and getattr(w, "state", "active") != "active":
                self.container_layout.removeWidget(w)
                w.setParent(None)
                w.deleteLater()

    def add_download_card(self, card):
        self.empty_label.hide()
        self.container_layout.insertWidget(self.container_layout.count() - 1, card)

def get_steam_download_link(appid):
    if not FIREBASE_DB_URL:
        return None, 0
    base = FIREBASE_DB_URL.strip().rstrip("/")
    url = f"{base}/steam_links/{appid}.json"
    try:
        data = _http_json(url, "GET")
        if isinstance(data, str) and data.strip():
            return data.strip(), 0
        if isinstance(data, dict):
            link = data.get("link") or data.get("url") or data.get("download_link")
            size_val = data.get("size") or data.get("file_size") or data.get("bytes") or data.get("length") or 0
            try:
                size = int(size_val)
            except (TypeError, ValueError):
                size = 0
            if isinstance(link, str) and link.strip():
                return link.strip(), size
    except Exception:
        pass
    return None, 0

def _change_steam_game_cover(self, appid):
    path, _filter = QFileDialog.getOpenFileName(
        self, "Choose Custom Cover", os.path.expanduser("~"),
        "Images (*.png *.jpg *.jpeg *.bmp *.webp)"
    )
    if not path:
        return
    saved_cover = path
    try:
        os.makedirs(get_library_cover_dir(), exist_ok=True)
        saved_cover = os.path.join(get_library_cover_dir(), f"{appid}.img")
        shutil.copyfile(path, saved_cover)
    except Exception:
        pass
    games = load_steam_library()
    for g in games:
        if g["appid"] == appid:
            g["cover"] = saved_cover
    save_steam_library(games)
    old_card = self.steam_cards.get(appid)
    if old_card:
        title_text = ""
        for card, title in self.cards:
            if card is old_card:
                title_text = title
                break
        _drop_steam_card_widget(self, appid)   # card only: remove_steam_game() also deleted the library entry + the new cover
        self.create_steam_card(appid, title_text, saved_cover)
        self.apply_filter()

AdaptiveApp.change_steam_game_cover = _change_steam_game_cover

_original_init = AdaptiveApp.__init__

def _patched_init(self):
    _original_init(self)
    self.downloads_page = DownloadsPage()
    self.pages.addWidget(self.downloads_page)
    
    downloads_btn = self.make_nav_button("Downloads", checkable=True)
    self.nav_group.addButton(downloads_btn, 6)
    
    sidebar_layout = self.nav_group.button(0).parent().layout()
    sidebar_layout.insertWidget(2, downloads_btn)

AdaptiveApp.__init__ = _patched_init

def sanitize_folder_name(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def _safe_installed_game_dir(title):
    return installed_game_dir(sanitize_folder_name(title))

def find_best_game_exe(root, title):
    if not os.path.isdir(root):
        return None
    exes = []
    for folder, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith('.exe'):
                exes.append(os.path.join(folder, f))
    if not exes:
        return None
    if len(exes) == 1:
        return exes[0]
    
    title_tokens = set(re.findall(r'[a-z0-9]+', title.lower()))
    best_exe = exes[0]
    best_score = -1
    for exe in exes:
        base_name = os.path.splitext(os.path.basename(exe))[0].lower()
        exe_tokens = set(re.findall(r'[a-z0-9]+', base_name))
        score = len(title_tokens.intersection(exe_tokens))
        if base_name in title.lower() or title.lower() in base_name:
            score += 10
        if score > best_score:
            best_score = score
            best_exe = exe
    return best_exe

_original_create_steam_card = AdaptiveApp.create_steam_card

def _patched_create_steam_card(self, appid, title_text, cover_path):
    _original_create_steam_card(self, appid, title_text, cover_path)
    card = self.steam_cards.get(appid)
    if card:
        cover_label = card.findChild(QLabel)
        if cover_label and not cover_label.pixmap():
            cover_label.setObjectName("CoverPlaceholder")
            cover_label.setText("No\nCover")
            cover_label.style().polish(cover_label)

        btn_play = card.findChild(QPushButton, "Primary")
        status_label = card.findChild(QLabel, "CardStatus")
        
        def update_steam_card_state():
            game_dir = _safe_installed_game_dir(title_text)
            exe_path = find_best_game_exe(game_dir, title_text)
            installed = bool(exe_path and os.path.exists(exe_path))
            
            try:
                btn_play.clicked.disconnect()
            except Exception:
                pass

            if installed:
                btn_play.setText("Launch")
                if status_label:
                    status_label.setText("● Installed")
                    status_label.setStyleSheet(f"color: {GREEN};")
                
                def launch_game():
                    if sys.platform.startswith("win"):
                        try:
                            subprocess.Popen([exe_path] + launch_args_for(str(appid)), cwd=os.path.dirname(exe_path))
                        except Exception as e:
                            QMessageBox.warning(self, "Launch Game", f"Couldn't launch game: {e}")
                    elif sys.platform.startswith("linux"):
                        _launch_exe(self, exe_path, load_compat_config().get(str(appid), ""), _d3d_for(str(appid)),
                                    launch_args_for(str(appid)))
                    else:
                        QMessageBox.warning(self, "Platform", "Launching Windows games is not supported on this platform.")
                
                btn_play.clicked.connect(launch_game)
            else:
                btn_play.setText("Install")
                if status_label:
                    status_label.setText("Steam game")
                    status_label.setStyleSheet("")
                
                def on_steam_install(_c=False, a=appid, t=title_text, p=cover_path):
                    link, expected_size = pw_game_url(t), 0  # Playwright finds the real link
                    cancelled, existing = _ask_existing_archive(self, t)
                    if cancelled:
                        return
                    card_widget = DownloadCard(t, link, expected_size, p, self, existing_zip_path=existing)
                    
                    def on_dl_finished(_path):
                        update_steam_card_state()
                    card_widget.worker.finished.connect(on_dl_finished)
                    
                    self.downloads_page.add_download_card(card_widget)
                    self.nav_group.button(6).setChecked(True)
                    self.pages.setCurrentWidget(self.downloads_page)

                btn_play.clicked.connect(on_steam_install)

        update_steam_card_state()

        more_btn = card.findChild(QPushButton, "MoreButton")
        if more_btn and more_btn.menu():
            menu = more_btn.menu()
            menu.addSeparator()
            menu.addAction("Change Cover").triggered.connect(lambda _c=False, a=appid: self.change_steam_game_cover(a))
            
            def uninstall_steam_game():
                game_dir = _safe_installed_game_dir(title_text)
                if os.path.exists(game_dir):
                    shutil.rmtree(game_dir, ignore_errors=True)
                update_steam_card_state()

            menu.addAction("Uninstall").triggered.connect(uninstall_steam_game)
            menu.addAction("Open Game Location").triggered.connect(lambda: self.open_game_location(sanitize_folder_name(title_text)))
            menu.addAction("Install Common Redist").triggered.connect(
                lambda _c=False, t=title_text, a=appid: install_redist_for_game(
                    self, lambda: _safe_installed_game_dir(t), t,
                    lambda: (find_best_game_exe(_safe_installed_game_dir(t), t), load_compat_config().get(str(a), ""))))

            copy_action = menu.addAction("Copy Game ID")
            copy_action.triggered.connect(lambda _c=False, a=appid: QApplication.clipboard().setText(str(a)))

AdaptiveApp.create_steam_card = _patched_create_steam_card

_original_store_card_init = StoreCard.__init__

def _patched_store_card_init(self, appid, title, parent=None):
    _original_store_card_init(self, appid, title, parent)
    
    def mousePressEvent(event):
        if event.button() == Qt.MouseButton.RightButton:
            QApplication.clipboard().setText(str(self.appid))
            QMessageBox.information(self, "Copied", f"Steam App ID ({self.appid}) copied to clipboard!")
            event.accept()
        else:
            QFrame.mousePressEvent(self, event)
            if event.isAccepted() or self.rect().contains(event.position().toPoint()):
                self.clicked.emit(self.appid, self.title, self.cover_path)
            
    self.mousePressEvent = mousePressEvent

StoreCard.__init__ = _patched_store_card_init

_original_get_steam_download_link = get_steam_download_link

def _patched_get_steam_download_link(appid):
    if not FIREBASE_DB_URL:
        return None, 0
    base = FIREBASE_DB_URL.strip().rstrip("/")
    url = f"{base}/steam_links/{appid}.json"
    try:
        data = _http_json(url, "GET")
        if isinstance(data, dict):
            link = data.get("link") or data.get("url") or data.get("download_link")
            cover_url = data.get("cover_url")
            size_val = data.get("size") or data.get("file_size") or data.get("bytes") or data.get("length") or 0
            try:
                size = int(size_val)
            except (TypeError, ValueError):
                size = 0
            
            if cover_url and isinstance(cover_url, str):
                try:
                    req = urllib.request.Request(cover_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        img_data = resp.read()
                    os.makedirs(get_library_cover_dir(), exist_ok=True)
                    local_path = os.path.join(get_library_cover_dir(), f"{appid}.img")
                    with open(local_path, 'wb') as f:
                        f.write(img_data)
                except Exception:
                    pass

            if isinstance(link, str) and link.strip():
                return link.strip(), size
    except Exception:
        pass
    return _original_get_steam_download_link(appid)

get_steam_download_link = _patched_get_steam_download_link

_original_build_library_page = AdaptiveApp.build_library_page

def _patched_build_library_page(self):
    widget = _original_build_library_page(self)
    
    def fetch_missing_covers():
        for game in load_steam_library():
            appid = game["appid"]
            local_cover = os.path.join(get_library_cover_dir(), f"{appid}.img")
            if not os.path.exists(local_cover) and FIREBASE_DB_URL:
                try:
                    base = FIREBASE_DB_URL.strip().rstrip("/")
                    url = f"{base}/steam_links/{appid}.json"
                    data = _http_json(url, "GET")
                    if isinstance(data, dict):
                        cover_url = data.get("cover_url")
                        if cover_url and isinstance(cover_url, str):
                            req = urllib.request.Request(cover_url, headers={'User-Agent': 'Mozilla/5.0'})
                            with urllib.request.urlopen(req, timeout=10) as resp:
                                img_data = resp.read()
                            os.makedirs(get_library_cover_dir(), exist_ok=True)
                            with open(local_cover, 'wb') as f:
                                f.write(img_data)
                except Exception:
                    pass

    threading.Thread(target=fetch_missing_covers, daemon=True).start()
    return widget

AdaptiveApp.build_library_page = _patched_build_library_page

COMPAT_CONFIG_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "steam_compat_config.json"))

def load_compat_config():
    if os.path.exists(COMPAT_CONFIG_FILE):
        try:
            with open(COMPAT_CONFIG_FILE) as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}

def save_compat_config(config):
    try:
        with open(COMPAT_CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass

def get_available_compatibility_tools():
    tools = {}
    base_paths = [
        os.path.expanduser("~/.local/share/Steam/compatibilitytools.d"),
        os.path.expanduser("~/.steam/root/compatibilitytools.d"),
        os.path.expanduser("~/.local/share/Steam/steamapps/common"),
        os.path.expanduser("~/.steam/steam/steamapps/common")
    ]
    for base in base_paths:
        if os.path.exists(base):
            try:
                for entry in os.listdir(base):
                    full_path = os.path.join(base, entry)
                    if os.path.isdir(full_path):
                        proton_bin = os.path.join(full_path, "proton")
                        if os.path.exists(proton_bin):
                            tools[entry] = proton_bin
            except Exception:
                pass
    for name, wine_bin in wine_installed_builds().items():
        tools[f"{name} (Wine)"] = wine_bin
    return tools

class GameDownloadWorker(QThread):
    progress = pyqtSignal(object, object, float)
    status_update = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, game_id, title, version_url, linux_cmd, existing_zip_path=None, wine_extract_dir=None, download_only=False):
        super().__init__()
        self.game_id = game_id
        self.title = title
        self.version_url = version_url
        self.linux_cmd = linux_cmd
        self.existing_zip_path = existing_zip_path
        self.wine_extract_dir = wine_extract_dir
        self.download_only = download_only
        self.remote_version = ""
        self._is_cancelled = False
        self._response = None

    def cancel(self):
        self._is_cancelled = True
        if self._response:
            try:
                self._response.close()
            except Exception:
                pass

    # run() is _new_game_worker_run, assigned further down


class DownloadCard(QFrame):
    CARD_W, CARD_H = 60, 90

    def __init__(self, title, download_url, expected_size=0, cover_path="", parent=None, worker=None, existing_zip_path=None):
        super().__init__(parent)
        self.setObjectName("Panel")
        self.setFixedHeight(120)
        self.title = title
        
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(14, 12, 14, 12)
        main_layout.setSpacing(14)

        self.cover = QLabel()
        self.cover.setObjectName("CoverPlaceholder")
        self.cover.setFixedSize(self.CARD_W, self.CARD_H)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if cover_path and os.path.exists(cover_path):
            pix = rounded_cover_pixmap(cover_path, self.CARD_W, self.CARD_H, 6)
            if pix:
                self.cover.setPixmap(pix)
            else:
                self.cover.setText("No\nCover")
        else:
            self.cover.setText("No\nCover")
        main_layout.addWidget(self.cover)

        layout = QVBoxLayout()
        layout.setSpacing(6)

        top_row = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setObjectName("RowTitle")
        self.status_label = QLabel("Connecting...")
        self.status_label.setObjectName("RowDesc")
        top_row.addWidget(self.title_label, 1)
        top_row.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(top_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)

        bottom_row = QHBoxLayout()
        self.info_label = QLabel("Speed: 0 KB/s | 0 MB / 0 MB")
        self.info_label.setObjectName("RowDesc")
        bottom_row.addWidget(self.info_label, 1)
        
        self.state = "active"   # active (incl. paused) / done / failed
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setFixedSize(80, 28)
        self.pause_btn.clicked.connect(self.toggle_pause)
        bottom_row.addWidget(self.pause_btn)

        self.action_btn = QPushButton("Cancel")
        self.action_btn.setFixedSize(80, 28)
        self.action_btn.clicked.connect(self.cancel_download)
        bottom_row.addWidget(self.action_btn)
        layout.addLayout(bottom_row)

        main_layout.addLayout(layout, 1)

        if worker is not None:
            self.worker = worker
        else:
            self.worker = FirebaseDownloadWorker(title, download_url, expected_size, existing_zip_path=existing_zip_path)
            
        if not hasattr(self.worker, "pause"):
            self.pause_btn.hide()
        self.worker.progress.connect(self.on_progress)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def toggle_pause(self):
        if not hasattr(self.worker, "pause"):
            return
        if getattr(self.worker, "_paused", False):
            self.worker.resume()
            self.pause_btn.setText("Pause")
        else:
            self.worker.pause()
            self.pause_btn.setText("Resume")
            self.status_label.setText("Paused")

    def cancel_download(self):
        if hasattr(self.worker, "cancel"):
            self.worker.cancel()
        file_name = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', self.title)}.zip"
        dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except Exception:
                pass
        self.setParent(None)
        self.deleteLater()

    def on_status_update(self, message):
        self.status_label.setText(message)

    def on_progress(self, downloaded, total, speed):
        dl_mb = downloaded / (1024 * 1024)
        speed_str = f"{speed / 1024:.1f} KB/s" if speed < 1024 * 1024 else f"{speed / (1024 * 1024):.2f} MB/s"

        if total > 0:
            percent = int((downloaded / total) * 100)
            total_mb = total / (1024 * 1024)
            self.progress_bar.setValue(percent)
            self.status_label.setText(f"{percent}%")
            self.info_label.setText(f"Speed: {speed_str} | {_fmt_size(downloaded)} / {_fmt_size(total)}")
        else:
            self.progress_bar.setRange(0, 0)
            self.status_label.setText("Downloading...")
            self.info_label.setText(f"Speed: {speed_str} | Downloaded: {_fmt_size(downloaded)}")

    def on_finished(self, zip_path):
        self.state = "done"
        self.pause_btn.hide()
        self.status_label.setText("Installed")
        self.status_label.setStyleSheet(f"color: {GREEN};")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.action_btn.setText("Open Folder")
        try:
            self.action_btn.clicked.disconnect()
        except Exception:
            pass
        self.action_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(GAMES_DIR)))

    def on_failed(self, error_msg):
        if error_msg == "CANCELLED":
            self.setParent(None)
            self.deleteLater()
            return
        self.state = "failed"
        self.pause_btn.hide()
        self.status_label.setText("Game is unreleased" if error_msg.strip() == "Game is unreleased" else "Failed")
        self.status_label.setStyleSheet(f"color: {RED};")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.action_btn.setText("Copy Error")
        try:
            self.action_btn.clicked.disconnect()
        except Exception:
            pass
        self.action_btn.clicked.connect(lambda: QApplication.clipboard().setText(error_msg))

class GameDownloadCard(DownloadCard):
    def __init__(self, game_id, title, version_url, linux_cmd, expected_size=0, cover_path="", parent=None, existing_zip_path=None, wine_extract_dir=None):
        worker = GameDownloadWorker(game_id, title, version_url, linux_cmd, existing_zip_path, wine_extract_dir)
        super().__init__(title, "", expected_size, cover_path, parent, worker=worker)

def get_custom_games_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "custom_games.json")

def load_custom_games():
    try:
        with open(get_custom_games_path(), encoding="utf-8") as f:
            data = json.load(f)
        return [g for g in data if isinstance(g, dict) and g.get("exe_path")]
    except Exception:
        return []

def save_custom_games(games):
    path = get_custom_games_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(games, f, indent=4)

class AddCustomGameDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Custom Game")
        self.setFixedSize(420, 320)
        self.result_data = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        self.title_edit = QLineEdit()
        form.addRow(QLabel("Game Title"), self.title_edit)

        exe_layout = QHBoxLayout()
        self.exe_edit = QLineEdit()
        exe_btn = QPushButton("Browse...")
        exe_btn.clicked.connect(self.browse_exe)
        exe_layout.addWidget(self.exe_edit, 1)
        exe_layout.addWidget(exe_btn)
        form.addRow(QLabel("Executable"), exe_layout)

        cover_layout = QHBoxLayout()
        self.cover_edit = QLineEdit()
        cover_btn = QPushButton("Browse...")
        cover_btn.clicked.connect(self.browse_cover)
        cover_layout.addWidget(self.cover_edit, 1)
        cover_layout.addWidget(cover_btn)
        form.addRow(QLabel("Cover Image"), cover_layout)

        if sys.platform.startswith("linux"):
            self.compat_combo = QComboBox()
            self.compat_combo.addItem("Default (System Wine / Native)", "")
            tools = get_available_compatibility_tools()
            for name, path in sorted(tools.items()):
                self.compat_combo.addItem(name, path)
            form.addRow(QLabel("Compatibility Tool"), self.compat_combo)
        else:
            self.compat_combo = None

        layout.addLayout(form)

        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("Add Game")
        btn_ok.setObjectName("Primary")
        btn_cancel = QPushButton("Cancel")
        btn_ok.clicked.connect(self.accept_data)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_ok)
        layout.addLayout(btn_layout)

    def browse_exe(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Executable", os.path.expanduser("~"))
        if path:
            self.exe_edit.setText(path)
            if not self.title_edit.text().strip():
                base = os.path.splitext(os.path.basename(path))[0]
                self.title_edit.setText(base.replace("_", " ").title())

    def browse_cover(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Cover Image", os.path.expanduser("~"), "Images (*.png *.jpg *.jpeg *.webp)")
        if path:
            self.cover_edit.setText(path)

    def accept_data(self):
        title = self.title_edit.text().strip()
        exe = self.exe_edit.text().strip()
        if not title or not exe or not os.path.exists(exe):
            QMessageBox.warning(self, "Invalid Input", "Please provide a valid game title and existing executable path.")
            return
        cover = self.cover_edit.text().strip()
        compat = self.compat_combo.currentData() if self.compat_combo else ""
        self.result_data = {
            "id": f"custom_{int(time.time())}",
            "title": title,
            "exe_path": exe,
            "cover_path": cover,
            "compat_tool": compat
        }
        self.accept()

def _open_add_custom_game_dialog(self):
    dlg = AddCustomGameDialog(self)
    if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_data:
        data = dlg.result_data
        saved_cover = data["cover_path"]
        if saved_cover and os.path.exists(saved_cover):
            try:
                os.makedirs(get_library_cover_dir(), exist_ok=True)
                dest_cover = os.path.join(get_library_cover_dir(), f"{data['id']}.img")
                shutil.copyfile(saved_cover, dest_cover)
                data["cover_path"] = dest_cover
            except Exception:
                pass
        games = load_custom_games()
        games.append(data)
        save_custom_games(games)
        self.create_custom_card(data["id"], data["title"], data["exe_path"], data["cover_path"], data["compat_tool"])
        self.apply_filter()

AdaptiveApp.open_add_custom_game_dialog = _open_add_custom_game_dialog

def _custom_tool(game_id, fallback=""):
    """The compatibility tool currently saved for a custom game (it can change after the card was built)."""
    for g in load_custom_games():
        if g.get("id") == game_id:
            return g.get("compat_tool", "")
    return fallback

def _patched_create_custom_card(self, game_id, title_text, exe_path, cover_path, compat_tool):
    card = QFrame(self.grid_container)
    card.setObjectName("GameCard")
    card.setFixedWidth(COVER_W + 18)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(8, 8, 8, 10)
    layout.setSpacing(0)

    cover = QLabel()
    cover.setFixedSize(COVER_W, COVER_H)
    cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
    pix = self.get_rounded_pixmap(cover_path, COVER_W, COVER_H, 8) if cover_path and os.path.exists(cover_path) else None
    if pix:
        cover.setPixmap(pix)
    else:
        cover.setObjectName("CoverPlaceholder")
        cover.setText("No\nCover")
    layout.addWidget(cover)
    layout.addSpacing(10)

    title = QLabel(title_text)
    title.setObjectName("CardTitle")
    title.setWordWrap(True)
    title.setFixedHeight(38)
    title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    layout.addWidget(title)

    status = QLabel("Custom game")
    status.setObjectName("CardStatus")
    layout.addWidget(status)
    layout.addSpacing(12)

    actions = QHBoxLayout()
    actions.setSpacing(6)
    btn_play = QPushButton("Launch")
    btn_play.setObjectName("Primary")
    btn_play.setFixedHeight(38)
    btn_play.setCursor(Qt.CursorShape.PointingHandCursor)

    def launch_custom():
        if not os.path.exists(exe_path):
            QMessageBox.warning(self, "Launch Error", f"Executable not found:\n{exe_path}")
            return
        if sys.platform.startswith("win"):
            try:
                subprocess.Popen([exe_path] + launch_args_for(f"custom-{game_id}"), cwd=os.path.dirname(exe_path))
            except Exception as e:
                QMessageBox.warning(self, "Launch Game", f"Couldn't launch game: {e}")
        elif sys.platform.startswith("linux"):
            _launch_exe(self, exe_path, _custom_tool(game_id, compat_tool), _d3d_for(f"custom-{game_id}"),
                        launch_args_for(f"custom-{game_id}"))

    btn_play.clicked.connect(launch_custom)

    more_btn = QPushButton("\u2022\u2022\u2022")
    more_btn.setObjectName("MoreButton")
    more_btn.setFixedSize(42, 38)
    more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    menu = QMenu(more_btn)
    more_btn.setMenu(menu)

    def remove_custom_game():
        games = [g for g in load_custom_games() if g.get("id") != game_id]
        save_custom_games(games)
        card.hide()
        card.setParent(None)
        card.deleteLater()
        self.cards = [(c, t) for c, t in self.cards if c is not card]
        self.apply_filter()

    def switch_custom_tool():
        games = load_custom_games()
        entry = next((g for g in games if g.get("id") == game_id), None)
        if entry is None:
            return
        picked = prompt_compat_tool(self, entry.get("compat_tool", ""), _d3d_for(f"custom-{game_id}"))
        if picked is None:
            return
        entry["compat_tool"], d3d = picked
        save_custom_games(games)
        save_game_d3d(f"custom-{game_id}", d3d)
        QMessageBox.information(self, "Compatibility Tool", "Compatibility settings updated for this game.")

    if sys.platform.startswith("linux"):
        menu.addAction("Switch Compatibility Tool").triggered.connect(switch_custom_tool)
    menu.addAction("Remove from Library").triggered.connect(remove_custom_game)
    menu.addAction("Open Game Location").triggered.connect(lambda: self.open_game_location(os.path.dirname(exe_path)))
    menu.addAction("Install Common Redist").triggered.connect(
        lambda _c=False: install_redist_for_game(
            self, lambda: os.path.dirname(exe_path), os.path.basename(os.path.dirname(exe_path)) or "this game",
            lambda: (exe_path, _custom_tool(game_id, compat_tool))))

    actions.addWidget(btn_play, 1)
    actions.addWidget(more_btn)
    layout.addLayout(actions)

    if not hasattr(self, "custom_cards"):
        self.custom_cards = {}
    self.custom_cards[game_id] = card
    self.cards.append((card, title_text))

AdaptiveApp.create_custom_card = _patched_create_custom_card

class CompatToolDialog(QDialog):
    def __init__(self, current_tool, tools_dict, parent=None, current_d3d=""):
        super().__init__(parent)
        self.setWindowTitle("Switch Compatibility Tool")
        self.setFixedSize(460, 230)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        
        layout.addWidget(QLabel("Select Proton / Wine Tool:"))
        
        self.combo = QComboBox()
        self.combo.addItem("Default (System Wine / Auto)", "")
        
        self.tools_map = tools_dict
        sorted_tools = sorted(tools_dict.keys())
        
        idx = 0
        for i, name in enumerate(sorted_tools):
            self.combo.addItem(name, tools_dict[name])
            if tools_dict[name] == current_tool:
                idx = i + 1
        self.combo.setCurrentIndex(idx)
        layout.addWidget(self.combo)

        layout.addWidget(QLabel("Direct3D renderer (Wine / Proton):"))
        self.d3d_combo = QComboBox()
        self.d3d_combo.addItem("Launcher setting", "")
        for mode, label in WINE_D3D_MODES.items():
            if mode != "default":
                self.d3d_combo.addItem(label, mode)
        self.d3d_combo.setCurrentIndex(max(0, self.d3d_combo.findData(current_d3d)))
        layout.addWidget(self.d3d_combo)
        
        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("OK")
        btn_ok.setObjectName("Primary")
        btn_cancel = QPushButton("Cancel")
        btn_ok.clicked.connect(self.accept)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_ok)
        layout.addLayout(btn_layout)

    def get_selected(self):
        return self.combo.currentData()

    def get_d3d(self):
        return self.d3d_combo.currentData() or ""

def prompt_compat_tool(app, current="", current_d3d=""):
    """Asks which compatibility tool and Direct3D renderer to use.
    Returns (tool path or '' for default, renderer or '' for the launcher setting), or None if cancelled."""
    dlg = CompatToolDialog(current, get_available_compatibility_tools(), app, current_d3d)
    if dlg.exec() != int(QDialog.DialogCode.Accepted):
        return None
    return dlg.get_selected() or "", dlg.get_d3d()

def save_game_d3d(key, mode):
    config = load_compat_config()
    if mode:
        config["d3d:" + key] = mode
    else:
        config.pop("d3d:" + key, None)
    save_compat_config(config)

def find_existing_archive(prefixes):
    if not os.path.isdir(DOWNLOAD_DIR):
        return None
    for f in sorted(os.listdir(DOWNLOAD_DIR)):
        if f.lower().endswith((".zip", ".rar")) and f.lower().startswith(prefixes):
            return os.path.join(DOWNLOAD_DIR, f)
    return None

# ---------------------------------------------------------------- finding archive tools (7-Zip / WinRAR aren't on PATH on Windows)
def find_tool(*names):
    """Full path of the first tool found: on PATH, next to the launcher, or in the usual Windows install folders."""
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    folders = []
    for getter in (_bundled_dir, _writable_dir):
        try:
            folders.append(getter())
        except Exception:
            pass
    folders += [ASSETS_DIR, os.path.join(SCRIPT_DIR, "tools")]
    if sys.platform.startswith("win"):
        roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(os.path.join(local, "Programs"))
        for root in filter(None, roots):
            for sub in ("7-Zip", "7-Zip-Zstandard", "WinRAR", "NanaZip", "PeaZip", os.path.join("PeaZip", "res", "bin", "7z")):
                folders.append(os.path.join(root, sub))
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for key in (r"SOFTWARE\7-Zip", r"SOFTWARE\WOW6432Node\7-Zip", r"SOFTWARE\WinRAR", r"SOFTWARE\WOW6432Node\WinRAR"):
                    for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0)):
                        try:
                            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as k:
                                for val in ("Path64", "Path", "exe64", "exe32"):
                                    try:
                                        v = winreg.QueryValueEx(k, val)[0]
                                    except OSError:
                                        continue
                                    folders.append(v if os.path.isdir(v) else os.path.dirname(v))
                        except OSError:
                            pass
        except ImportError:
            pass
    for folder in folders:
        for n in names:
            for cand in (n, n + ".exe"):
                path = os.path.join(folder, cand)
                if os.path.isfile(path):
                    return path
    return None

def _extract_with_tools(path, out_dir, rar=True):
    """Tries every extractor we can find. Returns True on success."""
    os.makedirs(out_dir, exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    attempts = []
    for exe in filter(None, [find_tool("7z", "7za", "7zz", "7zr")]):
        attempts.append([exe, "x", "-y", f"-o{out_dir}", path])
    if rar:
        unrar = find_tool("unrar", "UnRAR")
        if unrar:
            attempts.append([unrar, "x", "-y", path, out_dir + os.sep])
        winrar = find_tool("WinRAR")
        if winrar:
            attempts.append([winrar, "x", "-y", "-ibck", path, out_dir + os.sep])
    unar = find_tool("unar")
    if unar:
        attempts.append([unar, "-f", "-o", out_dir, path])
    tar = find_tool("bsdtar", "tar")   # Windows 10+ ships a tar.exe that can read rar and 7z
    if tar:
        attempts.append([tar, "-xf", path, "-C", out_dir])
    for cmd in attempts:
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=3600, creationflags=flags)
            if res.returncode == 0:
                return True
        except Exception:
            continue
    return False

def extract_archive(dest_path, extract_dir):
    os.makedirs(extract_dir, exist_ok=True)
    if zipfile.is_zipfile(dest_path):
        with zipfile.ZipFile(dest_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        return

    is_rar = dest_path.lower().endswith('.rar')
    if not is_rar:
        try:
            with open(dest_path, 'rb') as f:
                if f.read(7).startswith(b'Rar!\x1a\x07'):
                    is_rar = True
        except Exception:
            pass

    if is_rar:
        if _extract_with_tools(dest_path, extract_dir):
            return
        try:  # last resort: the rarfile package, pointed at whatever extractor we found
            import rarfile
            for attr, names in (("UNRAR_TOOL", ("unrar", "UnRAR")), ("SEVENZIP_TOOL", ("7z", "7za", "7zz")),
                                ("UNAR_TOOL", ("unar",)), ("BSDTAR_TOOL", ("bsdtar", "tar"))):
                found = find_tool(*names)
                if found and hasattr(rarfile, attr):
                    setattr(rarfile, attr, found)
            with rarfile.RarFile(dest_path) as rf:
                rf.extractall(extract_dir)
            return
        except Exception:
            pass
        if sys.platform.startswith("win"):
            hint = "Install 7-Zip (7-zip.org) or WinRAR, then try again. If it's already installed, reinstall it so it's registered."
        else:
            hint = "Install 7-Zip (p7zip) or unrar."
        raise ValueError("RAR archive detected, but no extraction tool could open it. " + hint)

    raise ValueError("Downloaded file is neither a valid ZIP nor a supported RAR archive.")

# --- Update install triggers to check for existing .rar archives ---
def _patched_start_ae_install(self):
    existing_archive = find_existing_archive(("five nights at ncz ae", "fnancz_ae", "fnanczae"))
    version_url = GAME_INFO["ae"]["version_url"]
    linux_cmd = GAME_INFO["ae"]["linux_cmd"]
    cover_path = asset_path("fnanczaecover.png")

    existing_zip_path = None
    if existing_archive:
        file_name = os.path.basename(existing_archive)
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = existing_archive
            elif dlg.choice == "overwrite":
                try:
                    os.remove(existing_archive)
                except Exception:
                    pass
        else:
            return

    card_widget = GameDownloadCard("ae", AE_GAME_NAME, version_url, linux_cmd, 0, cover_path, self, existing_zip_path=existing_zip_path)
    
    def on_dl_finished(_path):
        self.update_ae_button_state()
    card_widget.worker.finished.connect(on_dl_finished)
    
    self.downloads_page.add_download_card(card_widget)
    self.nav_group.button(6).setChecked(True)
    self.pages.setCurrentWidget(self.downloads_page)

AdaptiveApp.start_ae_install = _patched_start_ae_install

def _patched_start_ncz2_install(self):
    existing_archive = find_existing_archive(("five nights at ncz 2", "fnancz_2", "fnancz 2", "fnancz2"))
    version_url = GAME_INFO["ncz2"]["version_url"]
    linux_cmd = GAME_INFO["ncz2"]["linux_cmd"]
    cover_path = asset_path("fnancz2cover.png")

    existing_zip_path = None
    if existing_archive:
        file_name = os.path.basename(existing_archive)
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = existing_archive
            elif dlg.choice == "overwrite":
                try:
                    os.remove(existing_archive)
                except Exception:
                    pass
        else:
            return

    card_widget = GameDownloadCard("ncz2", NCZ2_GAME_NAME, version_url, linux_cmd, 0, cover_path, self, existing_zip_path=existing_zip_path)
    
    def on_dl_finished(_path):
        self.update_ncz2_button_state()
    card_widget.worker.finished.connect(on_dl_finished)
    
    self.downloads_page.add_download_card(card_widget)
    self.nav_group.button(6).setChecked(True)
    self.pages.setCurrentWidget(self.downloads_page)

AdaptiveApp.start_ncz2_install = _patched_start_ncz2_install

class FirebaseDownloadWorker(QThread):
    progress = pyqtSignal(object, object, float)
    status_update = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, title, download_url, expected_size=0, existing_zip_path=None):
        super().__init__()
        self.title = title
        self.download_url = download_url
        self.expected_size = expected_size
        self.existing_zip_path = existing_zip_path
        self._is_cancelled = False
        self._response = None

    def cancel(self):
        self._is_cancelled = True
        if self._response:
            try:
                self._response.close()
            except Exception:
                pass

    # run() is assigned further down (FirebaseDownloadWorker.run = ...)


def safe_filename_from_headers(response, fallback_title, ext=".zip"):
    cd = response.headers.get('Content-Disposition')
    raw_name = ""
    if cd:
        for part in cd.split(';'):
            part = part.strip()
            if part.lower().startswith('filename='):
                raw_name = part.split('=', 1)[1].strip()
                if raw_name.startswith('"') and raw_name.endswith('"'):
                    raw_name = raw_name[1:-1]
                elif raw_name.startswith("'") and raw_name.endswith("'"):
                    raw_name = raw_name[1:-1]
                break
    
    if not raw_name:
        url_path = urllib.parse.unquote(response.geturl().split('?')[0])
        raw_name = os.path.basename(url_path)
    
    if not raw_name or not raw_name.lower().endswith(('.zip', '.rar')):
        safe_title_clean = re.sub(r'[<>:"/\\|?*]', '_', fallback_title)
        raw_name = f"{safe_title_clean}{ext}"
    
    # Final cleanup of illegal Windows characters
    return re.sub(r'[<>:"/\\|?*]', '_', raw_name).strip()

# Let's cleanly patch FirebaseDownloadWorker.run to use safe_filename_from_headers
def _new_fb_run(self):
    dest_path = None
    try:
        total_size = self.expected_size
        if total_size <= 0 and not self._is_cancelled:
            try:
                head_req = urllib.request.Request(self.download_url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(head_req, timeout=10) as head_resp:
                    total_size = int(head_resp.headers.get('Content-Length', 0))
            except Exception:
                pass

        if self._is_cancelled:
            self.failed.emit("CANCELLED")
            return

        ext = ".rar" if self.download_url.lower().endswith('.rar') else ".zip"
        headers = {'User-Agent': 'Mozilla/5.0'}
        dest_path = resumable_download(
            self, lambda: (self.download_url, headers),
            lambda response, url: safe_filename_from_headers(response, self.title, ext), total_size)

        if self._is_cancelled:
            if dest_path and os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except Exception:
                    pass
            self.failed.emit("CANCELLED")
            return

        self.status_update.emit("Extracting...")
        safe_title = sanitize_folder_name(self.title)
        extract_dir = installed_game_dir(safe_title)
        extract_archive(dest_path, extract_dir)

        if self._is_cancelled:
            self.failed.emit("CANCELLED")
            return

        self.finished.emit(dest_path)
    except Exception as e:
        if self._is_cancelled:
            self.failed.emit("CANCELLED")
        else:
            self.failed.emit(str(e))

FirebaseDownloadWorker.run = _new_fb_run

# --- Firebase games: find the real file link with a hidden Playwright browser, then download it normally ---
# Every game except Five Nights at NCZ / NCZFront opens PW_BASE_URL + game-slug (e.g. .../dying-light-the-beast).
# Playwright clicks through to the file host, copies the final download link (plus the cookies the host needs),
# cancels the browser's own download, and the launcher's normal downloader fetches that link.
PW_BASE_URL = "https://steamrip.com/"                   # <-- change this
PW_BUTTON_TEXT = "DOWNLOAD HERE"                       # button on the game page
PW_BLOCKED_HOSTS = ("megadb.net",)           # buttons leading here are skipped for the next DOWNLOAD HERE button
PW_RELAY_HOSTS = ("filecrypt.cc",)                     # link-container sites: you solve the captcha in the browser window
PW_RELAY_BUTTONS = 'button.download, a.download, [onclick*="openLink"]'  # link buttons on the container page
PW_ALLOWED_HOSTS = ("gofile.io", "bzzhr.to", "pixeldrain.com")           # file hosts; PW_BASE_URL's site is allowed too, anything else is closed
PW_HEADLESS = True                                     # True hides the browser; it only pops up while you solve a captcha
PW_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/124.0.0.0 Safari/537.36")

_fb_direct_run = FirebaseDownloadWorker.run  # previous behaviour, still used for the NCZ games

# Games whose page on PW_BASE_URL doesn't match their title: (regex on the lowercase title, slug to use instead).
PW_SLUG_OVERRIDES = (
    (r"dying light (2|two)\b.*", "dying-l-two-stay-human"),                    # any edition of Dying Light 2
    (r"dying light( the following)?( enhanced edition)?", "dying-light-the-following"),
    (r"risk of rain (2|two)\b.*", "risk-of-rain-two"),
)

def pw_game_url(title):
    t = title.lower().replace("'", "")
    plain = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t)).strip()
    for pattern, slug in PW_SLUG_OVERRIDES:
        if re.fullmatch(pattern, plain):
            return PW_BASE_URL.rstrip("/") + "/" + slug
    slug = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return PW_BASE_URL.rstrip("/") + "/" + slug

# Describes the block around a DOWNLOAD HERE button (its label, logo, link...): which host does it mention?
PW_LABEL_JS = r"""el => {
    const hits = n => ((n.innerText || "").toLowerCase().split("download here").length - 1);
    let node = el;
    for (let i = 0; i < 4; i++) {
        const p = node.parentElement;
        if (!p || p === document.body || hits(p) > 1) break;
        node = p;
    }
    const h = node.outerHTML;
    return {mega: /mega\s*-?db/i.test(h), bzzhr: /bzzhr/i.test(h), gofile: /go\s*-?file/i.test(h), pixeldrain: /pixel\s*-?drain/i.test(h)};
}"""

def _pw_host_ok(url, hosts):
    if not url or url.startswith(("about:", "blob:", "data:")):
        return True
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts if h)

def _pw_on(url, hosts):
    """Strict version of _pw_host_ok: the page really is on one of these hosts (about:blank doesn't count)."""
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts if h)

def _pw_captcha_shown(pg):
    loc = pg.locator('iframe[src*="captcha"], .captcha, #captcha, [class*="captcha"], input[name*="captcha"]')
    try:
        return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 10)))
    except Exception:
        return False

def _pw_wait_captcha(self, pg):
    self.status_update.emit("Solve the captcha in the browser window...")
    try:
        pg.bring_to_front()
    except Exception:
        pass
    deadline = time.time() + 600
    while _pw_captcha_shown(pg):
        if self._is_cancelled:
            raise RuntimeError("CANCELLED")
        if time.time() > deadline:
            raise RuntimeError("The captcha wasn't solved in time.")
        pg.wait_for_timeout(1000)
    pg.wait_for_timeout(1500)

def _pw_pick_link(self, context, tab):
    """Presses the Download buttons on a container page until one reaches a file host; returns that tab."""
    self.status_update.emit("Pressing Download...")
    btns = tab.locator(PW_RELAY_BUTTONS)
    if btns.count() == 0:  # fall back to anything that just says "Download"
        btns = tab.get_by_text(re.compile(r"^\s*download\s*$", re.IGNORECASE))
    for i in range(btns.count()):
        try:
            with context.expect_page(timeout=10000) as info:
                btns.nth(i).click()
            new = info.value
            end = time.time() + 20
            while time.time() < end and not new.is_closed():
                if _pw_on(new.url, PW_ALLOWED_HOSTS):
                    return new
                new.wait_for_timeout(500)
        except Exception:
            tab.wait_for_timeout(500)
    return None

def _pw_through_relay(self, context, tab, p):
    """Container page (filecrypt.cc): you solve the captcha, Download is pressed on that same page, and the
    file-host link it opens is used. With PW_HEADLESS the captcha window is a temporary visible browser; once
    the link is found it closes and the hidden browser carries on from there."""
    tab.wait_for_timeout(3000)  # let the captcha load
    if not _pw_captcha_shown(tab):
        return _pw_pick_link(self, context, tab)
    if not PW_HEADLESS:
        _pw_wait_captcha(self, tab)
        return _pw_pick_link(self, context, tab)
    vb = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
    try:
        vctx = vb.new_context(user_agent=PW_USER_AGENT)
        vctx.add_cookies(context.cookies())
        vtab = vctx.new_page()
        _pw_guard(vctx, vtab)
        vtab.goto(tab.url, timeout=60000)
        vtab.wait_for_timeout(3000)
        if _pw_captcha_shown(vtab):
            _pw_wait_captcha(self, vtab)
        found = _pw_pick_link(self, vctx, vtab)
        if not found:
            return None
        final_url = found.url
        context.add_cookies(vctx.cookies())
    finally:
        vb.close()
    self.status_update.emit("Captcha solved, continuing in the background...")
    page = context.new_page()
    page.goto(final_url, timeout=60000)
    return page

def _pw_guard(context, page):
    """Watches every tab: a popup that lands anywhere unexpected is closed, the main tab is sent back."""
    allowed = (urllib.parse.urlparse(PW_BASE_URL).hostname,) + tuple(PW_ALLOWED_HOSTS) + tuple(PW_RELAY_HOSTS)
    state = {"closed": 0, "blocked": [], "allow": set()}  # extra tabs closed so far, the blocked URLs, hosts allowed later
    def ok(url):
        return _pw_host_ok(url, allowed + tuple(state["allow"]))
    def watch(pg):
        if pg is not page:
            pg.on("close", lambda _p=None: state.__setitem__("closed", state["closed"] + 1))
        def on_nav(frame):
            if frame != pg.main_frame or ok(frame.url):
                return
            state["blocked"].append(frame.url)
            try:
                if pg is page:
                    pg.go_back()
                else:
                    pg.close()
            except Exception:
                pass
        pg.on("framenavigated", on_nav)
        if pg is not page and not ok(pg.url):  # popup that was already on a bad site
            state["blocked"].append(pg.url)
            try:
                pg.close()
            except Exception:
                pass
    watch(page)
    context.on("page", watch)
    return state

PW_MEGADB = ("megadb.net",)

def _pw_find_visible(pg, rx):
    """First visible button / link / plain text on the page (any frame) whose text matches rx, else None."""
    for fr in pg.frames:
        for loc in (fr.get_by_role("button", name=rx), fr.get_by_role("link", name=rx), fr.get_by_text(rx)):
            try:
                for i in range(min(loc.count(), 10)):
                    if loc.nth(i).is_visible():
                        return loc.nth(i)
            except Exception:
                continue
    return None

def _pw_visible_labels(pg):
    out = []
    try:
        loc = pg.locator("button, a, input[type=submit], input[type=button]")
        for i in range(min(loc.count(), 40)):
            el = loc.nth(i)
            if el.is_visible():
                t = (el.inner_text() or el.get_attribute("value") or "").strip().replace("\n", " ")
                if t:
                    out.append(t[:30])
    except Exception:
        pass
    return out[:10]

def _pw_press(el):
    try:
        el.click(timeout=5000)
    except Exception:
        try:
            el.click(timeout=3000, force=True)   # something (an ad overlay) is on top of it
        except Exception:
            el.evaluate("e => e.click()")

def _pw_megadb_button(self, pg, guard):
    """MegaDB page: press DOWNLOAD, then return the DOWNLOAD NOW button once it shows up."""
    first_rx = re.compile(r"^\s*(free\s+)?download\s*$", re.IGNORECASE)
    now_rx = re.compile(r"^\s*(free\s+)?download\s*now\s*$", re.IGNORECASE)
    self.status_update.emit("MegaDB: looking for the DOWNLOAD button...")
    deadline = time.time() + 60
    pressed, seen_closed = False, guard["closed"]
    while True:
        if self._is_cancelled:
            raise RuntimeError("CANCELLED")
        now = _pw_find_visible(pg, now_rx)
        if now is not None:
            return now
        if time.time() > deadline:
            raise RuntimeError(f"Couldn't find the MegaDB download button on {pg.url}. "
                               f"Buttons on the page: {', '.join(_pw_visible_labels(pg)) or 'none'}")
        if not pressed or guard["closed"] > seen_closed:  # first time, or an ad tab ate the click
            seen_closed = guard["closed"]
            first = _pw_find_visible(pg, first_rx)
            if first is not None:
                self.status_update.emit("MegaDB: pressing DOWNLOAD...")
                _pw_press(first)
                pressed = True
        pg.wait_for_timeout(500)

def _pw_is_ncz(title):
    t = title.lower()
    return "five nights at ncz" in t or "nczfront" in t

def _pw_find_link(self, start_url):
    """Returns (download_url, headers) copied from the browser."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright isn't installed. Run: pip install playwright && playwright install chromium")
    with sync_playwright() as p:
        self.status_update.emit("Starting browser...")
        browser = p.chromium.launch(headless=PW_HEADLESS, args=["--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(user_agent=PW_USER_AGENT)
            page = context.new_page()
            guard = _pw_guard(context, page)
            self.status_update.emit("Opening download page...")
            page.goto(start_url, timeout=60000)

            target = None
            idx, bad, order = 0, set(), None  # idx = which DOWNLOAD HERE button to click; bad = ones to skip (MegaDB)
            mega_only = False                 # every DOWNLOAD HERE button is MegaDB, so MegaDB is used after all
            for attempt in range(1, 11):
                if self._is_cancelled:
                    raise RuntimeError("CANCELLED")
                self.status_update.emit(f"Finding download link (attempt {attempt})...")
                blocked_before = len(guard["blocked"])
                tab = None
                try:
                    buttons = page.get_by_text(PW_BUTTON_TEXT, exact=False)
                    n = buttons.count()
                    if n and order is None:  # BZZHR is preferred, then GoFile, then whatever is left; MegaDB is skipped
                        flags = [buttons.nth(i).evaluate(PW_LABEL_JS) for i in range(n)]
                        if all(f["mega"] for f in flags):
                            mega_only = True
                            guard["allow"].update(PW_MEGADB)
                            self.status_update.emit("MegaDB is the only option, using it...")
                        else:
                            bad |= {i for i, f in enumerate(flags) if f["mega"]}
                        order = ([i for i, f in enumerate(flags) if f["bzzhr"]] +
                                 [i for i, f in enumerate(flags) if f["pixeldrain"]] +
                                 [i for i, f in enumerate(flags) if f["gofile"]] + list(range(n)))
                        if any(f["bzzhr"] for f in flags):
                            self.status_update.emit("Found a BZZHR button, using it...")
                        elif any(f["pixeldrain"] for f in flags):
                            self.status_update.emit("No BZZHR button, using Pixeldrain...")
                        elif any(f["gofile"] for f in flags):
                            self.status_update.emit("No BZZHR or Pixeldrain button, using GoFile...")
                    idx = next((i for i in (order or range(n)) if i not in bad), None) if n else 0
                    if idx is None:
                        raise RuntimeError("No other DOWNLOAD HERE button found.")
                    with context.expect_page(timeout=10000) as info:
                        buttons.nth(idx).click()
                    tab = info.value
                    tab.wait_for_load_state("domcontentloaded")
                    if _pw_on(tab.url, PW_RELAY_HOSTS):
                        target = _pw_through_relay(self, context, tab, p)
                        if target:
                            break
                    elif _pw_on(tab.url, PW_ALLOWED_HOSTS):
                        target = tab
                        break
                    elif mega_only:
                        end = time.time() + 15
                        while time.time() < end and not tab.is_closed() and not _pw_on(tab.url, PW_MEGADB):
                            tab.wait_for_timeout(500)
                        if _pw_on(tab.url, PW_MEGADB):
                            target = tab
                            break
                    if not tab.is_closed():
                        tab.close()
                except RuntimeError:
                    raise
                except Exception:
                    page.wait_for_timeout(1000)
                blocked_hosts = () if mega_only else PW_BLOCKED_HOSTS
                if any(h in u for u in guard["blocked"][blocked_before:] for h in blocked_hosts) or \
                        (tab is not None and any(h in (tab.url or "") for h in blocked_hosts)):
                    self.status_update.emit("Got a blocked site, trying a different DOWNLOAD HERE button...")
                    bad.add(idx)
            if not target:
                raise RuntimeError("Couldn't reach a valid download link.")

            self.status_update.emit("Getting file link...")
            mega = _pw_on(target.url, PW_MEGADB)
            if mega:
                btn = _pw_megadb_button(self, target, guard)
            elif "gofile.io" in target.url or "pixeldrain.com" in target.url:
                btn = target.get_by_role("button", name=re.compile("Download", re.IGNORECASE)).first
            else:
                btn = target.get_by_text("Download File", exact=False).first
            btn.wait_for(state="visible", timeout=30000)
            if "bzzhr.to" in target.url or mega:
                # Ads open in new tabs and get closed; click Download File again every time one closes.
                got = {}
                target.on("download", lambda d: got.setdefault("d", d))
                seen = guard["closed"]
                btn.click()
                deadline = time.time() + 90
                while "d" not in got and time.time() < deadline:
                    if self._is_cancelled:
                        raise RuntimeError("CANCELLED")
                    target.wait_for_timeout(300)
                    if guard["closed"] > seen:
                        seen = guard["closed"]
                        self.status_update.emit("Ad tab closed, clicking Download File again...")
                        try:
                            btn.click()
                        except Exception:
                            pass
                if "d" not in got:
                    raise RuntimeError("The download didn't start.")
                download = got["d"]
            else:
                with target.expect_download(timeout=60000) as dl_info:
                    btn.click()
                download = dl_info.value
            url = download.url
            cookies = "; ".join(f"{c['name']}={c['value']}" for c in context.cookies(url))
            try:
                download.cancel()  # the launcher downloads it itself
            except Exception:
                pass
            headers = {"User-Agent": PW_USER_AGENT, "Referer": target.url}
            if cookies:
                headers["Cookie"] = cookies
            return url, headers
        finally:
            browser.close()

def _fb_stream(self, url, headers, relink=None):
    """Downloads url; relink() (optional) finds a brand-new link if this one dies mid-download."""
    def name(response, u):
        ext = ".rar" if u.lower().split("?")[0].endswith(".rar") else ".zip"
        return safe_filename_from_headers(response, self.title, ext)
    return resumable_download(self, relink or (lambda: (url, headers)), name, self.expected_size, first=(url, headers))

def _fb_pw_run(self):
    if _pw_is_ncz(self.title):
        return _fb_direct_run(self)
    dest_path = self.existing_zip_path if self.existing_zip_path and os.path.exists(self.existing_zip_path) else None
    fresh = False
    try:
        if not dest_path:
            url, headers = _pw_find_link(self, pw_game_url(self.title))
            self.download_url = url  # bridged: the copied link becomes this download's link
            dest_path = _fb_stream(self, url, headers, lambda: _pw_find_link(self, pw_game_url(self.title)))
            fresh = True
        if self._is_cancelled:
            if fresh and os.path.exists(dest_path):
                os.remove(dest_path)
            self.failed.emit("CANCELLED")
            return
        self.status_update.emit("Extracting...")
        extract_archive(dest_path, installed_game_dir(sanitize_folder_name(self.title)))
        self.finished.emit(dest_path)
    except Exception as e:
        if self._is_cancelled or str(e) == "CANCELLED":
            self.failed.emit("CANCELLED")
            return
        msg = str(e)
        try:  # keep the full traceback so customers can send it over
            import traceback
            log = os.path.join(os.path.dirname(get_launcher_settings_path()), "download_error.log")
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} | {self.title} | Python {sys.version.split()[0]}\n")
                f.write(traceback.format_exc() + "\n")
            msg += f"\n\nFull details saved to:\n{log}"
        except Exception:
            pass
        self.failed.emit(msg)

FirebaseDownloadWorker.run = _fb_pw_run


def _new_game_worker_run(self):
    dest_path = self.existing_zip_path
    try:
        downloaded_here = False
        if not dest_path or not os.path.exists(dest_path):
            downloaded_here = True
            dest_path = resumable_download(
                self, lambda: _resolve_version_link(self),
                lambda response, url: safe_filename_from_headers(response, self.title))

        if self._is_cancelled:
            if downloaded_here and dest_path and os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except Exception:
                    pass
            self.failed.emit("CANCELLED")
            return

        if self.download_only:
            self.finished.emit(dest_path)
            return

        if self.wine_extract_dir:
            self.status_update.emit("Extracting game files...")
            part_dir = self.wine_extract_dir + ".part"
            shutil.rmtree(part_dir, ignore_errors=True)
            extract_archive(dest_path, part_dir)
            shutil.rmtree(self.wine_extract_dir, ignore_errors=True)
            os.rename(part_dir, self.wine_extract_dir)
            if downloaded_here:
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
        elif sys.platform.startswith("linux") and self.linux_cmd:
            self.status_update.emit("Linux Conversion Script Running...\nPlease complete installation in the new terminal window.")
            os.makedirs(GAMES_DIR, exist_ok=True)
            linux_install_terminal(self.linux_cmd, os.path.abspath(DOWNLOAD_DIR), dest_path).wait()
        else:
            self.status_update.emit("Extracting game files...")
            extract_dir = installed_game_dir(self.title)
            extract_archive(dest_path, extract_dir)

        if downloaded_here and self.remote_version and not self.wine_extract_dir:
            record_installed_version(self.title, self.remote_version)
        
        self.finished.emit(dest_path)
    except Exception as e:
        if self._is_cancelled:
            self.failed.emit("CANCELLED")
        else:
            self.failed.emit(str(e))

GameDownloadWorker.run = _new_game_worker_run
class AddExistingGameConfigDialog(QDialog):
    def __init__(self, appid, title, cover_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Configure Existing Game: {title}")
        self.setFixedSize(440, 260)
        self.appid = appid
        self.game_title = title
        self.cover_path = cover_path
        self.result_data = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        exe_layout = QHBoxLayout()
        self.exe_edit = QLineEdit()
        exe_btn = QPushButton("Browse...")
        exe_btn.clicked.connect(self.browse_exe)
        exe_layout.addWidget(self.exe_edit, 1)
        exe_layout.addWidget(exe_btn)
        form.addRow(QLabel("Executable"), exe_layout)

        if sys.platform.startswith("linux"):
            self.compat_combo = QComboBox()
            self.compat_combo.addItem("Default (System Wine / Native)", "")
            tools = get_available_compatibility_tools()
            for name, path in sorted(tools.items()):
                self.compat_combo.addItem(name, path)
            form.addRow(QLabel("Compatibility Tool"), self.compat_combo)
        else:
            self.compat_combo = None

        layout.addLayout(form)

        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("Add to Library")
        btn_ok.setObjectName("Primary")
        btn_cancel = QPushButton("Cancel")
        btn_ok.clicked.connect(self.accept_data)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_ok)
        layout.addLayout(btn_layout)

    def browse_exe(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Executable", os.path.expanduser("~"))
        if path:
            self.exe_edit.setText(path)

    def accept_data(self):
        exe = self.exe_edit.text().strip()
        if not exe or not os.path.exists(exe):
            QMessageBox.warning(self, "Invalid Input", "Please provide a valid, existing executable path.")
            return
        compat = self.compat_combo.currentData() if self.compat_combo else ""
        self.result_data = {
            "appid": self.appid,
            "title": self.game_title,
            "cover": self.cover_path,
            "exe_path": exe,
            "compat_tool": compat
        }
        self.accept()

class SteamStorePickerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Steam Game")
        self.resize(600, 500)
        self.selected_game = None
        self._workers = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        search_layout = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search Steam store...")
        self.search_edit.returnPressed.connect(self.do_search)
        search_btn = QPushButton("Search")
        search_btn.setObjectName("Primary")
        search_btn.clicked.connect(self.do_search)
        search_layout.addWidget(self.search_edit, 1)
        search_layout.addWidget(search_btn)
        layout.addLayout(search_layout)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.grid = QGridLayout(self.container)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self.loader = CoverLoader()
        self.loader.set_key(get_steamgriddb_key())
        self.loader.loaded.connect(self._on_cover_loaded)
        self.cards = {}

        self.do_search()

    def do_search(self):
        term = self.search_edit.text().strip()
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.cards = {}

        def work():
            try:
                games, _ = fetch_steam_games(term, 0, 30)
                return games
            except Exception:
                return []

        worker = TaskWorker(work)
        worker.done.connect(self._on_games_loaded)
        self._workers.append(worker)
        worker.start()

    def _on_games_loaded(self, games):
        cols = 3
        for idx, (appid, title, img_base) in enumerate(games):
            card = StoreCard(appid, title)
            card.clicked.connect(self.on_card_clicked)
            self.cards[appid] = card
            self.grid.addWidget(card, idx // cols, idx % cols)
            self.loader.request(appid, img_base, title)

    def _on_cover_loaded(self, appid, path):
        card = self.cards.get(appid)
        if card:
            card.set_cover(path)

    def on_card_clicked(self, appid, title, cover_path):
        saved_cover = cover_path
        if cover_path and os.path.exists(cover_path):
            try:
                os.makedirs(get_library_cover_dir(), exist_ok=True)
                saved_cover = os.path.join(get_library_cover_dir(), f"{appid}.img")
                shutil.copyfile(cover_path, saved_cover)
            except Exception:
                pass

        cfg_dlg = AddExistingGameConfigDialog(appid, title, saved_cover, self)
        if cfg_dlg.exec() == QDialog.DialogCode.Accepted and cfg_dlg.result_data:
            self.selected_game = cfg_dlg.result_data
            self.accept()

def _open_add_existing_game_dialog(self):
    dlg = SteamStorePickerDialog(self)
    if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_game:
        data = dlg.selected_game
        appid = data["appid"]
        title = data["title"]
        cover_path = data["cover"]
        exe_path = data["exe_path"]
        compat_tool = data["compat_tool"]

        games = [g for g in load_steam_library() if g["appid"] != appid]
        games.append({"appid": appid, "title": title, "cover": cover_path, "exe_path": exe_path})
        save_steam_library(games)

        if compat_tool:
            compat_config = load_compat_config()
            compat_config[str(appid)] = compat_tool
            save_compat_config(compat_config)

        self.create_steam_card(appid, title, cover_path)
        self.apply_filter()

AdaptiveApp.open_add_existing_game_dialog = _open_add_existing_game_dialog

AdaptiveApp._original_create_steam_card_compat = AdaptiveApp.create_steam_card

def _patched_create_steam_card_combined(self, appid, title_text, cover_path):
    AdaptiveApp._original_create_steam_card_compat(self, appid, title_text, cover_path)
    card = self.steam_cards.get(appid)
    if card:
        btn_play = card.findChild(QPushButton, "Primary")
        status_label = card.findChild(QLabel, "CardStatus")
        
        custom_exe = None
        for g in load_steam_library():
            if g.get("appid") == appid and g.get("exe_path"):
                custom_exe = g.get("exe_path")
                break

        if custom_exe:
            btn_play.setText("Launch")
            if status_label:
                status_label.setText("● Installed")
                status_label.setStyleSheet(f"color: {GREEN};")

            try:
                btn_play.clicked.disconnect()
            except Exception:
                pass

            def launch_existing_game():
                if not os.path.exists(custom_exe):
                    QMessageBox.warning(self, "Launch Error", f"Executable not found:\n{custom_exe}")
                    return
                if sys.platform.startswith("win"):
                    try:
                        subprocess.Popen([custom_exe] + launch_args_for(str(appid)), cwd=os.path.dirname(custom_exe))
                    except Exception as e:
                        QMessageBox.warning(self, "Launch Game", f"Couldn't launch game: {e}")
                elif sys.platform.startswith("linux"):
                    _launch_exe(self, custom_exe, load_compat_config().get(str(appid), ""), _d3d_for(str(appid)),
                                launch_args_for(str(appid)))

            btn_play.clicked.connect(launch_existing_game)

        if sys.platform.startswith("linux"):
            more_btn = card.findChild(QPushButton, "MoreButton")
            if more_btn and more_btn.menu():
                menu = more_btn.menu()
                has_compat_action = any("Compatibility Tool" in action.text() for action in menu.actions())
                if not has_compat_action:
                    def open_compat_dialog():
                        self.switch_compat_tool(str(appid))

                    menu.addAction("Switch Compatibility Tool").triggered.connect(open_compat_dialog)

AdaptiveApp.create_steam_card = _patched_create_steam_card_combined

# Every custom-card patch before this one was removed; wrap the plain library page.
AdaptiveApp._true_original_build_library_page = AdaptiveApp.build_library_page

def _unified_build_library_page(self):
    widget = AdaptiveApp._true_original_build_library_page(self)
    
    main_layout = widget.layout()
    if main_layout and main_layout.count() > 0:
        header_item = main_layout.itemAt(0)
        if header_item and header_item.layout():
            header_layout = header_item.layout()
            
            # Remove ALL existing '+' buttons left over from previous patches
            buttons_to_remove = []
            for i in range(header_layout.count()):
                item = header_layout.itemAt(i)
                if item and item.widget() and isinstance(item.widget(), QPushButton):
                    if item.widget().text() == "+":
                        buttons_to_remove.append(item.widget())
            
            for btn in buttons_to_remove:
                header_layout.removeWidget(btn)
                btn.setParent(None)
                btn.deleteLater()
            
            # Add exactly ONE clean '+' button
            add_btn = QPushButton("+")
            add_btn.setFixedSize(28, 28)
            add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            add_btn.setToolTip("Add game")
            add_btn.setStyleSheet("font-size: 18px; font-weight: bold; border-radius: 14px; padding: 0px;")
            
            menu = QMenu(add_btn)
            menu.addAction("Add Custom Game").triggered.connect(self.open_add_custom_game_dialog)
            menu.addAction("Add Existing Game").triggered.connect(self.open_add_existing_game_dialog)
            menu.addAction("Add Existing Fan Game").triggered.connect(lambda _c=False: self._fan_add_existing())
            
            def show_menu():
                pos = add_btn.mapToGlobal(add_btn.rect().bottomLeft())
                if hasattr(menu, "exec"):
                    menu.exec(pos)
                else:
                    menu.exec_(pos)
                
            add_btn.clicked.connect(show_menu)
            header_layout.insertWidget(2, add_btn)

    for game in load_custom_games():
        self.create_custom_card(game.get("id"), game.get("title"), game.get("exe_path"), game.get("cover_path"), game.get("compat_tool"))
        
    self.apply_filter()
    return widget

AdaptiveApp.build_library_page = _unified_build_library_page

# ================================================================ friends system
import secrets as _secrets

FRIEND_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
FRIEND_ONLINE_WINDOW_MS = 180000

def _friends_cache_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "friends.json")

def _fdb(path, token, query=""):
    base = FIREBASE_DB_URL.strip().rstrip("/")
    q = ("&" + query) if query else ""
    return f"{base}/{path}.json?auth={urllib.parse.quote(token, safe='')}{q}"

def friend_code_cached():
    acct = load_account()
    if not acct:
        return ""
    try:
        with open(_friends_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("uid") == acct["uid"]:
            return str(data.get("code") or "")
    except Exception:
        pass
    return ""

def _save_friend_code_cache(uid, code):
    os.makedirs(os.path.dirname(_friends_cache_path()), exist_ok=True)
    with open(_friends_cache_path(), "w", encoding="utf-8") as f:
        json.dump({"uid": uid, "code": code}, f)

def cloud_ensure_friend_code():
    token, uid = get_id_token()
    cached = friend_code_cached()
    if cached:
        return cached
    code = _http_json(_fdb(f"public/{uid}/code", token))
    if not (isinstance(code, str) and code):
        for _ in range(8):
            cand = "".join(_secrets.choice(FRIEND_CODE_ALPHABET) for _ in range(8))
            if _http_json(_fdb(f"codes/{cand}", token)) is None:
                _http_json(_fdb(f"codes/{cand}", token), "PUT", uid)
                code = cand
                break
        else:
            raise CloudError("Couldn't create a friend code, try again.", "NO_CODE")
        _http_json(_fdb(f"public/{uid}/code", token), "PUT", code)
    _save_friend_code_cache(uid, code)
    return code

# --- what the player is currently playing
_now_playing = {"name": None}
_playing_listeners = []

def pretty_game_name(name):
    """'deltarune' -> 'Deltarune'. Names that already have any capitals are left alone."""
    name = str(name or "")
    if name and name == name.lower():
        return re.sub(r"(^|[\s\-_:.])([a-z])", lambda m: m.group(1) + m.group(2).upper(), name)
    return name

_library_snapshot = []  # every game card in the launcher, kept up to date from the GUI thread

def _canonical_title(name):
    low = str(name).lower()
    for t in _library_snapshot:
        if t.lower() == low:
            return t
    return name

def friends_library_titles():
    if _library_snapshot:
        return list(_library_snapshot)
    titles = []
    for info in GAME_INFO.values():
        if os.path.isdir(installed_game_dir(info["name"])) or os.path.isdir(wine_game_dir(info["name"])):
            titles.append(info["name"])
    for g in load_steam_library() + load_custom_games() + load_fan_library():
        t = g.get("title")
        if t and t not in titles:
            titles.append(t)
    return [pretty_game_name(t) for t in titles]

def _game_name_for_args(args):
    known = {}
    try:
        fan = load_fan_library()
    except Exception:
        fan = []
    for g in load_steam_library() + load_custom_games() + fan:
        if g.get("exe_path") and g.get("title"):
            known[os.path.normcase(os.path.abspath(g["exe_path"]))] = g["title"]
    for a in args:
        if not isinstance(a, str):
            continue
        full = os.path.abspath(a)
        norm = os.path.normcase(full)
        if norm in known:
            return _canonical_title(known[norm])
        for root in library_roots():
            if norm.startswith(os.path.normcase(root) + os.sep):
                first = os.path.relpath(full, root).split(os.sep)[0]
                for info in GAME_INFO.values():
                    if first.lower().startswith(info["name"].lower()):
                        return _canonical_title(info["name"])
                return _canonical_title(first)
    return None

_running = {}  # game name -> Popen of the running game

def _track_playing(name, proc):
    name = pretty_game_name(name)
    _running[name] = proc
    _now_playing["name"] = name
    for cb in _playing_listeners:
        cb()
    def wait():
        try:
            proc.wait()
        except Exception:
            pass
        if _running.get(name) is proc:
            del _running[name]
        if _now_playing["name"] == name:
            _now_playing["name"] = next(iter(_running), None)
            for cb in _playing_listeners:
                cb()
    threading.Thread(target=wait, daemon=True).start()

_RealPopen = subprocess.Popen

class _TrackedPopen(_RealPopen):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        try:
            args = a[0] if a else k.get("args")
            if isinstance(args, (list, tuple)):
                name = _game_name_for_args(args)
                if name:
                    _track_playing(name, self)
        except Exception:
            pass

subprocess.Popen = _TrackedPopen

import hashlib as _hashlib

_library_covers = {}   # pretty game title -> cover image path (kept up to date from the GUI thread)
_covers_sig = {"v": None}
_cover_enc_cache = {}

def _cover_key(title):
    return _hashlib.md5(str(title).lower().encode("utf-8")).hexdigest()[:12]

def _encode_cover(path):
    """Small JPEG (base64) of a cover image, cached until the file changes."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    cached = _cover_enc_cache.get(path)
    if cached and cached[0] == mt:
        return cached[1]
    img = QImage(path)
    enc = None
    if not img.isNull():
        img = img.scaledToWidth(120, Qt.TransformationMode.SmoothTransformation)
        img = img.convertToFormat(QImage.Format.Format_RGB32)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        if img.save(buf, "JPEG", 72) and buf.data():
            enc = base64.b64encode(bytes(buf.data())).decode("ascii")
            if len(enc) > 60000:
                enc = None
    _cover_enc_cache[path] = (mt, enc)
    return enc

def cloud_publish_covers(token, uid):
    covers = {}
    for title, path in list(_library_covers.items()):
        enc = _encode_cover(path)
        if enc:
            covers[_cover_key(title)] = enc
    sig = (uid, tuple(sorted((k, hash(v)) for k, v in covers.items())))
    if not covers or sig == _covers_sig["v"]:
        return
    _http_json(_fdb(f"covers/{uid}", token), "PUT", covers)
    _covers_sig["v"] = sig

def cloud_fetch_covers(friend_uid):
    token, _uid = get_id_token()
    data = _http_json(_fdb(f"covers/{friend_uid}", token))
    return data if isinstance(data, dict) else {}

def library_cover_paths():
    """{game title: cover image path} for every game in the library, one method for all of them:
    NCZ games (assets folder), store games + Add Existing Game, and custom games."""
    out = {}
    for name, fname in ((AE_GAME_NAME, "fnanczaecover.png"), (NCZ2_GAME_NAME, "fnancz2cover.png"),
                        ("NCZFront", "nczfront-cover.png")):
        path = asset_path(fname)
        if os.path.exists(path):
            out[pretty_game_name(name)] = path
    for g in load_steam_library():
        if g.get("title") and g.get("cover") and os.path.exists(g["cover"]):
            out[pretty_game_name(g["title"])] = g["cover"]
    for g in load_custom_games():
        if g.get("title") and g.get("cover_path") and os.path.exists(g["cover_path"]):
            out[pretty_game_name(g["title"])] = g["cover_path"]
    for g in load_fan_library():
        if g.get("title") and g.get("cover") and os.path.exists(g["cover"]):
            out[pretty_game_name(g["title"])] = g["cover"]
    return out

def _friend_cover_file(uid, key, b64):
    """Writes a friend's cover to a small cache file so the normal cover renderer can draw it."""
    raw = _decode_photo(b64)
    if not raw:
        return None
    folder = os.path.join(os.path.dirname(get_launcher_settings_path()), "friend_covers")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, re.sub(r"[^A-Za-z0-9]", "_", uid) + "_" + key + ".jpg")
    try:
        if not os.path.exists(path) or os.path.getsize(path) != len(raw):
            with open(path, "wb") as f:
                f.write(raw)
    except OSError:
        return None
    return path

# Idle = the launcher window is minimized (or hidden in the tray).
_window_state = {"minimized": False}

def is_idle():
    return _window_state["minimized"]

def cloud_publish_presence():
    token, uid = get_id_token()
    username, avatar = load_profile_cache()
    payload = {
        "username": username or (load_account() or {}).get("email", "").split("@")[0] or "Player",
        "library": friends_library_titles(),
        "playing": _now_playing["name"] or "",
        "idle": is_idle(),
        "seen": {".sv": "timestamp"},
        "photo": None,
    }
    if avatar and os.path.exists(avatar):
        with open(avatar, "rb") as f:
            payload["photo"] = base64.b64encode(f.read()).decode("ascii")
    _http_json(_fdb(f"public/{uid}", token), "PATCH", payload)
    try:
        cloud_publish_covers(token, uid)
    except CloudError:
        pass  # covers are optional; never break presence over them

def cloud_publish_offline():
    token, uid = get_id_token()
    _http_json(_fdb(f"public/{uid}", token), "PATCH", {"seen": 0, "idle": False, "playing": ""})

def cloud_accept_friend(other):
    token, uid = get_id_token()
    _http_json(_fdb(f"friends/{uid}/{other}", token), "PUT", True)
    _http_json(_fdb(f"friends/{other}/{uid}", token), "PUT", True)
    _http_json(_fdb(f"requests/{uid}/{other}", token), "DELETE")

def cloud_decline_friend(other):
    token, uid = get_id_token()
    _http_json(_fdb(f"requests/{uid}/{other}", token), "DELETE")

def cloud_add_friend(code):
    """Sends a friend request. Returns 'sent', or 'accepted' if they had already asked us."""
    code = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    if len(code) != 8:
        raise CloudError("Friend codes are 8 characters long.", "BAD_CODE")
    token, uid = get_id_token()
    other = _http_json(_fdb(f"codes/{code}", token))
    if not isinstance(other, str):
        raise CloudError("No one has that friend code.", "NO_SUCH_CODE")
    if other == uid:
        raise CloudError("That's your own code!", "SELF")
    if _http_json(_fdb(f"friends/{uid}/{other}", token)):
        raise CloudError("You're already friends.", "ALREADY")
    if _http_json(_fdb(f"requests/{uid}/{other}", token)):
        cloud_accept_friend(other)
        return "accepted"
    _http_json(_fdb(f"requests/{other}/{uid}", token), "PUT", True)
    return "sent"

def cloud_fetch_requests():
    token, uid = get_id_token()
    ids = _http_json(_fdb(f"requests/{uid}", token))
    ids = list(ids.keys()) if isinstance(ids, dict) else []
    out = {}
    for rid in ids:
        try:
            d = _http_json(_fdb(f"public/{rid}", token))
        except CloudError:
            d = {}
        out[rid] = d if isinstance(d, dict) else {}
    return out

def cloud_fetch_friends():
    token, uid = get_id_token()
    ids = _http_json(_fdb(f"friends/{uid}", token))
    ids = list(ids.keys()) if isinstance(ids, dict) else []
    def one(fid):
        try:
            d = _http_json(_fdb(f"public/{fid}", token))
            return fid, (d if isinstance(d, dict) else {})
        except CloudError:
            return fid, {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        return dict(ex.map(one, ids))

def _chat_id(a, b):
    return "_".join(sorted([a, b]))

def cloud_get_messages(other):
    token, uid = get_id_token()
    data = _http_json(_fdb(f"chats/{_chat_id(uid, other)}", token,
                           'orderBy=%22%24key%22&limitToLast=60'))
    if not isinstance(data, dict):
        return uid, [], None
    keys = [k for k in sorted(data) if isinstance(data[k], dict)]
    for k in keys:
        data[k]["_key"] = k
    return uid, [data[k] for k in keys], (keys[-1] if keys else None)

def cloud_fetch_last_messages(friend_ids):
    """{friend uid: (message key, message)} for the newest message in each chat."""
    token, uid = get_id_token()
    def one(fid):
        try:
            data = _http_json(_fdb(f"chats/{_chat_id(uid, fid)}", token,
                                   'orderBy=%22%24key%22&limitToLast=1'))
            if isinstance(data, dict) and data:
                k = sorted(data)[-1]
                if isinstance(data[k], dict):
                    return fid, (k, data[k])
            return fid, ("", None)       # fetched fine, the chat is just empty (a failed fetch returns None below)
        except CloudError:
            pass
        return fid, None
    with ThreadPoolExecutor(max_workers=6) as ex:
        return {f: v for f, v in ex.map(one, friend_ids) if v}

CHAT_MAX_ATTACH = 4
CHAT_IMAGE_MAX_SIDE = 4096                  # only used when a pasted/odd-format image has to be re-encoded
CHAT_IMAGE_MAX_BYTES = 10 * 1024 * 1024
CHAT_GIF_MAX_BYTES = CHAT_IMAGE_MAX_BYTES
CHAT_VIDEO_MAX_BYTES = 10 * 1024 * 1024
CHAT_CHUNK_CHARS = 4_000_000                # a Realtime Database string tops out at 10 MB, so big files are stored in chunks
CHAT_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
CHAT_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v")
_ATT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_EMOJI_CODE_RE = re.compile(r"^[0-9a-f]{2,6}(-[0-9a-f]{2,6})*$")
_VIDEO_MIME = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
               ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".m4v": "video/x-m4v"}

def _chat_fmt_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"

def chat_media_dir():
    path = os.path.join(os.path.dirname(get_launcher_settings_path()), "chat_media")
    os.makedirs(path, exist_ok=True)
    return path

# ---- Steam game invites: a chat message whose text is a marker + a small json (works with the existing chat storage).
# Accepting one opens the steam:// link, which is exactly what Steam itself does: Steam starts the game (if it isn't
# running) and hands it the join info.
INVITE_MARK = "::ncz-invite::"
_INVITE_LINK_RE = re.compile(r"^steam://(?:connect|joinlobby)/[A-Za-z0-9.:/_+\-]{1,120}$")
_RUNGAME_LINK_RE = re.compile(r"^steam://rungame/\d{1,10}/\d{15,20}/([A-Za-z0-9%+._\-]{1,200})$")
# a friend's invite must never be able to start a game with arbitrary launch options: only join-style flags pass
_RUNGAME_ARG_RE = re.compile(r"^[+\-]{1,2}[A-Za-z_]*(?:connect|join|lobby|server|invite)[A-Za-z_]*[ =][A-Za-z0-9.:_\-]{1,80}$", re.I)

def _valid_invite_link(link):
    if _INVITE_LINK_RE.match(link):
        return True
    m = _RUNGAME_LINK_RE.match(link)
    return bool(m and _RUNGAME_ARG_RE.match(urllib.parse.unquote(m.group(1))))

def normalize_steam_join(raw):
    """What the user pasted -> a steam://connect/... or steam://joinlobby/... link, or "" when it isn't one."""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.lower().startswith("steam://"):
        link = "steam://" + s[8:]
    elif re.fullmatch(r"\d+/\d+/\d+", s):                                       # appid/lobbyid/steamid
        link = "steam://joinlobby/" + s
    elif re.fullmatch(r"[A-Za-z0-9.\-]+:\d{2,5}(/[A-Za-z0-9._\-]+)?", s):          # ip:port (/password)
        link = "steam://connect/" + s
    else:
        return ""
    return link if _valid_invite_link(link) else ""

def make_invite_text(game, link, appid=0):
    d = {"g": str(game)[:60], "l": link}
    if isinstance(appid, int) and 0 < appid < 10 ** 9:
        d["a"] = appid                      # lets the other launcher fetch the game's Steam cover
    d["k"] = _cover_key(game)               # lets it find the cover among the sender's published covers (NCZ / custom games)
    return INVITE_MARK + json.dumps(d, separators=(",", ":"))

def parse_invite(text):
    """-> {'game', 'link', 'appid', 'key'} for an invite message (link re-checked: it comes from the other person), else None."""
    if not isinstance(text, str) or not text.startswith(INVITE_MARK):
        return None
    try:
        d = json.loads(text[len(INVITE_MARK):])
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    link, game = str(d.get("l", "")), str(d.get("g", ""))[:60]
    appid = d.get("a") if isinstance(d.get("a"), int) and not isinstance(d.get("a"), bool) else 0
    if not (0 < appid < 10 ** 9):
        appid = 0
    key = str(d.get("k", ""))
    key = key if re.fullmatch(r"[0-9a-f]{12}", key) else ""
    if not _valid_invite_link(link):
        return None
    return {"game": game, "link": link, "appid": appid, "key": key}

# ---- automatic invites: the game being played, its cover and (when Steam knows it) the join link are all found for you.
import weakref as _weakref

def _steam_appid_for_title(title):
    low = pretty_game_name(title).casefold()
    for g in load_steam_library():
        if pretty_game_name(g.get("title", "")).casefold() == low:
            return int(g["appid"])
    return 0

def current_game_for_invite():
    """The game being played right now -> {'game', 'appid'} (appid 0 for games that aren't from the Steam store), or None."""
    name = _now_playing["name"]
    if name == "NCZFront":                  # NCZFront is a website, not a game you invite people to
        name = next((n for n in _running if n != "NCZFront"), None)
    if not name:
        return None
    name = pretty_game_name(name)
    return {"game": name, "appid": _steam_appid_for_title(name)}

def _steam_root_dirs():
    dirs = []
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                dirs.append(winreg.QueryValueEx(k, "SteamPath")[0])
        except Exception:
            pass
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env)
            if base:
                dirs.append(os.path.join(base, "Steam"))
    else:
        home = os.path.expanduser("~")
        dirs += [home + "/.steam/steam", home + "/.local/share/Steam",
                 home + "/.var/app/com.valvesoftware.Steam/.local/share/Steam"]
    return [d for d in dirs if d and os.path.isdir(d)]

def local_steam_id():
    """SteamID64 of the account Steam is signed into on this PC ('' when Steam isn't installed)."""
    first = ""
    for root in _steam_root_dirs():
        try:
            with open(os.path.join(root, "config", "loginusers.vdf"), encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for sid, body in re.findall(r'"(7656\d{13})"\s*\{(.*?)\}', text, re.S):
            if re.search(r'"MostRecent"\s*"1"', body):
                return sid
            first = first or sid
    return first

def steam_profile_join(sid):
    """What Steam's public profile says this account is doing -> {'appid', 'name', 'link'}, or {} when it isn't in a game
    (or the profile's game details are private). 'link' is '' when the game has no joinable lobby/server."""
    out = {}
    try:
        xml = _http_get(f"https://steamcommunity.com/profiles/{sid}/?xml=1", timeout=8).decode("utf-8", "replace")
    except Exception:
        xml = ""
    info = re.search(r"<inGameInfo>(.*?)</inGameInfo>", xml, re.S)
    if info:
        body = info.group(1)
        m = re.search(r"<gameName>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</gameName>", body, re.S)
        if m:
            out["name"] = html_lib.unescape(m.group(1).strip())[:60]
        m = re.search(r"/app/(\d+)", body)
        if m:
            out["appid"] = int(m.group(1))
        m = re.search(r"<inGameServerIP>\s*([^<\s]+)\s*</inGameServerIP>", xml)
        if m:
            out["link"] = normalize_steam_join(m.group(1))
    if out and not out.get("link"):
        try:
            page = _http_get(f"https://steamcommunity.com/profiles/{sid}", timeout=8).decode("utf-8", "replace")
            m = re.search(r"steam://(?:joinlobby|connect)/[A-Za-z0-9.:/_+\-]{1,120}", page)
            if m:
                out["link"] = normalize_steam_join(m.group(0))
        except Exception:
            pass
    out.setdefault("link", "")
    return out if out.get("name") or out.get("appid") else {}

def _steam_api_lib_for(appid):
    """The game's own steam_api library (it ships next to the game), or ''."""
    name = "steam_api64.dll" if sys.platform.startswith("win") else "libsteam_api.so"
    for g in load_steam_library():
        exe = g.get("exe_path")
        if g.get("appid") == appid and exe:
            base = os.path.dirname(exe)
            for root, dirs, files in os.walk(base):
                if root[len(base):].count(os.sep) >= 3:
                    dirs[:] = []
                if name in files:
                    return os.path.join(root, name)
    return ""

def steam_join_via_steamworks(appid, sid, timeout=15):
    """Asks the Steam client (through the game's own steam_api library, in a throw-away helper process) for the game's
    rich presence 'connect' text and turns it into a join link. Worker thread. -> link or ''."""
    lib = _steam_api_lib_for(appid)
    if not lib or not sid:
        return ""
    cmd = ([sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)])
    cmd += ["--ncz-rp-helper", str(appid), lib, sid]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    except Exception:
        return ""
    if not out:
        return ""
    return normalize_steam_join(f"steam://rungame/{appid}/{sid}/" + urllib.parse.quote(out.splitlines()[-1], safe=""))

def detect_invite_info():
    """Runs on a worker thread. -> {'game', 'appid', 'link'}; each is empty/0 when it couldn't be found."""
    cur = current_game_for_invite() or {}
    game, appid, link = cur.get("game", ""), cur.get("appid", 0), ""
    sid = local_steam_id()
    if sid and game and appid:              # first choice: ask the Steam client itself
        link = steam_join_via_steamworks(appid, sid)
    prof = steam_profile_join(sid) if (sid and not link) else {}
    if prof:                                # the launcher's game wins for name + cover; the link comes from Steam either way
        if not game:
            game = pretty_game_name(prof.get("name", ""))
        appid = appid or prof.get("appid", 0)
        link = link or prof.get("link", "")
    if game and not appid:
        appid = _steam_appid_for_title(game)
    return {"game": game, "appid": appid, "link": link}

# ---- accepting an invite: if the invite's game (matched by NAME) isn't running, start it from this launcher's own
# library card, wait 3 s, then accept the invite
INVITE_JOIN_DELAY_MS = 3000
INVITE_JOIN_MAX_RETRIES = 10     # extra 3 s rounds to wait for the game / a failed open before joining anyway
_invite_games = {}           # join link -> game name (the overlay toast only knows the link)
_invite_pending = set()      # links currently waiting for their game to start

def _invite_game_is_open(game):
    low = pretty_game_name(game or "").casefold()
    return bool(low) and any(pretty_game_name(n).casefold() == low for n in list(_running))

def _invite_norm(name):
    return re.sub(r"[^a-z0-9]", "", pretty_game_name(name or "").casefold())

def _invite_find_card(win, game):
    low = _invite_norm(game)
    if not low:
        return None
    for card, title in list(getattr(win, "cards", [])) + list(getattr(win, "fan_lib_list", [])):
        try:
            if _invite_norm(title) == low:
                card.objectName()           # raises if the card was deleted
                return card
        except RuntimeError:
            continue
    return None

def _invite_main_window():
    return next((w for w in QApplication.topLevelWidgets() if isinstance(w, AdaptiveApp)), None)

def _invite_start_card(card):
    """Presses the card's Launch (-> 'launched') or Install (-> 'installing') button, else 'missing'."""
    for b in card.findChildren(QPushButton):
        if b.objectName() != "Primary" or not b.isEnabled():
            continue
        t = b.text().strip().lower()
        if t in ("launch", "play"):
            b.click()
            return "launched", b
        if t == "install":
            b.click()
            return "installing", b
    return "missing", None

def _invite_join_after_delay(link, on_state=None, tries=0):
    """Opens the join link after 3 s. If the game isn't running yet or the link couldn't be opened, tries again in 3 s."""
    def go():
        game = _invite_games.get(link, "")
        if tries < INVITE_JOIN_MAX_RETRIES and game and not _invite_game_is_open(game):
            _invite_join_after_delay(link, on_state, tries + 1)         # game not up yet
            return
        ok = QDesktopServices.openUrl(QUrl(link))
        if not ok and tries < INVITE_JOIN_MAX_RETRIES:
            _invite_join_after_delay(link, on_state, tries + 1)         # couldn't open: try again
            return
        _invite_pending.discard(link)
        if on_state:
            on_state(False)
    QTimer.singleShot(INVITE_JOIN_DELAY_MS, go)

def _invite_wait_for_install(card, link, on_state, deadline):
    """Polls the card until its install has finished (button says Launch), then launches and joins 3 s later."""
    def poll():
        try:
            btns = [b for b in card.findChildren(QPushButton) if b.objectName() == "Primary"]
        except RuntimeError:                        # card removed
            _invite_pending.discard(link)
            return
        if btns and btns[0].text().strip().lower() in ("launch", "play"):
            btns[0].click()
            if on_state:
                on_state(True)
            _invite_join_after_delay(link, on_state)
        elif time.time() < deadline:
            QTimer.singleShot(3000, poll)
        else:
            _invite_pending.discard(link)
    QTimer.singleShot(3000, poll)

def _invite_proceed(link, game, on_state):
    win = _invite_main_window()
    card = _invite_find_card(win, game) if win else None
    state, _b = _invite_start_card(card) if card is not None else ("missing", None)
    if state == "launched":
        if on_state:
            on_state(True)
        _invite_join_after_delay(link, on_state)
    elif state == "installing":                      # keeps waiting for the download, then launches and joins
        _invite_wait_for_install(card, link, on_state, time.time() + 7200)
    else:                                            # can't be started from the launcher: just join
        _invite_pending.discard(link)
        QDesktopServices.openUrl(QUrl(link))

class _InviteStoreBus(QObject):
    found = pyqtSignal(str, str, object)             # link, game, (appid, title, cover path) or None

_invite_store_bus = None
_invite_cbs = {}                                     # link -> on_state callback

def _invite_on_store_result(link, game, hit):
    on_state = _invite_cbs.pop(link, None)
    win = _invite_main_window()
    if hit and win is not None:
        appid, title, cover = hit
        try:
            win.add_steam_game(appid, title, cover)
        except Exception as e:
            print(f"invite: couldn't add {title} to the library: {e}")
    _invite_proceed(link, game, on_state)

def _invite_add_from_store(link, game, on_state):
    """Game isn't in the library: look it up by NAME in the Steam store, add it to the library, then launch/install it."""
    global _invite_store_bus
    if _invite_store_bus is None:
        _invite_store_bus = _InviteStoreBus()
        _invite_store_bus.found.connect(_invite_on_store_result)
    bus = _invite_store_bus
    _invite_cbs[link] = on_state
    want = _invite_norm(game)

    def work():
        hit = None
        try:
            games, _t = fetch_steam_games(game, 0, 20)
            for appid, title, base in games:
                if _invite_norm(title) == want:
                    cover = ""
                    if base:
                        try:
                            os.makedirs(get_store_cache_dir(), exist_ok=True)
                            cover = os.path.join(get_store_cache_dir(), f"invite-{appid}.img")
                            with open(cover, "wb") as f:
                                f.write(_http_get(base + "/library_600x900.jpg"))
                        except Exception:
                            cover = ""
                    hit = (appid, title, cover)
                    break
        except Exception:
            hit = None
        bus.found.emit(link, game, hit)
    threading.Thread(target=work, daemon=True).start()

def accept_invite(link, game="", on_state=None):
    """Join button / overlay Accept. If the invite's game (matched by NAME) isn't open: launch it from the launcher,
    adding it to the library from the Steam store first when it isn't there, wait 3 s, then open the join link.
    A game that still has to be installed is launched and joined as soon as its install has finished.
    on_state(bool) is called with True while waiting and False when done (to update a button)."""
    game = game or _invite_games.get(link, "")
    if link in _invite_pending:
        return
    if _invite_game_is_open(game):
        QDesktopServices.openUrl(QUrl(link))
        return
    win = _invite_main_window()
    if win is None or not _invite_norm(game):
        QDesktopServices.openUrl(QUrl(link))
        return
    _invite_pending.add(link)
    if _invite_find_card(win, game) is not None:
        _invite_proceed(link, game, on_state)
    else:
        _invite_add_from_store(link, game, on_state)

def _accept_invite_from_overlay(link):
    accept_invite(link)

# ---- invite cover art: own library first, then the Steam cover (cached), then the sender's published covers
INVITE_COVER_W, INVITE_COVER_H = 72, 96

def _invite_cover_dir():
    path = os.path.join(os.path.dirname(get_launcher_settings_path()), "invite_covers")
    os.makedirs(path, exist_ok=True)
    return path

def _friend_cover_cache_path(uid, key):
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "friend_covers",
                        re.sub(r"[^A-Za-z0-9]", "_", uid) + "_" + key + ".jpg")

def invite_cover_path(inv, sender_uid=None):
    """A cover file that is already on this PC, or '' (fast: never touches the network)."""
    low = pretty_game_name(inv.get("game", "")).casefold()
    for title, path in list(_library_covers.items()):
        if title.casefold() == low and os.path.exists(path):
            return path
    candidates = []
    if inv.get("appid"):
        candidates.append(os.path.join(_invite_cover_dir(), f"{inv['appid']}.jpg"))
    if sender_uid and inv.get("key"):
        candidates.append(_friend_cover_cache_path(sender_uid, inv["key"]))
    for path in candidates:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return ""

def _fetch_invite_cover(inv, sender_uid):
    """Worker thread -> cover file path or ''."""
    appid = inv.get("appid")
    if appid:
        dest = os.path.join(_invite_cover_dir(), f"{appid}.jpg")
        for url in steam_cover_urls(appid, ""):
            try:
                data = _http_get(url, timeout=10)
            except Exception:
                continue
            if data:
                with open(dest, "wb") as f:
                    f.write(data)
                return dest
    if sender_uid and inv.get("key"):
        covers = cloud_fetch_covers(sender_uid)
        return _friend_cover_file(sender_uid, inv["key"], covers.get(inv["key"])) or ""
    return ""

class _InviteCoverBus(QObject):
    ready = pyqtSignal(str, str)            # waiter key, cover path ('' when none was found)

    def __init__(self):
        super().__init__()
        self.waiters = {}                   # waiter key -> [weak refs to the cover labels]
        self.ready.connect(self._on_ready)

    def _on_ready(self, wkey, path):
        for ref in self.waiters.pop(wkey, []):
            lab = ref()
            if lab is None or not path:
                continue
            try:
                pix = rounded_cover_pixmap(path, INVITE_COVER_W, INVITE_COVER_H, 8)
                if pix:
                    lab.setPixmap(pix)
            except RuntimeError:            # the chat was rebuilt and this label is gone
                pass

_invite_bus = _InviteCoverBus()

def want_invite_cover(inv, sender_uid, label):
    """Fills `label` with the invite's cover as soon as it has been downloaded."""
    wkey = f"a{inv['appid']}" if inv.get("appid") else f"{sender_uid}:{inv.get('key', '')}"
    if not inv.get("appid") and not (sender_uid and inv.get("key")):
        return
    first = wkey not in _invite_bus.waiters
    _invite_bus.waiters.setdefault(wkey, []).append(_weakref.ref(label))
    if not first:
        return                              # already being fetched
    def work():
        try:
            path = _fetch_invite_cover(inv, sender_uid)
        except Exception:
            path = ""
        _invite_bus.ready.emit(wkey, path or "")
    threading.Thread(target=work, daemon=True).start()

def chat_preview(m):
    """One-line text for notifications / the overlay: the message text, or a tag for attachments."""
    text = str(m.get("text", "") or "").strip()
    inv = parse_invite(text)
    if inv:
        return f"Game invite: {inv['game'] or 'a Steam game'}"
    atts = [a for a in (m.get("att") if isinstance(m.get("att"), list) else []) if isinstance(a, dict)]
    if not atts:
        return text
    tag = {"img": "Image", "gif": "GIF", "vid": "Video"}.get(atts[0].get("k"), "File")
    tag = f"[{tag}]" if len(atts) == 1 else f"[{len(atts)} attachments]"
    return f"{text} {tag}" if text else tag

# ---- preparing files for sending (runs on a worker thread, so only QImage here, never QPixmap)
def _encode_qimage(img):
    """Shrinks / re-encodes a QImage until it fits the size cap. Returns (bytes, mime, ext, w, h)."""
    if img.isNull():
        raise ValueError("That image couldn't be read.")
    if max(img.width(), img.height()) > CHAT_IMAGE_MAX_SIDE:
        img = img.scaled(CHAT_IMAGE_MAX_SIDE, CHAT_IMAGE_MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    alpha = img.hasAlphaChannel()
    quality = 88
    for _ in range(6):
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        if alpha:
            ok, mime, ext = img.save(buf, "PNG"), "image/png", ".png"
        else:
            ok, mime, ext = img.save(buf, "JPEG", quality), "image/jpeg", ".jpg"
        data = bytes(buf.data())
        if ok and 0 < len(data) <= CHAT_IMAGE_MAX_BYTES:
            return data, mime, ext, img.width(), img.height()
        quality = max(50, quality - 12)
        img = img.scaled(max(16, int(img.width() * 0.8)), max(16, int(img.height() * 0.8)),
                         Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    raise ValueError("That image is too large to send.")

def prepare_attachment_image(img, name=""):
    data, mime, ext, w, h = _encode_qimage(img)
    base = os.path.splitext(name or time.strftime("image-%Y%m%d-%H%M%S"))[0]
    return {"kind": "img", "name": base + ext, "data": data, "mime": mime, "ext": ext,
            "w": w, "h": h, "size": len(data)}

def prepare_attachment(path):
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if not os.path.isfile(path):
        raise ValueError(f"Couldn't find {name}.")
    size = os.path.getsize(path)
    if ext in CHAT_VIDEO_EXTS:
        if size > CHAT_VIDEO_MAX_BYTES:
            raise ValueError(f"{name} is too big. Videos can be up to {CHAT_VIDEO_MAX_BYTES // (1024 * 1024)} MB.")
        with open(path, "rb") as f:
            data = f.read()
        return {"kind": "vid", "name": name, "data": data, "mime": _VIDEO_MIME.get(ext, "video/mp4"),
                "ext": ext, "w": 0, "h": 0, "size": len(data)}
    if ext == ".gif":
        if size > CHAT_GIF_MAX_BYTES:
            raise ValueError(f"{name} is too big. GIFs can be up to {CHAT_GIF_MAX_BYTES // (1024 * 1024)} MB.")
        img = QImage(path)
        with open(path, "rb") as f:
            data = f.read()
        return {"kind": "gif", "name": name, "data": data, "mime": "image/gif", "ext": ".gif",
                "w": img.width(), "h": img.height(), "size": len(data)}
    if ext in CHAT_IMAGE_EXTS:
        img = QImage(path)
        if ext in (".png", ".jpg", ".jpeg", ".webp") and not img.isNull() and size <= CHAT_IMAGE_MAX_BYTES:
            with open(path, "rb") as f:                 # send the original untouched
                data = f.read()
            ext = ".jpg" if ext == ".jpeg" else ext
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[ext]
            return {"kind": "img", "name": name, "data": data, "mime": mime, "ext": ext,
                    "w": img.width(), "h": img.height(), "size": len(data)}
        return prepare_attachment_image(img, name)
    raise ValueError(f"{name}: only images and videos can be attached.")

# ---- where a downloaded attachment lives on disk (the id/extension come from the other person's data, so they're validated)
def _att_cache_path(att):
    aid = str(att.get("id", ""))
    if not _ATT_ID_RE.match(aid):
        return None
    kind = att.get("k")
    if kind == "gif":
        ext = ".gif"
    elif kind == "vid":
        ext = os.path.splitext(str(att.get("n", "")))[1].lower()
        ext = ext if ext in CHAT_VIDEO_EXTS else ".mp4"
    else:
        ext = str(att.get("x", "")).lower()
        ext = ext if ext in (".png", ".jpg", ".webp") else ".jpg"
    return os.path.join(chat_media_dir(), aid + ext)

def cloud_fetch_media(other, att):
    """Downloads one attachment (once; later calls hit the disk cache) and returns its file path."""
    path = _att_cache_path(att)
    if not path:
        raise CloudError("This attachment is invalid.", "BAD_ATT")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    token, uid = get_id_token()
    base = f"chatmedia/{_chat_id(uid, other)}/{att['id']}"
    count = _http_json(_fdb(f"{base}/n", token), timeout=60)
    if isinstance(count, int) and 0 < count <= 64:
        parts = []
        for i in range(count):
            piece = _http_json(_fdb(f"{base}/c/{i}", token), timeout=180)
            if not isinstance(piece, str):
                raise CloudError("This attachment is no longer available.", "GONE")
            parts.append(piece)
        b64 = "".join(parts)
    else:                                               # single-value format
        rec = _http_json(_fdb(base, token), timeout=180)
        b64 = rec.get("d") if isinstance(rec, dict) else None
        if not isinstance(b64, str):
            raise CloudError("This attachment is no longer available.", "GONE")
    data = base64.b64decode(b64)
    if not data or len(data) > 40 * 1024 * 1024:
        raise CloudError("This attachment is invalid.", "BAD_ATT")
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path

def cloud_send_message(other, text, attachments=None, reply=None):
    """attachments: dicts from prepare_attachment(); reply: {'k': message key, 'f': author uid, 't': snippet, 'a': kind}."""
    token, uid = get_id_token()
    chat = _chat_id(uid, other)
    atts = []
    for a in (attachments or [])[:CHAT_MAX_ATTACH]:
        aid = _secrets.token_hex(8)
        b64 = base64.b64encode(a["data"]).decode("ascii")
        pieces = [b64[i:i + CHAT_CHUNK_CHARS] for i in range(0, len(b64), CHAT_CHUNK_CHARS)]
        for i, piece in enumerate(pieces):
            _http_json(_fdb(f"chatmedia/{chat}/{aid}/c/{i}", token), "PUT", piece, timeout=300)
        _http_json(_fdb(f"chatmedia/{chat}/{aid}/m", token), "PUT", a["mime"])
        _http_json(_fdb(f"chatmedia/{chat}/{aid}/n", token), "PUT", len(pieces))
        atts.append({"id": aid, "k": a["kind"], "n": str(a["name"])[:80], "x": a["ext"],
                     "s": a["size"], "w": a["w"], "h": a["h"]})
    msg = {"from": uid, "text": (text or "")[:500], "ts": {".sv": "timestamp"}}
    if atts:
        msg["att"] = atts
    if isinstance(reply, dict) and reply.get("k"):
        msg["re"] = {"k": str(reply["k"]), "f": str(reply.get("f", "")),
                     "t": str(reply.get("t", ""))[:100], "a": str(reply.get("a", ""))}
    _http_json(_fdb(f"chats/{chat}", token), "POST", msg)

def cloud_set_reaction(other, msg_key, code, on):
    """Adds / removes this user's reaction. code is the Twemoji file name, e.g. '1f44d'."""
    if not _EMOJI_CODE_RE.match(code or "") or not msg_key:
        return
    token, uid = get_id_token()
    url = _fdb(f"chats/{_chat_id(uid, other)}/{msg_key}/rx/{code}/{uid}", token)
    if on:
        _http_json(url, "PUT", True)
    else:
        _http_json(url, "DELETE")

def _friend_status(d):
    if d.get("playing"):
        return f"Playing {pretty_game_name(d['playing'])}", GREEN
    seen = d.get("seen")
    if isinstance(seen, (int, float)) and time.time() * 1000 - seen < FRIEND_ONLINE_WINDOW_MS:
        if d.get("idle") is True:
            return "Idle", "#3b82f6"
        return "Online", GREEN
    return "Offline", "#888888"

def _friend_avatar_file(uid, photo):
    raw = _decode_photo(photo)
    if not raw:
        return None
    folder = os.path.join(os.path.dirname(get_launcher_settings_path()), "friend_avatars")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, re.sub(r"[^A-Za-z0-9]", "_", uid) + ".png")
    with open(path, "wb") as f:
        f.write(raw)
    return path

# ---------------------------------------------------------------- chat: Twemoji, picker, message rows
# Apple emoji art (iamcal/emoji-data). Files there are named like '1f44d.png' / '2764-fe0f.png' (FE0F kept), so several
# spellings of a code are tried. Twemoji is only the last resort when an emoji isn't in the Apple set.
APPLE_EMOJI_URLS = (
    "https://cdn.jsdelivr.net/gh/iamcal/emoji-data@master/img-apple-64/{code}.png",
    "https://raw.githubusercontent.com/iamcal/emoji-data/master/img-apple-64/{code}.png",
    "https://cdn.jsdelivr.net/npm/emoji-datasource-apple@15.0.1/img/apple/64/{code}.png",
)
TWEMOJI_URLS = (
    "https://raw.githubusercontent.com/jdecked/twemoji/v15.1.0/assets/72x72/{code}.png",
    "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/{code}.png",
)

def _apple_code_variants(code):
    parts = code.split("-")
    out = [code, code + "-fe0f"]
    if len(parts) > 1:
        out.append("-".join([parts[0], "fe0f"] + parts[1:]))
    seen, res = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            res.append(c)
    return res

# Flags: every country flag is two regional-indicator letters.
_FLAG_COUNTRIES = ("AF AL DZ AD AO AG AR AM AU AT AZ BS BH BD BB BY BE BZ BJ BT BO BA BW BR BN BG BF BI KH CM CA CV CF TD CL "
                   "CN CO KM CG CD CR CI HR CU CY CZ DK DJ DM DO EC EG SV GQ ER EE SZ ET FJ FI FR GA GM GE DE GH GR GD GT GN "
                   "GW GY HT HN HU IS IN ID IR IQ IE IL IT JM JP JO KZ KE KI KW KG LA LV LB LS LR LY LI LT LU MG MW MY MV ML "
                   "MT MH MR MU MX FM MD MC MN ME MA MZ MM NA NR NP NL NZ NI NE NG KP MK NO OM PK PW PS PA PG PY PE PH PL PT "
                   "QA RO RU RW KN LC VC WS SM ST SA SN RS SC SL SG SK SI SB SO ZA KR SS ES LK SD SR SE CH SY TW TJ TZ TH TL "
                   "TG TO TT TN TR TM TV UG UA AE GB US UY UZ VU VA VE VN YE ZM ZW XK HK MO PR GL FO GI GU AS VG KY BM AW CW "
                   "AX EU UN").split()
_FLAGS_ITEMS = "🏁 🚩 🎌 🏴 🏳️ 🏳️‍🌈 🏴‍☠️ " + " ".join("".join(chr(0x1F1E6 + ord(c) - 65) for c in cc) for cc in _FLAG_COUNTRIES)

EMOJI_CATEGORIES = [
    ("Smileys", "\U0001F600",
     "😀 😃 😄 😁 😆 😅 😂 🤣 😊 😇 🙂 😉 😍 🥰 😘 😋 😛 😜 🤪 😎 🤩 🥳 😏 😒 😞 😔 😟 😕 🙁 😣 😖 😫 😩 🥺 😢 😭 😤 😠 😡 🤬 🤯 😳 🥵 🥶 😱 😨 😰 😓 🤗 🤔 🤭 🤫 😶 😐 😑 😬 🙄 😯 😮 😲 🥱 😴 🤤 😵 🤐 🥴 🤢 🤮 🤧 😷 🤒 🤕 🤑 🤠 😈 👿 💀 ☠️ 👻 👽 👾 🤖 💩 🤡"),
    ("Gestures", "\U0001F44D",
     "👍 👎 👌 ✌️ 🤞 🤟 🤘 🤙 👈 👉 👆 👇 ☝️ 👋 ✋ 🖖 👏 🙌 👐 🤲 🤝 🙏 ✍️ 💪 🧠 👀 👁️ 👅 👄 🫡 🫶"),
    ("Hearts", "\u2764\uFE0F",
     "❤️ 🧡 💛 💚 💙 💜 🖤 🤍 🤎 💔 ❣️ 💕 💞 💓 💗 💖 💘 💝 💯 💢 💥 💫 💦 💨 🔥 ✨ ⭐ 🌟 💤 ✅ ❌ ❓ ❗ ‼️ ⚠️ 🚫"),
    ("Animals", "\U0001F431",
     "🐶 🐱 🐭 🐹 🐰 🦊 🐻 🐼 🐨 🐯 🦁 🐮 🐷 🐸 🐵 🙈 🙉 🙊 🐔 🐧 🐦 🦆 🦉 🐺 🐴 🦄 🐝 🐛 🦋 🐌 🐞 🐢 🐍 🐙 🦑 🦀 🐠 🐬 🐳 🦈 🐊 🌵 🌲 🌳 🌴 🌸 🌹 🌻 🍀 🍄 🌈 ☀️ 🌙 ⚡ ❄️ 🌊"),
    ("Food", "\U0001F355",
     "🍎 🍌 🍇 🍓 🍒 🍑 🍍 🥭 🥝 🍅 🥑 🌽 🥕 🍞 🧀 🍔 🍟 🍕 🌭 🌮 🍿 🍩 🍪 🎂 🍰 🍫 🍬 🍭 ☕ 🍺 🍷 🥤"),
    ("Fun", "\U0001F3AE",
     "⚽ 🏀 🏈 ⚾ 🎾 🏐 🎱 🎮 🕹️ 🎲 🧩 🎯 🎳 🎸 🎹 🎧 🎤 🎬 🏆 🥇 🎉 🎊 🎁 🎈 💻 📱 ⌨️ 🖥️ 💡 🔑 🔒 📷 📚 📌 ✏️ 🚀 ✈️ 🚗"),
    ("Flags", "\U0001F3C1", _FLAGS_ITEMS),
]

# ---- the full emoji list (names + search keywords) from iamcal/emoji-data, cached on disk; the short built-in list
# above is only used until it has been downloaded (or when offline)
EMOJI_CATALOG_URLS = (
    "https://cdn.jsdelivr.net/gh/iamcal/emoji-data@master/emoji.json",
    "https://raw.githubusercontent.com/iamcal/emoji-data/master/emoji.json",
)
_EMOJI_CAT_ICONS = {"Smileys & Emotion": "\U0001F600", "People & Body": "\U0001F44B", "Animals & Nature": "\U0001F431",
                    "Food & Drink": "\U0001F355", "Travel & Places": "\U0001F697", "Activities": "\u26BD",
                    "Objects": "\U0001F4A1", "Symbols": "\u2764\uFE0F", "Flags": "\U0001F3C1"}
_emoji_catalog_mem = None
_emoji_catalog_fetching = False

def _emoji_catalog_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "store_cache", "emoji_catalog_v1.json")

def _build_emoji_rows(data):
    rows = []
    for e in sorted((x for x in data if isinstance(x, dict)), key=lambda x: x.get("sort_order") or 0):
        cat, uni = e.get("category"), e.get("unified")
        if not cat or cat == "Component" or not uni:
            continue
        try:
            ch = "".join(chr(int(c, 16)) for c in uni.split("-"))
        except ValueError:
            continue
        names = [str(e.get("name") or "")] + [str(n).replace("_", " ") for n in (e.get("short_names") or [])]
        rows.append([cat, ch, " ".join(names).lower()])
    return rows

def _prefetch_emoji_catalog():
    """Downloads the emoji list in the background (once a month at most)."""
    global _emoji_catalog_fetching
    path = _emoji_catalog_path()
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 30 * 86400:
            return
    except OSError:
        pass
    if _emoji_catalog_fetching:
        return
    _emoji_catalog_fetching = True
    def work():
        global _emoji_catalog_mem, _emoji_catalog_fetching
        try:
            for url in EMOJI_CATALOG_URLS:
                try:
                    rows = _build_emoji_rows(json.loads(_http_get(url, timeout=30).decode("utf-8")))
                    if len(rows) > 1000:
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path + ".part", "w", encoding="utf-8") as f:
                            json.dump(rows, f, ensure_ascii=False)
                        os.replace(path + ".part", path)
                        _emoji_catalog_mem = None
                        break
                except Exception:
                    pass
        finally:
            _emoji_catalog_fetching = False
    threading.Thread(target=work, daemon=True).start()

def emoji_catalog():
    """[[category, emoji, search text]] - every emoji once downloaded, else the built-in list."""
    global _emoji_catalog_mem
    if _emoji_catalog_mem:
        return _emoji_catalog_mem
    try:
        with open(_emoji_catalog_path(), encoding="utf-8") as f:
            rows = json.load(f)
        if isinstance(rows, list) and len(rows) > 1000:
            _emoji_catalog_mem = rows
            return rows
    except Exception:
        pass
    import unicodedata
    rows = []
    for name, _icon, items in EMOJI_CATEGORIES:
        for ch in items.split():
            try:
                nm = unicodedata.name(ch[0]).lower()
            except ValueError:
                nm = ""
            rows.append([name, ch, nm])
    return rows


# ---- flags are searchable by country name (the downloaded list only has codes like "flag-eg")
_FLAG_NAMES_RAW = "AC:Ascension Island|AD:Andorra|AE:United Arab Emirates uae emirates|AF:Afghanistan|AG:Antigua & Barbuda antigua barbuda|AI:Anguilla|AL:Albania|AM:Armenia|AO:Angola|AQ:Antarctica|AR:Argentina|AS:American Samoa|AT:Austria|AU:Australia|AW:Aruba|AX:Åland Islands|AZ:Azerbaijan|BA:Bosnia & Herzegovina bosnia|BB:Barbados|BD:Bangladesh|BE:Belgium|BF:Burkina Faso|BG:Bulgaria|BH:Bahrain|BI:Burundi|BJ:Benin|BL:St. Barthélemy|BM:Bermuda|BN:Brunei|BO:Bolivia|BQ:Caribbean Netherlands|BR:Brazil|BS:Bahamas|BT:Bhutan|BV:Bouvet Island|BW:Botswana|BY:Belarus|BZ:Belize|CA:Canada|CC:Cocos (Keeling) Islands|CD:Congo - Kinshasa congo drc|CF:Central African Republic|CG:Congo - Brazzaville congo|CH:Switzerland|CI:Côte d’Ivoire ivory coast cote d'ivoire|CK:Cook Islands|CL:Chile|CM:Cameroon|CN:China|CO:Colombia|CP:Clipperton Island|CQ:Sark|CR:Costa Rica|CU:Cuba|CV:Cape Verde cape verde|CW:Curaçao|CX:Christmas Island|CY:Cyprus|CZ:Czechia czechia czech republic|DE:Germany|DG:Diego Garcia|DJ:Djibouti|DK:Denmark|DM:Dominica|DO:Dominican Republic|DZ:Algeria|EA:Ceuta & Melilla|EC:Ecuador|EE:Estonia|EG:Egypt|EH:Western Sahara|ER:Eritrea|ES:Spain|ET:Ethiopia|EU:European Union european union europe|EZ:Eurozone|FI:Finland|FJ:Fiji|FK:Falkland Islands|FM:Micronesia|FO:Faroe Islands|FR:France|GA:Gabon|GB:United Kingdom uk britain great britain england|GD:Grenada|GE:Georgia|GF:French Guiana|GG:Guernsey|GH:Ghana|GI:Gibraltar|GL:Greenland|GM:Gambia|GN:Guinea|GP:Guadeloupe|GQ:Equatorial Guinea|GR:Greece|GS:South Georgia & South Sandwich Islands|GT:Guatemala|GU:Guam|GW:Guinea-Bissau|GY:Guyana|HK:Hong Kong SAR China hong kong|HM:Heard & McDonald Islands|HN:Honduras|HR:Croatia|HT:Haiti|HU:Hungary|IC:Canary Islands|ID:Indonesia|IE:Ireland|IL:Israel|IM:Isle of Man|IN:India|IO:British Indian Ocean Territory|IQ:Iraq|IR:Iran|IS:Iceland|IT:Italy|JE:Jersey|JM:Jamaica|JO:Jordan|JP:Japan|KE:Kenya|KG:Kyrgyzstan|KH:Cambodia|KI:Kiribati|KM:Comoros|KN:St. Kitts & Nevis saint kitts nevis|KP:North Korea korea north korea|KR:South Korea korea south korea|KW:Kuwait|KY:Cayman Islands|KZ:Kazakhstan|LA:Laos|LB:Lebanon|LC:St. Lucia|LI:Liechtenstein|LK:Sri Lanka|LR:Liberia|LS:Lesotho|LT:Lithuania|LU:Luxembourg|LV:Latvia|LY:Libya|MA:Morocco|MC:Monaco|MD:Moldova|ME:Montenegro|MF:St. Martin|MG:Madagascar|MH:Marshall Islands|MK:North Macedonia macedonia|ML:Mali|MM:Myanmar (Burma) burma|MN:Mongolia|MO:Macao SAR China macao macau|MP:Northern Mariana Islands|MQ:Martinique|MR:Mauritania|MS:Montserrat|MT:Malta|MU:Mauritius|MV:Maldives|MW:Malawi|MX:Mexico|MY:Malaysia|MZ:Mozambique|NA:Namibia|NC:New Caledonia|NE:Niger|NF:Norfolk Island|NG:Nigeria|NI:Nicaragua|NL:Netherlands holland|NO:Norway|NP:Nepal|NR:Nauru|NU:Niue|NZ:New Zealand|OM:Oman|PA:Panama|PE:Peru|PF:French Polynesia|PG:Papua New Guinea|PH:Philippines|PK:Pakistan|PL:Poland|PM:St. Pierre & Miquelon|PN:Pitcairn Islands|PR:Puerto Rico|PS:Palestinian Territories palestine|PT:Portugal|PW:Palau|PY:Paraguay|QA:Qatar|RE:Réunion|RO:Romania|RS:Serbia|RU:Russia russia|RW:Rwanda|SA:Saudi Arabia|SB:Solomon Islands|SC:Seychelles|SD:Sudan|SE:Sweden|SG:Singapore|SH:St. Helena saint helena|SI:Slovenia|SJ:Svalbard & Jan Mayen|SK:Slovakia|SL:Sierra Leone|SM:San Marino|SN:Senegal|SO:Somalia|SR:Suriname|SS:South Sudan|ST:São Tomé & Príncipe sao tome principe|SV:El Salvador|SX:Sint Maarten|SY:Syria|SZ:Eswatini swaziland eswatini|TA:Tristan da Cunha|TC:Turks & Caicos Islands|TD:Chad|TF:French Southern Territories|TG:Togo|TH:Thailand|TJ:Tajikistan|TK:Tokelau|TL:Timor-Leste east timor|TM:Turkmenistan|TN:Tunisia|TO:Tonga|TR:Türkiye turkey turkiye|TT:Trinidad & Tobago trinidad tobago|TV:Tuvalu|TW:Taiwan taiwan|TZ:Tanzania|UA:Ukraine|UG:Uganda|UM:U.S. Outlying Islands|UN:United Nations united nations|US:United States usa america united states of america|UY:Uruguay|UZ:Uzbekistan|VA:Vatican City vatican holy see|VC:St. Vincent & Grenadines saint vincent grenadines|VE:Venezuela|VG:British Virgin Islands|VI:U.S. Virgin Islands|VN:Vietnam|VU:Vanuatu|WF:Wallis & Futuna|WS:Samoa|XK:Kosovo|YE:Yemen|YT:Mayotte|ZA:South Africa|ZM:Zambia|ZW:Zimbabwe"
_FLAG_NAMES = None

def _flag_names():
    global _FLAG_NAMES
    if _FLAG_NAMES is None:
        import unicodedata
        out = {"GBENG": "england", "GBSCT": "scotland", "GBWLS": "wales"}
        for part in _FLAG_NAMES_RAW.split("|"):
            code, _, name = part.partition(":")
            plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
            out[code] = plain.replace("&", "and").lower() + " " + name.replace("&", "and").lower()
        _FLAG_NAMES = out
    return _FLAG_NAMES

def _flag_search_text(ch):
    """'country name' words for a flag emoji (regional-indicator pair or England / Scotland / Wales), else ''."""
    cps = [ord(c) for c in ch]
    if len(cps) == 2 and all(0x1F1E6 <= c <= 0x1F1FF for c in cps):
        code = "".join(chr(c - 0x1F1E6 + 65) for c in cps)
    else:
        code = "".join(chr(c - 0xE0000).upper() for c in cps if 0xE0061 <= c <= 0xE007A)
    return _flag_names().get(code, "")


def emoji_code(ch):
    """Character(s) -> Twemoji file name ('1f44d'): lowercase hex code points, FE0F dropped unless there's a ZWJ."""
    cps = [f"{ord(c):x}" for c in ch]
    if "200d" not in cps:
        cps = [c for c in cps if c != "fe0f"]
    return "-".join(cps)

def emoji_from_code(code):
    try:
        return "".join(chr(int(c, 16)) for c in code.split("-"))
    except ValueError:
        return "?"

_EMOJI_DL_SLOTS = threading.Semaphore(8)

class _EmojiStore(QObject):
    """Downloads Apple emoji PNGs on demand (cached on disk) and hands them to whoever asked."""
    loaded = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._pix = {}
        self._waiters = {}
        self._busy = set()
        self._failed = {}
        self.loaded.connect(self._on_loaded)

    def _file(self, code):
        folder = os.path.join(os.path.dirname(get_launcher_settings_path()), "emoji_apple")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, code + ".png")

    def pixmap(self, code):
        pm = self._pix.get(code)
        if pm is not None:
            return pm
        path = self._file(code)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            pm = QPixmap(path)
            if not pm.isNull():
                self._pix[code] = pm
                return pm
        return None

    def bind(self, code, setter):
        """Calls setter(QPixmap) now if the emoji is cached, otherwise as soon as it has downloaded."""
        if not _EMOJI_CODE_RE.match(code or ""):
            return
        pm = self.pixmap(code)
        if pm is not None:
            setter(pm)
            return
        self._waiters.setdefault(code, []).append(setter)
        self._fetch(code)

    def _fetch(self, code):
        if code in self._busy or time.time() - self._failed.get(code, 0) < 60:
            return
        self._busy.add(code)
        path = self._file(code)
        def work():
            data = None
            with _EMOJI_DL_SLOTS:                       # at most a few downloads at once (the Flags tab has ~250)
                for url in APPLE_EMOJI_URLS:
                    for c in _apple_code_variants(code):
                        try:
                            blob = _http_get(url.format(code=c), timeout=10)
                            if blob[:4] == b"\x89PNG":
                                data = blob
                                break
                        except Exception:
                            pass
                    if data:
                        break
                if not data:
                    for url in TWEMOJI_URLS:
                        try:
                            blob = _http_get(url.format(code=code), timeout=10)
                            if blob[:4] == b"\x89PNG":
                                data = blob
                                break
                        except Exception:
                            pass
            if data:
                try:
                    with open(path + ".part", "wb") as f:
                        f.write(data)
                    os.replace(path + ".part", path)
                except OSError:
                    data = None
            if not data:
                self._failed[code] = time.time()
            self.loaded.emit(code)
        threading.Thread(target=work, daemon=True).start()

    def _on_loaded(self, code):
        self._busy.discard(code)
        pm = self.pixmap(code)
        setters = self._waiters.pop(code, [])
        if pm is None:
            return
        for setter in setters:
            try:
                setter(pm)
            except RuntimeError:        # the widget was deleted while the download ran
                pass

_emoji_store_obj = None

def emoji_store():
    global _emoji_store_obj
    if _emoji_store_obj is None:
        _emoji_store_obj = _EmojiStore()
        try:
            _prefetch_emoji_catalog()
        except Exception:
            pass
    return _emoji_store_obj

def _emoji_button(ch, size, parent=None):
    """A flat button that shows the Apple emoji image for ch (the plain character until the image arrives)."""
    btn = QPushButton(ch, parent)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFlat(True)
    btn.setFixedSize(size + 12, size + 10)
    btn.setStyleSheet("QPushButton{border:none;border-radius:6px;background:transparent;padding:0;}"
                      "QPushButton:hover{background:rgba(128,128,128,0.30);}")
    def put(pm, b=btn):
        b.setText("")
        b.setIcon(QIcon(pm))
        b.setIconSize(QSize(size, size))
    emoji_store().bind(emoji_code(ch), put)
    return btn

# ---- Apple emoji inside normal text (chat bubbles, reply quotes): the OS font would draw its own emoji otherwise
_EL = (r"(?:[\U0001F1E6-\U0001F1FF]{2}"                                   # flag
       r"|[0-9#*]\uFE0F?\u20E3"                                           # keycap
       r"|(?:[\U0001F300-\U0001FAFF\u231A\u231B\u23E9-\u23EC\u23F0\u23F3\u25FD\u25FE\u2614\u2615\u2648-\u2653"
       r"\u267F\u2693\u26A1\u26AA\u26AB\u26BD\u26BE\u26C4\u26C5\u26CE\u26D4\u26EA\u26F2\u26F3\u26F5\u26FA\u26FD"
       r"\u2705\u270A\u270B\u2728\u274C\u274E\u2753-\u2755\u2757\u2795-\u2797\u27B0\u27BF\u2B1B\u2B1C\u2B50\u2B55]"
       r"|[\u00A9\u00AE\u203C\u2049\u2122\u2139\u2190-\u21FF\u2300-\u23FF\u24C2\u25A0-\u25FF\u2600-\u27BF"
       r"\u2900-\u297F\u2B00-\u2BFF\u3030\u303D\u3297\u3299]\uFE0F)"
       r"[\U0001F3FB-\U0001F3FF]?\uFE0F?[\U000E0020-\U000E007F]*)")
_EMOJI_TEXT_RE = re.compile(_EL + r"(?:\u200D" + _EL + r")*")

def emoji_html(text, size=20, refresh=None):
    """Plain text -> rich text with every emoji as its Apple image. None when the text has no emoji.
    Emoji that haven't been downloaded yet stay as characters; refresh() is called when they arrive."""
    if not _EMOJI_TEXT_RE.search(text):
        return None
    store = emoji_store()
    out, pos = [], 0
    for mt in _EMOJI_TEXT_RE.finditer(text):
        out.append(html_lib.escape(text[pos:mt.start()], quote=False))
        ch = mt.group(0)
        code = emoji_code(ch)
        path = store._file(code)
        if _EMOJI_CODE_RE.match(code) and os.path.exists(path) and os.path.getsize(path) > 0:
            out.append(f'<img src="{QUrl.fromLocalFile(path).toString()}" width="{size}" height="{size}" '
                       f'style="vertical-align:middle">')
        else:
            out.append(html_lib.escape(ch, quote=False))
            if refresh is not None and _EMOJI_CODE_RE.match(code):
                store.bind(code, lambda _pm, r=refresh: r())
        pos = mt.end()
    out.append(html_lib.escape(text[pos:], quote=False))
    return '<span style="white-space:pre-wrap">' + "".join(out).replace("\n", "<br>") + "</span>"

def set_emoji_text(label, text, size=None):
    """Sets label's text; emoji are drawn as Apple images sized to the label's font (re-rendered once their pictures
    have downloaded)."""
    def render():
        try:
            label.ensurePolished()
            base = size or max(12, int(label.fontMetrics().height() * 0.95))
            only = not _EMOJI_TEXT_RE.sub("", text).strip() and len(_EMOJI_TEXT_RE.findall(text)) <= 8
            h = emoji_html(text, base * 2 if only else base, render)
            if h is None:
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setText(text)
            else:
                label.setTextFormat(Qt.TextFormat.RichText)
                label.setText(h)
        except RuntimeError:            # the label was deleted while an image downloaded
            pass
    render()

class EmojiPicker(QFrame):
    picked = pyqtSignal(str)
    COLS = 8
    CHUNK = 80                       # buttons are created a chunk at a time (and only then downloaded)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("EmojiPicker")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setFixedSize(self.COLS * 38 + 28, 380)
        self._cats = {}
        for cat, ch, search in emoji_catalog():
            fl = _flag_search_text(ch)
            if fl:
                search = search + " flag " + fl
            self._cats.setdefault(cat, []).append((ch, search))
        self._all = [(ch, search) for items in self._cats.values() for ch, search in items]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search emoji...")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        lay.addWidget(self._search)
        tabs = QHBoxLayout()
        tabs.setSpacing(2)
        self._cat_names = list(self._cats)
        for i, name in enumerate(self._cat_names):
            icon = _EMOJI_CAT_ICONS.get(name) or self._cats[name][0][0]
            b = _emoji_button(icon, 20)
            b.setToolTip(name)
            b.clicked.connect(lambda _=False, n=i: self._pick_cat(n))
            tabs.addWidget(b)
        tabs.addStretch()
        lay.addLayout(tabs)
        self._title = QLabel("")
        self._title.setStyleSheet("color:#888888; font-size:11px;")
        lay.addWidget(self._title)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.verticalScrollBar().valueChanged.connect(self._maybe_more)
        lay.addWidget(self._scroll, 1)
        self._items, self._built, self._grid = [], 0, None
        self._show_items(self._cat_names[0] if self._cat_names else "", self._cats.get(self._cat_names[0], []) if self._cat_names else [])

    def _pick_cat(self, i):
        if self._search.text():
            self._search.blockSignals(True)
            self._search.clear()
            self._search.blockSignals(False)
        name = self._cat_names[i]
        self._show_items(name, self._cats[name])

    def _on_search(self, text):
        terms = text.lower().split()
        if not terms:
            if self._cat_names:
                self._show_items(self._cat_names[0], self._cats[self._cat_names[0]])
            return
        hits = [(ch, s) for ch, s in self._all if all(t in s for t in terms)]
        self._show_items(f"{len(hits)} result{'s' if len(hits) != 1 else ''}" if hits else "No emoji found", hits)

    def _show_items(self, title, items):
        self._title.setText(title)
        self._items, self._built = items, 0
        holder = QWidget()
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        gw = QWidget()
        self._grid = QGridLayout(gw)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(2)
        outer.addWidget(gw)
        outer.addStretch(1)
        self._scroll.setWidget(holder)
        self._more()

    def _more(self):
        grid = self._grid
        if grid is None:
            return
        for n in range(self._built, min(self._built + self.CHUNK, len(self._items))):
            ch = self._items[n][0]
            b = _emoji_button(ch, 24)
            b.clicked.connect(lambda _=False, c=ch: self._pick(c))
            grid.addWidget(b, n // self.COLS, n % self.COLS)
        self._built = min(self._built + self.CHUNK, len(self._items))

    def _maybe_more(self, value):
        bar = self._scroll.verticalScrollBar()
        if self._built < len(self._items) and value >= bar.maximum() - 160:
            self._more()

    def _pick(self, ch):
        self.close()
        self.picked.emit(ch)

    def show_near(self, anchor):
        below = anchor.mapToGlobal(QPoint(0, anchor.height()))
        above = anchor.mapToGlobal(QPoint(0, 0))
        screen = QApplication.screenAt(below) or QApplication.primaryScreen()
        g = screen.availableGeometry()
        x = max(g.left(), min(below.x(), g.right() - self.width()))
        y = below.y() if below.y() + self.height() <= g.bottom() else above.y() - self.height()
        self.move(x, max(g.top(), y))
        self.show()
        self._search.setFocus()


class _ClickLabel(QLabel):
    clicked = pyqtSignal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


_CI_PROP = 0x100001      # QTextFormat user property holding the emoji an inline image stands for

def _doc_text_with_emoji(doc):
    """Document text with every inline emoji image turned back into its emoji character(s)."""
    plain = doc.toPlainText()
    if "\ufffc" not in plain:
        return plain
    cur = QTextCursor(doc)
    out, u16 = [], 0
    for c in plain:
        if c == "\ufffc":
            cur.setPosition(u16 + 1)
            f = cur.charFormat()
            out.append(f.toImageFormat().stringProperty(_CI_PROP) if f.isImageFormat() else "")
        else:
            out.append(c)
        u16 += 2 if ord(c) > 0xFFFF else 1
    return "".join(out)

class _ChatInput(QTextEdit):
    """One-line message box (looks like a line edit) that shows emoji as Apple images, also accepts pasted images /
    copied files, Enter to send and Esc to cancel a reply."""
    image_pasted = pyqtSignal(object)
    files_pasted = pyqtSignal(object)
    escaped = pyqtSignal()
    returnPressed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatInput")
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.document().setDocumentMargin(0)
        self.document().setUndoRedoEnabled(False)       # the emoji -> image swap would otherwise pollute undo
        self._max, self._busy, self._img = 0, False, 18
        self.setFixedHeight(max(self.fontMetrics().height(), self._img) + 18)
        self.textChanged.connect(self._on_changed)

    # ---- the QLineEdit-style API the chat page uses
    def text(self):
        return _doc_text_with_emoji(self.document()).replace("\n", " ")

    def setText(self, t):
        self.setPlainText(str(t).replace("\n", " "))

    def insert(self, s):
        c = self.textCursor()
        c.insertText(str(s).replace("\n", " "))
        self.setTextCursor(c)

    def setMaxLength(self, n):
        self._max = int(n)

    # ---- emoji -> images
    def _on_changed(self):
        if self._busy:
            return
        self._busy = True
        try:
            if self._max:
                cur = None
                while len(self.text()) > self._max:
                    if cur is None:
                        cur = QTextCursor(self.document())
                        cur.movePosition(QTextCursor.MoveOperation.End)
                    cur.deletePreviousChar()
            self._convert()
        finally:
            self._busy = False

    def _later(self):
        try:
            self._on_changed()
        except RuntimeError:        # the widget was deleted while an image downloaded
            pass

    def _convert(self):
        text = self.document().toPlainText()
        matches = list(_EMOJI_TEXT_RE.finditer(text))
        if not matches:
            return
        store = emoji_store()
        cur = QTextCursor(self.document())
        for mt in reversed(matches):                    # last first, so earlier positions stay valid
            ch = mt.group(0)
            code = emoji_code(ch)
            if not _EMOJI_CODE_RE.match(code):
                continue
            path = store._file(code)
            if not (os.path.exists(path) and os.path.getsize(path) > 0):
                store.bind(code, lambda _pm: self._later())      # converts once it has downloaded
                continue
            s16 = len(text[:mt.start()].encode("utf-16-le")) // 2
            e16 = s16 + len(ch.encode("utf-16-le")) // 2
            cur.setPosition(s16)
            cur.setPosition(e16, QTextCursor.MoveMode.KeepAnchor)
            fmt = QTextImageFormat()
            fmt.setName(QUrl.fromLocalFile(path).toString())
            img = max(12, int(self.fontMetrics().height() * 0.95))
            fmt.setWidth(img)
            fmt.setHeight(img)
            fmt.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignMiddle)
            fmt.setProperty(_CI_PROP, ch)
            cur.insertImage(fmt)

    # ---- clipboard: always plain one-line text, emoji kept as characters
    def insertFromMimeData(self, source):
        if source.hasText():
            self.insertPlainText(source.text().replace("\r", " ").replace("\n", " "))

    def createMimeDataFromSelection(self):
        d = QTextDocument()
        QTextCursor(d).insertFragment(self.textCursor().selection())
        md = QMimeData()
        md.setText(_doc_text_with_emoji(d))
        return md

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.returnPressed.emit()
            return
        if e.key() == Qt.Key.Key_Escape:
            self.escaped.emit()
            return
        if e.matches(QKeySequence.StandardKey.Paste):
            md = QApplication.clipboard().mimeData()
            if md is not None:
                files = [u.toLocalFile() for u in md.urls() if u.isLocalFile()] if md.hasUrls() else []
                if files:
                    self.files_pasted.emit(files)
                    return
                if md.hasImage() and not md.hasText():
                    img = QApplication.clipboard().image()
                    if not img.isNull():
                        self.image_pasted.emit(img)
                        return
        super().keyPressEvent(e)


class _MsgRow(QWidget):
    """One message: optional reply quote, bubble (text + images/videos), reaction chips, time, hover actions."""

    def __init__(self, chat, m, me, mine):
        super().__init__()
        self.chat, self.m, self.mine = chat, m, mine
        self.key = m.get("_key")
        text = str(m.get("text", "") or "")
        inv = parse_invite(text)
        atts = [a for a in (m.get("att") if isinstance(m.get("att"), list) else []) if isinstance(a, dict)]
        align = Qt.AlignmentFlag.AlignRight if mine else Qt.AlignmentFlag.AlignLeft

        col = QVBoxLayout()
        col.setSpacing(2)

        re_ = m.get("re")
        if isinstance(re_, dict):
            col.addWidget(self._quote(re_), 0, align)

        self._bubble_base = (f"#ChatBubble{{background:{THEMES['dark']['accent']}; border-radius:12px;}}" if mine
                             else "#ChatBubble{background:rgba(128,128,128,0.28); border-radius:12px;}")
        if inv:
            self._bubble_base = "#ChatBubble{background:transparent;}"
        self.bubble = QFrame()
        self.bubble.setObjectName("ChatBubble")
        self.bubble.setStyleSheet(self._bubble_base)
        bl = QVBoxLayout(self.bubble)
        pad = (0, 0, 0, 0) if inv else ((10, 7, 10, 7) if text else (4, 4, 4, 4))
        bl.setContentsMargins(*pad)
        bl.setSpacing(6)
        if inv:
            bl.addWidget(self._invite_card(inv, mine))
        elif text:
            lab = QLabel()
            set_emoji_text(lab, text)
            lab.setWordWrap(True)
            lab.setMaximumWidth(380)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lab.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            lab.setStyleSheet("QLabel{background:transparent;" + ("color:#ffffff;" if mine else "") + "}")
            bl.addWidget(lab)
        for att in atts[:CHAT_MAX_ATTACH]:
            bl.addWidget(chat.attachment_widget(att, mine))
        if not text and not atts:
            bl.addWidget(QLabel("(empty message)"))
        col.addWidget(self.bubble, 0, align)

        rx = m.get("rx") if isinstance(m.get("rx"), dict) else {}
        chips = []
        for code in sorted(rx):
            users = [u for u, v in rx[code].items() if v] if isinstance(rx[code], dict) else []
            if users and _EMOJI_CODE_RE.match(code):
                chips.append((code, users))
        for i in range(0, len(chips), 6):
            line = QHBoxLayout()
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(4)
            for code, users in chips[i:i + 6]:
                line.addWidget(self._chip(code, users, me))
            line.addStretch()
            holder = QWidget()
            holder.setLayout(line)
            col.addWidget(holder, 0, align)

        ts = m.get("ts")
        stamp = QLabel(time.strftime("%H:%M", time.localtime(ts / 1000.0)) if isinstance(ts, (int, float)) else "")
        stamp.setStyleSheet("color:#888888; font-size:10px;")
        col.addWidget(stamp, 0, align)

        # hover actions sit beside the bubble, in the empty space, so showing them never moves the bubble
        self.bar = QFrame()
        self.bar.setObjectName("MsgBar")
        self.bar.setStyleSheet("#MsgBar{border:1px solid rgba(128,128,128,0.5); border-radius:8px;"
                               "background:rgba(128,128,128,0.15);}")
        bar_l = QHBoxLayout(self.bar)
        bar_l.setContentsMargins(3, 2, 3, 2)
        bar_l.setSpacing(2)
        self.react_btn = _emoji_button("\U0001F642", 18)
        self.react_btn.setToolTip("Add reaction")
        self.react_btn.clicked.connect(self._pick_reaction)
        reply_btn = QPushButton("Reply")
        reply_btn.setFlat(True)
        reply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reply_btn.setStyleSheet("QPushButton{border:none;padding:2px 6px;border-radius:6px;}"
                                "QPushButton:hover{background:rgba(128,128,128,0.30);}")
        reply_btn.clicked.connect(lambda: chat.reply_requested.emit(self.m))
        bar_l.addWidget(self.react_btn)
        bar_l.addWidget(reply_btn)
        self.bar.hide()

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        if mine:
            outer.addStretch()
            outer.addWidget(self.bar, 0, Qt.AlignmentFlag.AlignTop)
            outer.addLayout(col)
        else:
            outer.addLayout(col)
            outer.addWidget(self.bar, 0, Qt.AlignmentFlag.AlignTop)
            outer.addStretch()

    def _quote(self, re_):
        who = "You" if re_.get("f") == self.chat.me else (self.chat.names.get(re_.get("f")) or "Unknown")
        snippet = str(re_.get("t") or "").strip() or {"img": "Image", "gif": "GIF", "vid": "Video"}.get(
            re_.get("a"), "Attachment" if re_.get("a") else "Original message")
        snippet = snippet if len(snippet) <= 60 else snippet[:57] + "..."
        lab = _ClickLabel()
        set_emoji_text(lab, f"\u21AA {who}: {snippet}")
        lab.setCursor(Qt.CursorShape.PointingHandCursor)
        lab.setStyleSheet("QLabel{color:#888888; font-size:11px; border-left:3px solid " + THEMES["dark"]["accent"] + "; padding-left:6px;}")
        lab.setMaximumWidth(380)
        lab.setToolTip("Jump to the original message")
        key = re_.get("k")
        lab.clicked.connect(lambda: self.chat.jump_to(key))
        return lab

    def _chip(self, code, users, me):
        mine_on = me in users
        btn = QPushButton(f"{emoji_from_code(code)} {len(users)}")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        names = ["You" if u == me else (self.chat.names.get(u) or "Someone") for u in users]
        btn.setToolTip(", ".join(names))
        if mine_on:
            _ac = QColor(THEMES["dark"]["accent"])
            style = (f"border:1px solid {_ac.name()}; "
                     f"background:rgba({_ac.red()},{_ac.green()},{_ac.blue()},0.28);")
        else:
            style = "border:1px solid rgba(128,128,128,0.45); background:rgba(128,128,128,0.18);"
        btn.setStyleSheet("QPushButton{" + style + "border-radius:10px; padding:1px 7px;}"
                          "QPushButton:hover{background:rgba(128,128,128,0.35);}")
        def put(pm, b=btn, n=len(users)):
            b.setIcon(QIcon(pm))
            b.setIconSize(QSize(16, 16))
            b.setText(f" {n}")
        emoji_store().bind(code, put)
        key = self.key
        btn.clicked.connect(lambda: key and self.chat.react_requested.emit(key, code))
        return btn

    def _pick_reaction(self, anchor=None):
        key = self.key
        if not key:
            return
        self.chat.open_picker(anchor if isinstance(anchor, QWidget) else self.react_btn,
                              lambda ch: self.chat.react_requested.emit(key, emoji_code(ch)))

    def flash(self):
        try:
            self.bubble.setStyleSheet(self._bubble_base + "#ChatBubble{border:2px solid #ffd666;}")
            QTimer.singleShot(1200, self._unflash)
        except RuntimeError:
            pass

    def _unflash(self):
        try:
            self.bubble.setStyleSheet(self._bubble_base)
        except RuntimeError:
            pass

    def enterEvent(self, e):
        if self.key:
            self.bar.show()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.bar.hide()
        super().leaveEvent(e)

    def contextMenuEvent(self, e):
        if not self.key:
            return
        menu = QMenu(self)
        a_reply = menu.addAction("Reply")
        a_react = menu.addAction("Add reaction...")
        text = str(self.m.get("text", "") or "")
        a_copy = menu.addAction("Copy text") if text else None
        act = menu.exec(e.globalPos())
        if act is a_reply:
            self.chat.reply_requested.emit(self.m)
        elif act is a_react:
            self._pick_reaction(self.bubble)
        elif a_copy is not None and act is a_copy:
            QApplication.clipboard().setText(text)



def _msgrow_invite_card(self, inv, mine):
    acc = QColor(THEMES["dark"]["accent"])
    rgb = f"{acc.red()},{acc.green()},{acc.blue()}"
    card = QFrame()
    card.setObjectName("InviteCard")
    card.setMinimumWidth(280)
    card.setMaximumWidth(340)
    card.setStyleSheet(
        ("#InviteCard{background:rgba(" + rgb + ",0.14);border:1px solid rgba(" + rgb + ",0.55);border-radius:14px;}")
        if mine else
        "#InviteCard{background:rgba(128,128,128,0.16);border:1px solid rgba(128,128,128,0.35);border-radius:14px;}")
    row = QHBoxLayout(card)
    row.setContentsMargins(10, 10, 14, 10)
    row.setSpacing(12)

    cover = QLabel()
    cover.setFixedSize(INVITE_COVER_W, INVITE_COVER_H)
    cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
    cover.setStyleSheet("QLabel{background:rgba(128,128,128,0.25);border-radius:10px;font-size:24px;font-weight:bold;}")
    sender = None if mine else getattr(self.chat, "other_uid", None)
    path = invite_cover_path(inv, sender)
    pix = rounded_cover_pixmap(path, INVITE_COVER_W, INVITE_COVER_H, 8) if path else None
    if pix:
        cover.setPixmap(pix)
    else:
        cover.setText((inv["game"] or "?")[:1].upper())
        want_invite_cover(inv, sender, cover)
    row.addWidget(cover, 0, Qt.AlignmentFlag.AlignTop)

    col = QVBoxLayout()
    col.setSpacing(3)
    tag = QLabel("GAME INVITE")
    tf = tag.font()
    tf.setPixelSize(10)
    tf.setBold(True)
    tf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
    tag.setFont(tf)
    tag.setStyleSheet("QLabel{background:transparent;color:" + acc.name() + ";}")
    name = QLabel(inv["game"] or "A Steam game")
    name.setWordWrap(True)
    name.setMaximumWidth(220)
    name.setStyleSheet("QLabel{background:transparent;font-size:17px;font-weight:bold;}")
    sub = QLabel("Invite sent" if mine else "Wants you to join their game")
    sub.setWordWrap(True)
    sub.setMaximumWidth(220)
    sub.setStyleSheet("QLabel{background:transparent;color:#888888;font-size:11px;}")
    col.addWidget(tag)
    col.addWidget(name)
    col.addWidget(sub)
    col.addStretch()
    if not mine:                                # your own invite has nothing to join
        btn = QPushButton("Join Game")
        btn.setObjectName("Primary")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedHeight(30)
        btn.setToolTip("Opens Steam and joins, the same as accepting an invite in Steam")
        def _join(_c=False, b=btn, i=inv):
            def state(waiting, b=b):
                try:
                    b.setEnabled(not waiting)
                    b.setText("Starting game..." if waiting else "Join Game")
                except RuntimeError:        # the card was closed meanwhile
                    pass
            accept_invite(i["link"], i.get("game", ""), state)
        btn.clicked.connect(_join)
        col.addWidget(btn)
    row.addLayout(col)
    return card

_MsgRow._invite_card = _msgrow_invite_card

class ChatView(QScrollArea):
    """Scrollable chat: bubbles (purple = you, grey = them) with replies, attachments and reactions."""
    reply_requested = pyqtSignal(object)        # the message dict to reply to
    react_requested = pyqtSignal(str, str)      # message key, Twemoji code (toggles the reaction)
    _media_ready = pyqtSignal(str, str, str)    # attachment id, file path, error

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._holder = QWidget()
        self._lay = QVBoxLayout(self._holder)
        self._lay.setContentsMargins(4, 8, 4, 8)
        self._lay.setSpacing(6)
        self._lay.addStretch()
        self.setWidget(self._holder)
        self._stick = False
        self._force_bottom = True
        self.other_uid = None
        self.me = None
        self.names = {}
        self._rows = {}
        self._pix_cache = {}
        self._media_waiters = {}
        self._media_busy = set()
        self._picker = None
        self._picker_cb = None
        self._media_ready.connect(self._on_media_ready)
        bar = self.verticalScrollBar()
        # whenever the content grows, stay glued to the newest message while sticking
        bar.rangeChanged.connect(lambda _mn, mx: bar.setValue(mx) if self._stick else None)

    def reset(self):
        """Call when opening a chat so it starts at the newest message."""
        self._force_bottom = True
        self.clear()

    def clear(self):
        self._rows = {}
        while self._lay.count() > 1:
            w = self._lay.takeAt(0).widget()
            if w:
                w.hide()                        # gone from the screen right away, freed on the next event-loop pass
                w.deleteLater()

    # -- emoji picker (shared by the message rows and the composer)
    def open_picker(self, anchor, callback):
        if self._picker is None:
            self._picker = EmojiPicker(self)
            self._picker.picked.connect(lambda ch: self._picker_cb(ch) if self._picker_cb else None)
        self._picker_cb = callback
        self._picker.show_near(anchor)

    def jump_to(self, key):
        row = self._rows.get(key)
        if row is not None:
            self.ensureWidgetVisible(row, 0, 80)
            row.flash()

    # -- attachments
    def attachment_widget(self, att, mine):
        if att.get("k") == "vid":
            return self._video_button(att, mine)
        return self._image_label(att, att.get("k") == "gif")

    def _cached(self, att):
        path = _att_cache_path(att)
        return path if path and os.path.exists(path) and os.path.getsize(path) > 0 else None

    def _want_media(self, att, cb):
        aid = str(att.get("id", ""))
        self._media_waiters.setdefault(aid, []).append(cb)
        if aid in self._media_busy:
            return
        self._media_busy.add(aid)
        other = self.other_uid
        def work():
            try:
                path, err = cloud_fetch_media(other, att), ""
            except Exception as e:
                path, err = "", str(e) or "Couldn't load this attachment."
            self._media_ready.emit(aid, path, err)
        threading.Thread(target=work, daemon=True).start()

    def _on_media_ready(self, aid, path, err):
        self._media_busy.discard(aid)
        for cb in self._media_waiters.pop(aid, []):
            try:
                cb(path or None, err)
            except RuntimeError:                # the bubble was rebuilt while this downloaded
                pass

    def _image_label(self, att, is_gif):
        w, h = int(att.get("w") or 0), int(att.get("h") or 0)
        if w <= 0 or h <= 0:
            w, h = 320, 200
        scale = min(1.0, 320 / w, 260 / h)
        dw, dh = max(48, int(w * scale)), max(48, int(h * scale))
        lab = _ClickLabel("Loading...")
        lab.setFixedSize(dw, dh)
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lab.setCursor(Qt.CursorShape.PointingHandCursor)
        lab.setStyleSheet("QLabel{background:rgba(128,128,128,0.25); border-radius:8px; color:#aaaaaa;}")
        def paint(path, _err=""):
            if not path:
                lab.setText("Couldn't load")
                return
            if is_gif:
                mv = QMovie(path)
                mv.setScaledSize(QSize(dw, dh))
                mv.setParent(lab)
                lab.setMovie(mv)
                lab.setStyleSheet("QLabel{background:transparent;}")
                mv.start()
                return
            ck = (att.get("id"), dw, dh)
            pm = self._pix_cache.get(ck)
            if pm is None:
                pm = rounded_cover_pixmap(path, dw, dh, 8)
                if pm is not None:
                    if len(self._pix_cache) > 80:
                        self._pix_cache.clear()
                    self._pix_cache[ck] = pm
            if pm is None:
                lab.setText("Couldn't load")
                return
            lab.setStyleSheet("QLabel{background:transparent;}")
            lab.setPixmap(pm)
        cached = self._cached(att)
        if cached:
            paint(cached)
        else:
            self._want_media(att, paint)
        lab.clicked.connect(lambda: self._open_attachment(att))
        return lab

    def _video_button(self, att, mine):
        name = str(att.get("n") or "video")
        name = name if len(name) <= 34 else name[:31] + "..."
        label = f"\u25B6  {name}   ({_chat_fmt_size(att.get('s'))})"
        btn = QPushButton(label)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("Open in your video player")
        btn.setStyleSheet("QPushButton{text-align:left; padding:10px 14px; border:none; border-radius:8px;"
                          "background:rgba(0,0,0,0.25);" + ("color:#ffffff;" if mine else "") + "}"
                          "QPushButton:hover{background:rgba(0,0,0,0.38);}")
        def open_it(path, err=""):
            try:
                btn.setEnabled(True)
                btn.setText(label if path else "Couldn't load this video")
            except RuntimeError:
                return
            if path:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        def clicked():
            cached = self._cached(att)
            if cached:
                open_it(cached)
                return
            btn.setEnabled(False)
            btn.setText("Downloading...")
            self._want_media(att, open_it)
        btn.clicked.connect(clicked)
        return btn

    def _open_attachment(self, att):
        cached = self._cached(att)
        if cached:
            QDesktopServices.openUrl(QUrl.fromLocalFile(cached))
        else:
            self._want_media(att, lambda p, e="": QDesktopServices.openUrl(QUrl.fromLocalFile(p)) if p else None)

    # -- the messages
    def set_messages(self, me, msgs, names=None):
        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 30
        keep = bar.value()
        self.me = me
        self.names = names or {}
        self.clear()
        if not msgs:
            hint = QLabel("No messages yet. Say hi!")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet("color: #888888;")
            self._lay.insertWidget(0, hint)
        for i, m in enumerate(msgs):
            row = _MsgRow(self, m, me, m.get("from") == me)
            if m.get("_key"):
                self._rows[m["_key"]] = row
            self._lay.insertWidget(i, row)
        self._stick = self._force_bottom or at_bottom
        self._force_bottom = False
        if self._stick:
            QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
            QTimer.singleShot(300, lambda: setattr(self, "_stick", False))
        else:                                   # a reaction in the middle of the history shouldn't throw you to the bottom
            for ms in (0, 60):
                QTimer.singleShot(ms, lambda: bar.setValue(keep))


class FriendsPage(QWidget):
    _done = pyqtSignal(object, object, object)
    code_ready = pyqtSignal(str)
    friend_event = pyqtSignal(str, str, str)
    notif_clicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Content")
        self._prev_state = None
        self._prev_reqs = None
        self._prev_last = None
        self.covers = {}
        self._lib_sig = None
        self.notify_uid = None
        self.requests = {}
        self.friends = {}
        self.current_uid = None
        self._last_msg_key = ""
        self._msgs = []
        self._me = None
        self._pending = []          # prepared attachments waiting to be sent
        self._preparing = 0
        self._reply = None
        self._sending = False
        self._rx_inflight = 0
        self._att_seq = 0
        self.setAcceptDrops(True)
        self._done.connect(lambda cb, res, err: cb(res, err))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 28, 36, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.stack.addWidget(self._build_list())
        self.stack.addWidget(self._build_profile())

        self.list_timer = QTimer(self)
        self.list_timer.setInterval(10000)
        self.list_timer.timeout.connect(self.refresh)
        self.chat_timer = QTimer(self)
        self.chat_timer.setInterval(2000)
        self.chat_timer.timeout.connect(self.load_messages)
        self.beat_timer = QTimer(self)
        self.beat_timer.setInterval(60000)
        self.beat_timer.timeout.connect(self.heartbeat)
        self.beat_timer.start()
        self.list_timer.start()  # keeps polling in the background for notifications
        self.msg_timer = QTimer(self)           # new messages: cheap check every 2 s instead of waiting for the 10 s refresh
        self.msg_timer.setInterval(2000)
        self.msg_timer.timeout.connect(self._poll_messages)
        self.msg_timer.start()
        _playing_listeners.append(self._playing_changed)
        QTimer.singleShot(3000, self.heartbeat)

    # -- helpers
    def _run(self, fn, cb, key=None):
        """Runs fn on a thread, then cb(result, error) on the GUI thread. With a key, a call is
        skipped while the previous one with that key is still running, so slow networks don't pile up threads."""
        if key is not None:
            busy = self.__dict__.setdefault("_busy_keys", set())
            if key in busy:
                return
            busy.add(key)
            done_cb = cb
            def cb(res, err):
                busy.discard(key)
                done_cb(res, err)
        def work():
            try:
                res, err = fn(), None
            except Exception as e:
                res, err = None, str(e) or "Something went wrong."
            self._done.emit(cb, res, err)
        threading.Thread(target=work, daemon=True).start()

    def _signed_in(self):
        return cloud_configured() and load_account() is not None

    def _playing_changed(self):
        if self._signed_in():
            self._run(cloud_publish_presence, lambda r, e: None)

    def heartbeat(self):
        if self._signed_in():
            self._run(cloud_publish_presence, lambda r, e: None)

    # -- list page
    def _build_list(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Friends")
        title.setObjectName("PageTitle")
        lay.addWidget(title)
        lay.addSpacing(16)

        panel = QFrame()
        panel.setObjectName("Panel")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(22, 16, 22, 16)
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel("Your friend code")
        t.setObjectName("RowTitle")
        self.code_label = QLabel("-")
        self.code_label.setObjectName("RowDesc")
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(t)
        col.addWidget(self.code_label)
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.code_label.text()))
        row.addLayout(col, 1)
        row.addWidget(copy_btn)
        pl.addLayout(row)
        pl.addSpacing(10)
        add_row = QHBoxLayout()
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("Enter a friend's code")
        self.code_input.returnPressed.connect(self.add_friend)
        add_btn = QPushButton("Add friend")
        add_btn.setObjectName("Primary")
        add_btn.clicked.connect(self.add_friend)
        add_row.addWidget(self.code_input, 1)
        add_row.addWidget(add_btn)
        pl.addLayout(add_row)
        self.status_label = QLabel("")
        self.status_label.setObjectName("RowDesc")
        pl.addWidget(self.status_label)
        lay.addWidget(panel)
        lay.addSpacing(14)

        self.req_box = QFrame()
        self.req_box.setObjectName("Panel")
        self.req_layout = QVBoxLayout(self.req_box)
        self.req_layout.setContentsMargins(22, 14, 22, 14)
        self.req_box.hide()
        lay.addWidget(self.req_box)
        lay.addSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self.rows_layout = QVBoxLayout(holder)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        self.rows_layout.addStretch()
        scroll.setWidget(holder)
        lay.addWidget(scroll, 1)
        return page

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()
        self.list_timer.start()

    def refresh(self):
        if not self._signed_in():
            self._prev_state = None
            self._prev_reqs = None
            self._prev_last = None
            self.code_label.setText("-")
            self.status_label.setText("Sign in from the profile button (bottom left) to use friends.")
            self._clear_rows()
            return
        def work():
            code = cloud_ensure_friend_code()
            cloud_publish_presence()
            friends = cloud_fetch_friends()
            return code, friends, cloud_fetch_requests(), cloud_fetch_last_messages(list(friends))
        self._run(work, self._on_fetched, key="refresh")

    def _poll_messages(self):
        if not self.friends or not self._signed_in():
            return
        ids = list(self.friends)
        self._run(lambda: cloud_fetch_last_messages(ids),
                  lambda res, err: None if err else self._detect_messages(res), key="msgs")

    def _on_fetched(self, res, err):
        if err:
            self.status_label.setText(err)
            return
        code, friends, reqs, last = res
        self.requests = reqs
        self.code_label.setText(code)
        self.code_ready.emit(code)
        self.friends = friends
        self._detect_events()
        if self._prev_last is None:
            self._detect_messages(last)      # baseline only; _poll_messages raises the notifications
        self._rebuild_requests()
        self._rebuild_rows()
        if self.current_uid in self.friends and self.stack.currentIndex() == 1:
            self._fill_profile()

    def _detect_events(self):
        state = {}
        for uid, d in self.friends.items():
            state[uid] = (_friend_status(d)[0] != "Offline", d.get("playing") or "")
        if self._prev_state is not None:
            for uid, (online, playing) in state.items():
                was_online, was_playing = self._prev_state.get(uid, (False, ""))
                name = self.friends[uid].get("username") or "A friend"
                if playing and playing != was_playing:
                    self.friend_event.emit(name, f"is now playing {pretty_game_name(playing)}", uid)
                elif online and not was_online:
                    self.friend_event.emit(name, "is now online", uid)
        self._prev_state = state
        reqs = set(self.requests)
        if self._prev_reqs is not None:
            for uid in reqs - self._prev_reqs:
                name = self.requests[uid].get("username") or "Someone"
                self.friend_event.emit(name, "sent you a friend request", uid)
        self._prev_reqs = reqs

    def _detect_messages(self, last):
        me = (load_account() or {}).get("uid")
        prev = self._prev_last
        if prev is not None:
            for uid, (key, msg) in last.items():
                old = prev.get(uid)
                # old is None: first time this chat is seen (new friend, or an earlier fetch of it failed),
                # so it only sets the baseline. A key that isn't newer is a deleted/edited last message.
                if old is None or msg is None or key <= old or msg.get("from") == me:
                    continue
                viewing = (self.current_uid == uid and self.stack.currentIndex() == 1
                           and self.window().isActiveWindow())
                if viewing:
                    continue
                name = self.friends.get(uid, {}).get("username") or "A friend"
                text = chat_preview(msg)
                self.__dict__.setdefault("_ov_msgs", {})[uid] = msg      # the overlay toast shows pictures from it
                self.friend_event.emit(name, text if len(text) <= 100 else text[:97] + "...", uid)
        # merge instead of replace: a friend whose fetch failed this round keeps their old baseline
        merged = dict(prev or {})
        merged.update({uid: key for uid, (key, _m) in last.items()})
        self._prev_last = {uid: key for uid, key in merged.items() if uid in self.friends}

    def add_friend(self):
        if not self._signed_in():
            self.status_label.setText("Sign in first.")
            return
        code = self.code_input.text()
        self.status_label.setText("Adding...")
        def done(res, err):
            if err:
                self.status_label.setText(err)
            else:
                self.code_input.clear()
                self.status_label.setText("Friend added!" if res == "accepted"
                                          else "Friend request sent! They need to accept it.")
                self.refresh()
        self._run(lambda: cloud_add_friend(code), done)

    def _rebuild_requests(self):
        while self.req_layout.count():
            item = self.req_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w:
                        w.deleteLater()
        self.req_box.setVisible(bool(self.requests))
        if not self.requests:
            return
        head = QLabel(f"Friend requests ({len(self.requests)})")
        head.setObjectName("RowTitle")
        self.req_layout.addWidget(head)
        for uid, d in self.requests.items():
            row = QHBoxLayout()
            name = QLabel(d.get("username") or "Unknown")
            accept = QPushButton("Accept")
            accept.setObjectName("Primary")
            decline = QPushButton("Decline")
            row.addWidget(name, 1)
            row.addWidget(accept)
            row.addWidget(decline)
            accept.clicked.connect(lambda _=False, u=uid: self._answer_request(u, True))
            decline.clicked.connect(lambda _=False, u=uid: self._answer_request(u, False))
            self.req_layout.addLayout(row)

    def _answer_request(self, uid, accept):
        fn = cloud_accept_friend if accept else cloud_decline_friend
        def done(res, err):
            self.status_label.setText(err or ("Friend added!" if accept else "Request declined."))
            self.refresh()
        self._run(lambda: fn(uid), done)

    def _clear_rows(self):
        while self.rows_layout.count() > 1:
            w = self.rows_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

    def _rebuild_rows(self):
        self._clear_rows()
        if not self.friends:
            empty = QLabel("No friends yet. Share your code or enter someone else's above.")
            empty.setObjectName("RowDesc")
            self.rows_layout.insertWidget(0, empty)
            return
        for i, (uid, d) in enumerate(sorted(self.friends.items(),
                                            key=lambda kv: (kv[1].get("username") or "").lower())):
            btn = QPushButton()
            btn.setFixedHeight(56)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            r = QHBoxLayout(btn)
            r.setContentsMargins(10, 0, 10, 0)
            av = QLabel("?")
            av.setObjectName("Avatar")
            av.setFixedSize(36, 36)
            av.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name = d.get("username") or "Unknown"
            apply_avatar(av, _friend_avatar_file(uid, d.get("photo")), name[:1].upper(), 36)
            c = QVBoxLayout()
            c.setSpacing(0)
            n = QLabel(name)
            n.setObjectName("RowTitle")
            status, color = _friend_status(d)
            s = QLabel(status)
            s.setStyleSheet(f"color: {color};")
            c.addStretch()
            c.addWidget(n)
            c.addWidget(s)
            c.addStretch()
            r.addWidget(av)
            r.addLayout(c, 1)
            for w in (av, n, s):
                w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            btn.clicked.connect(lambda _=False, u=uid: self.open_friend(u))
            self.rows_layout.insertWidget(i, btn)

    # -- profile page
    def _build_profile(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        back = QPushButton("< Friends")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.setFixedWidth(100)
        back.clicked.connect(self.close_friend)
        lay.addWidget(back)
        lay.addSpacing(8)

        head = QHBoxLayout()
        self.p_avatar = QLabel("?")
        self.p_avatar.setObjectName("Avatar")
        self.p_avatar.setFixedSize(72, 72)
        self.p_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hc = QVBoxLayout()
        self.p_name = QLabel("")
        self.p_name.setObjectName("PageTitle")
        self.p_status = QLabel("")
        hc.addWidget(self.p_name)
        hc.addWidget(self.p_status)
        head.addWidget(self.p_avatar)
        head.addSpacing(12)
        head.addLayout(hc, 1)
        lay.addLayout(head)
        lay.addSpacing(10)

        lib_title = QLabel("Library")
        lib_title.setObjectName("RowTitle")
        lay.addWidget(lib_title)
        self.lib_scroll = QScrollArea()
        self.lib_scroll.setWidgetResizable(True)
        self.lib_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.lib_scroll.setFixedHeight(200)
        self.lib_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lib_holder = QWidget()
        self.lib_row = QHBoxLayout(lib_holder)
        self.lib_row.setContentsMargins(0, 0, 0, 0)
        self.lib_row.setSpacing(12)
        self.lib_row.addStretch()
        self.lib_scroll.setWidget(lib_holder)
        lay.addWidget(self.lib_scroll)
        lay.addSpacing(10)

        msg_title = QLabel("Messages")
        msg_title.setObjectName("RowTitle")
        lay.addWidget(msg_title)
        self.chat_view = ChatView()
        self.chat_view.reply_requested.connect(self.start_reply)
        self.chat_view.react_requested.connect(self.toggle_reaction)
        lay.addWidget(self.chat_view, 1)

        self.reply_bar = QFrame()
        self.reply_bar.setObjectName("ReplyBar")
        self.reply_bar.setStyleSheet("#ReplyBar{background:rgba(128,128,128,0.18); border-radius:8px;}")
        rb = QHBoxLayout(self.reply_bar)
        rb.setContentsMargins(10, 4, 6, 4)
        self.reply_label = QLabel("")
        self.reply_label.setStyleSheet("color:#888888;")
        cancel_reply = QPushButton("\u2715")
        cancel_reply.setFlat(True)
        cancel_reply.setFixedSize(24, 24)
        cancel_reply.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_reply.setToolTip("Cancel reply (Esc)")
        cancel_reply.clicked.connect(self.cancel_reply)
        rb.addWidget(self.reply_label, 1)
        rb.addWidget(cancel_reply)
        self.reply_bar.hide()
        lay.addWidget(self.reply_bar)

        self.att_bar = QWidget()
        self.att_row = QHBoxLayout(self.att_bar)
        self.att_row.setContentsMargins(0, 0, 0, 0)
        self.att_row.setSpacing(6)
        self.att_row.addStretch()
        self.att_bar.hide()
        lay.addWidget(self.att_bar)

        self.chat_status = QLabel("")
        self.chat_status.setStyleSheet("color:#888888; font-size:11px;")
        self.chat_status.hide()
        lay.addWidget(self.chat_status)

        send_row = QHBoxLayout()
        self.attach_btn = QPushButton("+")
        self.attach_btn.setFixedSize(36, 34)
        self.attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.attach_btn.setToolTip("Attach images or videos (you can also paste or drop them)")
        self.attach_btn.clicked.connect(self.pick_attachments)
        self.chat_input = _ChatInput()
        self.chat_input.setPlaceholderText("Write a message...")
        self.chat_input.setMaxLength(500)
        self.chat_input.setAcceptDrops(False)       # so dropped files reach the page instead of becoming text
        self.chat_input.returnPressed.connect(self.send_message)
        self.chat_input.image_pasted.connect(self._on_image_pasted)
        self.chat_input.files_pasted.connect(self.add_files)
        self.chat_input.escaped.connect(self.cancel_reply)
        self.emoji_btn = _emoji_button("\U0001F642", 22)
        self.emoji_btn.setToolTip("Emoji")
        self.emoji_btn.clicked.connect(self._emoji_for_input)
        send = QPushButton("Send")
        send.setObjectName("Primary")
        send.clicked.connect(self.send_message)
        self.invite_btn = QPushButton("Invite to Game")
        self.invite_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.invite_btn.setToolTip("Invite to a Steam game")
        self.invite_btn.clicked.connect(self.invite_to_game)
        send_row.addWidget(self.attach_btn)
        send_row.addWidget(self.chat_input, 1)
        send_row.addWidget(self.invite_btn)
        send_row.addWidget(self.emoji_btn)
        send_row.addWidget(send)
        lay.addLayout(send_row)
        lay.addSpacing(16)
        return page

    def open_friend(self, uid):
        self.current_uid = uid
        self._last_msg_key = ""
        self._msgs = []
        self._pending = []
        self._refresh_att_bar()
        self.cancel_reply()
        self._set_status("")
        self.chat_view.other_uid = uid
        self.chat_view.reset()
        self._lib_sig = None
        self._fill_profile()
        self.stack.setCurrentIndex(1)
        self.load_messages()
        self.chat_timer.start()
        self._run(lambda: cloud_fetch_covers(uid), lambda r, e: self._on_covers(uid, r))

    def close_friend(self):
        self.chat_timer.stop()
        self.current_uid = None
        self.stack.setCurrentIndex(0)
        self.refresh()

    def _fill_profile(self):
        d = self.friends.get(self.current_uid, {})
        name = d.get("username") or "Unknown"
        self.p_name.setText(name)
        status, color = _friend_status(d)
        self.p_status.setText(status)
        self.p_status.setStyleSheet(f"color: {color};")
        apply_avatar(self.p_avatar, _friend_avatar_file(self.current_uid, d.get("photo")),
                     name[:1].upper(), 72)
        self._fill_library(d.get("library"))

    def _fill_library(self, lib):
        uid = self.current_uid
        covers = self.covers.get(uid, {})
        titles = [pretty_game_name(t) for t in lib] if isinstance(lib, list) else []
        sig = (uid, tuple(titles), tuple(sorted(covers)))
        if sig == self._lib_sig:
            return
        self._lib_sig = sig
        while self.lib_row.count() > 1:
            w = self.lib_row.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not titles:
            empty = QLabel("No games yet.")
            empty.setObjectName("RowDesc")
            self.lib_row.insertWidget(0, empty)
            return
        for i, t in enumerate(titles):
            tile = QWidget()
            tile.setFixedWidth(96)
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(0, 0, 0, 0)
            tl.setSpacing(4)
            cover = QLabel()
            cover.setFixedSize(96, 128)
            cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
            path = _friend_cover_file(uid, _cover_key(t), covers.get(_cover_key(t)))
            pix = rounded_cover_pixmap(path, 96, 128, 8) if path else None
            if pix:
                cover.setPixmap(pix)
            else:
                cover.setText(t[:1].upper())
                cover.setStyleSheet("background: rgba(128,128,128,0.25); border-radius: 8px; "
                                    "font-size: 28px; font-weight: bold;")
            name = QLabel(t)
            name.setWordWrap(True)
            name.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            name.setStyleSheet("font-size: 11px;")
            name.setFixedHeight(32)
            tl.addWidget(cover)
            tl.addWidget(name)
            self.lib_row.insertWidget(i, tile)

    def _on_covers(self, uid, res):
        self.covers[uid] = res if isinstance(res, dict) else {}
        if self.current_uid == uid and self.stack.currentIndex() == 1:
            self._fill_profile()

    # -- chat: loading, replies, attachments, reactions
    def _chat_names(self):
        names = {}
        if self._me:
            names[self._me] = "You"
        if self.current_uid:
            names[self.current_uid] = self.friends.get(self.current_uid, {}).get("username") or "Unknown"
        return names

    @staticmethod
    def _sig_of(msgs):
        return json.dumps(msgs, sort_keys=True, default=str)

    def _render_chat(self):
        self.chat_view.other_uid = self.current_uid
        self.chat_view.set_messages(self._me, self._msgs, self._chat_names())

    def _set_status(self, text):
        self.chat_status.setText(text or "")
        self.chat_status.setVisible(bool(text))

    def load_messages(self, force=False):
        uid = self.current_uid
        if not uid:
            return
        def done(res, err):
            if err or uid != self.current_uid:
                return
            if self._rx_inflight > 0 and not force:     # a reaction is still being saved: don't flash the old state back
                return
            me, msgs, _last_key = res
            sig = self._sig_of(msgs)
            if sig == self._last_msg_key:
                return
            self._last_msg_key = sig
            self._me, self._msgs = me, msgs
            self._render_chat()
        self._run(lambda: cloud_get_messages(uid), done, key=("messages", uid))

    def send_message(self):
        uid = self.current_uid
        if not uid or self._sending:
            return
        if self._preparing:
            self._set_status("Still preparing your files...")
            return
        text = self.chat_input.text().strip()
        atts = list(self._pending)
        if not text and not atts:
            return
        reply = self._reply
        self.chat_input.clear()
        self._pending = []
        self._refresh_att_bar()
        self.cancel_reply()
        if atts:
            self._sending = True
            self.attach_btn.setEnabled(False)
            self._set_status(f"Uploading {len(atts)} file{'s' if len(atts) != 1 else ''}...")
        def done(res, err):
            if atts:
                self._sending = False
                self.attach_btn.setEnabled(True)
            if err:
                self._set_status(err)
                if uid == self.current_uid:             # give the user their message back
                    if not self.chat_input.text():
                        self.chat_input.setText(text)
                    if atts and not self._pending:
                        self._pending = atts
                        self._refresh_att_bar()
                    if reply and not self._reply:
                        self._show_reply(reply)
            else:
                self._set_status("")
            self.load_messages(force=True)
        self._run(lambda: cloud_send_message(uid, text, atts, reply), done)

    # replies
    def start_reply(self, m):
        key = m.get("_key")
        if not key:
            return
        atts = [a for a in (m.get("att") if isinstance(m.get("att"), list) else []) if isinstance(a, dict)]
        self._show_reply({"k": key, "f": m.get("from") or "", "t": str(m.get("text") or "")[:100],
                          "a": (atts[0].get("k") or "file") if atts else ""})
        self.chat_input.setFocus()

    def _show_reply(self, reply):
        self._reply = reply
        who = "yourself" if reply.get("f") == self._me else (self._chat_names().get(reply.get("f")) or "Unknown")
        snippet = reply.get("t") or {"img": "an image", "gif": "a GIF", "vid": "a video"}.get(reply.get("a"), "an attachment")
        snippet = self.reply_label.fontMetrics().elidedText(snippet, Qt.TextElideMode.ElideRight, 420)
        self.reply_label.setText(f"Replying to {who}: {snippet}")
        self.reply_bar.show()

    def cancel_reply(self):
        self._reply = None
        self.reply_bar.hide()

    # attachments
    def pick_attachments(self):
        exts = " ".join("*" + e for e in CHAT_IMAGE_EXTS + CHAT_VIDEO_EXTS)
        paths, _f = QFileDialog.getOpenFileNames(self, "Attach images or videos", os.path.expanduser("~"),
                                                 f"Images and videos ({exts});;All files (*)")
        if paths:
            self.add_files(paths)

    def add_files(self, paths):
        if not self.current_uid or self._sending:
            return
        room = CHAT_MAX_ATTACH - len(self._pending) - self._preparing
        if room <= 0:
            self._set_status(f"You can attach up to {CHAT_MAX_ATTACH} files per message.")
            return
        if len(paths) > room:
            self._set_status(f"Only {CHAT_MAX_ATTACH} files fit in one message, the rest were skipped.")
        for p in list(paths)[:room]:
            self._queue_prepare(lambda p=p: prepare_attachment(p))

    def _on_image_pasted(self, img):
        if not self.current_uid or self._sending:
            return
        if len(self._pending) + self._preparing >= CHAT_MAX_ATTACH:
            self._set_status(f"You can attach up to {CHAT_MAX_ATTACH} files per message.")
            return
        self._queue_prepare(lambda: prepare_attachment_image(img))

    def _queue_prepare(self, fn):
        """Prepares a file on a worker thread; results are re-sorted so attachments keep the order they were picked in."""
        self._preparing += 1
        self._att_seq += 1
        seq = self._att_seq
        self._run(fn, lambda att, err, n=seq: self._on_prepared(att, err, n))

    def _on_prepared(self, att, err, seq=0):
        self._preparing = max(0, self._preparing - 1)
        if err or not att:
            self._set_status(err or "Couldn't attach that file.")
            return
        if att["kind"] != "vid":
            thumb = QImage.fromData(att["data"])
            if not thumb.isNull():
                att["thumb"] = QPixmap.fromImage(thumb.scaled(
                    40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        att["_seq"] = seq
        self._pending.append(att)
        self._pending.sort(key=lambda a: a.get("_seq", 0))
        self._refresh_att_bar()

    def _remove_attachment(self, i):
        if 0 <= i < len(self._pending):
            del self._pending[i]
            self._refresh_att_bar()

    def _refresh_att_bar(self):
        while self.att_row.count() > 1:
            w = self.att_row.takeAt(0).widget()
            if w:
                w.deleteLater()
        for i, a in enumerate(self._pending):
            chip = QFrame()
            chip.setObjectName("AttChip")
            chip.setStyleSheet("#AttChip{background:rgba(128,128,128,0.2); border-radius:8px;}")
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(4, 4, 4, 4)
            cl.setSpacing(6)
            thumb = QLabel()
            thumb.setFixedSize(40, 40)
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if a.get("thumb") is not None:
                thumb.setPixmap(a["thumb"])
            else:
                thumb.setText("\u25B6")
            name = str(a["name"])
            cl.addWidget(thumb)
            cl.addWidget(QLabel(name if len(name) <= 18 else name[:15] + "..."))
            rm = QPushButton("\u2715")
            rm.setFlat(True)
            rm.setFixedSize(22, 22)
            rm.setCursor(Qt.CursorShape.PointingHandCursor)
            rm.setToolTip("Remove")
            rm.clicked.connect(lambda _=False, n=i: self._remove_attachment(n))
            cl.addWidget(rm)
            self.att_row.insertWidget(i, chip)
        self.att_bar.setVisible(bool(self._pending))

    def dragEnterEvent(self, e):
        if self.stack.currentIndex() == 1 and e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_files(paths)
            e.acceptProposedAction()

    # emoji + reactions
    def _emoji_for_input(self):
        def insert(ch):
            self.chat_input.insert(ch)
            self.chat_input.setFocus()
        self.chat_view.open_picker(self.emoji_btn, insert)

    def toggle_reaction(self, key, code):
        uid = self.current_uid
        if not uid or not self._me or not _EMOJI_CODE_RE.match(code or ""):
            return
        msg = next((m for m in self._msgs if m.get("_key") == key), None)
        if msg is None:
            return
        rx = dict(msg["rx"]) if isinstance(msg.get("rx"), dict) else {}
        users = dict(rx[code]) if isinstance(rx.get(code), dict) else {}
        on = not users.get(self._me)
        if on:
            users[self._me] = True
        else:
            users.pop(self._me, None)
        if users:
            rx[code] = users
        else:
            rx.pop(code, None)
        if rx:
            msg["rx"] = rx
        else:
            msg.pop("rx", None)
        self._last_msg_key = self._sig_of(self._msgs)       # show the change instantly, then confirm with the server
        self._render_chat()
        self._rx_inflight += 1
        def done(res, err):
            self._rx_inflight = max(0, self._rx_inflight - 1)
            if err:
                self._set_status(err)
            self.load_messages(force=True)
        self._run(lambda: cloud_set_reaction(uid, key, code, on), done)


class SteamInviteDialog(QDialog):
    """Only shown when the invite couldn't be completed automatically; whatever was found is already filled in."""
    def __init__(self, parent, game="", link="", appid=0, note=""):
        super().__init__(parent)
        self.setWindowTitle("Invite to game")
        self.setMinimumWidth(440)
        self._game0, self._appid0 = game, appid
        lay = QVBoxLayout(self)
        if note:
            info = QLabel(note)
            info.setWordWrap(True)
            lay.addWidget(info)
        form = QFormLayout()
        self.name_edit = QLineEdit(game)
        self.name_edit.setPlaceholderText("Game name")
        self.link_edit = QLineEdit(link)
        self.link_edit.setPlaceholderText("steam://joinlobby/...  or  steam://connect/...  or  ip:port")
        form.addRow("Game", self.name_edit)
        form.addRow("Join link", self.link_edit)
        lay.addLayout(form)
        hint = QLabel("Paste the game's join link (a steam://joinlobby/ or steam://connect/ link) or a server ip:port. "
                      "Your friend's Join button then opens Steam and joins, the same as accepting an invite in Steam.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888888; font-size:11px;")
        lay.addWidget(hint)
        self.err = QLabel("")
        self.err.setStyleSheet("color:#e5484d;")
        self.err.hide()
        lay.addWidget(self.err)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Send invite")
        ok.setObjectName("Primary")
        ok.setDefault(True)
        ok.clicked.connect(self._ok)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def _ok(self):
        if not self.name_edit.text().strip():
            self.err.setText("Enter the game's name.")
        elif not normalize_steam_join(self.link_edit.text()):
            self.err.setText("That isn't a steam://joinlobby/ or steam://connect/ link (or ip:port).")
        else:
            self.accept()
            return
        self.err.show()

    def values(self):
        game = self.name_edit.text().strip()
        appid = self._appid0 if game.casefold() == self._game0.casefold() else _steam_appid_for_title(game)
        return game, normalize_steam_join(self.link_edit.text()), appid

def _friends_send_invite(self, uid, game, link, appid):
    text = make_invite_text(game, link, appid)
    def done(res, err):
        self._set_status(err or "")
        self.load_messages(force=True)
    self._run(lambda: cloud_send_message(uid, text), done)

FriendsPage._send_invite = _friends_send_invite

def _friends_invite_to_game(self):
    """One click: finds the game you're playing, its cover and the join link, and sends the invite.
    If the join link can't be found, a small dialog opens with everything else already filled in."""
    uid = self.current_uid
    if not uid or self._sending:
        return
    clip = normalize_steam_join(QApplication.clipboard().text())      # fallback: a link you just copied from the game
    self._set_status("Finding your game and join link...")
    def done(info, err):
        if uid != self.current_uid:
            return
        info = info or {}
        game, appid, link = info.get("game", ""), info.get("appid", 0), info.get("link", "")
        if game and link:
            self._send_invite(uid, game, link, appid)
            return
        self._set_status("")
        if not game:
            note = "No game is running right now, so enter the game and its join link."
        elif clip:
            note = f"Found {game}. Used the link from your clipboard; check it, then send."
        else:
            note = f"Found {game}, but Steam has no join link for it right now (it only has one while you're in a joinable lobby or server)."
        dlg = SteamInviteDialog(self, game, link or clip, appid, note)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        g, l, a = dlg.values()
        self._send_invite(uid, g, l, a)
    self._run(detect_invite_info, done, key="invite-detect")

FriendsPage.invite_to_game = _friends_invite_to_game

_friends_prev_init = AdaptiveApp.__init__

def _friends_init(self):
    _friends_prev_init(self)
    self.friends_page = FriendsPage()
    self.pages.addWidget(self.friends_page)
    idx = self.pages.indexOf(self.friends_page)
    btn = self.make_nav_button("Friends", checkable=True)
    self.nav_group.addButton(btn, idx)
    self.nav_group.button(0).parent().layout().insertWidget(5, btn)

    # friend code row in Settings
    panel = QFrame()
    panel.setObjectName("Panel")
    pl = QHBoxLayout(panel)
    pl.setContentsMargins(22, 18, 22, 18)
    col = QVBoxLayout()
    col.setSpacing(2)
    t = QLabel("Friend code")
    t.setObjectName("RowTitle")
    d = QLabel("Share this so friends can add you in the Friends tab")
    d.setObjectName("RowDesc")
    col.addWidget(t)
    col.addWidget(d)
    code_lbl = QLabel(friend_code_cached() or "Open Friends tab once")
    code_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    code_lbl.setStyleSheet("font-weight: bold; font-size: 16px;")
    copy = QPushButton("Copy")
    copy.clicked.connect(lambda: QApplication.clipboard().setText(friend_code_cached() or ""))
    pl.addLayout(col, 1)
    pl.addWidget(code_lbl)
    pl.addSpacing(8)
    pl.addWidget(copy)
    lay = self.settings_page.layout()
    lay.insertSpacing(3, 14)
    lay.insertWidget(4, panel)
    self.friends_page.code_ready.connect(code_lbl.setText)
    self.account_page.account_changed.connect(
        lambda: code_lbl.setText(friend_code_cached() or "Open Friends tab once"))

AdaptiveApp.__init__ = _friends_init

# ---------------------------------------------------------------- background + tray notifications
from PyQt6.QtWidgets import QSystemTrayIcon, QStyle
from PyQt6.QtGui import QAction

_tray_prev_init = AdaptiveApp.__init__

def _tray_init(self):
    _tray_prev_init(self)
    self._really_quit = False
    self._tray = None
    self._tray_hint_shown = False
    app = QApplication.instance()

    # The tray icon is optional. GNOME (without the AppIndicator extension) and some Wayland
    # setups report "no system tray", but desktop notifications can still work through
    # notify-send, so only the tray-specific parts are skipped when it's missing.
    icon = self.windowIcon()
    if icon.isNull():
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        app.setQuitOnLastWindowClosed(False)
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip("NCZ Games Launcher")

    def show_window():
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def show_friends():
        show_window()
        self.pages.setCurrentWidget(self.friends_page)
        uid = self.friends_page.notify_uid
        if uid and uid in self.friends_page.friends:
            self.friends_page.open_friend(uid)
        b = self.nav_group.button(self.pages.indexOf(self.friends_page))
        if b:
            b.setChecked(True)

    if tray is not None:
        menu = QMenu(self)
        self._tray_menu = menu
        open_act = QAction("Open launcher", menu)
        quit_act = QAction("Quit", menu)
        menu.addAction(open_act)
        menu.addAction(quit_act)
        tray.setContextMenu(menu)

        def quit_app():
            self._really_quit = True
            tray.hide()
            app.quit()

        open_act.triggered.connect(show_window)
        quit_act.triggered.connect(quit_app)
        tray.activated.connect(lambda reason: show_window()
                               if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.messageClicked.connect(show_friends)

    has_ns = sys.platform.startswith("linux") and bool(shutil.which("notify-send"))
    ns_actions = False
    if has_ns:
        try:
            r = subprocess.run(["notify-send", "--help"], capture_output=True, text=True, timeout=5)
            ns_actions = "--action" in ((r.stdout or "") + (r.stderr or ""))
        except Exception:
            ns_actions = False

    def friend_picture(uid):
        d = self.friends_page.friends.get(uid) or self.friends_page.requests.get(uid) or {}
        path = _friend_avatar_file(uid, d.get("photo")) if d.get("photo") else None
        if not path:
            return None, None
        pix = CreditsPage.circular_pixmap(path, 128)
        if pix:
            round_path = path[:-4] + "_round.png"
            if pix.save(round_path, "PNG"):
                path = round_path
            return path, QIcon(pix)
        return path, QIcon(path)

    def notify_send(title, msg, icon_path, uid):
        args = ["notify-send", "-a", "NCZ Games Launcher", "-t", "5000"]
        if ns_actions:
            args += ["-A", "default=Open", "-w"]
        if icon_path:
            args += ["-i", icon_path]
        args += ["--", title, msg]  # "--" so a message starting with "-" isn't read as an option
        def work():
            try:
                p = _RealPopen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                try:
                    out, _ = p.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    p.kill()
                    return
                if ns_actions and out.strip() == "default":
                    self.friends_page.notif_clicked.emit(uid)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def notify(name, msg, uid):
        self.friends_page.notify_uid = uid
        return   # system notifications are muted for good: the overlay toasts replace them
        try:
            icon_path, pic = friend_picture(uid)
        except Exception:
            icon_path, pic = None, None
        if has_ns:
            fallback_icon = asset_path("icon.png")
            notify_send(name, msg, icon_path or (fallback_icon if os.path.exists(fallback_icon) else None), uid)
        elif tray is not None:
            tray.showMessage(name, msg, pic or icon, 5000)

    def on_clicked(uid):
        self.friends_page.notify_uid = uid
        show_friends()

    self.friends_page.notif_clicked.connect(on_clicked)
    self.friends_page.friend_event.connect(notify)
    if tray is not None:
        tray.show()
    self._tray = tray


def _tray_close_event(self, event):
    tray = getattr(self, "_tray", None)
    if getattr(self, "_really_quit", False):
        event.accept()
    elif tray is not None and tray.isVisible() and load_launcher_settings().get("close_to_tray", True):
        event.ignore()
        self.hide()
        self._tray_hint_shown = True     # no "still running in the background" popup (notifications muted)
    else:
        event.accept()
        if tray is not None:             # closing the last window doesn't quit while a tray icon is in use
            self._really_quit = True
            tray.hide()
            QTimer.singleShot(0, QApplication.quit)

AdaptiveApp.__init__ = _tray_init
AdaptiveApp.closeEvent = _tray_close_event



# ---------------------------------------------------------------- Launch -> Stop while a game runs
from PyQt6.QtCore import QEvent

def stop_game(name):
    proc = _running.get(name)
    if not proc or proc.poll() is not None:
        return
    try:
        import psutil
        parent = psutil.Process(proc.pid)
        procs = parent.children(recursive=True) + [parent]
        for p_ in procs:
            try:
                p_.terminate()
            except Exception:
                pass
        _gone, alive = psutil.wait_procs(procs, timeout=3)
        for p_ in alive:
            try:
                p_.kill()
            except Exception:
                pass
        return
    except ImportError:
        pass
    except Exception:
        pass
    if sys.platform.startswith("win"):
        _RealPopen(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            _RealPopen(["pkill", "-TERM", "-P", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        proc.terminate()
        def hard_kill():
            time.sleep(3)
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        threading.Thread(target=hard_kill, daemon=True).start()

class _StopButtonFilter(QObject):
    """While a button is in 'Stop' mode, swallow its normal click and stop the game instead."""
    def eventFilter(self, obj, ev):
        name = obj.property("ncz_running_name")
        if not name:
            return False
        t = ev.type()
        if t in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            return True
        if t == QEvent.Type.MouseButtonRelease:
            if ev.button() == Qt.MouseButton.LeftButton and obj.rect().contains(ev.position().toPoint()):
                stop_game(name)
            return True
        if t == QEvent.Type.KeyPress and ev.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            stop_game(name)
            return True
        return False

_stop_prev_init = AdaptiveApp.__init__

def _stop_init(self):
    _stop_prev_init(self)
    self._stop_filter = _StopButtonFilter(self)

    def sync():
        running = {n.lower(): n for n in list(_running)}
        for card, title in list(self.cards):
            try:
                btn = card.findChild(QPushButton, "Primary")
                if btn is None:
                    continue
                name = running.get(str(title).lower())
                if name:
                    if not btn.property("ncz_filter_on"):
                        btn.installEventFilter(self._stop_filter)
                        btn.setProperty("ncz_filter_on", True)
                    if btn.property("ncz_running_name") != name:
                        btn.setProperty("ncz_orig_text", btn.text())
                        btn.setProperty("ncz_running_name", name)
                    if btn.text() != "Stop":
                        if btn.text() != btn.property("ncz_orig_text") and btn.text() != "Stop":
                            btn.setProperty("ncz_orig_text", btn.text())
                        btn.setText("Stop")
                    btn.setEnabled(True)
                elif btn.property("ncz_running_name"):
                    btn.setProperty("ncz_running_name", "")
                    if btn.text() == "Stop":
                        btn.setText(btn.property("ncz_orig_text") or "Launch")
            except RuntimeError:
                continue  # card was deleted

    self._stop_timer = QTimer(self)
    self._stop_timer.setInterval(700)
    self._stop_timer.timeout.connect(sync)
    self._stop_timer.start()

AdaptiveApp.__init__ = _stop_init


# ---------------------------------------------------------------- self-update at launch
SELF_UPDATE_STABLE_URL = "https://raw.githubusercontent.com/justmm33/FNaNCZ-AE-Releases/refs/heads/main/main.py"
SELF_UPDATE_NIGHTLY_URL = "https://raw.githubusercontent.com/justmm33/FNaNCZ-AE-Releases/refs/heads/main/nightly/main.py"
NIGHTLY_UIDS = {"XcqSn5Zvhsf1npoNl3FNG1bKpU63", "LtVp5drXTcfedfGmWGMhHSVzGiU2"}

def self_update_check():
    """Replaces this script with the latest release if it differs. True if it was replaced."""
    if getattr(sys, "frozen", False):
        return False  # packaged builds can't rewrite themselves
    if os.environ.get("NCZ_NO_UPDATE"):
        return False  # handy for testing a local build
    path = os.path.abspath(__file__)
    acct = load_account()
    base = SELF_UPDATE_NIGHTLY_URL if (acct and acct.get("uid") in NIGHTLY_UIDS) else SELF_UPDATE_STABLE_URL
    req = urllib.request.Request(f"{base}?t={int(time.time())}",
                                 headers={"User-Agent": "NCZ-Games-Launcher", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        remote = resp.read()
    if len(remote) < 2000:
        return False
    def norm(b):
        return b.replace(b"\r\n", b"\n").strip()
    with open(path, "rb") as f:
        local = f.read()
    if norm(remote) == norm(local):
        return False
    compile(remote, path, "exec")  # refuse to install a broken download
    tmp = path + ".update"
    with open(tmp, "wb") as f:
        f.write(remote)
    try:
        shutil.copy2(path, path + ".bak")
    except OSError:
        pass
    os.replace(tmp, path)
    return True

# files that update from the same repo + channel (stable / nightly) as main.py
COMPANION_FILES = [  # (file name in the repo, local path, is it a python file?)
    ("overlay.py", lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay.py"), True),
    ("controller_input.py", lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "controller_input.py"), True),
    ("overlay_hook.dll", lambda: asset_path("overlay_hook.dll"), False),
]

def _companion_names_on_disk():
    """File names listed in COMPANION_FILES of the main.py currently on disk (it may be newer than the running one)."""
    try:
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            src = f.read()
        blk = src[src.index("COMPANION_FILES = ["):]
        blk = blk[:blk.index("\n]")]
        return re.findall(r'^\s*\("([\w.\-]+)",', blk, re.M)
    except Exception:
        return []

def companion_update_check():
    """Updates overlay.py / controller_input.py / overlay_hook.dll. True if anything changed."""
    if os.environ.get("NCZ_NO_UPDATE"):
        return False
    frozen = getattr(sys, "frozen", False)
    acct = load_account()
    nightly = bool(acct and acct.get("uid") in NIGHTLY_UIDS)
    base = (SELF_UPDATE_NIGHTLY_URL if nightly else SELF_UPDATE_STABLE_URL).rsplit("/", 1)[0] + "/"
    norm = lambda b: b.replace(b"\r\n", b"\n").strip()
    changed = False
    # The list in THIS (possibly older) running code may lack files that the main.py we just downloaded knows about.
    # Read the names from the new main.py on disk too, so one pass fetches everything and one restart is enough.
    items = list(COMPANION_FILES)
    known = {n for n, _f, _p in items}
    for n in _companion_names_on_disk():
        if n not in known:
            known.add(n)
            if n.endswith(".py"):
                items.append((n, (lambda n=n: os.path.join(os.path.dirname(os.path.abspath(__file__)), n)), True))
            else:
                items.append((n, (lambda n=n: asset_path(n)), False))
    for name, local_fn, is_py in items:
        try:
            if (is_py and frozen) or (name.endswith(".dll") and not sys.platform.startswith("win")):
                continue
            req = urllib.request.Request(f"{base}{name}?t={int(time.time())}",
                                         headers={"User-Agent": "NCZ-Games-Launcher", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                remote = resp.read()
            path = local_fn()
            if is_py:
                if len(remote) < 200:
                    continue
                compile(remote, path, "exec")                     # refuse a broken download
            elif len(remote) < 10000 or remote[:2] != b"MZ":      # not a real DLL
                continue
            try:
                with open(path, "rb") as f:
                    local = f.read()
            except OSError:
                local = None
            if local is not None and ((norm(remote) == norm(local)) if is_py else (remote == local)):
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".update"
            with open(tmp, "wb") as f:
                f.write(remote)
            try:
                os.replace(tmp, path)
            except OSError:                                       # e.g. the DLL is loaded in a running game
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                continue
            changed = True
        except Exception:
            continue        # not in the repo yet (404), offline, ...
    return changed

class _SelfUpdateSignal(QObject):
    updated = pyqtSignal()

_selfupdate_prev_init = AdaptiveApp.__init__

def _selfupdate_init(self):
    _selfupdate_prev_init(self)
    self._selfupdate_sig = _SelfUpdateSignal(self)
    self._selfupdate_sig.updated.connect(lambda: QMessageBox.information(
        self, "Launcher updated",
        "The launcher has been updated to the latest version.\n\n"
        "Please restart the tool to apply the update."))
    def work():
        if not load_launcher_settings().get("auto_update", True):
            return                       # "Automatic Updates" is turned off in Settings
        try:
            updated = self_update_check()
        except Exception:
            updated = False
        try:
            updated = companion_update_check() or updated
        except Exception:
            pass
        if updated:
            self._selfupdate_sig.updated.emit()
    QTimer.singleShot(1500, lambda: threading.Thread(target=work, daemon=True).start())

AdaptiveApp.__init__ = _selfupdate_init


# ---------------------------------------------------------------- full library for friends + NCZFront "playing"
_lib_prev_init = AdaptiveApp.__init__

def _lib_init(self):
    _lib_prev_init(self)
    def snap():
        titles = []
        for card, title in list(self.cards):
            try:
                card.objectName()  # raises if the card was deleted
            except RuntimeError:
                continue
            t = pretty_game_name(title)
            if t and t not in titles:
                titles.append(t)
        for card, title in list(getattr(self, "fan_lib_list", [])):
            try:
                card.objectName()
            except RuntimeError:
                continue
            t = pretty_game_name(title)
            if t and t not in titles:
                titles.append(t)
        covers = library_cover_paths()
        if titles and (titles != _library_snapshot or covers != _library_covers):
            first = not _library_snapshot
            _library_snapshot[:] = titles
            _library_covers.clear()
            _library_covers.update(covers)
            if not first:
                for cb in _playing_listeners:
                    cb()
    self._lib_timer = QTimer(self)
    self._lib_timer.setInterval(3000)
    self._lib_timer.timeout.connect(snap)
    self._lib_timer.start()
    snap()

AdaptiveApp.__init__ = _lib_init

# NCZFront is a website, so there is no process to watch: count it as "playing" for 30 minutes after opening.
_web_until = {"t": 0.0}
_real_webbrowser_open = webbrowser.open

def _tracked_webbrowser_open(url, *a, **k):
    result = _real_webbrowser_open(url, *a, **k)
    try:
        if str(url).rstrip("/") == NCZFRONT_URL.rstrip("/"):
            _web_until["t"] = time.time() + 1800
            _now_playing["name"] = "NCZFront"
            for cb in _playing_listeners:
                cb()
            def expire():
                time.sleep(1805)
                if time.time() >= _web_until["t"] and _now_playing["name"] == "NCZFront":
                    _now_playing["name"] = next(iter(_running), None)
                    for cb in _playing_listeners:
                        cb()
            threading.Thread(target=expire, daemon=True).start()
    except Exception:
        pass
    return result

webbrowser.open = _tracked_webbrowser_open

# ---------------------------------------------------------------- idle (minimized) + offline (quit) for friends
_idle_prev_init = AdaptiveApp.__init__

def _idle_init(self):
    _idle_prev_init(self)

    def refresh_state():
        minimized = self.isMinimized() or not self.isVisible()
        if minimized != _window_state["minimized"]:
            _window_state["minimized"] = minimized
            self.friends_page._playing_changed()  # publishes presence right away if signed in

    self._state_timer = QTimer(self)
    self._state_timer.setInterval(1000)
    self._state_timer.timeout.connect(refresh_state)
    self._state_timer.start()

    def go_offline():
        if not self.friends_page._signed_in():
            return
        def work():
            try:
                cloud_publish_offline()
            except Exception:
                pass
        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(3)  # don't hang the exit if the network is down

    QApplication.instance().aboutToQuit.connect(go_offline)

AdaptiveApp.__init__ = _idle_init

# ---------------------------------------------------------------- Garry's Mod Addon Manager tab
# Pure-Python port of "Gmod Alternate Addon Manager" (extract workshop addons + enable/disable).
# The tab only appears while Garry's Mod is installed through the launcher.
import struct as _struct
import lzma as _lzma

def find_gmod_root():
    """Return the folder that contains 'garrysmod/' for Garry's Mod installed via the launcher
    or added with Add Existing Game, else None."""
    for g in load_steam_library() + load_custom_games():
        exe = g.get("exe_path")
        key = re.sub(r"[^a-z0-9]", "", str(g.get("title", "")).lower())
        if not exe or not (g.get("appid") == 4000 or "garrysmod" in key or "gmod" in key):
            continue
        d = os.path.dirname(os.path.abspath(exe))
        for _ in range(4):  # gmod.exe can sit in bin/win64, so walk up to the folder with garrysmod/
            if os.path.isdir(os.path.join(d, "garrysmod")):
                return d
            if os.path.dirname(d) == d:
                break
            d = os.path.dirname(d)
    for root in library_roots():
        if not os.path.isdir(root):
            continue
        try:
            children = sorted(os.listdir(root))
        except OSError:
            continue
        for name in children:
            if "garrysmod" not in re.sub(r"[^a-z0-9]", "", name.lower()):
                continue
            base = os.path.join(root, name)
            if not os.path.isdir(base):
                continue
            if os.path.isdir(os.path.join(base, "garrysmod")):
                return base
            for folder, dirs, _files in os.walk(base):  # archive may have nested one folder deeper
                if "garrysmod" in dirs:
                    return folder
                if folder[len(base):].count(os.sep) >= 2:
                    dirs[:] = []
    return None

def _gma_cstr(buf, pos):
    end = buf.index(b"\0", pos)
    return bytes(buf[pos:end]).decode("utf-8", "replace"), end + 1

def extract_gma(data, dest):
    """Extract GMA bytes into dest. Returns the addon title (from its JSON description, else its name)."""
    buf = memoryview(data)
    if bytes(buf[:4]) != b"GMAD":
        raise ValueError("not a GMA file")
    version = buf[4]
    pos = 5 + 16  # version, steamid, timestamp
    if version > 1:
        while True:
            s, pos = _gma_cstr(data, pos)
            if not s:
                break
    name, pos = _gma_cstr(data, pos)
    desc, pos = _gma_cstr(data, pos)
    _author, pos = _gma_cstr(data, pos)
    pos += 4  # addon version
    entries = []
    while True:
        num = _struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if num == 0:
            break
        fname, pos = _gma_cstr(data, pos)
        size = _struct.unpack_from("<q", data, pos)[0]
        pos += 12  # size + crc
        entries.append((fname, size))
    info = {}
    try:
        parsed = json.loads(desc)
        if isinstance(parsed, dict):
            info = parsed
    except Exception:
        pass
    title = str(info.get("title") or name or "").strip()
    os.makedirs(dest, exist_ok=True)
    root = os.path.abspath(dest)
    for fname, size in entries:
        rel = fname.replace("\\", "/").lstrip("/")
        target = os.path.abspath(os.path.join(root, rel))
        if target != root and target.startswith(root + os.sep):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(buf[pos:pos + size])
        pos += size
    with open(os.path.join(root, "addon.json"), "w", encoding="utf-8") as f:
        json.dump({"title": title, "type": info.get("type", ""), "tags": info.get("tags", []),
                   "ignore": info.get("ignore", [])}, f, indent=2)
    return title

def read_workshop_file(path):
    with open(path, "rb") as f:
        raw = f.read()
    if path.lower().endswith(".bin"):  # legacy workshop files are LZMA-compressed GMAs
        raw = _lzma.LZMADecompressor(format=_lzma.FORMAT_ALONE).decompress(raw)
    return raw

def safe_addon_name(title, fallback):
    safe = re.sub(r'[<>:"/\\|?*]', "_", title or "").strip().rstrip(".")
    return safe or fallback

def extract_workshop_folder(workshop, addons_dir, log, progress):
    files = []
    for folder, _d, names in os.walk(workshop):
        for n in names:
            if n.lower().endswith((".gma", ".bin")):
                files.append(os.path.join(folder, n))
    bins = {os.path.splitext(p)[0].lower() for p in files if p.lower().endswith(".bin")}
    files = [p for p in files if not (p.lower().endswith(".gma") and os.path.splitext(p)[0].lower() in bins)]
    total, done, skipped, failed = len(files), 0, 0, 0
    if not total:
        log("No .gma or .bin addons found in that folder.", "warn")
        return
    for i, path in enumerate(files, 1):
        progress(i - 1, total)
        stem = os.path.splitext(os.path.basename(path))[0]
        tmp = None
        try:
            data = read_workshop_file(path)
            tmp = os.path.join(addons_dir, f".extract_tmp_{os.getpid()}")
            shutil.rmtree(tmp, ignore_errors=True)
            title = extract_gma(data, tmp)
            safe = safe_addon_name(title, stem)
            final = os.path.join(addons_dir, safe)
            if os.path.isdir(final) or os.path.isdir(os.path.join(addons_dir, "disabled", safe + "(Disabled)")):
                log(f'Directory "{safe}" already exists, skipped', "warn")
                skipped += 1
                shutil.rmtree(tmp, ignore_errors=True)
                continue
            os.replace(tmp, final)
            log(f'Extracted "{title or safe}"', "info")
            done += 1
        except Exception as e:
            log(f"Failed to extract {os.path.basename(path)}: {e}", "error")
            failed += 1
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    progress(total, total)
    msg = f"Total found addons [{total}], extracted [{done}], skipped [{skipped}], failed [{failed}]"
    log(msg, "ok" if not failed else "error")
    log("Extraction complete", "ok")

class GmodExtractWorker(QThread):
    log = pyqtSignal(str, str)
    progress = pyqtSignal(int, int)

    def __init__(self, workshop, addons_dir, parent=None):
        super().__init__(parent)
        self.workshop = workshop
        self.addons_dir = addons_dir

    def run(self):
        try:
            extract_workshop_folder(self.workshop, self.addons_dir,
                                    lambda t, k: self.log.emit(t, k),
                                    lambda a, b: self.progress.emit(a, b))
        except Exception as e:
            self.log.emit(f"Error: {e}", "error")

class GmodAddonManagerPage(QWidget):
    DISABLED_SUFFIX = "(Disabled)"

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QProgressBar, QSizePolicy
        self._QSizePolicy = QSizePolicy
        self.root = None
        self.worker = None
        self.rows = []
        self.setObjectName("Content")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 28, 36, 20)
        outer.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(0)
        title = QLabel("Garry's Mod Addon Manager")
        title.setObjectName("PageTitle")
        self.count_label = QLabel()
        self.count_label.setObjectName("PageCount")
        head.addWidget(title)
        head.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBottom)
        head.addStretch()
        outer.addLayout(head)
        outer.addSpacing(4)
        self.path_label = QLabel()
        self.path_label.setObjectName("RowDesc")
        outer.addWidget(self.path_label)
        outer.addSpacing(18)

        tabs = QHBoxLayout()
        tabs.setSpacing(8)
        self.tab_group = QButtonGroup(self)
        for i, text in enumerate(("Installed addons", "Install from Workshop")):
            b = QPushButton(text)
            b.setObjectName("GmodTab")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.tab_group.addButton(b, i)
            tabs.addWidget(b)
        tabs.addStretch()
        self.tab_group.button(0).setChecked(True)
        outer.addLayout(tabs)
        outer.addSpacing(16)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.tab_group.idClicked.connect(self.stack.setCurrentIndex)

        # ---------------- page 0: installed addons
        lib = QWidget()
        ll = QVBoxLayout(lib)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(12)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("Search")
        self.filter_edit.setPlaceholderText("Search addons")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setFixedHeight(36)
        self.filter_edit.textChanged.connect(self.apply_addon_filter)
        bar.addWidget(self.filter_edit, 1)
        for text, fn in (("Enable all", lambda: self.set_all(True)),
                         ("Disable all", lambda: self.set_all(False)),
                         ("Refresh", self.refresh_addons)):
            b = QPushButton(text)
            b.setFixedHeight(36)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, f=fn: f())
            bar.addWidget(b)
        ll.addLayout(bar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_container = QWidget()
        self.list_container.setObjectName("GridContainer")
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 12, 16)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch()
        scroll.setWidget(self.list_container)
        ll.addWidget(scroll, 1)
        self.empty_label = QLabel("No addons found.\nUse \"Install from Workshop\" to add some.")
        self.empty_label.setObjectName("EmptyState")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ll.addWidget(self.empty_label)
        self.empty_label.hide()
        self.stack.addWidget(lib)

        # ---------------- page 1: install from workshop
        inst = QWidget()
        il = QVBoxLayout(inst)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(14)
        panel = QFrame()
        panel.setObjectName("Panel")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(22, 18, 22, 18)
        pl.setSpacing(6)
        t = QLabel("Workshop addons folder")
        t.setObjectName("RowTitle")
        d = QLabel("Folder with your downloaded .gma and legacy .bin addons. "
                   "Each one is extracted into garrysmod/addons under its title.")
        d.setObjectName("RowDesc")
        d.setWordWrap(True)
        pl.addWidget(t)
        pl.addWidget(d)
        pl.addSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Choose your Workshop addons folder")
        self.path_edit.setText(ws_cfg_get("gmod_workshop_path", ""))
        browse = QPushButton("Browse")
        browse.clicked.connect(self.browse_workshop)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        pl.addLayout(row)
        pl.addSpacing(8)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.extract_btn = QPushButton("Extract && Install addons")
        self.extract_btn.setObjectName("Primary")
        self.extract_btn.setFixedHeight(38)
        self.extract_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.extract_btn.clicked.connect(self.start_extract)
        clear = QPushButton("Clear path cache")
        clear.setFixedHeight(38)
        clear.clicked.connect(self.clear_path_cache)
        actions.addWidget(self.extract_btn)
        actions.addWidget(clear)
        actions.addStretch()
        pl.addLayout(actions)
        il.addWidget(panel)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        il.addWidget(self.bar)
        self.log_view = QTextEdit()
        self.log_view.setObjectName("GmodLog")
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Extraction log will appear here")
        il.addWidget(self.log_view, 1)
        self.stack.addWidget(inst)

        self._apply_css()

    # ---- styling (follows the launcher's light/dark theme)
    def _apply_css(self):
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        t = THEMES["dark" if dark else "light"]
        self.setStyleSheet(f"""
QPushButton#GmodTab {{ background: transparent; color: {t['subtext']}; border: 1px solid {t['border']};
    border-radius: 17px; padding: 8px 20px; font-size: 13px; }}
QPushButton#GmodTab:hover {{ background: {t['hover']}; color: {t['text']}; }}
QPushButton#GmodTab:checked {{ background: {t['accent']}; color: #ffffff; border: 1px solid {t['accent']}; }}
QFrame#GmodRow {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QFrame#GmodRow:hover {{ border: 1px solid {t['accent']}; }}
QPushButton#GmodToggle {{ background: {t['button']}; color: {t['subtext']}; border-radius: 13px;
    padding: 0 14px; min-width: 70px; font-size: 12px; }}
QPushButton#GmodToggle:hover {{ background: {t['button_hover']}; }}
QPushButton#GmodToggle:checked {{ background: {t['accent']}; color: #ffffff; }}
QPushButton#GmodToggle:checked:hover {{ background: {t['accent_hover']}; }}
QTextEdit#GmodLog {{ background: {t['panel']}; color: {t['text']}; border: 1px solid {t['border']};
    border-radius: 10px; padding: 10px; font-family: Consolas, "DejaVu Sans Mono", monospace; font-size: 12px; }}
""")

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_css()

    # ---- helpers
    def addons_dir(self):
        return os.path.join(self.root, "garrysmod", "addons") if self.root else None

    def set_root(self, root):
        changed = root != self.root
        self.root = root
        self.path_label.setText(os.path.join(root, "garrysmod", "addons") if root else "")
        if root:
            try:
                os.makedirs(os.path.join(self.addons_dir(), "disabled"), exist_ok=True)
            except OSError:
                pass
        if changed:
            self.refresh_addons()

    def log(self, text, kind="info"):
        color = {"ok": GREEN, "warn": "#eab308", "error": RED}.get(kind)
        esc = html_lib.escape(text)
        # Always wrap in a tag: append() guesses the format, and a bare escaped line counts as
        # plain text, which showed &quot; literally.
        self.log_view.append(f'<span style="color:{color}">{esc}</span>' if color else f"<span>{esc}</span>")

    def browse_workshop(self):
        start = self.path_edit.text() or os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(self, "Workshop addons folder", start)
        if path:
            self.path_edit.setText(path)
            ws_cfg_set("gmod_workshop_path", path)

    def clear_path_cache(self):
        ws_cfg_set("gmod_workshop_path", None)
        self.path_edit.clear()
        self.log("Deleted all path cache", "warn")

    def start_extract(self):
        if not self.root:
            return
        workshop = self.path_edit.text().strip()
        if not os.path.isdir(workshop):
            QMessageBox.warning(self, "Workshop path", "Workshop path is invalid.")
            return
        ws_cfg_set("gmod_workshop_path", workshop)
        os.makedirs(self.addons_dir(), exist_ok=True)
        self.log_view.clear()
        self.extract_btn.setEnabled(False)
        self.extract_btn.setText("Extracting...")
        self.bar.setValue(0)
        self.worker = GmodExtractWorker(workshop, self.addons_dir(), self)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(lambda a, b: (self.bar.setMaximum(max(b, 1)), self.bar.setValue(a)))
        self.worker.finished.connect(self._extract_done)
        self.worker.start()

    def _extract_done(self):
        self.extract_btn.setEnabled(True)
        self.extract_btn.setText("Extract && Install addons")
        self.refresh_addons()

    # ---- installed addons list
    def _clean(self, name):
        return name[:-len(self.DISABLED_SUFFIX)] if name.endswith(self.DISABLED_SUFFIX) else name

    def refresh_addons(self):
        for r in self.rows:
            r["frame"].setParent(None)
            r["frame"].deleteLater()
        self.rows = []
        found = []
        if self.root:
            base = self.addons_dir()
            off = os.path.join(base, "disabled")
            def dirs(p):
                try:
                    return [n for n in os.listdir(p) if os.path.isdir(os.path.join(p, n)) and not n.startswith(".")]
                except OSError:
                    return []
            found = [(n, True) for n in dirs(base) if n != "disabled"] + [(n, False) for n in dirs(off)]
            found.sort(key=lambda r: self._clean(r[0]).lower())
        for real, enabled in found:
            self._add_row(real, enabled)
        self.apply_addon_filter()

    def _add_row(self, real, enabled):
        frame = QFrame()
        frame.setObjectName("GmodRow")
        frame.setFixedHeight(52)
        h = QHBoxLayout(frame)
        h.setContentsMargins(18, 0, 12, 0)
        h.setSpacing(12)
        name = QLabel(self._clean(real))
        name.setObjectName("RowTitle")
        name.setToolTip(self._clean(real))
        name.setSizePolicy(self._QSizePolicy.Policy.Ignored, self._QSizePolicy.Policy.Preferred)
        btn = QPushButton()
        btn.setObjectName("GmodToggle")
        btn.setCheckable(True)
        btn.setFixedHeight(26)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        h.addWidget(name, 1)
        h.addWidget(btn)
        row = {"real": real, "frame": frame, "btn": btn, "name": self._clean(real)}
        btn.setChecked(enabled)
        btn.setText("Enabled" if enabled else "Disabled")
        btn.toggled.connect(lambda checked, r=row: self.on_toggle(r, checked))
        self.list_layout.insertWidget(self.list_layout.count() - 1, frame)
        self.rows.append(row)

    def apply_addon_filter(self, *_):
        q = self.filter_edit.text().strip().lower()
        shown = enabled = 0
        for r in self.rows:
            vis = q in r["name"].lower()
            r["frame"].setVisible(vis)
            if vis:
                shown += 1
                enabled += r["btn"].isChecked()
        self.empty_label.setVisible(not shown)
        self.count_label.setText(f"{shown} addon{'s' if shown != 1 else ''}, {enabled} enabled" if self.rows else "")

    def _move(self, row, enable):
        base, off = self.addons_dir(), os.path.join(self.addons_dir(), "disabled")
        name = row["name"]
        if enable:
            src, dst = os.path.join(off, row["real"]), os.path.join(base, name)
        else:
            src, dst = os.path.join(base, row["real"]), os.path.join(off, name + self.DISABLED_SUFFIX)
        if os.path.exists(dst):
            raise OSError(f'"{os.path.basename(dst)}" already exists in the destination folder')
        shutil.move(src, dst)
        row["real"] = os.path.basename(dst)

    def on_toggle(self, row, enable):
        btn = row["btn"]
        try:
            self._move(row, enable)
            btn.setText("Enabled" if enable else "Disabled")
        except Exception as e:
            btn.blockSignals(True)
            btn.setChecked(not enable)
            btn.blockSignals(False)
            QMessageBox.warning(self, "Addon Manager", f"Couldn't move addon: {e}")
        self.apply_addon_filter()

    def set_all(self, enable):
        for r in self.rows:
            if not r["frame"].isVisible() or r["btn"].isChecked() == enable:
                continue
            try:
                self._move(r, enable)
                r["btn"].blockSignals(True)
                r["btn"].setChecked(enable)
                r["btn"].setText("Enabled" if enable else "Disabled")
                r["btn"].blockSignals(False)
            except Exception as e:
                self.log(f"Couldn't move {r['name']}: {e}", "error")
        self.apply_addon_filter()

_gmod_prev_init = AdaptiveApp.__init__

def _gmod_init(self):
    _gmod_prev_init(self)
    self.gmod_page = GmodAddonManagerPage()
    self.pages.addWidget(self.gmod_page)
    idx = self.pages.indexOf(self.gmod_page)
    self.gmod_btn = self.make_nav_button("Garry's Mod Addon Manager", checkable=True)
    self.nav_group.addButton(self.gmod_btn, idx)
    sidebar_layout = self.nav_group.button(0).parent().layout()
    sidebar_layout.insertWidget(sidebar_layout.indexOf(self.nav_group.button(1)), self.gmod_btn)
    self.gmod_btn.setStyleSheet("QPushButton#NavButton { padding: 0px 10px; }")
    self.gmod_btn.hide()

    def refresh_gmod():
        root = find_gmod_root()
        self.gmod_btn.setVisible(bool(root))
        self.gmod_page.set_root(root)
        if not root and self.pages.currentWidget() is self.gmod_page:
            self.nav_group.button(0).setChecked(True)
            self.pages.setCurrentIndex(0)

    refresh_gmod()
    self._gmod_timer = QTimer(self)
    self._gmod_timer.setInterval(4000)
    self._gmod_timer.timeout.connect(refresh_gmod)
    self._gmod_timer.start()
    self.pages.currentChanged.connect(
        lambda _i: self.gmod_page.refresh_addons() if self.pages.currentWidget() is self.gmod_page else None)

AdaptiveApp.__init__ = _gmod_init

# ---------------------------------------------------------------- Steam Workshop tab
import io as _io
import tarfile as _tarfile

STEAMCMD_WIN_URL = "https://client-update.steamstatic.com/installer/steamcmd.zip"
STEAMCMD_LINUX_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
WS_SORTS = (("Trending", "trend"), ("Most Recent", "mostrecent"),
            ("Most Subscribed", "totaluniquesubscribers"), ("Recently Updated", "lastupdated"))

# The launcher's load_launcher_settings() drops unknown keys, so these tabs keep their own small file.
def _ws_cfg_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "workshop_tools.json")

def _ws_cfg_load():
    try:
        with open(_ws_cfg_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def ws_cfg_get(key, default=""):
    return _ws_cfg_load().get(key, default)

def ws_cfg_set(key, value):
    data = _ws_cfg_load()
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    os.makedirs(os.path.dirname(_ws_cfg_path()), exist_ok=True)
    with open(_ws_cfg_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def ws_downloads_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "workshop_downloads.json")

def ws_load_downloads():
    try:
        with open(ws_downloads_path(), encoding="utf-8") as f:
            data = json.load(f)
        return [d for d in data if isinstance(d, dict) and d.get("id") and d.get("appid")]
    except Exception:
        return []

def ws_save_downloads(items):
    os.makedirs(os.path.dirname(ws_downloads_path()), exist_ok=True)
    with open(ws_downloads_path(), "w", encoding="utf-8") as f:
        json.dump(items, f, indent=4)

def steamcmd_dir():
    return os.path.join(SCRIPT_DIR, "steamcmd")

def steamcmd_exe():
    return os.path.join(steamcmd_dir(), "steamcmd.exe" if sys.platform.startswith("win") else "steamcmd.sh")

def workshop_dir():
    return os.path.join(SCRIPT_DIR, "workshop")

def workshop_item_dir(appid, item_id):
    return os.path.join(workshop_dir(), "steamapps", "workshop", "content", str(appid), str(item_id))

def ensure_steamcmd(status):
    exe = steamcmd_exe()
    if os.path.exists(exe):
        return exe
    os.makedirs(steamcmd_dir(), exist_ok=True)
    win = sys.platform.startswith("win")
    status("Downloading SteamCMD...")
    data = _http_get(STEAMCMD_WIN_URL if win else STEAMCMD_LINUX_URL, timeout=120)
    status("Extracting SteamCMD...")
    if win:
        with zipfile.ZipFile(_io.BytesIO(data)) as z:
            z.extractall(steamcmd_dir())
    else:
        with _tarfile.open(fileobj=_io.BytesIO(data), mode="r:gz") as t:
            try:
                t.extractall(steamcmd_dir(), filter="data")
            except TypeError:
                t.extractall(steamcmd_dir())
        try:
            os.chmod(exe, 0o755)
        except OSError:
            pass
    if not os.path.exists(exe):
        raise RuntimeError("SteamCMD could not be extracted")
    return exe

def _ws_has_files(path):
    if not os.path.isdir(path):
        return False
    for _folder, _dirs, names in os.walk(path):
        if names:
            return True
    return False

def locate_workshop_item(appid, item_id):
    """Path of a downloaded Workshop item, or None. Also rescues items SteamCMD put in its own folder."""
    canon = workshop_item_dir(appid, item_id)
    if _ws_has_files(canon):
        return canon
    alt = os.path.join(steamcmd_dir(), "steamapps", "workshop", "content", str(appid), str(item_id))
    if _ws_has_files(alt):
        try:
            shutil.rmtree(canon, ignore_errors=True)
            os.makedirs(os.path.dirname(canon), exist_ok=True)
            shutil.move(alt, canon)
            return canon
        except Exception:
            return None
    return None

def ws_steamcmd_log_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "workshop_steamcmd.log")

_WS_ID_RE = re.compile(r'data-publishedfileid="(\d+)"')
_WS_TITLE_RE = re.compile(r'class="workshopItemTitle[^"]*"[^>]*>(.*?)</div>', re.S)
_WS_AUTHOR_RE = re.compile(r'class="workshopItemAuthorName[^"]*"[^>]*>(.*?)</div>', re.S)

def _ws_text(fragment):
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", fragment or "")).replace("\xa0", " ").strip()
    return re.sub(r"\s+", " ", text)

def _parse_workshop_items_legacy(page):
    """Old Workshop markup (data-publishedfileid attributes)."""
    starts, seen = [], set()
    for m in _WS_ID_RE.finditer(page):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            starts.append((m.start(), m.group(1)))
    items = []
    for i, (pos, fid) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(page)
        chunk = page[pos:end]
        title = _WS_TITLE_RE.search(chunk)
        img = _IMG_RE.search(chunk)
        author = _WS_AUTHOR_RE.search(chunk)
        author_text = re.sub(r"^by\s+", "", _ws_text(author.group(1)) if author else "", flags=re.I)
        items.append({"id": fid, "title": _ws_text(title.group(1)) if title else f"Item {fid}",
                      "preview": html_lib.unescape(img.group(1)) if img else "", "author": author_text})
    return items

_WS_A_RE = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.S)
_WS_HREF_RE = re.compile(r'\bhref="([^"]*)"')
_WS_FILE_RE = re.compile(r'filedetails/\?id=(\d+)')
_WS_ALT_RE = re.compile(r'<img\b[^>]*?\balt="([^"]*)"', re.S)

def parse_workshop_items(page):
    """Reads Workshop items from the links Steam renders: each item is a link to
    sharedfiles/filedetails/?id=ID (image + title) followed by an author link."""
    order, info, current = [], {}, None
    for m in _WS_A_RE.finditer(page):
        hm = _WS_HREF_RE.search(m.group(1))
        if not hm:
            continue
        href, inner = html_lib.unescape(hm.group(1)), m.group(2)
        fm = _WS_FILE_RE.search(href)
        if fm:
            fid = fm.group(1)
            if fid not in info:
                info[fid] = {"id": fid, "title": "", "preview": "", "author": ""}
                order.append(fid)
            current = info[fid]
            img = _IMG_RE.search(inner)
            if img and not current["preview"]:
                current["preview"] = re.sub(r"(imw|imh)=\d+", r"\1=288", html_lib.unescape(img.group(1)))
            text = _ws_text(inner)
            if not text:
                alt = _WS_ALT_RE.search(inner)
                text = html_lib.unescape(alt.group(1)).strip() if alt else ""
            if text and not current["title"]:
                current["title"] = text
        elif "myworkshopfiles" in href and current is not None and not current["author"]:
            current["author"] = re.sub(r"^by\s+", "", _ws_text(inner), flags=re.I)
    items = []
    for fid in order:
        d = info[fid]
        d["title"] = d["title"] or f"Item {fid}"
        items.append(d)
    return items or _parse_workshop_items_legacy(page)

def _ws_installed_games():
    out = []
    for g in load_steam_library():
        exe = g.get("exe_path")
        installed = bool(exe and os.path.exists(exe))
        if not installed:
            installed = bool(find_best_game_exe(_safe_installed_game_dir(g["title"]), g["title"]))
        if installed:
            out.append(g)
    return out

def _fmt_size(n):
    """Format a byte count: MB below 1 GB, GB from 1 GB up."""
    n = float(n)
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    return f"{n / (1024 * 1024):.1f} MB"

def _ws_fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024

def _ws_dir_size(path):
    total = 0
    for folder, _d, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(folder, n))
            except OSError:
                pass
    return total

def _ws_crop_round(pm, w, h, radius=8):
    scale = 2
    scaled = pm.scaled(w * scale, h * scale, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                       Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(w * scale, h * scale)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, w * scale, h * scale), radius * scale, radius * scale)
    p.setClipPath(clip)
    p.drawPixmap((w * scale - scaled.width()) // 2, (h * scale - scaled.height()) // 2, scaled)
    p.end()
    out.setDevicePixelRatio(scale)
    return out

class _WsImages(QObject):
    loaded = pyqtSignal(str, bytes)

    def __init__(self):
        super().__init__()
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.cache, self.pending = {}, {}
        self.loaded.connect(self._on_loaded)
        QApplication.instance().aboutToQuit.connect(lambda: self.pool.shutdown(wait=False, cancel_futures=True))

    def request(self, url, setter):
        if not url:
            return
        if url in self.cache:
            setter(self.cache[url])
        elif url in self.pending:
            self.pending[url].append(setter)
        else:
            self.pending[url] = [setter]
            self.pool.submit(self._fetch, url)

    def _fetch(self, url):
        try:
            data = _http_get(url, timeout=15)
        except Exception:
            data = b""
        self.loaded.emit(url, data)

    def _on_loaded(self, url, data):
        setters = self.pending.pop(url, [])
        pm = QPixmap()
        if not data or not pm.loadFromData(data):
            return
        self.cache[url] = pm
        for s in setters:
            try:
                s(pm)
            except RuntimeError:  # the label was deleted meanwhile
                pass

class WorkshopFetchWorker(QThread):
    done = pyqtSignal(int, list, str)

    def __init__(self, token, appid, query, sort, page, parent=None):
        super().__init__(parent)
        self.token, self.appid, self.query, self.sort, self.page = token, appid, query, sort, page

    def run(self):
        try:
            params = {"appid": self.appid, "searchtext": self.query, "childpublishedfileid": 0,
                      "browsesort": self.sort, "section": "readytouseitems", "p": self.page,
                      "numperpage": 30, "l": "english"}
            if self.sort == "trend":
                params["days"] = 7
            headers = {"Accept-Language": "en-US,en;q=0.9", "Cookie": "birthtime=568022401; lastagecheckage=1-0-1988"}
            url = "https://steamcommunity.com/workshop/browse/?" + urllib.parse.urlencode(params)
            page = _http_get(url, headers=headers, timeout=20).decode("utf-8", "replace")
            items = parse_workshop_items(page)
            if not items and self.page == 1 and not self.query:  # fall back to the game's Workshop hub page
                page = _http_get(f"https://steamcommunity.com/app/{self.appid}/workshop/?l=english",
                                 headers=headers, timeout=20).decode("utf-8", "replace")
                items = parse_workshop_items(page)
            if not items and not self.query:
                dump = os.path.join(os.path.dirname(get_launcher_settings_path()), "workshop_debug.html")
                try:
                    os.makedirs(os.path.dirname(dump), exist_ok=True)
                    with open(dump, "w", encoding="utf-8") as f:
                        f.write(page)
                except OSError:
                    pass
                self.done.emit(self.token, [], f"Steam's page had no items I could read (saved a copy to {dump})")
                return
            self.done.emit(self.token, items, "")
        except Exception as e:
            self.done.emit(self.token, [], str(e))

class SteamCmdWorker(QThread):
    status = pyqtSignal(str)
    progress = pyqtSignal(float)
    finished_item = pyqtSignal(bool, str)

    def __init__(self, appid, item_id, login, parent=None):
        super().__init__(parent)
        self.appid, self.item_id, self.login = appid, str(item_id), login or "anonymous"
        self.proc = None
        self._cancel = False

    def cancel(self):
        self._cancel = True
        try:
            if self.proc:
                self.proc.kill()
        except Exception:
            pass

    def run(self):
        log = []
        try:
            exe = ensure_steamcmd(self.status.emit)
            os.makedirs(workshop_dir(), exist_ok=True)
            cmd = [exe, "+force_install_dir", workshop_dir(), "+login", self.login,
                   "+workshop_download_item", str(self.appid), self.item_id, "+quit"]
            kwargs = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}
            err, rc = "", None
            for attempt in range(3):  # the first run(s) may self-update and exit before downloading
                if self._cancel:
                    break
                self.status.emit("Downloading..." if attempt == 0 else "Retrying...")
                log.append(f"--- attempt {attempt + 1}: {' '.join(cmd)}")
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, text=True, errors="replace",
                                             cwd=steamcmd_dir(), **kwargs)
                for raw in self.proc.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    log.append(line)
                    m = re.search(r"progress:\s*([\d.]+)", line)
                    if m:
                        self.progress.emit(float(m.group(1)))
                    if re.search(r"ERROR!|^Failure|Login Failure", line):
                        err = line
                    self.status.emit(line[:100])
                rc = self.proc.wait()
                if locate_workshop_item(self.appid, self.item_id) or (err and attempt >= 1):
                    break
            try:
                with open(ws_steamcmd_log_path(), "w", encoding="utf-8") as f:
                    f.write("\n".join(log))
            except OSError:
                pass
            if self._cancel:
                self.finished_item.emit(False, "Cancelled")
            elif locate_workshop_item(self.appid, self.item_id):
                self.finished_item.emit(True, "")
            else:
                tail = [l for l in log if not l.startswith("---")][-3:]
                msg = err or ("SteamCMD finished without downloading the item. Last output: " + " | ".join(tail)
                              if tail else f"SteamCMD printed nothing (exit code {rc})")
                if any(k in msg for k in ("Failure", "No subscription", "Access Denied", "not logged")):
                    msg += " (this game's Workshop may need a Steam account that owns it)"
                self.finished_item.emit(False, f"{msg} [log: {ws_steamcmd_log_path()}]")
        except Exception as e:
            self.finished_item.emit(False, str(e))

class _WsClickFrame(QFrame):
    clicked = pyqtSignal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

_WS_MOD_COUNTS = {}   # appid -> number of Workshop items (only successful lookups are cached)
WS_MIN_MODS = 3       # a game is listed in the Workshop picker only with MORE than 2 mods

class WorkshopCountWorker(QThread):
    done = pyqtSignal(int, int)   # appid, count (-1 = lookup failed)

    def __init__(self, appid, parent=None):
        super().__init__(parent)
        self.appid = appid

    def run(self):
        try:
            params = {"appid": self.appid, "searchtext": "", "childpublishedfileid": 0,
                      "browsesort": "trend", "section": "readytouseitems", "p": 1,
                      "numperpage": 30, "l": "english", "days": 7}
            headers = {"Accept-Language": "en-US,en;q=0.9", "Cookie": "birthtime=568022401; lastagecheckage=1-0-1988"}
            url = "https://steamcommunity.com/workshop/browse/?" + urllib.parse.urlencode(params)
            page = _http_get(url, headers=headers, timeout=20).decode("utf-8", "replace")
            m = re.search(r"of\s+([\d,\.]+)\s+entries", page)
            count = int(re.sub(r"[^\d]", "", m.group(1))) if m else len(parse_workshop_items(page))
            self.done.emit(self.appid, count)
        except Exception:
            self.done.emit(self.appid, -1)

class WorkshopPage(QWidget):
    CARD_W = 200
    GAME_W = 170

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QProgressBar, QSizePolicy
        self._QProgressBar, self._QSizePolicy = QProgressBar, QSizePolicy
        self.img = _WsImages()
        self.appid = None
        self.game_title = ""
        self.queue = []
        self.worker = None
        self._fetchers = []
        self.token = 0
        self.page_num = 1
        self.seen_ids = set()
        self.cards = {}        # item id -> (frame, download button)
        self.card_list = []
        self.game_cards = []
        self.dl_rows = []
        self._cols = (0, 0)
        self.setObjectName("Content")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 28, 36, 20)
        outer.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(0)
        title = QLabel("Steam Workshop")
        title.setObjectName("PageTitle")
        self.game_label = QLabel()
        self.game_label.setObjectName("PageCount")
        self.back_btn = QPushButton("< Choose another game")
        self.back_btn.setFlat(True)
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self.show_picker)
        head.addWidget(title)
        head.addWidget(self.game_label, 0, Qt.AlignmentFlag.AlignBottom)
        head.addStretch()
        head.addWidget(self.back_btn)
        outer.addLayout(head)
        outer.addSpacing(18)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        # ---------------- picker
        pick = QWidget()
        pl = QVBoxLayout(pick)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(10)
        hint = QLabel("Choose a game you've downloaded to browse its Steam Workshop.")
        hint.setObjectName("RowDesc")
        pl.addWidget(hint)
        sc = self._scroll()
        self.games_container = QWidget()
        self.games_container.setObjectName("GridContainer")
        self.games_grid = QGridLayout(self.games_container)
        self.games_grid.setContentsMargins(0, 6, 12, 24)
        self.games_grid.setSpacing(14)
        self.games_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        sc.setWidget(self.games_container)
        pl.addWidget(sc, 1)
        self.games_empty = QLabel("No downloaded Steam games yet.\nInstall a game from the Library or "
                                  "use Add Existing Game, then come back.")
        self.games_empty.setObjectName("EmptyState")
        self.games_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pl.addWidget(self.games_empty)
        self.stack.addWidget(pick)

        # ---------------- game view
        gv = QWidget()
        gl = QVBoxLayout(gv)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(0)
        tabs = QHBoxLayout()
        tabs.setSpacing(8)
        self.tab_group = QButtonGroup(self)
        self.tab_btns = []
        for i, text in enumerate(("Browse", "Queue", "Downloaded")):
            b = QPushButton(text)
            b.setObjectName("WsTab")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.tab_group.addButton(b, i)
            self.tab_btns.append(b)
            tabs.addWidget(b)
        tabs.addStretch()
        self.tab_btns[0].setChecked(True)
        gl.addLayout(tabs)
        gl.addSpacing(14)
        self.inner = QStackedWidget()
        gl.addWidget(self.inner, 1)
        self.tab_group.idClicked.connect(self.inner.setCurrentIndex)

        # browse
        br = QWidget()
        bl = QVBoxLayout(br)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(12)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("Search")
        self.search_edit.setPlaceholderText("Search the Workshop")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedHeight(36)
        self.search_edit.returnPressed.connect(self.search)
        self.sort_combo = QComboBox()
        self.sort_combo.setFixedHeight(36)
        for label, key in WS_SORTS:
            self.sort_combo.addItem(label, key)
        self.sort_combo.currentIndexChanged.connect(lambda _i: self.search())
        go = QPushButton("Search")
        go.setObjectName("Primary")
        go.setFixedHeight(36)
        go.clicked.connect(self.search)
        bar.addWidget(self.search_edit, 1)
        bar.addWidget(self.sort_combo)
        bar.addWidget(go)
        bl.addLayout(bar)
        sc2 = self._scroll()
        self.browse_container = QWidget()
        self.browse_container.setObjectName("GridContainer")
        bc = QVBoxLayout(self.browse_container)
        bc.setContentsMargins(0, 0, 12, 20)
        bc.setSpacing(12)
        self.browse_grid = QGridLayout()
        self.browse_grid.setSpacing(14)
        self.browse_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        bc.addLayout(self.browse_grid)
        self.status_label = QLabel()
        self.status_label.setObjectName("EmptyState")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        bc.addWidget(self.status_label)
        self.more_btn = QPushButton("Load more")
        self.more_btn.setFixedHeight(38)
        self.more_btn.clicked.connect(lambda: self._fetch(self.page_num + 1))
        bc.addWidget(self.more_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        bc.addStretch()
        sc2.setWidget(self.browse_container)
        bl.addWidget(sc2, 1)
        self.inner.addWidget(br)

        # queue
        qw = QWidget()
        ql = QVBoxLayout(qw)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.setSpacing(12)
        qbar = QHBoxLayout()
        qbar.setSpacing(8)
        self.cmd_label = QLabel()
        self.cmd_label.setObjectName("RowDesc")
        self.login_edit = QLineEdit()
        self.login_edit.setPlaceholderText("Steam login: anonymous")
        self.login_edit.setToolTip("Leave empty for anonymous downloads. To use an account, run steamcmd once "
                                   "from the steamcmd folder and log in so it remembers you.")
        self.login_edit.setFixedWidth(220)
        self.login_edit.setText(ws_cfg_get("steam_login", ""))
        self.login_edit.editingFinished.connect(
            lambda: ws_cfg_set("steam_login", self.login_edit.text().strip() or None))
        clear = QPushButton("Clear finished")
        clear.clicked.connect(self.clear_finished)
        qbar.addWidget(self.cmd_label, 1)
        qbar.addWidget(self.login_edit)
        qbar.addWidget(clear)
        ql.addLayout(qbar)
        sc3 = self._scroll()
        self.queue_container = QWidget()
        self.queue_container.setObjectName("GridContainer")
        self.queue_layout = QVBoxLayout(self.queue_container)
        self.queue_layout.setContentsMargins(0, 0, 12, 16)
        self.queue_layout.setSpacing(8)
        self.queue_layout.addStretch()
        sc3.setWidget(self.queue_container)
        ql.addWidget(sc3, 1)
        self.queue_empty = QLabel("The queue is empty.\nPress Download on a Workshop item to add it.")
        self.queue_empty.setObjectName("EmptyState")
        self.queue_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ql.addWidget(self.queue_empty)
        self.inner.addWidget(qw)

        # downloaded
        dw = QWidget()
        dl = QVBoxLayout(dw)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(12)
        sc4 = self._scroll()
        self.dl_container = QWidget()
        self.dl_container.setObjectName("GridContainer")
        self.dl_layout = QVBoxLayout(self.dl_container)
        self.dl_layout.setContentsMargins(0, 0, 12, 16)
        self.dl_layout.setSpacing(8)
        self.dl_layout.addStretch()
        sc4.setWidget(self.dl_container)
        dl.addWidget(sc4, 1)
        self.dl_empty = QLabel("Nothing downloaded for this game yet.")
        self.dl_empty.setObjectName("EmptyState")
        self.dl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(self.dl_empty)
        self.inner.addWidget(dw)

        self.stack.addWidget(gv)
        self.back_btn.hide()
        self._apply_css()
        self._rebuild_games()
        QApplication.instance().aboutToQuit.connect(self._shutdown)

    # ------------------------------------------------------------ helpers
    def _scroll(self):
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return sc

    def _apply_css(self):
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        t = THEMES["dark" if dark else "light"]
        self.setStyleSheet(f"""
QPushButton#WsTab {{ background: transparent; color: {t['subtext']}; border: 1px solid {t['border']};
    border-radius: 17px; padding: 8px 20px; font-size: 13px; }}
QPushButton#WsTab:hover {{ background: {t['hover']}; color: {t['text']}; }}
QPushButton#WsTab:checked {{ background: {t['accent']}; color: #ffffff; border: 1px solid {t['accent']}; }}
QFrame#WsCard {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 12px; }}
QFrame#WsCard:hover {{ border: 1px solid {t['accent']}; }}
QFrame#WsRow {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QLabel#WsPlaceholder {{ background: {t['input']}; border-radius: 8px; color: {t['subtext']}; }}
""")

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_css()
        self.cmd_label.setText("SteamCMD is ready" if os.path.exists(steamcmd_exe())
                               else "SteamCMD will be downloaded on your first download")
        if self.stack.currentIndex() == 0:
            self._rebuild_games()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cols = (self._calc_cols(self.GAME_W), self._calc_cols(self.CARD_W))
        if cols != self._cols:
            self._cols = cols
            self._layout_grid(self.games_grid, self.game_cards, cols[0])
            self._layout_grid(self.browse_grid, self.card_list, cols[1])

    def _calc_cols(self, card_w):
        return max(1, (max(self.width() - 72 - 16, card_w) + 14) // (card_w + 14))

    def _layout_grid(self, grid, widgets, cols):
        while grid.count():
            grid.takeAt(0)
        top_left = Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        for i, w in enumerate(widgets):
            grid.addWidget(w, i // cols, i % cols, top_left)  # keep cards at their natural size
        for r in range(grid.rowCount()):
            grid.setRowStretch(r, 0)
        grid.setRowStretch(len(widgets) // cols + 1, 1)  # spare space goes below the last row

    def _clear_layout_rows(self, layout, rows):
        for r in rows:
            r["frame"].setParent(None)
            r["frame"].deleteLater()
        rows.clear()

    def _shutdown(self):
        try:
            if self.worker and self.worker.isRunning():
                self.worker.cancel()
                self.worker.wait(2000)
            for w in list(self._fetchers):
                w.wait(1000)
        except Exception:
            pass

    # ------------------------------------------------------------ game picker
    def _rebuild_games(self):
        games = _ws_installed_games()
        if not hasattr(self, "_counting"):
            self._counting = set()
        for g in games:
            aid = g["appid"]
            if aid in _WS_MOD_COUNTS or aid in self._counting:
                continue
            self._counting.add(aid)
            w = WorkshopCountWorker(aid, self)
            w.done.connect(self._on_mod_count)
            self._fetchers.append(w)
            w.finished.connect(lambda w=w: self._fetchers.remove(w) if w in self._fetchers else None)
            w.start()
        self._render_games(games)

    def _on_mod_count(self, appid, count):
        self._counting.discard(appid)
        if count >= 0:
            _WS_MOD_COUNTS[appid] = count
        if self.stack.currentIndex() == 0:
            self._render_games()

    def _render_games(self, games=None):
        for w in self.game_cards:
            w.setParent(None)
            w.deleteLater()
        self.game_cards = []
        games = games if games is not None else _ws_installed_games()
        shown = [g for g in games if _WS_MOD_COUNTS.get(g["appid"], 0) >= WS_MIN_MODS]
        for g in shown:
            self.game_cards.append(self._make_game_card(g))
        checking = any(g["appid"] in self._counting for g in games)
        if not games:
            self.games_empty.setText("No downloaded Steam games yet.\nInstall a game from the Library or "
                                     "use Add Existing Game, then come back.")
        elif checking and not shown:
            self.games_empty.setText("Checking which games have Workshop mods...")
        else:
            self.games_empty.setText("No games with more than 2 Workshop mods found.")
        self.games_empty.setVisible(not shown)
        self._cols = (self._calc_cols(self.GAME_W), self._calc_cols(self.CARD_W))
        self._layout_grid(self.games_grid, self.game_cards, self._cols[0])

    def _make_game_card(self, g):
        card = _WsClickFrame()
        card.setObjectName("WsCard")
        card.setFixedWidth(self.GAME_W)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        v = QVBoxLayout(card)
        v.setContentsMargins(8, 8, 8, 10)
        v.setSpacing(8)
        cover = QLabel()
        cover.setFixedSize(self.GAME_W - 16, 216)
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pm = rounded_cover_pixmap(g["cover"], self.GAME_W - 16, 216, 8) if g.get("cover") else None
        if pm:
            cover.setPixmap(pm)
        else:
            cover.setObjectName("WsPlaceholder")
            cover.setText("No\nCover")
        name = QLabel(g["title"])
        name.setObjectName("RowTitle")
        name.setWordWrap(True)
        v.addWidget(cover)
        v.addWidget(name)
        card.setSizePolicy(self._QSizePolicy.Policy.Fixed, self._QSizePolicy.Policy.Maximum)
        card.clicked.connect(lambda g=g: self.open_game(g))
        return card

    def show_picker(self):
        self.appid = None
        self.token += 1
        self.stack.setCurrentIndex(0)
        self.back_btn.hide()
        self.game_label.setText("")
        self._rebuild_games()

    def open_game(self, g):
        self.appid, self.game_title = g["appid"], g["title"]
        self.game_label.setText(self.game_title)
        self.back_btn.show()
        self.search_edit.clear()
        self.sort_combo.blockSignals(True)
        self.sort_combo.setCurrentIndex(0)
        self.sort_combo.blockSignals(False)
        self.tab_btns[0].setChecked(True)
        self.inner.setCurrentIndex(0)
        self.stack.setCurrentIndex(1)
        self.search()
        self._rebuild_downloaded()
        self._refresh_tab_titles()

    # ------------------------------------------------------------ browse
    def search(self, *_):
        if self.appid is None:
            return
        for frame, _btn in self.cards.values():
            frame.setParent(None)
            frame.deleteLater()
        self.cards, self.card_list, self.seen_ids = {}, [], set()
        self._fetch(1)

    def _fetch(self, page):
        self.token += 1
        self.page_num = page
        self.more_btn.hide()
        self.status_label.setText("Loading...")
        self.status_label.show()
        w = WorkshopFetchWorker(self.token, self.appid, self.search_edit.text().strip(),
                                self.sort_combo.currentData(), page, self)
        w.done.connect(self._on_fetched)
        self._fetchers.append(w)
        w.finished.connect(lambda w=w: self._fetchers.remove(w) if w in self._fetchers else None)
        w.start()

    def _on_fetched(self, token, items, err):
        if token != self.token:
            return
        if err:
            self.status_label.setText(f"Couldn't load the Workshop: {err}")
            return
        new = [it for it in items if it["id"] not in self.seen_ids]
        for it in new:
            self.seen_ids.add(it["id"])
            card = self._make_item_card(it)
            self.card_list.append(card)
        self._layout_grid(self.browse_grid, self.card_list, self._calc_cols(self.CARD_W))
        self._refresh_card_states()
        if not self.card_list:
            self.status_label.setText("No Workshop items found for this game.")
        else:
            self.status_label.hide()
        self.more_btn.setVisible(len(items) >= 9 and bool(new))

    def _make_item_card(self, it):
        card = QFrame()
        card.setObjectName("WsCard")
        card.setFixedWidth(self.CARD_W)
        v = QVBoxLayout(card)
        v.setContentsMargins(8, 8, 8, 10)
        v.setSpacing(6)
        prev = QLabel()
        prev.setObjectName("WsPlaceholder")
        prev.setFixedSize(self.CARD_W - 16, 104)
        prev.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img.request(it["preview"], lambda pm, l=prev: l.setPixmap(_ws_crop_round(pm, self.CARD_W - 16, 104)))
        title = QLabel(it["title"])
        title.setObjectName("RowTitle")
        title.setWordWrap(True)
        title.setFixedHeight(40)
        title.setToolTip(it["title"])
        author = QLabel(("by " + it["author"]) if it["author"] else "")
        author.setObjectName("RowDesc")
        author.setSizePolicy(self._QSizePolicy.Policy.Ignored, self._QSizePolicy.Policy.Preferred)
        row = QHBoxLayout()
        row.setSpacing(6)
        btn = QPushButton("Download")
        btn.setObjectName("Primary")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _c=False, it=it: self.enqueue(it))
        view = QPushButton("View")
        view.setFlat(True)
        view.setCursor(Qt.CursorShape.PointingHandCursor)
        view.clicked.connect(lambda _c=False, i=it["id"]: QDesktopServices.openUrl(
            QUrl(f"https://steamcommunity.com/sharedfiles/filedetails/?id={i}")))
        row.addWidget(btn, 1)
        row.addWidget(view)
        v.addWidget(prev)
        v.addWidget(title)
        v.addWidget(author)
        v.addLayout(row)
        self.cards[it["id"]] = (card, btn)
        return card

    def _refresh_card_states(self):
        done = {d["id"] for d in self._downloads_for_game()}
        active = {q["id"]: q["status"] for q in self.queue
                  if q["appid"] == self.appid and q["status"] in ("queued", "downloading")}
        for item_id, (_f, btn) in self.cards.items():
            if item_id in active:
                btn.setText("Downloading..." if active[item_id] == "downloading" else "Queued")
                btn.setEnabled(False)
            elif item_id in done:
                btn.setText("Downloaded")
                btn.setEnabled(False)
            else:
                btn.setText("Download")
                btn.setEnabled(True)

    # ------------------------------------------------------------ queue
    def _refresh_tab_titles(self):
        active = sum(1 for q in self.queue if q["status"] in ("queued", "downloading"))
        self.tab_btns[1].setText(f"Queue ({active})" if active else "Queue")
        n = len(self._downloads_for_game())
        self.tab_btns[2].setText(f"Downloaded ({n})" if n else "Downloaded")

    def enqueue(self, it):
        if any(q["id"] == it["id"] and q["appid"] == self.appid and q["status"] in ("queued", "downloading")
               for q in self.queue):
            return
        self.queue.append({"appid": self.appid, "game": self.game_title, "id": it["id"], "title": it["title"],
                           "preview": it["preview"], "author": it.get("author", ""),
                           "status": "queued", "msg": "", "line": "", "pct": None, "w": None})
        self._rebuild_queue()
        self._refresh_card_states()
        self._refresh_tab_titles()
        self._pump()

    def _pump(self):
        if self.worker and self.worker.isRunning():
            return
        nxt = next((q for q in self.queue if q["status"] == "queued"), None)
        if not nxt:
            return
        nxt["status"], nxt["line"], nxt["pct"] = "downloading", "Starting...", None
        self._update_queue_row(nxt)
        self._refresh_card_states()
        w = SteamCmdWorker(nxt["appid"], nxt["id"], ws_cfg_get("steam_login", ""), self)
        w.status.connect(lambda t, e=nxt: self._on_status(e, t))
        w.progress.connect(lambda p, e=nxt: self._on_progress(e, p))
        w.finished_item.connect(lambda ok, msg, e=nxt: self._on_item_done(e, ok, msg))
        w.finished.connect(self._pump)
        self.worker = w
        w.start()

    def _on_status(self, e, text):
        e["line"] = text
        self._update_queue_row(e)

    def _on_progress(self, e, pct):
        e["pct"] = pct
        self._update_queue_row(e)

    def _on_item_done(self, e, ok, msg):
        if e["status"] != "cancelled":
            if ok:
                e["status"] = "done"
                downloads = [d for d in ws_load_downloads()
                             if not (d["id"] == e["id"] and d["appid"] == e["appid"])]
                downloads.append({"appid": e["appid"], "id": e["id"], "title": e["title"], "game": e["game"],
                                  "preview": e["preview"], "author": e["author"], "time": int(time.time())})
                ws_save_downloads(downloads)
            else:
                e["status"], e["msg"] = "failed", msg
        self._update_queue_row(e)
        self._refresh_card_states()
        self._refresh_tab_titles()
        if self.appid is not None:
            self._rebuild_downloaded()

    def _rebuild_queue(self):
        while self.queue_layout.count() > 1:
            item = self.queue_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        for e in self.queue:
            self._make_queue_row(e)
        self.queue_empty.setVisible(not self.queue)

    def _make_queue_row(self, e):
        frame = QFrame()
        frame.setObjectName("WsRow")
        frame.setFixedHeight(68)
        h = QHBoxLayout(frame)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(12)
        thumb = QLabel()
        thumb.setObjectName("WsPlaceholder")
        thumb.setFixedSize(80, 45)
        self.img.request(e["preview"], lambda pm, l=thumb: l.setPixmap(_ws_crop_round(pm, 80, 45, 6)))
        col = QVBoxLayout()
        col.setSpacing(2)
        col.setContentsMargins(0, 0, 0, 0)
        name = QLabel(e["title"])
        name.setObjectName("RowTitle")
        name.setSizePolicy(self._QSizePolicy.Policy.Ignored, self._QSizePolicy.Policy.Preferred)
        sub = QLabel()
        sub.setObjectName("RowDesc")
        sub.setSizePolicy(self._QSizePolicy.Policy.Ignored, self._QSizePolicy.Policy.Preferred)
        bar = self._QProgressBar()
        bar.setTextVisible(False)
        col.addStretch()
        col.addWidget(name)
        col.addWidget(sub)
        col.addWidget(bar)
        col.addStretch()
        btn = QPushButton()
        btn.setFixedHeight(30)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _c=False, e=e: self._queue_action(e))
        h.addWidget(thumb)
        h.addLayout(col, 1)
        h.addWidget(btn)
        e["w"] = {"frame": frame, "sub": sub, "bar": bar, "btn": btn}
        self.queue_layout.insertWidget(self.queue_layout.count() - 1, frame)
        self._update_queue_row(e)

    def _update_queue_row(self, e):
        w = e.get("w")
        if not w:
            return
        try:
            st = e["status"]
            text = {"queued": f"Queued · {e['game']}", "downloading": e["line"] or "Downloading...",
                    "done": f"Downloaded · {e['game']}", "failed": e["msg"] or "Failed",
                    "cancelled": "Cancelled"}[st]
            w["sub"].setText(text)
            w["sub"].setToolTip(text)
            w["sub"].setStyleSheet({"done": f"color: {GREEN};", "failed": f"color: {RED};"}.get(st, ""))
            w["bar"].setVisible(st == "downloading")
            if st == "downloading":
                if e["pct"] is None:
                    w["bar"].setRange(0, 0)
                else:
                    w["bar"].setRange(0, 100)
                    w["bar"].setValue(int(e["pct"]))
            label = {"queued": "Remove", "downloading": "Cancel", "failed": "Retry", "cancelled": "Retry"}.get(st)
            w["btn"].setVisible(bool(label))
            if label:
                w["btn"].setText(label)
        except RuntimeError:
            pass

    def _queue_action(self, e):
        st = e["status"]
        if st == "queued":
            self.queue.remove(e)
            self._rebuild_queue()
        elif st == "downloading":
            e["status"] = "cancelled"
            if self.worker:
                self.worker.cancel()
            self._update_queue_row(e)
        elif st in ("failed", "cancelled"):
            e["status"], e["msg"] = "queued", ""
            self._update_queue_row(e)
            self._pump()
        self._refresh_card_states()
        self._refresh_tab_titles()

    def clear_finished(self):
        self.queue = [q for q in self.queue if q["status"] in ("queued", "downloading")]
        self._rebuild_queue()

    # ------------------------------------------------------------ downloaded
    def _downloads_for_game(self):
        return [d for d in ws_load_downloads()
                if d["appid"] == self.appid and os.path.isdir(workshop_item_dir(d["appid"], d["id"]))]

    def _rebuild_downloaded(self):
        while self.dl_layout.count() > 1:
            item = self.dl_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        entries = sorted(self._downloads_for_game(), key=lambda d: d.get("time", 0), reverse=True)
        for d in entries:
            self._make_downloaded_row(d)
        self.dl_empty.setVisible(not entries)
        self._refresh_tab_titles()

    def _make_downloaded_row(self, d):
        path = workshop_item_dir(d["appid"], d["id"])
        frame = QFrame()
        frame.setObjectName("WsRow")
        frame.setFixedHeight(68)
        h = QHBoxLayout(frame)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(12)
        thumb = QLabel()
        thumb.setObjectName("WsPlaceholder")
        thumb.setFixedSize(80, 45)
        self.img.request(d.get("preview", ""), lambda pm, l=thumb: l.setPixmap(_ws_crop_round(pm, 80, 45, 6)))
        col = QVBoxLayout()
        col.setSpacing(2)
        col.setContentsMargins(0, 0, 0, 0)
        name = QLabel(d["title"])
        name.setObjectName("RowTitle")
        name.setSizePolicy(self._QSizePolicy.Policy.Ignored, self._QSizePolicy.Policy.Preferred)
        when = time.strftime("%Y-%m-%d", time.localtime(d.get("time", 0)))
        sub = QLabel(f"ID {d['id']} · {_ws_fmt_size(_ws_dir_size(path))} · {when}")
        sub.setObjectName("RowDesc")
        col.addStretch()
        col.addWidget(name)
        col.addWidget(sub)
        col.addStretch()
        h.addWidget(thumb)
        h.addLayout(col, 1)
        if d["appid"] == 4000:
            inst = QPushButton("Install to Garry's Mod")
            inst.setObjectName("Primary")
            inst.setFixedHeight(30)
            inst.clicked.connect(lambda _c=False, d=d: self._install_to_gmod(d))
            h.addWidget(inst)
        op = QPushButton("Open folder")
        op.setFixedHeight(30)
        op.clicked.connect(lambda _c=False, p=path: self._open_folder(p))
        rm = QPushButton("Delete")
        rm.setFixedHeight(30)
        rm.clicked.connect(lambda _c=False, d=d: self._delete_download(d))
        h.addWidget(op)
        h.addWidget(rm)
        self.dl_layout.insertWidget(self.dl_layout.count() - 1, frame)

    def _open_folder(self, path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif not QDesktopServices.openUrl(QUrl.fromLocalFile(path)) and shutil.which("xdg-open"):
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.warning(self, "Open folder", f"Couldn't open the folder:\n{e}")

    def _delete_download(self, d):
        if QMessageBox.question(self, "Delete download", f"Delete \"{d['title']}\" from disk?") \
                != QMessageBox.StandardButton.Yes:
            return
        shutil.rmtree(workshop_item_dir(d["appid"], d["id"]), ignore_errors=True)
        ws_save_downloads([x for x in ws_load_downloads()
                           if not (x["id"] == d["id"] and x["appid"] == d["appid"])])
        self._rebuild_downloaded()
        self._refresh_card_states()

    def _install_to_gmod(self, d):
        root = find_gmod_root()
        if not root:
            QMessageBox.information(self, "Garry's Mod", "Garry's Mod isn't installed in the launcher.")
            return
        msgs = []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            addons = os.path.join(root, "garrysmod", "addons")
            os.makedirs(addons, exist_ok=True)
            extract_workshop_folder(workshop_item_dir(d["appid"], d["id"]), addons,
                                    lambda t, k: msgs.append(t), lambda a, b: None)
        except Exception as e:
            msgs.append(f"Error: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "Install to Garry's Mod", "\n".join(msgs[:-1][-5:] or msgs))

_ws_prev_init = AdaptiveApp.__init__

def _ws_init(self):
    _ws_prev_init(self)
    self.workshop_page = WorkshopPage()
    self.pages.addWidget(self.workshop_page)
    idx = self.pages.indexOf(self.workshop_page)
    btn = self.make_nav_button("Steam Workshop", checkable=True)
    self.nav_group.addButton(btn, idx)
    layout = self.nav_group.button(0).parent().layout()
    anchor = getattr(self, "gmod_btn", None) or self.nav_group.button(1)
    layout.insertWidget(layout.indexOf(anchor), btn)

AdaptiveApp.__init__ = _ws_init

# ---------------------------------------------------------------- GE Proton tab (Linux only)

GE_API = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases?per_page=30"

def ge_install_dir():
    # Scanned by both Steam and the launcher's compatibility tool picker.
    return os.path.expanduser("~/.local/share/Steam/compatibilitytools.d")

def ge_installed_paths():
    found = {}
    for base in (ge_install_dir(), os.path.expanduser("~/.steam/root/compatibilitytools.d")):
        try:
            for n in os.listdir(base):
                if os.path.exists(os.path.join(base, n, "proton")):
                    found.setdefault(n, os.path.join(base, n))
        except OSError:
            pass
    return found

def parse_ge_releases(data):
    out = []
    for r in data if isinstance(data, list) else []:
        assets = r.get("assets") or []
        tar = next((a for a in assets if a.get("name", "").endswith(".tar.gz")), None)
        if r.get("draft") or not tar:
            continue
        sha = next((a for a in assets if a.get("name", "").endswith(".sha512sum")), None)
        out.append({"name": tar["name"][:-7], "date": (r.get("published_at") or "")[:10],
                    "size": tar.get("size", 0), "url": tar["browser_download_url"],
                    "sha": sha["browser_download_url"] if sha else ""})
    return out

class GeFetchWorker(QThread):
    done = pyqtSignal(list, str)

    def run(self):
        try:
            data = json.loads(_http_get(GE_API, headers={"Accept": "application/vnd.github+json"}, timeout=20))
            self.done.emit(parse_ge_releases(data), "")
        except Exception as e:
            self.done.emit([], str(e))

class GeInstallWorker(QThread):
    progress = pyqtSignal(int)
    result = pyqtSignal(bool, str)

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.info = info

    def run(self):
        tmp = None
        try:
            dest = ge_install_dir()
            os.makedirs(dest, exist_ok=True)
            tmp = os.path.join(dest, self.info["name"] + ".tar.gz.part")
            expected = ""
            if self.info["sha"]:
                try:
                    expected = _http_get(self.info["sha"], timeout=20).decode().split()[0].lower()
                except Exception:
                    expected = ""
            digest = _hashlib.sha512()
            req = urllib.request.Request(self.info["url"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
                total = int(resp.headers.get("Content-Length") or self.info["size"] or 0)
                got = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
                    if total:
                        self.progress.emit(int(got * 90 / total))
            if expected and digest.hexdigest() != expected:
                raise RuntimeError("Checksum mismatch, the download was corrupted. Try again.")
            self.progress.emit(92)
            with _tarfile.open(tmp, "r:gz") as t:
                try:
                    t.extractall(dest, filter="tar")
                except TypeError:  # Python without extraction filters
                    root = os.path.abspath(dest)
                    for m in t.getmembers():
                        if not os.path.abspath(os.path.join(root, m.name)).startswith(root + os.sep):
                            raise RuntimeError("Unsafe path in archive")
                    t.extractall(dest)
            self.progress.emit(100)
            self.result.emit(True, "")
        except Exception as e:
            self.result.emit(False, str(e))
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

class GeProtonPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QProgressBar
        self._QProgressBar = QProgressBar
        self.releases, self.workers, self.rows = [], {}, {}
        self.loaded = False
        self.fetcher = None
        self.setObjectName("Content")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 28, 36, 20)
        outer.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(0)
        title = QLabel("GE Proton")
        title.setObjectName("PageTitle")
        self.count_label = QLabel()
        self.count_label.setObjectName("PageCount")
        refresh = QPushButton("Refresh")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.clicked.connect(self.load)
        head.addWidget(title)
        head.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBottom)
        head.addStretch()
        head.addWidget(refresh)
        outer.addLayout(head)
        outer.addSpacing(4)
        desc = QLabel("GloriousEggroll's Proton builds. Installed versions appear in the launcher's "
                      "compatibility tool picker, so you can run games with them.")
        desc.setObjectName("RowDesc")
        desc.setWordWrap(True)
        outer.addWidget(desc)
        outer.addSpacing(16)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.container.setObjectName("GridContainer")
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(0, 0, 12, 16)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch()
        sc.setWidget(self.container)
        outer.addWidget(sc, 1)
        self.status = QLabel()
        self.status.setObjectName("EmptyState")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.status.hide()
        QApplication.instance().aboutToQuit.connect(self._shutdown)

    def _shutdown(self):
        for w in list(self.workers.values()) + ([self.fetcher] if self.fetcher else []):
            try:
                w.wait(1500)
            except Exception:
                pass

    def showEvent(self, event):
        super().showEvent(event)
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        t = THEMES["dark" if dark else "light"]
        self.setStyleSheet(f"QFrame#GeRow {{ background: {t['panel']}; border: 1px solid {t['border']}; "
                           f"border-radius: 10px; }} QFrame#GeRow:hover {{ border: 1px solid {t['accent']}; }}")
        if not self.loaded:
            self.load()
        else:
            self.render_rows()

    def load(self):
        if self.fetcher and self.fetcher.isRunning():
            return
        self.status.setText("Loading versions...")
        self.status.show()
        self.fetcher = GeFetchWorker(self)
        self.fetcher.done.connect(self._on_loaded)
        self.fetcher.start()

    def _on_loaded(self, releases, err):
        if err:
            self.status.setText(f"Couldn't load GE Proton versions: {err}")
            return
        self.loaded = True
        self.releases = releases
        self.status.setVisible(not releases)
        if not releases:
            self.status.setText("No GE Proton releases found.")
        self.render_rows()

    def render_rows(self):
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        self.rows = {}
        installed = ge_installed_paths()
        for i, info in enumerate(self.releases):
            self._add_row(info, info["name"] in installed, i == 0)
        n = sum(1 for r in self.releases if r["name"] in installed)
        self.count_label.setText(f"{len(self.releases)} versions, {n} installed" if self.releases else "")

    def _add_row(self, info, installed, latest):
        name = info["name"]
        frame = QFrame()
        frame.setObjectName("GeRow")
        frame.setFixedHeight(64)
        h = QHBoxLayout(frame)
        h.setContentsMargins(18, 0, 12, 0)
        h.setSpacing(12)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.setContentsMargins(0, 0, 0, 0)
        title = QLabel(name)
        title.setObjectName("RowTitle")
        sub = QLabel(" · ".join(x for x in (info["date"], _ws_fmt_size(info["size"]) if info["size"] else "",
                                            "Latest" if latest else "") if x))
        sub.setObjectName("RowDesc")
        bar = self._QProgressBar()
        bar.setTextVisible(False)
        bar.setRange(0, 100)
        bar.setVisible(False)
        col.addStretch()
        col.addWidget(title)
        col.addWidget(sub)
        col.addWidget(bar)
        col.addStretch()
        btn = QPushButton()
        btn.setFixedHeight(32)
        btn.setMinimumWidth(110)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        h.addLayout(col, 1)
        if installed:
            tag = QLabel("Installed")
            tag.setStyleSheet(f"color: {GREEN}; font-weight: 600;")
            h.addWidget(tag)
        h.addWidget(btn)
        if name in self.workers:
            btn.setText("Installing...")
            btn.setEnabled(False)
            bar.setVisible(True)
        elif installed:
            btn.setText("Uninstall")
            btn.clicked.connect(lambda _c=False, n=name: self.uninstall(n))
        else:
            btn.setText("Install")
            btn.setObjectName("Primary")
            btn.clicked.connect(lambda _c=False, i=info: self.install(i))
        self.rows[name] = {"bar": bar, "btn": btn}
        self.list_layout.insertWidget(self.list_layout.count() - 1, frame)

    def install(self, info):
        name = info["name"]
        if name in self.workers:
            return
        w = GeInstallWorker(info, self)
        self.workers[name] = w
        w.progress.connect(lambda p, n=name: self._on_progress(n, p))
        w.result.connect(lambda ok, msg, n=name: self._on_result(n, ok, msg))
        w.start()
        self.render_rows()

    def _on_progress(self, name, pct):
        row = self.rows.get(name)
        if row:
            try:
                row["bar"].setValue(pct)
            except RuntimeError:
                pass

    def _on_result(self, name, ok, msg):
        self.workers.pop(name, None)
        self.render_rows()
        if not ok:
            QMessageBox.warning(self, "GE Proton", f"Couldn't install {name}:\n{msg}")

    def uninstall(self, name):
        path = ge_installed_paths().get(name)
        if not path:
            self.render_rows()
            return
        if QMessageBox.question(self, "GE Proton", f"Uninstall {name}?") != QMessageBox.StandardButton.Yes:
            return
        shutil.rmtree(path, ignore_errors=True)
        self.render_rows()

_ge_prev_init = AdaptiveApp.__init__

def _ge_init(self):
    _ge_prev_init(self)
    if not sys.platform.startswith("linux"):
        return
    self.ge_page = GeProtonPage()
    self.pages.addWidget(self.ge_page)
    btn = self.make_nav_button("GE Proton", checkable=True)
    self.nav_group.addButton(btn, self.pages.indexOf(self.ge_page))
    layout = self.nav_group.button(0).parent().layout()
    layout.insertWidget(layout.indexOf(self.nav_group.button(1)), btn)

AdaptiveApp.__init__ = _ge_init

# ---------------------------------------------------------------- Wine tab (Linux only)
# Plain Wine builds from Kron4ek's Wine-Builds. Installed builds show up in every game's
# "Switch Compatibility Tool" picker next to the Proton tools.

WINE_API = "https://api.github.com/repos/Kron4ek/Wine-Builds/releases?per_page=40"
WINE_ARCH_PREFERENCE = ("amd64-wow64", "amd64")   # wow64 runs 32-bit games without 32-bit system libraries
_WINE_ASSET = re.compile(r"wine-(?P<ver>\d+(?:\.\d+)*(?:-rc\d+)?)(?P<staging>-staging)?-(?P<arch>amd64-wow64|amd64)\.tar\.xz")

def parse_wine_releases(data):
    out = []
    for r in data if isinstance(data, list) else []:
        if r.get("draft"):
            continue
        hashes = {name.strip("`*"): h.lower()
                  for h, name in re.findall(r"\b([0-9a-fA-F]{64})\s+(\S+)", r.get("body") or "")}
        best = {}   # staging? -> (arch rank, asset, match): one build per flavor per release
        for a in r.get("assets") or []:
            m = _WINE_ASSET.fullmatch(a.get("name", ""))
            if not m:
                continue
            staging = bool(m.group("staging"))
            rank = WINE_ARCH_PREFERENCE.index(m.group("arch"))
            if staging not in best or rank < best[staging][0]:
                best[staging] = (rank, a, m)
        for staging, (_rank, a, m) in best.items():
            digest = a.get("digest") or ""
            sha = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else hashes.get(a["name"], "")
            out.append({"name": a["name"][:-len(".tar.xz")], "version": m.group("ver"), "staging": staging,
                        "date": (r.get("published_at") or "")[:10], "size": a.get("size", 0),
                        "url": a["browser_download_url"], "sha": sha})
    return out

class WineFetchWorker(QThread):
    done = pyqtSignal(list, str)

    def run(self):
        try:
            data = json.loads(_http_get(WINE_API, headers={"Accept": "application/vnd.github+json"}, timeout=20))
            self.done.emit(parse_wine_releases(data), "")
        except Exception as e:
            self.done.emit([], str(e))

class WineInstallWorker(QThread):
    progress = pyqtSignal(int)
    result = pyqtSignal(bool, str)

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.info = info

    def run(self):
        tmp = stage = None
        try:
            dest = wine_builds_dir()
            os.makedirs(dest, exist_ok=True)
            name = self.info["name"]
            tmp = os.path.join(dest, "." + name + ".tar.xz.part")
            digest = _hashlib.sha256()
            req = urllib.request.Request(self.info["url"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
                total = int(resp.headers.get("Content-Length") or self.info["size"] or 0)
                got = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
                    if total:
                        self.progress.emit(int(got * 80 / total))
            expected = self.info.get("sha", "")
            if expected and digest.hexdigest() != expected:
                raise RuntimeError("Checksum mismatch, the download was corrupted. Try again.")
            self.progress.emit(85)
            stage = os.path.join(dest, "." + name + ".tmp")
            shutil.rmtree(stage, ignore_errors=True)
            os.makedirs(stage)
            with _tarfile.open(tmp, "r:xz") as t:
                try:
                    t.extractall(stage, filter="tar")
                except TypeError:  # Python without extraction filters
                    root = os.path.abspath(stage)
                    for m in t.getmembers():
                        if not os.path.abspath(os.path.join(root, m.name)).startswith(root + os.sep):
                            raise RuntimeError("Unsafe path in archive")
                    t.extractall(stage)
            folders = [d for d in os.listdir(stage) if os.path.isdir(os.path.join(stage, d))]
            top = os.path.join(stage, folders[0]) if len(folders) == 1 else stage
            if not _wine_bin_in(top):
                raise RuntimeError("The download doesn't contain a wine binary.")
            final = os.path.join(dest, name)
            shutil.rmtree(final, ignore_errors=True)
            os.replace(top, final)
            self.progress.emit(100)
            self.result.emit(True, "")
        except Exception as e:
            self.result.emit(False, str(e))
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if stage:
                shutil.rmtree(stage, ignore_errors=True)

class WinePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.releases, self.workers, self.rows = [], {}, {}
        self.loaded = False
        self.fetcher = None
        self.system_wine = None
        self.setObjectName("Content")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 28, 36, 20)
        outer.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("Wine")
        title.setObjectName("PageTitle")
        self.count_label = QLabel()
        self.count_label.setObjectName("PageCount")
        self.flavor = QComboBox()
        self.flavor.addItem("Wine", False)
        self.flavor.addItem("Wine Staging", True)
        self.flavor.currentIndexChanged.connect(self.render_rows)
        refresh = QPushButton("Refresh")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.clicked.connect(self.load)
        head.addWidget(title)
        head.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBottom)
        head.addStretch()
        head.addWidget(self.flavor)
        head.addWidget(refresh)
        outer.addLayout(head)
        outer.addSpacing(4)
        desc = QLabel("Plain Wine builds. Installed builds appear in each game's Switch Compatibility Tool "
                      "menu, so you can run any game, fan games included, with a specific Wine version.")
        desc.setObjectName("RowDesc")
        desc.setWordWrap(True)
        outer.addWidget(desc)
        outer.addSpacing(6)
        self.system_label = QLabel()
        self.system_label.setObjectName("RowDesc")
        self.system_label.setWordWrap(True)
        outer.addWidget(self.system_label)
        outer.addSpacing(16)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.container.setObjectName("GridContainer")
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(0, 0, 12, 16)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch()
        sc.setWidget(self.container)
        outer.addWidget(sc, 1)
        self.status = QLabel()
        self.status.setObjectName("EmptyState")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.status.hide()
        QApplication.instance().aboutToQuit.connect(self._shutdown)

    def _shutdown(self):
        for w in list(self.workers.values()) + ([self.fetcher] if self.fetcher else []):
            try:
                w.wait(1500)
            except Exception:
                pass

    def showEvent(self, event):
        super().showEvent(event)
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        t = THEMES["dark" if dark else "light"]
        self.setStyleSheet(f"QFrame#WineRow {{ background: {t['panel']}; border: 1px solid {t['border']}; "
                           f"border-radius: 10px; }} QFrame#WineRow:hover {{ border: 1px solid {t['accent']}; }}")
        if self.system_wine is None:
            self.system_wine = find_wine() or ()
        if self.system_wine:
            self.system_label.setText(f"System Wine: {self.system_wine[1]} ({self.system_wine[0]}). "
                                      "It's what games use when no tool is picked.")
        else:
            self.system_label.setText("System Wine: not found. Install a build below and games will use it.")
        if not self.loaded:
            self.load()
        else:
            self.render_rows()

    def load(self):
        if self.fetcher and self.fetcher.isRunning():
            return
        self.status.setText("Loading versions...")
        self.status.show()
        self.fetcher = WineFetchWorker(self)
        self.fetcher.done.connect(self._on_loaded)
        self.fetcher.start()

    def _on_loaded(self, releases, err):
        if err:
            self.status.setText(f"Couldn't load Wine versions: {err}")
            self.render_rows()
            return
        self.loaded = True
        self.releases = releases
        self.status.setVisible(not releases)
        if not releases:
            self.status.setText("No Wine builds found.")
        self.render_rows()

    def _section(self, text):
        label = QLabel(text)
        label.setObjectName("RowDesc")
        label.setStyleSheet("font-weight: 600;")
        self.list_layout.insertWidget(self.list_layout.count() - 1, label)

    def render_rows(self, *_):
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        self.rows = {}
        installed = wine_installed_builds()
        if installed:
            self._section("Installed")
            for name in installed:
                self._add_row({"name": name}, True, False)
        staging = bool(self.flavor.currentData())
        available = [r for r in self.releases if r["staging"] == staging and r["name"] not in installed]
        if available:
            self._section("Available")
            for i, info in enumerate(available):
                self._add_row(info, False, i == 0)
        total = len([r for r in self.releases if r["staging"] == staging])
        self.count_label.setText(f"{len(installed)} installed" + (f", {total} available" if total else ""))

    def _add_row(self, info, installed, latest):
        name = info["name"]
        frame = QFrame()
        frame.setObjectName("WineRow")
        frame.setFixedHeight(64)
        h = QHBoxLayout(frame)
        h.setContentsMargins(18, 0, 12, 0)
        h.setSpacing(12)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.setContentsMargins(0, 0, 0, 0)
        title = QLabel(name)
        title.setObjectName("RowTitle")
        parts = [] if installed else [info.get("date", ""), _ws_fmt_size(info["size"]) if info.get("size") else "",
                                      "Latest" if latest else ""]
        sub = QLabel(" · ".join(x for x in parts if x))
        sub.setObjectName("RowDesc")
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setRange(0, 100)
        bar.setVisible(False)
        col.addStretch()
        col.addWidget(title)
        if sub.text():
            col.addWidget(sub)
        col.addWidget(bar)
        col.addStretch()
        btn = QPushButton()
        btn.setFixedHeight(32)
        btn.setMinimumWidth(110)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        h.addLayout(col, 1)
        if name in self.workers:
            btn.setText("Installing...")
            btn.setEnabled(False)
            bar.setVisible(True)
        elif installed:
            btn.setText("Uninstall")
            btn.clicked.connect(lambda _c=False, n=name: self.uninstall(n))
        else:
            btn.setText("Install")
            btn.setObjectName("Primary")
            btn.clicked.connect(lambda _c=False, i=info: self.install(i))
        h.addWidget(btn)
        self.rows[name] = {"bar": bar, "btn": btn}
        self.list_layout.insertWidget(self.list_layout.count() - 1, frame)

    def install(self, info):
        name = info["name"]
        if name in self.workers:
            return
        w = WineInstallWorker(info, self)
        self.workers[name] = w
        w.progress.connect(lambda p, n=name: self._on_progress(n, p))
        w.result.connect(lambda ok, msg, n=name: self._on_result(n, ok, msg))
        w.start()
        self.render_rows()

    def _on_progress(self, name, pct):
        row = self.rows.get(name)
        if row:
            try:
                row["bar"].setValue(pct)
            except RuntimeError:
                pass

    def _on_result(self, name, ok, msg):
        self.workers.pop(name, None)
        self.render_rows()
        if not ok:
            QMessageBox.warning(self, "Wine", f"Couldn't install {name}:\n{msg}")

    def uninstall(self, name):
        path = os.path.dirname(os.path.dirname(wine_installed_builds().get(name, "")))
        if not path or not os.path.isdir(path):
            self.render_rows()
            return
        if QMessageBox.question(self, "Wine", f"Uninstall {name}?\nGames set to use it will fall back to the default wine.") \
                != QMessageBox.StandardButton.Yes:
            return
        shutil.rmtree(path, ignore_errors=True)
        self.render_rows()

_wine_prev_init = AdaptiveApp.__init__

def _wine_init(self):
    _wine_prev_init(self)
    if not sys.platform.startswith("linux"):
        return
    self.wine_page = WinePage()
    self.pages.addWidget(self.wine_page)
    btn = self.make_nav_button("Wine", checkable=True)
    self.nav_group.addButton(btn, self.pages.indexOf(self.wine_page))
    layout = self.nav_group.button(0).parent().layout()
    ge_btn = self.nav_group.button(self.pages.indexOf(self.ge_page)) if hasattr(self, "ge_page") else None
    at = layout.indexOf(ge_btn) + 1 if ge_btn is not None else layout.indexOf(self.nav_group.button(1))
    layout.insertWidget(at, btn)

AdaptiveApp.__init__ = _wine_init

# ---------------------------------------------------------------- H toggles the download browser window
from PyQt6.QtCore import QObject as _QObject, QEvent as _QEvent, QRect as _QRect
from PyQt6.QtWidgets import (QTextEdit as _QTextEdit, QPlainTextEdit as _QPlainTextEdit,
                             QAbstractSpinBox as _QAbstractSpinBox, QToolTip as _QToolTip)
from PyQt6.QtGui import QCursor as _QCursor

class _PwHeadlessKey(_QObject):
    """Pressing H (outside text fields) flips PW_HEADLESS. Applies from the next download."""
    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def eventFilter(self, obj, ev):
        if (ev.type() == _QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_H
                and ev.modifiers() == Qt.KeyboardModifier.NoModifier and not ev.isAutoRepeat()):
            w = QApplication.focusWidget()
            if isinstance(w, (QLineEdit, _QTextEdit, _QPlainTextEdit, _QAbstractSpinBox)) or \
                    (isinstance(w, QComboBox) and w.isEditable()):
                return False  # let people type the letter h
            globals()["PW_HEADLESS"] = not globals()["PW_HEADLESS"]
            state = "hidden" if globals()["PW_HEADLESS"] else "visible"
            _QToolTip.showText(_QCursor.pos(), f"Download browser: {state} (next download)", self.window, _QRect(), 2500)
            return True
        return False

_pwh_prev_init = AdaptiveApp.__init__

def _pwh_init(self):
    _pwh_prev_init(self)
    self._pw_key_filter = _PwHeadlessKey(self)
    QApplication.instance().installEventFilter(self._pw_key_filter)

AdaptiveApp.__init__ = _pwh_init


# ---------------------------------------------------------------- Fan Games tab (Game Jolt list from fan_games.csv, same UI as the Store)
import csv as _csv

def _fan_csv_path():
    names = ("fan_games.csv", "gamejolt_by_tag.csv")
    dirs = []
    for getter in (_writable_dir, _bundled_dir):
        try:
            dirs.append(getter())
        except Exception:
            pass
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return ""

_FAN_CACHE = None

FAN_DEFAULT_CATEGORIES = (("Undertale", "undertale"), ("FNF", "fnf"), ("FNaF", "fnaf"), ("Bendy", "bendy"))

def get_custom_fan_csv_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "fan_games_custom.csv")

def get_fan_categories():
    """[(button label, tag)] : All, the built-in categories, then any custom ones you've added."""
    cats = [("All", "")] + list(FAN_DEFAULT_CATEGORIES)
    known = {t for _l, t in cats}
    path = get_custom_fan_csv_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                for row in _csv.DictReader(f):
                    for t in (row.get("tags") or "").split(","):
                        t = t.strip()
                        if t and t.lower() not in known:
                            known.add(t.lower())
                            cats.append((t, t.lower()))
        except Exception:
            pass
    return cats

def add_custom_fan_game(name, link, category):
    """Saves a game to fan_games_custom.csv. Returns an error message, or "" when it worked."""
    name, link, category = name.strip(), link.strip(), category.strip()
    if not name:
        return "Enter the game's name."
    if link and not link.lower().startswith("http"):
        link = "https://" + link
    m = re.search(r"/games/[^/]+/(\d+)", link)
    if not m:
        return "Paste a Game Jolt game link, like https://gamejolt.com/games/name/12345"
    if int(m.group(1)) in {g[0] for g in load_fan_games()}:
        return "That game is already in the list."
    if not category:
        return "Pick a category (or type a custom one)."
    path = get_custom_fan_csv_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = _csv.writer(f)
            if new:
                w.writerow(["name", "link", "tags"])
            w.writerow([name, link, category.replace(",", " ")])
    except OSError as e:
        return f"Couldn't save the game: {e}"
    return ""

def reload_fan_games():
    """Throws away the cached list so the csv files are read again."""
    global _FAN_CACHE
    _FAN_CACHE = None
    return load_fan_games()

class AddFanGameDialog(QDialog):
    def __init__(self, parent, categories):
        super().__init__(parent)
        self.setWindowTitle("Add Fan Game")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Game name")
        self.link_edit = QLineEdit()
        self.link_edit.setPlaceholderText("https://gamejolt.com/games/name/12345")
        self.cat_combo = QComboBox()
        for label, tag in categories:
            if tag:
                self.cat_combo.addItem(label, label)
        self.cat_combo.addItem("Custom category...", None)
        self.custom_edit = QLineEdit()
        self.custom_edit.setPlaceholderText("New category name")
        self.custom_edit.setVisible(self.cat_combo.currentData() is None)
        self.cat_combo.currentIndexChanged.connect(
            lambda _i: self.custom_edit.setVisible(self.cat_combo.currentData() is None))
        form.addRow("Name", self.name_edit)
        form.addRow("Link", self.link_edit)
        form.addRow("Category", self.cat_combo)
        form.addRow("", self.custom_edit)
        lay.addLayout(form)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Add")
        ok.setObjectName("Primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def values(self):
        cat = self.cat_combo.currentData()
        if cat is None:
            cat = self.custom_edit.text()
        return self.name_edit.text(), self.link_edit.text(), cat

def load_fan_games():
    """[(game_id, title, link, tags)] from the csv, one entry per Game Jolt game."""
    global _FAN_CACHE
    if _FAN_CACHE is not None:
        return _FAN_CACHE
    games, seen = [], set()
    for path in (_fan_csv_path(), get_custom_fan_csv_path()):
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in _csv.DictReader(f):
                name = (row.get("name") or "").strip()
                link = (row.get("link") or "").strip()
                m = re.search(r"/games/[^/]+/(\d+)", link)
                if not (name and m):
                    continue
                gid = int(m.group(1))
                if gid in seen:
                    continue
                seen.add(gid)
                tags = {t.strip().lower() for t in (row.get("tags") or "").split(",") if t.strip()}
                games.append((gid, name, link, tags))
    _FAN_CACHE = games
    return games

def get_fan_cover_dir():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "store_cache", "fan_covers")

def _find_cover_url(payload):
    """Looks through Game Jolt's JSON for the first image url."""
    if isinstance(payload, dict):
        for key in ("img_thumbnail", "img_url", "mediaserver_url"):
            v = payload.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in payload.values():
            found = _find_cover_url(v)
            if found:
                return found
    elif isinstance(payload, list):
        for v in payload:
            found = _find_cover_url(v)
            if found:
                return found
    return ""

_FAN_LAST_ERROR = ""

def _fan_overview_via_browser(gid, timeout_s=90):
    """Last resort: opens the game's page in a headless Chromium (Playwright) and grabs the overview reply the page
    itself loads. Needs `pip install playwright` + `playwright install chromium`; returns {} when that isn't available."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {}
    box = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36")
                def on_response(resp):
                    try:
                        if "/site-api/web/discover/games/overview/" in resp.url and resp.status == 200 and not box:
                            box.update(resp.json())
                    except Exception:
                        pass
                page.on("response", on_response)
                page.goto(f"https://gamejolt.com/games/x/{gid}", wait_until="domcontentloaded", timeout=timeout_s * 1000)
                for _ in range(timeout_s * 2):
                    if box:
                        break
                    page.wait_for_timeout(500)
            finally:
                browser.close()
    except Exception:
        return {}
    payload = box.get("payload") or {}
    merged = dict(payload)
    for src in (box.get("game"), payload.get("game")):
        if isinstance(src, dict):
            merged.update(src)
    return merged

def fetch_fan_overview(gid):
    """Game Jolt's own site api (the same one its pages use). Returns the page data as one dict.
    Retries when DNS/network hiccups ("Temporary failure in name resolution"), keeps a copy on disk so a page that
    loaded once still has its details later, and writes the last failure to fan_debug.log."""
    global _FAN_LAST_ERROR
    cache_dir = os.path.join(os.path.dirname(get_launcher_settings_path()), "store_cache", "fan_overview")
    cache_file = os.path.join(cache_dir, f"{gid}.json")
    def cached(max_age):
        try:
            if time.time() - os.path.getmtime(cache_file) < max_age:
                with open(cache_file, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return None
    hit = cached(6 * 3600)
    if hit:
        return hit
    merged = _fan_overview_via_browser(gid, 60)      # first choice: a headless browser (Playwright), if it is installed
    if merged:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(merged, f)
        except Exception:
            pass
        return merged
    errors = []
    hdr = {"Accept": "application/json, text/plain, */*", "Referer": "https://gamejolt.com/", "Origin": "https://gamejolt.com"}
    for rnd in range(3):
        for path in (f"overview/{gid}?ignore", f"overview/{gid}"):
            url = f"https://gamejolt.com/site-api/web/discover/games/{path}"
            try:
                data = json.loads(_http_get(url, headers=hdr, timeout=40))
                payload = data.get("payload") or {}
                merged = dict(payload)
                for src in (data.get("game"), payload.get("game")):
                    if isinstance(src, dict):
                        merged.update(src)
                if merged:
                    try:
                        os.makedirs(cache_dir, exist_ok=True)
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump(merged, f)
                    except Exception:
                        pass
                    return merged
                errors.append(f"{url}: empty reply, keys={list(data)[:8]}")
            except Exception as e:
                errors.append(f"{url}: {type(e).__name__}: {e}")
        time.sleep(0.7 * (rnd + 1))
    old = cached(10 ** 9)       # network is down: an old copy is better than nothing
    if old:
        return old
    _FAN_LAST_ERROR = " | ".join(errors[-2:])
    try:
        with open(os.path.join(_writable_dir(), "fan_debug.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} game {gid}: {_FAN_LAST_ERROR}\n")
    except Exception:
        pass
    raise RuntimeError(_FAN_LAST_ERROR)

def _microdata(game):
    md = game.get("microdata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = None
    if isinstance(md, list):
        md = next((m for m in md if isinstance(m, dict)), None)
    return md if isinstance(md, dict) else {}

def _url_of(v):
    if isinstance(v, str) and v.startswith("http"):
        return v
    if isinstance(v, list):
        for x in v:
            u = _url_of(x)
            if u:
                return u
    if isinstance(v, dict):
        for k in ("url", "contentUrl", "img_url", "mediaserver_url"):
            u = _url_of(v.get(k))
            if u:
                return u
    return ""

def fan_cover_from_game(game):
    for key in ("thumbnail_media_item", "header_media_item"):
        u = _url_of(game.get(key))
        if u:
            return u
    u = _url_of(game.get("img_thumbnail")) or _url_of(_microdata(game).get("image"))
    if u:
        return u
    for item in game.get("mediaItems") or []:
        u = _url_of(item)
        if u:
            return u
    return ""

def _rich_text(node):
    if isinstance(node, str):
        try:
            node = json.loads(node)
        except Exception:
            return node
    parts = []
    def walk(n):
        if isinstance(n, dict):
            if isinstance(n.get("text"), str):
                parts.append(n["text"])
            for v in n.values():
                if isinstance(v, (dict, list)):
                    walk(v)
            if n.get("type") in ("paragraph", "hardBreak", "heading"):
                parts.append("\n")
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(node)
    return "".join(parts)

def fan_clean_text(text):
    """Tidies a Game Jolt description: drops [Media 123] placeholders, bare links/markdown noise and extra blank lines."""
    text = html_lib.unescape(str(text or ""))
    text = re.sub(r"\[\s*(?:media|gif|video|image|embed)[^\]]*\]", " ", text, flags=re.I)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)                  # markdown images
    text = re.sub(r"\[([^\]]+)\]\((?:https?://)[^)]*\)", r"\1", text)    # [label](url) -> label
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)                   # markdown headings
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)                     # bold
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def fan_description(game):
    candidates = [game.get(k) for k in ("description_markdown", "description_compiled", "description_content", "description")]
    candidates += [game.get("metaDescription"), _microdata(game).get("description")]
    for i, v in enumerate(candidates):
        if not v:
            continue
        if i == 2:
            text = _rich_text(v)
        elif i == 1:
            text = html_lib.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"</p>|<br\s*/?>", "\n", str(v))))
        else:
            text = str(v)
        text = fan_clean_text(text)
        if text:
            return text[:2500]
    return ""

def fan_developer(game):
    dev = game.get("developer")
    if isinstance(dev, dict):
        name = dev.get("display_name") or dev.get("username")
        if name:
            return name
    author = _microdata(game).get("author")
    if isinstance(author, list) and author:
        author = author[0]
    if isinstance(author, dict):
        return author.get("name") or ""
    return author if isinstance(author, str) else ""

_OG_RES = (re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.I),
           re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I))

class FanCoverLoader(CoverLoader):
    """Same loader as the Store, but gets each cover from the game's Game Jolt page."""
    def _fetch(self, appid, img_base, key, title):
        folder = get_fan_cover_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{appid}.img")
        marker = os.path.join(folder, f"{appid}-none3")
        if os.path.exists(path):
            return path
        if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 3 * 24 * 3600:
            return ""
        url = ""
        try:  # 1) Game Jolt's site api
            url = fan_cover_from_game(fetch_fan_overview(appid))
        except Exception:
            pass
        if not url:
            try:  # 2) the page's own preview image
                page = _http_get(img_base, timeout=15).decode("utf-8", errors="replace")
                for rx in _OG_RES:
                    m = rx.search(page)
                    if m:
                        url = html_lib.unescape(m.group(1))
                        break
            except Exception:
                pass
        if url:
            try:
                data = _http_get(url, timeout=15)
                if data:
                    with open(path, "wb") as f:
                        f.write(data)
                    return path
            except Exception:
                pass
        open(marker, "w").close()
        return ""

_OGD_RES = (re.compile(r'<meta[^>]+(?:property|name)=["\']og:description["\'][^>]+content=["\']([^"\']*)', re.I),
            re.compile(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']og:description["\']', re.I),
            re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', re.I))
_FAN_TAG_NAMES = {"undertale": "Undertale", "fnf": "FNF", "fnaf": "FNaF", "bendy": "Bendy"}

def get_fan_shots_dir():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "store_cache", "fan_shots")

def fan_screenshot_urls(game, limit=6):
    """Image urls of the game's screenshots on Game Jolt (videos are skipped)."""
    urls = []
    for item in (game.get("mediaItems") or []) if isinstance(game, dict) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or item.get("media_type") or "").lower()
        if "video" in kind or "sketchfab" in kind:
            continue
        u = _url_of(item.get("mediaserver_url")) or _url_of(item.get("img_url")) or _url_of(item.get("url"))
        if u and u not in urls:
            urls.append(u)
    return urls[:limit]

def _fan_shot_bytes(url):
    """One screenshot as a small, low quality jpeg (cached on disk, so opening the page again is instant)."""
    import hashlib
    folder = get_fan_shots_dir()
    path = os.path.join(folder, hashlib.md5(url.encode("utf-8")).hexdigest()[:16] + "_v2.jpg")
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        pass
    raw = _http_get(url, timeout=12)
    img = QImage()
    if not img.loadFromData(raw):
        return None
    small = img.scaledToHeight(260, Qt.TransformationMode.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    small.save(buf, "JPG", 78)
    data = bytes(buf.data())
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except OSError:
        pass
    return data

def fetch_fan_screenshots(urls):
    """All the screenshots at once (a few at a time), in order. Ones that fail are skipped."""
    if not urls:
        return []
    def one(u):
        try:
            return _fan_shot_bytes(u)
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=5) as pool:
        return [b for b in pool.map(one, urls) if b]


from PyQt6.QtWidgets import QGraphicsOpacityEffect as _FanOpacity
from PyQt6.QtCore import QPropertyAnimation as _FanAnim, QEvent as _FanEvent
from PyQt6.QtGui import QLinearGradient as _FanGradient, QFontMetrics as _FanFM

def _fan_num(n):
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n // 1000}K"
    if n >= 1_000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)

def _fan_ts(v):
    """Game Jolt dates come as epoch seconds or milliseconds; returns seconds (0 if unusable)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    return v / 1000.0 if v > 1e11 else v

def _fan_ago(ts):
    d = max(0, time.time() - ts)
    for secs, name in ((365 * 86400, "yr"), (30 * 86400, "mo"), (7 * 86400, "wk"), (86400, "day")):
        if d >= secs:
            n = int(d // secs)
            return f"{n} {name}{'s' if n != 1 and name != 'mo' else ''} ago"
    return "today"

_FAN_STATUS = {1: ("Finished", "#22c55e"), 2: ("In development", "#f59e0b"), 3: ("Canceled", "#f44336")}

def fan_info(game):
    """Extra facts for the page, read defensively from Game Jolt's data: anything missing is simply left out."""
    info = {"stats": [], "status": None, "platforms": [], "released": "", "updated": ""}
    if not isinstance(game, dict):
        return info
    posted = _fan_ts(game.get("published_on") or game.get("posted_on"))
    updated = _fan_ts(game.get("modified_on") or game.get("updated_on"))
    try:
        if posted:
            info["released"] = time.strftime("%B %d, %Y", time.localtime(posted)).replace(" 0", " ")
        if updated:
            info["updated"] = time.strftime("%B %d, %Y", time.localtime(updated)).replace(" 0", " ")
    except Exception:
        pass
    if posted:
        info["stats"].append((time.strftime("%Y", time.localtime(posted)), "Released"))
    if updated and (not posted or updated - posted > 86400):
        info["stats"].append((_fan_ago(updated), "Updated"))
    def _find_num(*keys):
        """First positive number stored under any of these keys, on the game or one level down."""
        pools = [game] + [v for v in game.values() if isinstance(v, dict)]
        for pool in pools:
            for k in keys:
                try:
                    n = int(pool.get(k) or 0)
                except (TypeError, ValueError):
                    n = 0
                if n > 0:
                    return n
        return 0
    for keys, label in ((("like_count", "likes_count", "likes"), "Likes"),
                        (("view_count", "views_count", "views", "page_view_count"), "Views"),
                        (("comment_count", "comments_count", "comments"), "Comments"),
                        (("follower_count", "followers_count", "followers"), "Followers"),
                        (("download_count", "downloads_count", "downloads"), "Downloads"),
                        (("play_count", "plays_count", "plays"), "Plays")):
        n = _find_num(*keys)
        if n > 0:
            info["stats"].append((_fan_num(n), label))
    try:
        info["status"] = _FAN_STATUS.get(int(game.get("development_status")))
    except (TypeError, ValueError):
        pass
    comp = game.get("compatibility")
    if isinstance(comp, dict):
        for name, keys in (("Windows", ("os_windows", "os_windows_64")), ("Linux", ("os_linux", "os_linux_64")),
                           ("Mac", ("os_mac", "os_mac_64")), ("Web", ("type_html",))):
            if any(comp.get(k) for k in keys):
                info["platforms"].append(name)
    info["stats"] = info["stats"][:7]
    return info

def _fan_fade_in(widget, ms=380):
    eff = _FanOpacity(widget)
    widget.setGraphicsEffect(eff)
    anim = _FanAnim(eff, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start()
    widget._fade_anim = anim

def _fan_round(pm, radius):
    out = QPixmap(pm.size())
    out.setDevicePixelRatio(pm.devicePixelRatio())
    out.fill(Qt.GlobalColor.transparent)
    pr = QPainter(out)
    pr.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, pm.width() / pm.devicePixelRatio(), pm.height() / pm.devicePixelRatio()),
                        radius, radius)
    pr.setClipPath(clip)
    pr.drawPixmap(0, 0, pm)
    pr.end()
    return out

class _FanHero(QWidget):
    """A soft blurred, faded copy of the cover behind the top of the page (gives it some colour)."""
    HEIGHT = 420

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._color = None

    def set_cover(self, path):
        self._color = None
        if path:
            src = QImage(path)
            if not src.isNull():
                small = src.scaled(64, 36, Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation).convertToFormat(QImage.Format.Format_RGB32)
                buckets = {}
                for y in range(small.height()):
                    for x in range(small.width()):
                        c = QColor(small.pixel(x, y))
                        r, g, b = c.red(), c.green(), c.blue()
                        if max(r, g, b) < 28 or min(r, g, b) > 232:   # ignore near-black / near-white borders
                            continue
                        key = (r >> 4, g >> 4, b >> 4)
                        n, sr, sg, sb = buckets.get(key, (0, 0, 0, 0))
                        buckets[key] = (n + 1, sr + r, sg + g, sb + b)
                if buckets:
                    n, sr, sg, sb = max(buckets.values(), key=lambda v: v[0])
                    self._color = QColor(sr // n, sg // n, sb // n)
        self.update()

    def paintEvent(self, _e):
        if self._color is None or self.width() < 2 or self.height() < 2:
            return
        g = _FanGradient(0, 0, 0, self.height())
        top, bottom = QColor(self._color), QColor(self._color)
        top.setAlpha(190)
        bottom.setAlpha(0)
        g.setColorAt(0.0, top)
        g.setColorAt(1.0, bottom)
        pr = QPainter(self)
        pr.fillRect(0, 0, self.width(), self.height(), g)

class _FanClickable(QLabel):
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)

class _FanShotViewer(QDialog):
    """Full size screenshot viewer: Left/Right arrows (or the buttons) to browse, Esc to close."""
    def __init__(self, urls, index, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Screenshots")
        self.resize(1120, 720)
        self.setStyleSheet("QDialog { background: #0b0b0d; } QLabel { color: #d7d7dc; } "
                           "QPushButton { background: #1d1d22; color: #e6e6ea; border: none; border-radius: 8px; "
                           "padding: 8px 18px; } QPushButton:hover { background: #2a2a31; }")
        self._urls, self._i, self._cache, self._workers = list(urls), index, {}, []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        self.img = QLabel("Loading...")
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img.setMinimumSize(300, 200)
        self.img.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        lay.addWidget(self.img, 1)
        bar = QHBoxLayout()
        self.prev_btn = QPushButton("\u2039 Previous")
        self.next_btn = QPushButton("Next \u203a")
        self.count = QLabel()
        self.count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for b in (self.prev_btn, self.next_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        self.next_btn.clicked.connect(lambda: self.step(1))
        bar.addWidget(self.prev_btn)
        bar.addWidget(self.count, 1)
        bar.addWidget(self.next_btn)
        lay.addLayout(bar)
        self._load()

    def step(self, d):
        self._i = (self._i + d) % len(self._urls)
        self._load()

    def _load(self):
        self.count.setText(f"{self._i + 1} / {len(self._urls)}")
        self.prev_btn.setVisible(len(self._urls) > 1)
        self.next_btn.setVisible(len(self._urls) > 1)
        url = self._urls[self._i]
        if url in self._cache:
            return self._render()
        self.img.setPixmap(QPixmap())
        self.img.setText("Loading...")
        worker = TaskWorker(lambda u=url: (u, _http_get(u, timeout=20)))
        worker.done.connect(self._loaded)
        self._workers = [w for w in self._workers if w.isRunning()] + [worker]
        worker.start()

    def _loaded(self, result):
        url, raw = result
        pm = QPixmap()
        if raw and pm.loadFromData(raw):
            self._cache[url] = pm
        if url == self._urls[self._i]:
            self._render() if url in self._cache else self.img.setText("Couldn't load this screenshot.")

    def _render(self):
        pm = self._cache.get(self._urls[self._i])
        if pm is not None and not pm.isNull():
            self.img.setText("")
            self.img.setPixmap(pm.scaled(self.img.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._render()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Left:
            self.step(-1)
        elif e.key() == Qt.Key.Key_Right:
            self.step(1)
        else:
            super().keyPressEvent(e)


class FanDetailPage(StoreDetailPage):
    """Same details page as the Store, for Game Jolt fan games - with a colour backdrop from the cover, stat cards,
    status/platform chips, a readable About section and bigger, clickable screenshots."""
    DETAIL_W, DETAIL_H = 480, 270   # horizontal cover
    ABOUT_PREVIEW = 520             # characters shown before "Show more"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._link = ""
        self._about_full = ""
        self._about_open = False
        self._shot_urls = []
        for b in self.findChildren(QPushButton):
            if b.text().startswith("< Back"):
                b.setText("< Back to Fan Games")
        self.price_label.hide()
        self.price_flag.hide()
        self.install_cb = None  # set by the app: (id, title, cover, link)
        self.steam_btn.setText("Install")
        self.steam_btn.clicked.disconnect()
        self.steam_btn.clicked.connect(
            lambda: self.install_cb and self.install_cb(self._appid, self._title, self._cover_path, self._link))

        scroll = self.findChild(QScrollArea)
        body = scroll.widget()
        layout = body.layout()
        self._body = body
        self._hero = _FanHero(body)
        self._hero.lower()
        self._hero.setGeometry(0, 0, 10, _FanHero.HEIGHT)
        body.installEventFilter(self)

        # the right-hand column of the header (title, meta, chips, buttons)
        row = next(layout.itemAt(i).layout() for i in range(layout.count()) if layout.itemAt(i).layout() is not None)
        col = row.itemAt(1).layout()
        chips_idx = next((i for i in range(col.count()) if col.itemAt(i).layout() is self.chips_row), 2)
        self.title_label.setStyleSheet("font-size: 30px; font-weight: 800;")

        # stat cards
        self.stats_wrap = QWidget()
        self.stats_row = QHBoxLayout(self.stats_wrap)
        self.stats_row.setContentsMargins(0, 6, 0, 6)
        self.stats_row.setSpacing(10)
        col.insertWidget(chips_idx + 1, self.stats_wrap)
        self.stats_wrap.hide()

        # "Open on Game Jolt" next to Add to Library / Install
        self.web_btn = QPushButton("Game Jolt page")
        self.web_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.web_btn.setFixedHeight(38)
        self.web_btn.setToolTip("Open this game on Game Jolt")
        self.web_btn.clicked.connect(lambda: self._link and webbrowser.open(self._link))
        btn_row = next(col.itemAt(i).layout() for i in range(col.count())
                       if col.itemAt(i).layout() is not None and col.itemAt(i).layout().indexOf(self.lib_btn) >= 0)
        btn_row.insertWidget(btn_row.count() - 1, self.web_btn)

        # About panel (the full description, collapsed when long)
        self.about_wrap = QFrame()
        self.about_wrap.setObjectName("Panel")
        al = QVBoxLayout(self.about_wrap)
        al.setContentsMargins(22, 18, 22, 18)
        al.setSpacing(8)
        about_title = QLabel("About this game")
        about_title.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.about_label = QLabel()
        self.about_label.setWordWrap(True)
        self.about_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.about_label.setStyleSheet("font-size: 14px;")
        self.about_more = QPushButton("Show more")
        self.about_more.setFlat(True)
        self.about_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.about_more.clicked.connect(self._toggle_about)
        al.addWidget(about_title)
        al.addWidget(self.about_label)
        al.addWidget(self.about_more, 0, Qt.AlignmentFlag.AlignLeft)
        idx = layout.indexOf(self.reqs_wrap)
        layout.insertWidget(idx, self.about_wrap)
        layout.insertSpacing(idx + 1, 20)
        self.about_wrap.hide()

        # Details panel (developer, release, platforms ...)
        self.details_wrap = QFrame()
        self.details_wrap.setObjectName("Panel")
        dl = QVBoxLayout(self.details_wrap)
        dl.setContentsMargins(22, 18, 22, 18)
        dl.setSpacing(8)
        dtitle = QLabel("Details")
        dtitle.setStyleSheet("font-size: 16px; font-weight: bold;")
        dl.addWidget(dtitle)
        self.details_grid = QGridLayout()
        self.details_grid.setHorizontalSpacing(24)
        self.details_grid.setVerticalSpacing(8)
        self.details_grid.setColumnStretch(1, 1)
        dl.addLayout(self.details_grid)
        layout.insertWidget(idx + 2, self.details_wrap)
        layout.insertSpacing(idx + 3, 20)
        self.details_wrap.hide()

        # "More like this" row
        self.similar_wrap = QFrame()
        self.similar_wrap.setObjectName("Panel")
        sl = QVBoxLayout(self.similar_wrap)
        sl.setContentsMargins(22, 18, 22, 18)
        sl.setSpacing(10)
        stitle = QLabel("More like this")
        stitle.setStyleSheet("font-size: 16px; font-weight: bold;")
        sl.addWidget(stitle)
        self.similar_scroll = QScrollArea()
        self.similar_scroll.setWidgetResizable(True)
        self.similar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.similar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.similar_scroll.setFixedHeight(168)
        self.similar_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        holder = QWidget()
        self.similar_row = QHBoxLayout(holder)
        self.similar_row.setContentsMargins(0, 0, 0, 0)
        self.similar_row.setSpacing(14)
        self.similar_scroll.setWidget(holder)
        sl.addWidget(self.similar_scroll)
        layout.insertWidget(idx + 4, self.similar_wrap)
        layout.insertSpacing(idx + 5, 20)
        self.similar_wrap.hide()
        self.open_cb = None   # set by the app: (id, title, cover)
        self._pulse = None

        # roomier screenshot strip
        self.shots_scroll.setFixedHeight(232)

    def eventFilter(self, obj, ev):
        if obj is getattr(self, "_body", None) and ev.type() == _FanEvent.Type.Resize:
            self._hero.setGeometry(0, 0, obj.width(), _FanHero.HEIGHT)
        return super().eventFilter(obj, ev)

    # ---- small builders
    def _chip(self, text, color=None):
        chip = QLabel(text)
        if color:
            c = QColor(color)
            chip.setStyleSheet(f"background: rgba({c.red()},{c.green()},{c.blue()},0.22); color: {color}; "
                               "border-radius: 11px; padding: 3px 12px; font-size: 12px; font-weight: bold;")
        else:
            chip.setStyleSheet(self.CHIP_STYLE)
        return chip

    def _stat_card(self, value, label):
        card = QFrame()
        card.setStyleSheet("QFrame { background: rgba(128,128,128,0.16); border-radius: 12px; }")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 10, 16, 10)
        cl.setSpacing(0)
        v = QLabel(value)
        v.setStyleSheet("background: transparent; font-size: 20px; font-weight: 800;")
        k = QLabel(label.upper())
        k.setStyleSheet("background: transparent; font-size: 10px; font-weight: bold; color: rgba(160,160,170,1);")
        cl.addWidget(v)
        cl.addWidget(k)
        return card

    def _toggle_about(self):
        self._about_open = not self._about_open
        self._apply_about()

    def _apply_about(self):
        text = self._about_full
        long = len(text) > self.ABOUT_PREVIEW
        shown = text if (self._about_open or not long) else text[:self.ABOUT_PREVIEW].rsplit(" ", 1)[0] + "\u2026"
        self.about_label.setText(shown)
        self.about_more.setVisible(long)
        self.about_more.setText("Show less" if self._about_open else "Show more")

    def show_game(self, appid, title, cover_path, link="", tags=()):
        self._appid = appid
        self._title = title
        self._link = link
        self._cover_path = cover_path or ""
        self._about_full = ""
        self._about_open = False
        self._shot_urls = []
        self.title_label.setText(title)
        self.meta_label.setText("Fan game  \u2022  Game Jolt")
        self.desc_label.setText("Loading details...")
        self.desc_label.setStyleSheet("font-size: 14px; color: rgba(160,160,170,1);")
        self.desc_label.show()
        self._start_pulse()
        _a = appid
        QTimer.singleShot(8000, lambda: self._appid == _a and self.desc_label.isVisible() and
                          self.desc_label.text().startswith("Loading") and
                          self.desc_label.setText("Loading details... Game Jolt can be slow, hang tight."))
        self.about_wrap.hide()
        self.details_wrap.hide()
        self.similar_wrap.hide()
        self._clear(self.details_grid)
        self._clear(self.similar_row)
        self.stats_wrap.hide()
        self._clear(self.stats_row)
        self._clear(self.chips_row)
        self._clear(self.shots_row)
        self._set_extras_visible(False)
        self._tag_chips = [t for t in sorted(tags)]
        for t in self._tag_chips:
            self.chips_row.addWidget(self._chip(_FAN_TAG_NAMES.get(t, t.title())))
        self.chips_row.addStretch()
        self.web_btn.setVisible(bool(link))
        self._hero.set_cover(cover_path)
        pix = rounded_cover_pixmap(cover_path, self.DETAIL_W, self.DETAIL_H, 14) if cover_path else None
        if pix:
            self.cover.setText("")
            self.cover.setPixmap(pix)
        else:
            self.cover.setPixmap(QPixmap())
            self.cover.setText("No\ncover")

        def work():
            desc, dev, shot_urls, info = "", "", [], {}
            try:
                game = fetch_fan_overview(appid)
                desc, dev = fan_description(game), fan_developer(game)
                shot_urls = fan_screenshot_urls(game)
                info = fan_info(game)
            except Exception:
                pass
            if not desc:
                try:
                    page = _http_get(link, timeout=40).decode("utf-8", errors="replace")
                    for rx in _OGD_RES:
                        m = rx.search(page)
                        if m and m.group(1).strip():
                            desc = fan_clean_text(m.group(1))
                            if desc:
                                break
                except Exception:
                    pass
            return (appid, desc, dev, shot_urls, info)

        worker = TaskWorker(work)
        worker.done.connect(self._on_fan_details)
        self._workers = [w for w in self._workers if w.isRunning()] + [worker]
        worker.start()

        # "More like this": games sharing tags, covers are fetched in the background
        def find_similar():
            similar = []
            try:
                mine = set(tags)
                scored = []
                for g in load_fan_games():
                    if g[0] != appid and mine & g[3]:
                        scored.append((-len(mine & g[3]), hash((g[0], appid)) & 0xffff, g))
                scored.sort(key=lambda s: (s[0], s[1]))
                loader = FanCoverLoader()
                for _s, _h, g in scored[:10]:
                    p = os.path.join(get_fan_cover_dir(), f"{g[0]}.img")
                    if not os.path.exists(p):
                        try:
                            p = loader._fetch(g[0], g[2], None, g[1])
                        except Exception:
                            p = ""
                    similar.append((g[0], g[1], p))
            except Exception:
                pass
            return (appid, similar)

        self._find_similar = find_similar     # started once the details are in, so the two don't compete for the network

    # ---- loading pulse, details panel, similar row
    def _start_pulse(self):
        self._stop_pulse()
        eff = _FanOpacity(self.desc_label)
        self.desc_label.setGraphicsEffect(eff)
        anim = _FanAnim(eff, b"opacity", self.desc_label)
        anim.setDuration(1100)
        anim.setKeyValueAt(0.0, 1.0)
        anim.setKeyValueAt(0.5, 0.35)
        anim.setKeyValueAt(1.0, 1.0)
        anim.setLoopCount(-1)
        anim.start()
        self._pulse = anim

    def _stop_pulse(self):
        if self._pulse is not None:
            try:
                self._pulse.stop()
            except RuntimeError:
                pass
            self._pulse = None
        try:
            self.desc_label.setGraphicsEffect(None)
        except RuntimeError:
            pass

    def _fill_details(self, dev, info):
        self._clear(self.details_grid)
        rows = []
        if dev:
            rows.append(("Developer", dev))
        if info.get("released"):
            rows.append(("Released", info["released"]))
        if info.get("updated") and info.get("updated") != info.get("released"):
            rows.append(("Last updated", info["updated"]))
        if info.get("status"):
            rows.append(("Status", info["status"][0]))
        if info.get("platforms"):
            rows.append(("Platforms", ", ".join(info["platforms"])))
        if self._tag_chips:
            rows.append(("Franchise", ", ".join(_FAN_TAG_NAMES.get(t, t.title()) for t in self._tag_chips)))
        for n, (k, v) in enumerate(rows):
            kl = QLabel(k.upper())
            kl.setStyleSheet("font-size: 11px; font-weight: bold; color: rgba(160,160,170,1);")
            vl = QLabel(v)
            vl.setWordWrap(True)
            vl.setStyleSheet("font-size: 14px;")
            self.details_grid.addWidget(kl, n, 0)
            self.details_grid.addWidget(vl, n, 1)
        return bool(rows)

    def _on_fan_similar(self, result):
        appid, similar = result
        if appid != self._appid or not similar or not self.open_cb:
            return
        self._clear(self.similar_row)
        for gid, title, cover in similar:
            tile = QWidget()
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(0, 0, 0, 0)
            tl.setSpacing(6)
            pic = _FanClickable()
            pic.setFixedSize(176, 99)
            pic.setCursor(Qt.CursorShape.PointingHandCursor)
            pix = rounded_cover_pixmap(cover, 176, 99, 10) if cover else None
            if pix:
                pic.setPixmap(pix)
            else:
                pic.setText("No cover")
                pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
                pic.setStyleSheet("background: rgba(128,128,128,0.18); border-radius: 10px;")
            name = _FanClickable()
            name.setFixedWidth(176)
            name.setCursor(Qt.CursorShape.PointingHandCursor)
            name.setText(_FanFM(name.font()).elidedText(title, Qt.TextElideMode.ElideRight, 176))
            name.setStyleSheet("font-size: 12px; font-weight: bold;")
            name.setToolTip(title)
            for w in (pic, name):
                w.clicked.connect(lambda g=gid, t=title, c=cover: self.open_cb(g, t, c))
            tl.addWidget(pic)
            tl.addWidget(name)
            tl.addStretch()
            self.similar_row.addWidget(tile)
        self.similar_row.addStretch()
        self.similar_wrap.show()
        _fan_fade_in(self.similar_wrap)

    def _on_fan_details(self, result):
        appid, desc, dev, shot_urls, info = result
        if appid != self._appid:
            return
        sim_worker = TaskWorker(self._find_similar)
        sim_worker.done.connect(self._on_fan_similar)
        self._workers = [w for w in self._workers if w.isRunning()] + [sim_worker]
        sim_worker.start()
        parts = []
        if dev:
            parts.append(f"by {dev}")
        parts.append("Game Jolt fan game")
        self.meta_label.setText("  \u2022  ".join(parts))

        # status + platform chips after the tag chips
        self._clear(self.chips_row)
        status = info.get("status")
        if status:
            self.chips_row.addWidget(self._chip(status[0], status[1]))
        for t in self._tag_chips:
            self.chips_row.addWidget(self._chip(_FAN_TAG_NAMES.get(t, t.title())))
        for plat in info.get("platforms", []):
            self.chips_row.addWidget(self._chip(plat))
        self.chips_row.addStretch()

        # stat cards
        stats = info.get("stats") or []
        if stats:
            for value, label in stats:
                self.stats_row.addWidget(self._stat_card(value, label))
            self.stats_row.addStretch()
            self.stats_wrap.show()
            _fan_fade_in(self.stats_wrap)

        self._stop_pulse()
        self.desc_label.hide()
        self._about_full = desc or ""
        if desc:
            self._apply_about()
            self.about_wrap.show()
            _fan_fade_in(self.about_wrap)
        else:
            self.desc_label.setText("No description available. Open the game on Game Jolt for more.")
            self.desc_label.show()

        if self._fill_details(dev, info):
            self.details_wrap.show()
            _fan_fade_in(self.details_wrap)

        self._shot_urls = list(shot_urls)
        if shot_urls:
            # the text shows right away; the screenshots follow as soon as they're downloaded
            shots_worker = TaskWorker(lambda u=shot_urls, a=appid: (a, fetch_fan_screenshots(u)))
            shots_worker.done.connect(self._on_fan_shots)
            self._workers = [w for w in self._workers if w.isRunning()] + [shots_worker]
            shots_worker.start()

    def _on_fan_shots(self, result):
        appid, shots = result
        if appid != self._appid or not shots:
            return
        self._clear(self.shots_row)
        for n, raw in enumerate(shots):
            pm = QPixmap()
            if pm.loadFromData(raw):
                shot = _FanClickable()
                scaled = pm.scaledToHeight(200, Qt.TransformationMode.SmoothTransformation)
                shot.setPixmap(_fan_round(scaled, 10))
                shot.setToolTip("Click to enlarge")
                shot.clicked.connect(lambda i=n: self._open_viewer(i))
                self.shots_row.addWidget(shot)
        self.shots_row.addStretch()
        self._set_extras_visible(True)
        _fan_fade_in(self.shots_scroll)

    def _open_viewer(self, index):
        if self._shot_urls:
            _FanShotViewer(self._shot_urls, min(index, len(self._shot_urls) - 1), self.window()).exec()

# ---------------------------------------------------------------- Scrape Games (downloads gamejolt_scraper.py and rebuilds fan_games.csv)
GAMEJOLT_SCRAPER_URL = "https://github.com/justmm33/FNaNCZ-AE-Releases/raw/refs/heads/main/gamejolt_scraper.py"

def _scraper_python():
    """A python that can run the scraper. Frozen builds have no python of their own, so look on the system."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    names = ("python", "py", "python3") if sys.platform.startswith("win") else ("python3", "python")
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None

class ScrapeWorker(QThread):
    stage = pyqtSignal(str)          # what is happening right now
    line = pyqtSignal(str)           # one line of scraper output
    finished_ok = pyqtSignal(bool, str)   # success, message

    def __init__(self, count):
        super().__init__()
        self.count = count
        self._proc = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def run(self):
        folder = _writable_dir()
        script = os.path.join(folder, "gamejolt_scraper.py")
        csv_path = os.path.join(folder, "fan_games.csv")
        backup = csv_path + ".bak"
        had_csv = os.path.exists(csv_path)
        try:
            # 1) download the newest scraper (an older copy is used if the download fails)
            self.stage.emit("Downloading the scraper...")
            try:
                req = urllib.request.Request(GAMEJOLT_SCRAPER_URL, headers={"User-Agent": "NCZ-Games-Launcher"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                if not data.strip():
                    raise ValueError("the downloaded file is empty")
                with open(script, "wb") as f:
                    f.write(data)
                self.line.emit("Downloaded gamejolt_scraper.py")
            except Exception as e:
                if not os.path.exists(script):
                    self.finished_ok.emit(False, f"Could not download the scraper: {e}")
                    return
                self.line.emit(f"Download failed ({e}), using the copy that is already here.")
            if self._cancelled:
                self.finished_ok.emit(False, "Cancelled.")
                return

            py = _scraper_python()
            if not py:
                self.finished_ok.emit(False, "Python was not found on this PC. Install Python and try again.")
                return

            # 2) run it. The old csv is kept as a backup until the new one is written.
            if had_csv:
                shutil.copy2(csv_path, backup)
            self.stage.emit("Scraping games...")
            cmd = [py, "-u", "gamejolt_scraper.py", "--count", str(self.count), "--out", "fan_games.csv"]
            env = system_env()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            kw = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}
            self._proc = subprocess.Popen(cmd, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                          bufsize=1, **kw)
            for raw in self._proc.stdout:
                text = raw.rstrip()
                if text:
                    self.line.emit(text)
            code = self._proc.wait()

            if self._cancelled:
                self._restore(csv_path, backup, had_csv)
                self.finished_ok.emit(False, "Cancelled. Your old game list was kept.")
                return
            if code != 0:
                self._restore(csv_path, backup, had_csv)
                self.finished_ok.emit(False, f"The scraper stopped with an error (code {code}). Your old game list was kept.")
                return
            if not os.path.exists(csv_path) or os.path.getsize(csv_path) < 10:
                self._restore(csv_path, backup, had_csv)
                self.finished_ok.emit(False, "The scraper finished but did not create fan_games.csv. Your old game list was kept.")
                return
            try:
                os.remove(backup)
            except OSError:
                pass
            self.finished_ok.emit(True, "Done!")
        except Exception as e:
            self._restore(csv_path, backup, had_csv)
            self.finished_ok.emit(False, f"Something went wrong: {e}")

    @staticmethod
    def _restore(csv_path, backup, had_csv):
        try:
            if had_csv and os.path.exists(backup):
                shutil.copy2(backup, csv_path)
                os.remove(backup)
        except OSError:
            pass

class ScrapeOptionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QSpinBox
        self.setWindowTitle("Scrape Games")
        self.setMinimumWidth(400)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)
        title = QLabel("Scrape Games")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        lay.addWidget(title)
        lay.addWidget(QLabel("How many games from each genre?"))
        self.spin = QSpinBox()
        self.spin.setRange(1, 5000)
        self.spin.setValue(50)
        self.spin.setMinimumHeight(32)
        lay.addWidget(self.spin)
        warn = QLabel("This can affect PC performance. It also replaces your current fan games list.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #e0a030;")
        lay.addSpacing(6)
        lay.addWidget(warn)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        go = QPushButton("Scrape")
        go.setObjectName("Primary")
        go.setDefault(True)
        go.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(go)
        lay.addLayout(row)

    def count(self):
        return int(self.spin.value())

class ScrapeProgressDialog(QDialog):
    def __init__(self, parent, count):
        super().__init__(parent)
        self.success = False
        self._done = False
        self._t0 = time.time()
        self.setWindowTitle("Scraping Game Jolt")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        self.title = QLabel("Scraping Game Jolt")
        self.title.setStyleSheet("font-size: 18px; font-weight: bold;")
        lay.addWidget(self.title)
        self.stage = QLabel("Starting...")
        lay.addWidget(self.stage)
        bar_row = QHBoxLayout()
        bar_row.setSpacing(10)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)          # moving bar until the scraper reports real numbers
        self.bar.setTextVisible(False)   # the bar is only a few pixels tall, so the % is a separate label
        self.percent = QLabel("")
        self.percent.setMinimumWidth(42)
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.percent.setStyleSheet("font-weight: bold;")
        bar_row.addWidget(self.bar, 1)
        bar_row.addWidget(self.percent)
        lay.addLayout(bar_row)
        self.detail = QLabel(f"{count} games per genre")
        self.detail.setStyleSheet("color: gray;")
        lay.addWidget(self.detail)
        self.log_btn = QPushButton("Show details")
        self.log_btn.setFlat(True)
        self.log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_btn.clicked.connect(self._toggle_log)
        lay.addWidget(self.log_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(160)
        self.log.setStyleSheet("font-family: monospace; font-size: 11px;")
        self.log.hide()
        lay.addWidget(self.log)
        row = QHBoxLayout()
        self.elapsed = QLabel("0:00")
        self.elapsed.setStyleSheet("color: gray;")
        row.addWidget(self.elapsed)
        row.addStretch()
        self.btn = QPushButton("Cancel")
        self.btn.clicked.connect(self._on_button)
        row.addWidget(self.btn)
        lay.addLayout(row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

        self.worker = ScrapeWorker(count)
        self.worker.stage.connect(self.stage.setText)
        self.worker.line.connect(self._on_line)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.start()

    def _toggle_log(self):
        show = not self.log.isVisible()
        self.log.setVisible(show)
        self.log_btn.setText("Hide details" if show else "Show details")
        self.adjustSize()

    def _tick(self):
        s = int(time.time() - self._t0)
        self.elapsed.setText(f"{s // 60}:{s % 60:02d}")

    def _on_line(self, text):
        self.log.append(text)
        short = text if len(text) <= 70 else text[:67] + "..."
        self.detail.setText(short)
        m = re.search(r"(\d+)\s*/\s*(\d+)", text)
        if m and int(m.group(2)) > 0 and int(m.group(1)) <= int(m.group(2)):
            self.bar.setRange(0, int(m.group(2)))
            self.bar.setValue(int(m.group(1)))
            self.percent.setText(f"{int(m.group(1)) * 100 // int(m.group(2))}%")
            return
        m = re.search(r"\b(\d{1,3})\s*%", text)
        if m and int(m.group(1)) <= 100:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(m.group(1)))
            self.percent.setText(f"{int(m.group(1))}%")

    def _on_finished(self, ok, message):
        self._done = True
        self.success = ok
        self._timer.stop()
        self.bar.setRange(0, 100)
        self.bar.setValue(100 if ok else 0)
        self.percent.setText("100%" if ok else "")
        if ok:
            self.title.setText("All done")
            self.stage.setText("Fan games list updated.")
            self.detail.setText("")
        else:
            self.title.setText("Scrape did not finish")
            self.stage.setText(message)
            self.stage.setWordWrap(True)
            if not self.log.isVisible():
                self._toggle_log()
        self.btn.setText("Close")
        self.btn.setObjectName("Primary")
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
        self.btn.setDefault(True)

    def _on_button(self):
        if self._done:
            self.accept()
            return
        self.btn.setEnabled(False)
        self.btn.setText("Cancelling...")
        self.stage.setText("Stopping...")
        self.worker.cancel()

    def reject(self):          # Esc must not close the window while the scraper is running
        if self._done:
            super().reject()

    def done(self, r):
        if getattr(self, "worker", None) is not None:
            self.worker.wait(3000)
        super().done(r)

FAN_COVER_W, FAN_COVER_H = 220, 124   # horizontal covers
FAN_CARD_W = FAN_COVER_W + 18

class FanGamesPage(StorePage):
    fan_selected = pyqtSignal(int, str, str)  # id, title, cover path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tags_btn.hide()    # Fan Games has its own tag buttons
        self.loader.shutdown()
        self.loader = FanCoverLoader()
        self.loader.loaded.connect(self._on_cover_loaded)
        self._links = {gid: link for gid, _t, link, _tg in load_fan_games()}
        lbl = self.findChild(QLabel, "PageTitle")
        if lbl:
            lbl.setText("Fan Games")
        self.search_edit.setPlaceholderText("Search fan games")

        # category buttons (All + built-in + any custom ones) and the Add Game button
        self._tag = ""
        self._tag_buttons = {}
        self._tag_row = QHBoxLayout()
        self._tag_row.setSpacing(8)
        self._build_tag_buttons()
        lay = self.layout()
        lay.insertLayout(2, self._tag_row)
        lay.insertSpacing(3, 10)

    def _build_tag_buttons(self):
        while self._tag_row.count():
            w = self._tag_row.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._tag_buttons = {}
        for label, tag in get_fan_categories():
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(32)
            b.setObjectName("Primary" if tag == self._tag else "")
            b.clicked.connect(lambda _c=False, t=tag: self._set_tag(t))
            self._tag_row.addWidget(b)
            self._tag_buttons[tag] = b
        self._tag_row.addStretch()
        scrape = QPushButton("Scrape Games")
        scrape.setCursor(Qt.CursorShape.PointingHandCursor)
        scrape.setFixedHeight(32)
        scrape.setToolTip("Download the Game Jolt scraper and rebuild fan_games.csv")
        scrape.clicked.connect(self._scrape_games)
        self._tag_row.addWidget(scrape)
        add = QPushButton("+ Add Game")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.setFixedHeight(32)
        add.clicked.connect(self._add_game)
        self._tag_row.addWidget(add)

    def _add_game(self):
        dlg = AddFanGameDialog(self, get_fan_categories())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, link, cat = dlg.values()
        err = add_custom_fan_game(name, link, cat)
        if err:
            QMessageBox.warning(self, "Add Fan Game", err)
            return
        games = reload_fan_games()                 # read the csv files again
        self._links = {gid: l for gid, _t, l, _tg in games}
        self._tag = cat.strip().lower()
        self._build_tag_buttons()
        self._set_tag(self._tag)

    def _scrape_games(self):
        dlg = ScrapeOptionsDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        prog = ScrapeProgressDialog(self, dlg.count())
        prog.exec()
        if prog.success:
            games = reload_fan_games()                 # read the new csv
            self._links = {gid: l for gid, _t, l, _tg in games}
            self._build_tag_buttons()
            self._set_tag(self._tag)

    def _set_tag(self, tag):
        self._tag = tag
        for t, b in self._tag_buttons.items():
            b.setObjectName("Primary" if t == tag else "")
            b.style().unpolish(b)
            b.style().polish(b)
        self._start_search()

    def showEvent(self, event):
        QWidget.showEvent(self, event)
        if not self._loaded_once:
            self._loaded_once = True
            self._start_search()
        else:
            QTimer.singleShot(100, self._maybe_load_more)

    def _update_banner(self):
        self.banner.hide()

    def _columns(self):
        available = self.width() - 36 - 24 - 12
        return max(1, (available + STORE_GRID_GAP) // (FAN_CARD_W + STORE_GRID_GAP))

    def _set_fan_cover(self, card, path):
        card.cover_path = path or ""
        pix = rounded_cover_pixmap(path, FAN_COVER_W, FAN_COVER_H, 8) if path else None
        if pix:
            card.cover.setText("")
            card.cover.setPixmap(pix)
            card.cover.setStyleSheet("background: transparent; border: none;")
        else:
            card.cover.setText("No\ncover")

    def _load_page(self):
        if self._exhausted:
            return
        self._loading = True
        term = self._term.lower()
        games = [g for g in load_fan_games() if term in g[1].lower() and (not self._tag or self._tag in g[3])]
        start = self._next_start
        chunk = [(gid, title, link) for gid, title, link, _tg in games[start:start + STORE_PAGE_SIZE]]
        self._on_page((self._request_id, start, chunk, len(games), ""))
        if not load_fan_games():
            self._set_status("No fan_games.csv found next to main.py.")

    def _on_page(self, result):
        StorePage._on_page(self, result)
        for card in self._cards:
            if getattr(card, "_fan", False):
                continue
            card._fan = True
            card.setFixedWidth(FAN_CARD_W)
            card.cover.setFixedSize(FAN_COVER_W, FAN_COVER_H)
            card.set_cover = lambda path, c=card: self._set_fan_cover(c, path)
            card.setToolTip(f"{card.title}\nClick for details")
            def press(event, c=card):
                if event.button() == Qt.MouseButton.LeftButton:
                    self.fan_selected.emit(c.appid, c.title, c.cover_path)
            card.mousePressEvent = press

# ---------------------------------------------------------------- fan games in the Library (bottom section, horizontal cards)
def get_fan_library_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "fan_library.json")

def load_fan_library():
    try:
        with open(get_fan_library_path(), encoding="utf-8") as f:
            data = json.load(f)
        return [g for g in data if isinstance(g, dict) and isinstance(g.get("id"), int) and g.get("title")]
    except Exception:
        return []

def save_fan_library(games):
    path = get_fan_library_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(games, f, indent=4)

def _fan_build_library_section(self):
    self.fan_lib_cards = {}
    self.fan_lib_list = []
    self.fan_section = QWidget(self.grid_container)
    lay = QVBoxLayout(self.fan_section)
    lay.setContentsMargins(0, 16, 0, 0)
    lay.setSpacing(12)
    head = QLabel("Fan Games")
    head.setStyleSheet("font-size: 16px; font-weight: bold;")
    lay.addWidget(head)
    holder = QWidget()
    self.fan_grid = QGridLayout(holder)
    self.fan_grid.setContentsMargins(0, 0, 0, 0)
    self.fan_grid.setHorizontalSpacing(24)
    self.fan_grid.setVerticalSpacing(24)
    self.fan_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    lay.addWidget(holder)
    self.fan_section.hide()
    for g in load_fan_library():
        self._fan_create_library_card(g)

def _fan_create_library_card(self, g):
    gid, title_text, link = g["id"], g["title"], g.get("link", "")
    cover_path = g.get("cover", "")
    card = QFrame(self.fan_section)
    card.setObjectName("GameCard")
    card.setFixedWidth(FAN_CARD_W)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(8, 8, 8, 10)
    layout.setSpacing(0)

    cover = QLabel()
    cover.setFixedSize(FAN_COVER_W, FAN_COVER_H)
    cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
    pix = rounded_cover_pixmap(cover_path, FAN_COVER_W, FAN_COVER_H, 8) if cover_path and os.path.exists(cover_path) else None
    if pix:
        cover.setPixmap(pix)
    else:
        cover.setObjectName("CoverPlaceholder")
        cover.setText("No\ncover")
    layout.addWidget(cover)
    layout.addSpacing(10)

    title = QLabel(title_text)
    title.setObjectName("CardTitle")
    title.setWordWrap(True)
    title.setFixedHeight(38)
    title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    layout.addWidget(title)

    status = QLabel("Fan game")
    status.setObjectName("CardStatus")
    layout.addWidget(status)
    layout.addSpacing(12)

    actions = QHBoxLayout()
    actions.setSpacing(6)
    btn_open = QPushButton("Install")
    btn_open.setObjectName("Primary")
    btn_open.setFixedHeight(38)
    btn_open.setCursor(Qt.CursorShape.PointingHandCursor)
    btn_open.clicked.connect(lambda _c=False, i=gid, t=title_text, c=cover_path, u=link: self._fan_install(i, t, c, u))
    card._fan_btn = btn_open
    _fan_update_btn(btn_open, title_text)
    more_btn = QPushButton("\u2022\u2022\u2022")
    more_btn.setObjectName("MoreButton")
    more_btn.setFixedSize(42, 38)
    more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    menu = QMenu(more_btn)
    more_btn.setMenu(menu)
    menu.addAction("View Details").triggered.connect(lambda _c=False, i=gid: self._fan_view(i))
    menu.addAction("Open on Game Jolt").triggered.connect(lambda _c=False, u=link: webbrowser.open(u))
    if sys.platform.startswith("linux"):
        menu.addAction("Switch Compatibility Tool").triggered.connect(lambda _c=False, i=gid: self._fan_compat_dialog(i))
    menu.addAction("Open Game Location").triggered.connect(
        lambda _c=False, i=gid, t=title_text: self._fan_open_location(i, t))
    menu.addAction("Install Common Redist").triggered.connect(
        lambda _c=False, i=gid, t=title_text: install_redist_for_game(
            self, lambda: _fan_game_dir(i, t), t,
            lambda: (_fan_installed_exe(t), load_compat_config().get(f"fan-{i}", ""))))
    menu.addAction("Uninstall").triggered.connect(lambda _c=False, i=gid, t=title_text: self._fan_uninstall(i, t))
    menu.addSeparator()
    menu.addAction("Remove from Library").triggered.connect(lambda _c=False, i=gid: self._fan_remove(i))
    actions.addWidget(btn_open, 1)
    actions.addWidget(more_btn)
    layout.addLayout(actions)

    self.fan_lib_cards[gid] = card
    self.fan_lib_list.append((card, title_text))

def _fan_add(self, gid, title, cover_path):
    if gid in self.fan_lib_cards:
        return
    link, tags = "", ()
    for g in load_fan_games():
        if g[0] == gid:
            link, tags = g[2], g[3]
            break
    saved_cover = ""
    if cover_path and os.path.exists(cover_path):
        try:
            os.makedirs(get_library_cover_dir(), exist_ok=True)
            saved_cover = os.path.join(get_library_cover_dir(), f"fan-{gid}.img")
            shutil.copyfile(cover_path, saved_cover)
        except Exception:
            saved_cover = cover_path
    entry = {"id": gid, "title": title, "link": link, "tags": sorted(tags), "cover": saved_cover}
    save_fan_library([g for g in load_fan_library() if g["id"] != gid] + [entry])
    self._fan_create_library_card(entry)
    self.apply_filter()
    self.fan_detail_page.set_in_library(True)

def _fan_remove(self, gid):
    card = self.fan_lib_cards.pop(gid, None)
    if card is not None:
        self.fan_lib_list = [(c, t) for c, t in self.fan_lib_list if c is not card]
        card.hide()
        card.setParent(None)
        card.deleteLater()
    save_fan_library([g for g in load_fan_library() if g["id"] != gid])
    try:
        os.remove(os.path.join(get_library_cover_dir(), f"fan-{gid}.img"))
    except OSError:
        pass
    if getattr(self, "fan_detail_page", None) is not None and self.fan_detail_page._appid == gid:
        self.fan_detail_page.set_in_library(False)
    self.apply_filter()

def _fan_view(self, gid):
    g = next((x for x in load_fan_library() if x["id"] == gid), None)
    if not g:
        return
    self.fan_detail_page.show_game(gid, g["title"], g.get("cover", ""), g.get("link", ""), tuple(g.get("tags", [])))
    self.fan_detail_page.set_in_library(True)
    btn = self.nav_group.button(self.pages.indexOf(self.fan_page))
    if btn:
        btn.setChecked(True)
    self.pages.setCurrentWidget(self.fan_detail_page)

AdaptiveApp._fan_build_library_section = _fan_build_library_section
AdaptiveApp._fan_create_library_card = _fan_create_library_card
AdaptiveApp._fan_add = _fan_add
AdaptiveApp._fan_remove = _fan_remove
AdaptiveApp._fan_view = _fan_view

_fan_prev_apply_filter = AdaptiveApp.apply_filter

def _fan_apply_filter(self, text=""):
    _fan_prev_apply_filter(self, text)          # lays out the normal (steam / custom / NCZ) cards
    if not hasattr(self, "fan_section"):
        return
    query = (text if isinstance(text, str) else self.search_edit.text()).strip().lower()
    cols = max(1, self.grid_columns())
    while self.fan_grid.count():
        self.fan_grid.takeAt(0)
    if getattr(self, "_lib_tab", "games") != "fan":
        # Games tab: no fan cards
        for card, _t in self.fan_lib_list:
            card.hide()
        self.fan_section.hide()
        n = self.grid.count()
        self.empty_label.setText("No games match your search.")
        self.empty_label.setVisible(n == 0)
        return
    # Fan Games tab: no normal cards
    while self.grid.count():
        self.grid.takeAt(0)
    for card, _t in self.cards:
        card.hide()
    span_w = cols * (COVER_W + 18) + (cols - 1) * 24
    fan_cols = max(1, (span_w + 24) // (FAN_CARD_W + 24))
    shown = 0
    for card, title in self.fan_lib_list:
        if query in title.lower():
            self.fan_grid.addWidget(card, shown // fan_cols, shown % fan_cols)
            card.show()
            shown += 1
        else:
            card.hide()
    self.fan_section.setVisible(shown > 0)
    if shown:
        self.grid.addWidget(self.fan_section, 0, 0, 1, cols)
    self.count_label.setText(f"{shown} fan game{'s' if shown != 1 else ''}")
    self.empty_label.setText("No games match your search." if query else
                             "No fan games yet. Add some from the Fan Games page.")
    self.empty_label.setVisible(shown == 0)

AdaptiveApp.apply_filter = _fan_apply_filter

_fan_prev_init = AdaptiveApp.__init__

def _fan_init(self):
    _fan_prev_init(self)
    self.fan_page = FanGamesPage()
    self.pages.addWidget(self.fan_page)
    self.fan_detail_page = FanDetailPage()
    self.pages.addWidget(self.fan_detail_page)

    def open_fan_game(gid, title, cover):
        link, tags = "", ()
        for g in load_fan_games():
            if g[0] == gid:
                link, tags = g[2], g[3]
                break
        self.fan_detail_page.show_game(gid, title, cover, link, tags)
        self.fan_detail_page.set_in_library(gid in self.fan_lib_cards)
        self.pages.setCurrentWidget(self.fan_detail_page)

    self._fan_build_library_section()
    self.fan_section.layout().itemAt(0).widget().hide()      # the tab already says "Fan Games"
    # Games / Fan Games switch under the Library header
    self._lib_tab = "games"
    tabs = QWidget()
    tabs.setStyleSheet("QPushButton { padding: 6px 18px; border-radius: 8px; } "
                       "QPushButton:checked { background: palette(highlight); color: #ffffff; font-weight: bold; }")
    tl = QHBoxLayout(tabs)
    tl.setContentsMargins(0, 0, 0, 14)
    tl.setSpacing(8)
    group = QButtonGroup(tabs)
    group.setExclusive(True)
    for key, label in (("games", "Games"), ("fan", "Fan Games")):
        b = QPushButton(label)
        b.setCheckable(True)
        b.setChecked(key == "games")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        group.addButton(b)
        b.clicked.connect(lambda _c=False, k=key: (setattr(self, "_lib_tab", k),
                                                   self.apply_filter(self.search_edit.text())))
        tl.addWidget(b)
    tl.addStretch()
    self.library_tabs = tabs
    self.search_edit.parentWidget().layout().insertWidget(2, tabs)
    self.fan_detail_page.add_requested.connect(self._fan_add)
    self.fan_page.fan_selected.connect(open_fan_game)
    self.fan_detail_page.open_cb = open_fan_game
    self.fan_detail_page.back_requested.connect(lambda: self.pages.setCurrentWidget(self.fan_page))
    btn = self.make_nav_button("Fan Games", checkable=True)
    self.nav_group.addButton(btn, self.pages.indexOf(self.fan_page))
    layout = self.nav_group.button(0).parent().layout()
    i = layout.indexOf(self.nav_group.button(4))
    i = i + 1 if i >= 0 else layout.indexOf(self.nav_group.button(1))
    layout.insertWidget(i, btn)
    self.apply_filter()

AdaptiveApp.__init__ = _fan_init


# --- Game Jolt fan games: Playwright finds the real build link, the launcher downloads + extracts it ---
GJ_DOWNLOAD_BTN = re.compile(r"^\s*(download|get\s*game|install)\b", re.IGNORECASE)

GJ_API_JS = r"""async (gid) => {
    const hdr = {'Content-Type': 'application/json'};
    const r = await fetch(`/site-api/web/discover/games/overview/${gid}?ignore`, {credentials: 'include'});
    const j = await r.json();
    const found = [];
    const walk = o => {
        if (Array.isArray(o)) o.forEach(walk);
        else if (o && typeof o === 'object') {
            if (o.id && o.os_windows && (o.type === undefined || o.type === 'downloadable')) found.push(o);
            Object.values(o).forEach(walk);
        }
    };
    walk(j.payload || j);
    // Windows-only builds first, then primary ones
    found.sort((a, b) => (((a.os_linux || a.os_mac) ? 1 : 0) - ((b.os_linux || b.os_mac) ? 1 : 0)) ||
                         ((b.primary ? 1 : 0) - (a.primary ? 1 : 0)));
    for (const b of found) {
        try {
            const d = await fetch(`/site-api/web/discover/games/builds/get-download-url/${b.id}`,
                                  {method: 'POST', credentials: 'include', headers: hdr, body: '{}'});
            const dj = await d.json();
            const u = (dj.payload || dj).downloadUrl;
            if (u) return u;
        } catch (e) {}
    }
    return null;
}"""

# lists every Windows build of the game with its details (for the "pick a build" window)
GJ_BUILDS_JS = r"""async (gid) => {
    const r = await fetch(`/site-api/web/discover/games/overview/${gid}?ignore`, {credentials: 'include'});
    const j = await r.json();
    const found = {}, packages = {}, releases = {};
    const walk = (o, key) => {
        if (Array.isArray(o)) {
            if (key === 'packages') o.forEach(x => { if (x && x.id) packages[x.id] = x; });
            if (key === 'releases') o.forEach(x => { if (x && x.id) releases[x.id] = x; });
            o.forEach(x => walk(x, key));
        } else if (o && typeof o === 'object') {
            if (o.id && o.os_windows && (o.type === undefined || o.type === 'downloadable')) found[o.id] = o;
            Object.entries(o).forEach(([k, v]) => walk(v, k));
        }
    };
    walk(j.payload || j, '');
    const out = Object.values(found).map(b => {
        const pk = packages[b.game_package_id || b.package_id] || {};
        const rl = releases[b.game_release_id || b.release_id] || {};
        const f = b.primary_file || {};
        return {id: b.id, filename: f.filename || b.filename || '', size: f.filesize || b.filesize || 0,
                pkg: pk.title || pk.name || '', desc: pk.description || '', version: rl.version_number || '',
                added: b.modified_on || b.added_on || rl.published_on || 0, primary: !!b.primary,
                win64: !!b.os_windows_64, linux: !!(b.os_linux || b.os_linux_64), mac: !!(b.os_mac || b.os_mac_64)};
    });
    out.sort((a, b) => (((a.linux || a.mac) ? 1 : 0) - ((b.linux || b.mac) ? 1 : 0)) ||
                       ((b.primary ? 1 : 0) - (a.primary ? 1 : 0)));
    return out;
}"""

GJ_URL_JS = r"""async (id) => {
    try {
        const d = await fetch(`/site-api/web/discover/games/builds/get-download-url/${id}`,
                              {method: 'POST', credentials: 'include',
                               headers: {'Content-Type': 'application/json'}, body: '{}'});
        const dj = await d.json();
        return (dj.payload || dj).downloadUrl || null;
    } catch (e) { return null; }
}"""

# finds buttons whose own row mentions windows (and not linux/mac), tags them for Playwright
GJ_TAG_WIN_JS = r"""() => {
    let n = 0;
    document.querySelectorAll('[data-gjwin]').forEach(e => e.removeAttribute('data-gjwin'));
    const els = [...document.querySelectorAll('button, a')].filter(e => /^\s*download\b/i.test(e.innerText || ''));
    for (const el of els) {
        let node = el;
        for (let i = 0; i < 5 && node.parentElement; i++) {
            const p = node.parentElement;
            if (els.filter(e => p.contains(e)).length > 1) break;
            node = p;
        }
        const t = (node.innerText || '').toLowerCase();
        if (t.includes('windows') && !t.includes('linux') && !t.includes('mac')) {
            el.setAttribute('data-gjwin', '1'); n++;
        }
    }
    return n;
}"""

# tags the "Download" button that sits closest to the picked build's title text (data-gjpick); returns 1 if found
GJ_NEAR_JS = r"""(titles) => {
    document.querySelectorAll('[data-gjpick]').forEach(e => e.removeAttribute('data-gjpick'));
    const btns = [...document.querySelectorAll('button, a')]
        .filter(e => /^\s*download\b/i.test(e.innerText || '') && e.offsetParent !== null);
    if (!btns.length) return 0;
    const low = e => (e.innerText || '').toLowerCase().replace(/[’‘`]/g, "'");
    for (const t of titles) {
        const w = (t || '').trim().toLowerCase().replace(/[’‘`]/g, "'");
        if (!w) continue;
        const spots = [...document.querySelectorAll('body *')].filter(el =>
            low(el).includes(w) && ![...el.children].some(c => low(c).includes(w)) && !btns.some(b => b.contains(el)));
        if (!spots.length) continue;
        let best = null, bd = Infinity;
        for (const el of spots) {
            const r = el.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
            for (const b of btns) {
                const q = b.getBoundingClientRect();
                if (q.top < r.top) continue;          // a card's button is below its title, never above it
                const d = Math.hypot(q.left + q.width / 2 - cx, q.top + q.height / 2 - cy);
                if (d < bd) { bd = d; best = b; }
            }
        }
        if (best) { best.setAttribute('data-gjpick', '1'); return 1; }
    }
    return 0;
}"""

# every visible "Download" button with the text of the row it sits in (for matching + the debug log)
GJ_ROWS_JS = r"""() => {
    const all = [...document.querySelectorAll('button, a')]
        .filter(e => /^\s*download\b/i.test(e.innerText || '') && e.offsetParent !== null);
    return all.map(el => {
        let node = el;
        for (let i = 0; i < 6 && node.parentElement; i++) {
            const p = node.parentElement;
            if (all.filter(e => p.contains(e)).length > 1) break;
            node = p;
        }
        return (node.innerText || '').replace(/\s+/g, ' ').trim();
    });
}"""

GJ_TAG_IDX_JS = r"""(idx) => {
    document.querySelectorAll('[data-gjpick]').forEach(e => e.removeAttribute('data-gjpick'));
    const all = [...document.querySelectorAll('button, a')]
        .filter(e => /^\s*download\b/i.test(e.innerText || '') && e.offsetParent !== null);
    if (!all[idx]) return 0;
    all[idx].setAttribute('data-gjpick', '1');
    return 1;
}"""

def _gj_score_rows(rows, pick):
    """Index of the Download row (the whole card the button sits in) that mentions the most details of the picked
    build: name / file name count most, then version, then size."""
    norm = lambda v: str(v).strip().lower().replace("\u2019", "'")
    size = float(pick.get("size") or 0)
    keys = [(pick.get("pkg"), 3), (pick.get("filename"), 3), (pick.get("version"), 2)]
    if size:
        keys += [(f"{size / 1048576:.1f} mb", 1), (f"{size / 1048576:.2f} mb", 1), (f"{size / 1048576:.0f} mb", 1),
                 (f"{size / 1024:.0f} kb", 1), (f"{size / 1073741824:.2f} gb", 1)]
    keys = [(norm(k), w) for k, w in keys if k and str(k).strip()]
    best, best_i = 0, None
    for i, text in enumerate(rows):
        t = norm(text)
        score = sum(w for k, w in keys if k in t)
        if score > best:
            best, best_i = score, i
    return best_i

def _gj_click_picked(pg, pick):
    """Clicks the Download button of the picked build on this page. Returns how it was found, or ''."""
    how = ""
    idx = _gj_score_rows(pg.evaluate(GJ_ROWS_JS), pick)       # the card that holds the build's name / version / size
    if idx is not None and pg.evaluate(GJ_TAG_IDX_JS, idx):
        how = f"card {idx + 1} by name/version/size"
    elif pg.evaluate(GJ_NEAR_JS, [pick.get("pkg"), pick.get("filename")]):
        how = "nearest button below the title text"
    if how:
        pg.locator("[data-gjpick]").first.click(timeout=5000)
    return how

def _gj_debug(msg):
    try:
        with open(os.path.join(_writable_dir(), "gj_build_debug.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

def _gj_find_link(self, game_url, gid=None):
    """Opens the Game Jolt page, opens the builds list, clicks the (Windows) build's Download and copies the link."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright isn't installed. Run: pip install playwright && playwright install chromium")
    with sync_playwright() as p:
        self.status_update.emit("Starting browser...")
        browser = p.chromium.launch(headless=PW_HEADLESS, args=["--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(user_agent=PW_USER_AGENT, accept_downloads=True)
            page = context.new_page()
            got = {}
            page.on("download", lambda d: got.setdefault("d", d))
            context.on("page", lambda pg: pg.on("download", lambda d: got.setdefault("d", d)))
            # skip images/fonts: the page only needs its scripts, and loading art + ads is what made this slow
            context.route("**/*", lambda route: route.abort()
                          if route.request.resource_type in ("image", "font") else route.continue_())
            self.status_update.emit("Opening Game Jolt page...")
            # don't wait for the full "load" event (ads/trackers can take ages), the buttons are polled for below
            page.goto(game_url, timeout=60000, wait_until="domcontentloaded")

            def wait_download(sec):
                end = time.time() + sec
                while "d" not in got and time.time() < end:
                    if self._is_cancelled:
                        raise RuntimeError("CANCELLED")
                    page.wait_for_timeout(300)

            def click_first(loc_list):
                for loc in loc_list:
                    try:
                        if loc.count():
                            loc.first.click(timeout=5000)
                            return True
                    except Exception:
                        continue
                return False

            # 1) ask Game Jolt's own api (from inside the browser) for the Windows build's download url.
            #    Several Windows builds -> the launcher shows a window to pick one.
            pick, builds = None, []
            if gid:
                self.status_update.emit("Looking for the Windows build...")
                try:
                    builds = page.evaluate(GJ_BUILDS_JS, gid) or []
                except Exception:
                    builds = []
                named = [b for b in builds if b.get("pkg") or b.get("filename")]   # only builds that have a real name
                ask = getattr(self, "ask_build", None)
                chosen = getattr(self, "_gj_build_id", None)     # kept, so a re-link (expired url) doesn't ask again
                if len(named) > 1 and ask and chosen is None:
                    self.status_update.emit("Waiting for you to pick a build...")
                    chosen = ask(named)
                    if chosen is None:
                        raise RuntimeError("CANCELLED")
                    self._gj_build_id = chosen
                    self.status_update.emit("Getting the download link...")
                if chosen is not None:
                    # a build was picked: do it like the normal route - click Download and wait for the link on the page
                    pick = next((b for b in builds if b["id"] == chosen), None)
                    self.status_update.emit("Opening download options...")
                else:
                    api_url = None
                    for b in (named or builds):
                        try:
                            api_url = page.evaluate(GJ_URL_JS, b["id"])
                        except Exception:
                            api_url = None
                        if api_url:
                            break
                    if api_url:
                        cookies = "; ".join(f"{c['name']}={c['value']}" for c in context.cookies(api_url))
                        headers = {"User-Agent": PW_USER_AGENT, "Referer": game_url}
                        if cookies:
                            headers["Cookie"] = cookies
                        return api_url, headers

            # 2) fallback: click through the page, Windows build only
            self.status_update.emit("Opening download options...")
            main_btns = [page.get_by_role("button", name=GJ_DOWNLOAD_BTN),
                         page.get_by_role("link", name=GJ_DOWNLOAD_BTN)]
            # The game page can list every package with its own Download button right away. The generic click below would
            # press the FIRST one, so when a build was picked, press that build's button instead.
            clicked_main = False
            if pick:
                for _ in range(40):
                    if self._is_cancelled:
                        raise RuntimeError("CANCELLED")
                    try:
                        how = _gj_click_picked(page, pick)
                    except Exception:
                        how = ""
                    if how:
                        _gj_debug(f"picked build: {pick}")
                        _gj_debug(f"clicked on the game page: {how}")
                        clicked_main = True
                        break
                    page.wait_for_timeout(250)
            for _ in range(0 if clicked_main else 60):  # up to ~15s, checked every 250ms so it reacts as soon as the button shows up
                if self._is_cancelled:
                    raise RuntimeError("CANCELLED")
                if click_first(main_btns):
                    clicked_main = True
                    break
                page.wait_for_timeout(250)
            if not clicked_main:
                raise RuntimeError("Game is unreleased")
            # Be patient: Game Jolt may show a builds list and/or a "download starts after the video" page.
            # Poll for up to 5 minutes; click a Windows build if a list shows up, otherwise just wait.
            self.status_update.emit("Waiting for the download to start (may play a video first)...")
            end, clicked, saw_list, list_since, logged = time.time() + 300, clicked_main and bool(pick), False, 0.0, False
            while "d" not in got and time.time() < end:
                if self._is_cancelled:
                    raise RuntimeError("CANCELLED")
                for pg in list(context.pages):
                    try:
                        if pg.is_closed():
                            continue
                        n = pg.evaluate(GJ_TAG_WIN_JS)
                        if n:
                            saw_list = True
                            if pick and not clicked:
                                # the build the user chose: click the Download button closest to its title text
                                list_since = list_since or time.time()
                                if not logged:
                                    logged = True
                                    _gj_debug(f"picked build: {pick}")
                                    _gj_debug(f"all builds: {builds}")
                                    _gj_debug("download rows on the page: " + " || ".join(pg.evaluate(GJ_ROWS_JS)))
                                how = _gj_click_picked(pg, pick) if time.time() - list_since > 0.5 else ""
                                if how:
                                    _gj_debug(f"clicking: {how}")
                                    self.status_update.emit("Picking the build you chose...")
                                    clicked = True
                                    self.status_update.emit("Waiting for the download to start (may play a video first)...")
                                elif time.time() - list_since > 12:      # nothing matched: first Windows build
                                    _gj_debug("clicking: nothing matched, first Windows button")
                                    pg.locator("[data-gjwin]").first.click(timeout=5000)
                                    clicked = True
                            elif not clicked:
                                self.status_update.emit("Picking the Windows build...")
                                pg.locator("[data-gjwin]").first.click(timeout=5000)
                                clicked = True
                                self.status_update.emit("Waiting for the download to start (may play a video first)...")
                    except Exception:
                        pass
                page.wait_for_timeout(400)
            if "d" not in got:
                raise RuntimeError("The download didn't start.")
            download = got["d"]
            url = download.url
            cookies = "; ".join(f"{c['name']}={c['value']}" for c in context.cookies(url))
            try:
                download.cancel()  # the launcher downloads it itself
            except Exception:
                pass
            headers = {"User-Agent": PW_USER_AGENT, "Referer": game_url}
            if cookies:
                headers["Cookie"] = cookies
            return url, headers
        finally:
            browser.close()


def _gj_name(response, url, title):
    name = ""
    cd = response.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        name = urllib.parse.unquote(m.group(1).strip().strip('"'))
    if not name:
        name = os.path.basename(urllib.parse.unquote(response.geturl().split("?")[0]))
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or sanitize_folder_name(title) + ".bin"

def _gj_stream(self, url, headers, relink=None):
    """Like _fb_stream, but keeps the file's real name/extension (.exe, .zip, .rar, .7z...)."""
    return resumable_download(self, relink or (lambda: (url, headers)),
                              lambda response, u: _gj_name(response, u, self.title), 0, first=(url, headers))

def _gj_install_file(path, game_dir):
    """Installs whatever Game Jolt gave us: archive -> extracted, lone .exe -> copied into the game folder."""
    os.makedirs(game_dir, exist_ok=True)
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"MZ"):  # a Windows executable on its own
        name = os.path.basename(path)
        if not name.lower().endswith(".exe"):
            name = os.path.splitext(name)[0] + ".exe"
        shutil.move(path, os.path.join(game_dir, name))
        return
    if head.startswith(b"7z\xbc\xaf"):
        if _extract_with_tools(path, game_dir, rar=False):
            return
        raise ValueError("This is a .7z archive, but 7-Zip couldn't be found. Install 7-Zip (7-zip.org) and try again.")
    if head.startswith((b"<!DO", b"<htm", b"<HTM")):
        raise ValueError("Game Jolt returned a web page instead of a file (the download link expired?). Try again.")
    extract_archive(path, game_dir)

class GameJoltBuildDialog(QDialog):
    """Pop-up listing the Windows builds a Game Jolt game offers, with their details."""
    def __init__(self, parent, title, builds):
        super().__init__(parent)
        self.setWindowTitle("Choose a build")
        self.setMinimumWidth(500)
        self._builds = builds
        lay = QVBoxLayout(self)
        head = QLabel(f"<b>{html_lib.escape(title)}</b> has {len(builds)} Windows builds.<br>Pick the one you want to download:")
        head.setWordWrap(True)
        lay.addWidget(head)
        self.list = QListWidget()
        self.list.setMinimumHeight(260)
        dim = self.palette().placeholderText().color().name()
        for i, b in enumerate(builds):
            name = b.get("pkg") or b.get("filename") or f"Build {b['id']}"
            bits = []
            if b.get("version"):
                bits.append(f"Version {html_lib.escape(str(b['version']))}")
            if b.get("size"):
                bits.append(_fmt_size(b["size"]))
            bits.append("64-bit" if b.get("win64") else "Windows")
            if b.get("linux") or b.get("mac"):
                bits.append("also " + "/".join(x for x, k in (("Linux", "linux"), ("Mac", "mac")) if b.get(k)))
            if b.get("added"):
                try:
                    bits.append(time.strftime("%b %d, %Y", time.localtime(float(b["added"]) / 1000)))
                except Exception:
                    pass
            lines = [f"<b>{html_lib.escape(str(name))}</b>" + ("  <i>(recommended)</i>" if i == 0 and b.get("primary") else ""),
                     f'<span style="color:{dim}">{" · ".join(bits)}</span>']
            if b.get("filename") and b.get("pkg"):
                lines.append(f'<span style="color:{dim}">{html_lib.escape(str(b["filename"]))}</span>')
            desc = " ".join(fan_clean_text(b.get("desc") or "").split())
            if desc:
                lines.append(html_lib.escape(desc[:160] + ("..." if len(desc) > 160 else "")))
            lab = QLabel("<br>".join(lines))
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.TextFormat.RichText)
            lab.setFixedWidth(450)
            lab.setContentsMargins(8, 6, 8, 6)
            lab.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, b["id"])
            it.setSizeHint(QSize(460, lab.heightForWidth(450) + 4))
            self.list.addItem(it)
            self.list.setItemWidget(it, lab)
        self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _i: self.accept())
        lay.addWidget(self.list)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Download")
        ok.setObjectName("Primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def chosen_id(self):
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

def _gj_show_build_dialog(parent, worker, builds):
    dlg = GameJoltBuildDialog(parent, worker.title, builds)
    ok = dlg.exec() == QDialog.DialogCode.Accepted
    worker.answer_build(dlg.chosen_id() if ok else None)

class GameJoltDownloadWorker(FirebaseDownloadWorker):
    pick_build = pyqtSignal(object)      # list of build dicts -> the GUI thread shows the chooser

    def __init__(self, title, game_url, gid=None, existing_zip_path=None):
        super().__init__(title, game_url, 0)
        self.game_url = game_url
        self.gid = gid
        self.existing_zip_path = existing_zip_path
        self._pick_event = threading.Event()
        self._pick_result = None

    def ask_build(self, builds):
        """Called from the worker thread: blocks until the chooser window answers. Returns a build id or None."""
        self._pick_event.clear()
        self._pick_result = None
        self.pick_build.emit(builds)
        while not self._pick_event.wait(0.3):
            if self._is_cancelled:
                return None
        return self._pick_result

    def answer_build(self, build_id):
        self._pick_result = build_id
        self._pick_event.set()

    def run(self):
        dest_path, fresh = None, False
        try:
            if self.existing_zip_path and os.path.exists(self.existing_zip_path):
                dest_path = self.existing_zip_path   # user chose to reuse the file that's already downloaded
            else:
                url, headers = _gj_find_link(self, self.game_url, self.gid)
                self.download_url = url
                dest_path = _gj_stream(self, url, headers, lambda: _gj_find_link(self, self.game_url, self.gid))
                fresh = True
            if self._is_cancelled:
                if fresh and os.path.exists(dest_path):   # never delete a file the user chose to keep
                    os.remove(dest_path)
                self.failed.emit("CANCELLED")
                return
            self.status_update.emit("Installing...")
            _gj_install_file(dest_path, installed_game_dir(sanitize_folder_name(self.title)))
            self.finished.emit(dest_path)
        except Exception as e:
            if self._is_cancelled or str(e) == "CANCELLED":
                self.failed.emit("CANCELLED")
                return
            self.failed.emit(str(e))

def _fan_install(self, gid, title, cover_path, link):
    exe = _fan_installed_exe(title)
    if exe:  # already installed: this button is "Launch"
        _launch_exe(self, exe, load_compat_config().get(f"fan-{gid}", ""), _d3d_for(f"fan-{gid}"),
                    launch_args_for(f"fan-{gid}"))
        return
    if not link:
        QMessageBox.warning(self, "Install", "This game has no Game Jolt link.")
        return
    cancelled, existing = _ask_existing_archive(self, title)
    if cancelled:
        return
    worker = GameJoltDownloadWorker(title, link, gid, existing)
    worker.pick_build.connect(lambda builds, w=worker: _gj_show_build_dialog(self, w, builds))
    worker.finished.connect(lambda _p, g=gid: self._fan_refresh(g))
    card = DownloadCard(title, link, 0, cover_path or "", self, worker=worker)
    self.downloads_page.add_download_card(card)
    self.nav_group.button(6).setChecked(True)
    self.pages.setCurrentWidget(self.downloads_page)

AdaptiveApp._fan_install = _fan_install

_gj_prev_init = AdaptiveApp.__init__

def _gj_init(self):
    _gj_prev_init(self)
    self.fan_detail_page.install_cb = lambda i, t, c, u: self._fan_install(i, t, c, u)

AdaptiveApp.__init__ = _gj_init



# ---- "use existing file or download again" for steam + Game Jolt installs ----
def find_existing_archive_for_title(title):
    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    nt = norm(title)
    if len(nt) < 3 or not os.path.isdir(DOWNLOAD_DIR):
        return None
    for f in sorted(os.listdir(DOWNLOAD_DIR)):
        if not f.lower().endswith((".zip", ".rar", ".7z")):
            continue
        nf = norm(os.path.splitext(f)[0])
        i = nf.find(nt)
        if i != -1 and not (i + len(nt) < len(nf) and nf[i + len(nt)].isdigit()):  # "portal" must not match "portal2"
            return os.path.join(DOWNLOAD_DIR, f)
    return None

def _ask_existing_archive(parent, title):
    """(cancelled, path_to_reuse_or_None). Asks only when a matching zip/rar is already in the downloads folder."""
    existing = find_existing_archive_for_title(title)
    if not existing:
        return False, None
    dlg = ExistingFileDialog(os.path.basename(existing), parent)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return True, None
    if dlg.choice == "use":
        return False, existing
    try:
        os.remove(existing)
    except Exception:
        pass
    return False, None

# ======================= QoL: pausable downloads, exe icons, desktop shortcuts, fan-game launching =======================
def _wk_get_cancel(self):
    while self.__dict__.get("_paused") and not self.__dict__.get("_cancel_flag"):
        time.sleep(0.2)   # paused: every download loop that checks _is_cancelled just waits here
    return self.__dict__.get("_cancel_flag", False)

def _wk_set_cancel(self, v):
    self.__dict__["_cancel_flag"] = v

def _wk_pause(self):
    self.__dict__["_paused"] = True

def _wk_resume(self):
    self.__dict__["_paused"] = False

for _cls in (FirebaseDownloadWorker, GameDownloadWorker):
    _cls._is_cancelled = property(_wk_get_cancel, _wk_set_cancel)
    _cls.pause = _wk_pause
    _cls.resume = _wk_resume
    _cls._paused = property(lambda self: self.__dict__.get("_paused", False))

_BAD_EXE_WORDS = ("unins", "uninstall", "crash", "redist", "vcredist", "dxsetup", "dxwebsetup", "setup", "updater",
                  "helper", "notification_helper", "dotnet", "oalinst", "directx")

def _find_game_exe(folder):
    """Best guess at the game's .exe inside an install folder (shallow + big wins, installers/crash handlers skipped)."""
    if not folder or not os.path.isdir(folder):
        return None
    best, best_key = None, None
    for root, _dirs, files in os.walk(folder):
        depth = os.path.relpath(root, folder).count(os.sep) if root != folder else 0
        if depth > 3:
            continue
        for f in files:
            if not f.lower().endswith(".exe") or any(w in f.lower() for w in _BAD_EXE_WORDS):
                continue
            full = os.path.join(root, f)
            try:
                key = (-depth, os.path.getsize(full))
            except OSError:
                continue
            if best_key is None or key > best_key:
                best, best_key = full, key
    return best

def _fan_linked_exe(title):
    """The exe you pointed an existing fan game at (Add Existing Fan Game), if it's still there."""
    for g in load_fan_library():
        if g.get("title") == title and g.get("exe_path") and os.path.exists(g["exe_path"]):
            return g["exe_path"]
    return None

def _fan_installed_exe(title):
    return _fan_linked_exe(title) or _find_game_exe(installed_game_dir(sanitize_folder_name(title)))

def _fan_update_btn(btn, title):
    btn.setText("Launch" if _fan_installed_exe(title) else "Install")

def _launch_spec(app, exe, compat_tool=""):
    """(argv, cwd, extra_env) for starting a game exe the same way the launcher does for its other games."""
    cwd = os.path.dirname(exe)
    if sys.platform.startswith("linux"):
        kind = compat_tool_kind(compat_tool)
        if kind == "proton":
            return ([compat_tool, "run", exe], cwd,
                    {"STEAM_COMPAT_DATA_PATH": compat_data_dir_for(exe),
                     "STEAM_COMPAT_CLIENT_INSTALL_PATH": os.path.expanduser("~/.local/share/Steam")})
        if kind == "wine" and exe.lower().endswith(".exe"):
            return ([compat_tool, exe], cwd, {})
        if exe.lower().endswith(".sh"):
            return (["bash", exe], cwd, {})
        if exe.lower().endswith(".py"):
            return (["python3", exe], cwd, {})
        if exe.lower().endswith(".exe"):
            wine = default_wine_path(app)
            if wine:
                return ([wine, exe], cwd, {})
    return ([exe], cwd, {})

def compat_data_dir_for(exe):
    """Proton's STEAM_COMPAT_DATA_PATH for a game. Normally <game folder>_compat_data, but wine refuses a prefix it
    doesn't own (NTFS/exFAT drives usually report another owner), so then it goes in the home folder instead."""
    game_dir = os.path.dirname(exe)
    default = game_dir + "_compat_data"
    try:
        os.makedirs(default, exist_ok=True)
        ok = os.stat(default).st_uid == os.geteuid() and os.access(default, os.W_OK)
        pfx = os.path.join(default, "pfx")
        if ok and os.path.exists(pfx):
            ok = os.stat(pfx).st_uid == os.geteuid()
        if ok:
            return default
    except OSError:
        pass
    import hashlib
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.basename(game_dir)) or "game"
    tag = hashlib.md5(game_dir.encode("utf-8", "replace")).hexdigest()[:8]
    return os.path.expanduser(f"~/.local/share/NCZ_Games_Launcher/compat/{name}_{tag}")

def _clean_proton_env():
    """The environment Proton runs in: the system one (not the launcher's bundled libs / Python env)."""
    env = system_env()
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(k, None)
    return env

def _watch_proton_start(app, proc, log_path):
    """If the game exits within a few seconds of starting, tell the user and point at the log."""
    state = {"n": 0}
    timer = QTimer(app)
    def tick():
        state["n"] += 1
        code = proc.poll()
        if code is None:
            if state["n"] >= 40:        # still running after 20 s: it started fine
                timer.stop()
                timer.deleteLater()
            return
        timer.stop()
        timer.deleteLater()
        if code != 0 or state["n"] <= 10:
            tail = ""
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    tail = "".join(f.readlines()[-12:]).strip()
            except OSError:
                pass
            QMessageBox.warning(app, "Proton",
                                f"The game closed right after starting (exit code {code}).\n\n"
                                f"Full log: {log_path}\n\n{tail}")
    timer.timeout.connect(tick)
    timer.start(500)

def _d3d_for(key):
    """The Direct3D renderer picked for one game ('' = use the launcher setting)."""
    mode = load_compat_config().get("d3d:" + key, "")
    return mode if mode in WINE_D3D_MODES else ""

class LaunchOptionsDialog(QDialog):
    """Per-game command-line arguments that are added every time the game is started."""
    def __init__(self, key, title, parent=None):
        super().__init__(parent)
        self.key = key
        self.setWindowTitle(f"Launch Options - {title}")
        self.setMinimumWidth(440)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        label = QLabel("Arguments passed to the game when it starts:")
        label.setWordWrap(True)
        layout.addWidget(label)

        self.edit = QLineEdit(get_launch_options(key))
        self.edit.setPlaceholderText("")
        self.edit.setClearButtonEnabled(True)
        self.edit.returnPressed.connect(self.save_and_close)
        layout.addWidget(self.edit)

        hint = QLabel("")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_save = QPushButton("Save")
        btn_save.setDefault(True)
        btn_cancel.clicked.connect(self.reject)
        btn_save.clicked.connect(self.save_and_close)
        row.addWidget(btn_cancel)
        row.addWidget(btn_save)
        layout.addLayout(row)

    def save_and_close(self):
        try:
            set_launch_options(self.key, self.edit.text())
        except Exception as e:
            QMessageBox.warning(self, "Launch Options", f"Couldn't save launch options: {e}")
            return
        self.accept()

def _launch_exe(app, exe, compat_tool="", d3d="", args=None):
    if not exe or not os.path.exists(exe):
        QMessageBox.warning(app, "Launch Error", f"Executable not found:\n{exe}")
        return
    args = list(args or [])
    argv, cwd, extra = _launch_spec(app, exe, compat_tool)
    linux = sys.platform.startswith("linux")
    is_exe = exe.lower().endswith(".exe")
    if linux and is_exe and argv[0] == exe:
        QMessageBox.warning(app, "Wine Required",
                            "Wine wasn't found to run this Windows game.\nInstall it, or download a build in the Wine tab.")
        return
    _d3d_override["mode"] = d3d if d3d in WINE_D3D_MODES else ""
    try:
        if extra:  # Proton
            env = _clean_proton_env()
            compat = extra["STEAM_COMPAT_DATA_PATH"]
            os.makedirs(compat, exist_ok=True)
            env.update(extra)
            env.setdefault("PROTON_LOG", "1")            # Proton's own log (steam-*.log) goes next to the prefix
            env.setdefault("PROTON_LOG_DIR", compat)
            apply_proton_d3d(env)
            log_path = os.path.join(compat, "ncz_proton_launch.log")
            with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
                lf.write("Command: " + " ".join(shlex.quote(a) for a in argv + args) + "\n")
                lf.write(f"Working dir: {cwd}\nSTEAM_COMPAT_DATA_PATH={compat}\n\n")
                lf.flush()
                proc = subprocess.Popen(argv + args, cwd=cwd, env=env, stdout=lf, stderr=subprocess.STDOUT)
            _watch_proton_start(app, proc, log_path)
        elif linux and is_exe:  # wine (system or a Wine tab build): applies the chosen D3D renderer
            launch_wine(argv[0], exe, cwd, args)
        else:
            subprocess.Popen(argv + args, cwd=cwd, env=os.environ.copy())
    except Exception as e:
        QMessageBox.warning(app, "Launch Error", f"Couldn't launch the game: {e}")
    finally:
        _d3d_override["mode"] = ""

# ---- icon of an .exe (reads the PE resource table; no extra libraries) ----
def extract_exe_icon_ico(exe_path):
    import struct
    try:
        with open(exe_path, "rb") as f:
            d = f.read()
        if d[:2] != b"MZ":
            return None
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        if d[pe:pe + 4] != b"PE\0\0":
            return None
        nsec = struct.unpack_from("<H", d, pe + 6)[0]
        optsz = struct.unpack_from("<H", d, pe + 20)[0]
        opt = pe + 24
        magic = struct.unpack_from("<H", d, opt)[0]
        dd = opt + (112 if magic == 0x20b else 96)
        res_rva = struct.unpack_from("<II", d, dd + 16)[0]
        if not res_rva:
            return None
        secs = []
        for i in range(nsec):
            vs, va, rs, ro = struct.unpack_from("<IIII", d, opt + optsz + i * 40 + 8)
            secs.append((va, max(vs, rs), ro))

        def off(rva):
            for va, sz, ro in secs:
                if va <= rva < va + sz:
                    return rva - va + ro
            raise ValueError("rva")
        base = off(res_rva)

        def entries(o):
            named, ids = struct.unpack_from("<HH", d, base + o + 12)
            return [struct.unpack_from("<II", d, base + o + 16 + i * 8) for i in range(named + ids)]

        def leaf(o):
            while o & 0x80000000:
                o = entries(o & 0x7fffffff)[0][1]
            rva, size = struct.unpack_from("<II", d, base + o)
            fo = off(rva)
            return d[fo:fo + size]
        root = entries(0)
        groups = [e for e in root if e[0] == 14 and e[1] & 0x80000000]
        icons = [e for e in root if e[0] == 3 and e[1] & 0x80000000]
        if not groups or not icons:
            return None
        icon_map = {nm: ofs for nm, ofs in entries(icons[0][1] & 0x7fffffff)}
        grp = leaf(entries(groups[0][1] & 0x7fffffff)[0][1])
        count = struct.unpack_from("<H", grp, 4)[0]
        imgs = []
        for i in range(count):
            w, h, cc, _r, pl, bc, _br, iid = struct.unpack_from("<BBBBHHIH", grp, 6 + i * 14)
            if iid in icon_map:
                imgs.append((w or 256, h or 256, cc, pl, bc, leaf(icon_map[iid])))
        if not imgs:
            return None
        imgs.sort(key=lambda x: (x[0], x[4]), reverse=True)   # biggest first
        out = struct.pack("<HHH", 0, 1, len(imgs))
        pos, body = 6 + 16 * len(imgs), b""
        for w, h, cc, pl, bc, data in imgs:
            out += struct.pack("<BBBBHHII", w % 256, h % 256, cc, 0, pl, bc, len(data), pos + len(body))
            body += data
        return out + body
    except Exception:
        return None

def _exe_icon_png(exe, name):
    ico = extract_exe_icon_ico(exe)
    if not ico:
        return ""
    try:
        from PyQt6.QtGui import QImage
        img = QImage()
        if not img.loadFromData(ico):
            return ""
        folder = os.path.expanduser("~/.local/share/icons/game-launcher")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, re.sub(r"[^A-Za-z0-9_-]+", "_", name) + ".png")
        return path if img.save(path, "PNG") else ""
    except Exception:
        return ""

def create_game_shortcut(app, title, exe, compat_tool="", fallback_icon="", launch_args=None):
    import shlex
    launch_args = list(launch_args or [])
    if not exe or not os.path.exists(exe):
        QMessageBox.warning(app, "Desktop Shortcut", "Couldn't find the game's executable. Is it installed?")
        return
    from PyQt6.QtCore import QStandardPaths
    desktop = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation) or os.path.expanduser("~/Desktop")
    os.makedirs(desktop, exist_ok=True)
    safe = sanitize_folder_name(title) or "Game"
    try:
        if sys.platform.startswith("win"):
            q = lambda t: t.replace("'", "''")
            lnk = os.path.join(desktop, safe + ".lnk")
            ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');"
                  f"$s.TargetPath='{q(exe)}';$s.WorkingDirectory='{q(os.path.dirname(exe))}';"
                  + (f"$s.Arguments='{q(subprocess.list2cmdline(launch_args))}';" if launch_args else "")
                  + f"$s.IconLocation='{q(exe)},0';$s.Save()")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            target = lnk
        else:
            argv, cwd, extra = _launch_spec(app, exe, compat_tool)
            if extra:
                os.makedirs(extra["STEAM_COMPAT_DATA_PATH"], exist_ok=True)
            script_dir = os.path.expanduser("~/.local/share/game-launcher/shortcuts")
            os.makedirs(script_dir, exist_ok=True)
            script = os.path.join(script_dir, re.sub(r"[^A-Za-z0-9_-]+", "_", safe) + ".sh")
            cmd = " ".join(shlex.quote(a) for a in argv + launch_args)
            if extra:
                cmd = "env " + " ".join(f"{k}={shlex.quote(v)}" for k, v in extra.items()) + " " + cmd
            with open(script, "w", encoding="utf-8") as f:
                f.write(f"#!/bin/sh\ncd {shlex.quote(cwd)} || exit 1\nexec {cmd}\n")
            os.chmod(script, 0o755)
            icon = _exe_icon_png(exe, safe) or (fallback_icon if fallback_icon and os.path.exists(fallback_icon) else "")
            target = os.path.join(desktop, safe + ".desktop")
            with open(target, "w", encoding="utf-8") as f:
                f.write("[Desktop Entry]\nType=Application\n"
                        f"Name={title}\nExec=\"{script}\"\nPath={cwd}\n"
                        + (f"Icon={icon}\n" if icon else "") + "Terminal=false\nCategories=Game;\n")
            os.chmod(target, 0o755)
            try:  # GNOME: mark it trusted so it launches on double-click
                subprocess.run(["gio", "set", target, "metadata::trusted", "true"], capture_output=True, timeout=5)
            except Exception:
                pass
        QMessageBox.information(app, "Desktop Shortcut", f"Shortcut created:\n{target}")
    except Exception as e:
        QMessageBox.warning(app, "Desktop Shortcut", f"Couldn't create the shortcut: {e}")

def _add_shortcut_action(app, card, title, exe_fn, tool_fn=lambda: "", cover="", key=None):
    more = card.findChild(QPushButton, "MoreButton") if card is not None else None
    if not more or not more.menu():
        return
    menu = more.menu()
    if key is not None:
        opts = menu.addAction("Launch Options")
        opts.setToolTip("Command-line arguments passed to the game when it starts")
        opts.triggered.connect(lambda _c=False: LaunchOptionsDialog(key, title, app).exec())
    menu.addAction("Create Desktop Shortcut").triggered.connect(
        lambda _c=False: create_game_shortcut(app, title, exe_fn(), tool_fn(), cover,
                                              launch_args_for(key) if key is not None else None))

# --- hook every kind of library card ---
_sc_prev_game_card = AdaptiveApp.create_game_card
def _sc_create_game_card(self, title_text, image_filename, game_id):
    _sc_prev_game_card(self, title_text, image_filename, game_id)
    if game_id in ("ae", "ncz2"):
        name = AE_GAME_NAME if game_id == "ae" else NCZ2_GAME_NAME
        known = "FNaNCZ AE.exe" if game_id == "ae" else "FNANCZ 2.exe"
        def exe_fn(n=name, k=known):
            d = installed_game_dir(n)
            direct = os.path.join(d, k)
            return direct if os.path.exists(direct) else _find_game_exe(d)
        _add_shortcut_action(self, self.cards[-1][0], title_text, exe_fn, cover=image_filename, key=game_id)
AdaptiveApp.create_game_card = _sc_create_game_card

_sc_prev_steam_card = AdaptiveApp.create_steam_card
def _sc_create_steam_card(self, appid, title_text, cover_path):
    _sc_prev_steam_card(self, appid, title_text, cover_path)
    def exe_fn():
        for g in load_steam_library():
            if g.get("appid") == appid and g.get("exe_path"):
                return g["exe_path"]
        return _find_game_exe(_safe_installed_game_dir(title_text))
    _add_shortcut_action(self, self.steam_cards.get(appid), title_text, exe_fn,
                         lambda: load_compat_config().get(str(appid), ""), cover_path, key=str(appid))
AdaptiveApp.create_steam_card = _sc_create_steam_card

_sc_prev_custom_card = AdaptiveApp.create_custom_card
def _sc_create_custom_card(self, game_id, title_text, exe_path, cover_path, compat_tool):
    _sc_prev_custom_card(self, game_id, title_text, exe_path, cover_path, compat_tool)
    _add_shortcut_action(self, getattr(self, "custom_cards", {}).get(game_id), title_text,
                         lambda: exe_path, lambda: _custom_tool(game_id, compat_tool), cover_path,
                         key=f"custom-{game_id}")
AdaptiveApp.create_custom_card = _sc_create_custom_card

# ---------------------------------------------------------------- Add Existing Fan Game
from PyQt6.QtWidgets import QListWidget as _FanListWidget, QListWidgetItem

class AddExistingFanGameDialog(QDialog):
    """Pick a fan game from the list, then point it at the exe you already have."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Existing Fan Game")
        self.setMinimumSize(460, 500)
        self.result_data = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(10)
        lay.addWidget(QLabel("1. Choose the game"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search fan games")
        self.search.textChanged.connect(self._fill)
        self.games = _FanListWidget()
        lay.addWidget(self.search)
        lay.addWidget(self.games, 1)
        lay.addWidget(QLabel("2. Choose its executable"))
        row = QHBoxLayout()
        self.exe_edit = QLineEdit()
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.exe_edit, 1)
        row.addWidget(browse)
        lay.addLayout(row)
        if sys.platform.startswith("linux"):
            crow = QHBoxLayout()
            crow.addWidget(QLabel("Compatibility Tool"))
            self.compat_combo = QComboBox()
            self.compat_combo.addItem("Default (System Wine / Native)", "")
            for name, path in sorted(get_available_compatibility_tools().items()):
                self.compat_combo.addItem(name, path)
            crow.addWidget(self.compat_combo, 1)
            lay.addLayout(crow)
        else:
            self.compat_combo = None
        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Add to Library")
        ok.setObjectName("Primary")
        ok.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)
        self._fill()

    def _fill(self, _text=""):
        term = self.search.text().strip().lower()
        self.games.clear()
        shown = 0
        for gid, title, link, tags in load_fan_games():
            if term in title.lower():
                it = QListWidgetItem(title)
                it.setData(Qt.ItemDataRole.UserRole, (gid, title, link, tags))
                self.games.addItem(it)
                shown += 1
                if shown >= 80:
                    break

    def _browse(self):
        flt = "Executables (*.exe);;All files (*)" if sys.platform.startswith(("win", "linux")) else "All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Select Executable", os.path.expanduser("~"), flt)
        if path:
            self.exe_edit.setText(path)

    def _accept(self):
        item = self.games.currentItem()
        if item is None:
            QMessageBox.warning(self, "Add Existing Fan Game", "Pick a game from the list first.\n"
                                "Not there? Add it first with \"+ Add Game\" on the Fan Games page.")
            return
        exe = self.exe_edit.text().strip()
        if not exe or not os.path.exists(exe):
            QMessageBox.warning(self, "Add Existing Fan Game", "Please provide a valid, existing executable path.")
            return
        gid, title, link, tags = item.data(Qt.ItemDataRole.UserRole)
        self.result_data = {"id": gid, "title": title, "link": link, "tags": sorted(tags), "exe_path": exe,
                            "compat_tool": self.compat_combo.currentData() if self.compat_combo else ""}
        self.accept()

def _fan_game_dir(gid, title):
    entry = next((g for g in load_fan_library() if g["id"] == gid), None)
    exe = entry.get("exe_path") if entry else ""
    if exe and os.path.exists(exe):
        return os.path.dirname(exe)
    return installed_game_dir(sanitize_folder_name(title))

def _fan_open_location(self, gid, title):
    entry = next((g for g in load_fan_library() if g["id"] == gid), None)
    exe = entry.get("exe_path") if entry else ""
    if exe and os.path.exists(exe):
        folder = os.path.dirname(exe)
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        except Exception as e:
            QMessageBox.warning(self, "Open Game Location", f"Couldn't open the folder: {e}")
        return
    self.open_game_location(sanitize_folder_name(title))

def _fan_add_existing(self):
    dlg = AddExistingFanGameDialog(self)
    if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
        return
    d = dlg.result_data
    gid, title, link, exe = d["id"], d["title"], d["link"], d["exe_path"]
    if d["compat_tool"]:
        cfg = load_compat_config()
        cfg[f"fan-{gid}"] = d["compat_tool"]
        save_compat_config(cfg)
    lib = load_fan_library()
    existing = next((g for g in lib if g["id"] == gid), None)
    if existing is not None:               # already in the library: just link the exe
        existing["exe_path"] = exe
        save_fan_library(lib)
        self._fan_refresh(gid)
        QMessageBox.information(self, "Add Existing Fan Game",
                                f"{title} was already in your library, so its executable was updated.")
        return
    saved_cover = ""
    cached = os.path.join(get_fan_cover_dir(), f"{gid}.img")
    if os.path.exists(cached):
        try:
            os.makedirs(get_library_cover_dir(), exist_ok=True)
            saved_cover = os.path.join(get_library_cover_dir(), f"fan-{gid}.img")
            shutil.copyfile(cached, saved_cover)
        except Exception:
            saved_cover = cached
    entry = {"id": gid, "title": title, "link": link, "tags": d["tags"], "cover": saved_cover, "exe_path": exe}
    save_fan_library(lib + [entry])
    self._fan_create_library_card(entry)
    if self.fan_detail_page._appid == gid:
        self.fan_detail_page.set_in_library(True)
    # show it: switch the library to the Fan Games tab
    self._lib_tab = "fan"
    btns = self.library_tabs.findChildren(QPushButton) if hasattr(self, "library_tabs") else []
    if len(btns) > 1:
        btns[1].setChecked(True)
    self.apply_filter(self.search_edit.text())
    if saved_cover:
        return
    # no cover cached yet: fetch it from Game Jolt in the background
    card = self.fan_lib_cards.get(gid)
    label = card.findChildren(QLabel)[0] if card is not None else None
    worker = TaskWorker(lambda: self.fan_page.loader._fetch(gid, link, "", title))
    if not hasattr(self, "_fan_cover_workers"):
        self._fan_cover_workers = []
    self._fan_cover_workers.append(worker)

    def got(path):
        if worker in self._fan_cover_workers:
            self._fan_cover_workers.remove(worker)
        if not path or not os.path.exists(path):
            return
        try:
            os.makedirs(get_library_cover_dir(), exist_ok=True)
            dest = os.path.join(get_library_cover_dir(), f"fan-{gid}.img")
            shutil.copyfile(path, dest)
            lib2 = load_fan_library()
            for g in lib2:
                if g["id"] == gid:
                    g["cover"] = dest
            save_fan_library(lib2)
            pix = rounded_cover_pixmap(dest, FAN_COVER_W, FAN_COVER_H, 8)
            if label is not None and pix:
                label.setText("")
                label.setPixmap(pix)
                label.setStyleSheet("background: transparent; border: none;")
        except (OSError, RuntimeError):
            pass
    worker.done.connect(got)
    worker.failed.connect(lambda _m: None)
    worker.start()

AdaptiveApp._fan_open_location = _fan_open_location
AdaptiveApp._fan_add_existing = _fan_add_existing

# --- fan games: shortcut, compat tool, uninstall, button state ---
_sc_prev_fan_card = AdaptiveApp._fan_create_library_card
def _sc_fan_card(self, g):
    _sc_prev_fan_card(self, g)
    gid, title = g["id"], g["title"]
    _add_shortcut_action(self, self.fan_lib_cards.get(gid), title, lambda: _fan_installed_exe(title),
                         lambda: load_compat_config().get(f"fan-{gid}", ""), g.get("cover", ""),
                         key=f"fan-{gid}")
AdaptiveApp._fan_create_library_card = _sc_fan_card

def _fan_compat_dialog(self, gid):
    self.switch_compat_tool(f"fan-{gid}")

def _fan_uninstall(self, gid, title):
    lib = load_fan_library()
    entry = next((g for g in lib if g["id"] == gid), None)
    if entry and entry.get("exe_path"):
        if QMessageBox.question(self, "Unlink", f"Unlink {title} from its existing files?\n"
                                "Your game files won't be deleted.") != QMessageBox.StandardButton.Yes:
            return
        entry.pop("exe_path", None)
        save_fan_library(lib)
        self._fan_refresh(gid)
        return
    d = installed_game_dir(sanitize_folder_name(title))
    if os.path.isdir(d):
        if QMessageBox.question(self, "Uninstall", f"Delete the installed files for {title}?") != QMessageBox.StandardButton.Yes:
            return
        shutil.rmtree(d, ignore_errors=True)
    self._fan_refresh(gid)

def _fan_refresh(self, gid):
    card = self.fan_lib_cards.get(gid)
    btn = getattr(card, "_fan_btn", None)
    if btn is not None:
        g = next((x for x in load_fan_library() if x["id"] == gid), None)
        if g:
            _fan_update_btn(btn, g["title"])
    dp = self.fan_detail_page
    if dp._appid == gid:
        _fan_update_btn(dp.steam_btn, dp._title)

AdaptiveApp._fan_compat_dialog = _fan_compat_dialog
AdaptiveApp._fan_uninstall = _fan_uninstall
AdaptiveApp._fan_refresh = _fan_refresh

_fan_prev_show_game = FanDetailPage.show_game
def _fan_show_game(self, appid, title, *a, **k):
    _fan_prev_show_game(self, appid, title, *a, **k)
    _fan_update_btn(self.steam_btn, title)
FanDetailPage.show_game = _fan_show_game

# ---------------------------------------------------------------- drag to reorder the library
# The three NCZ games stay first and can't be moved, and nothing can be dropped in front of them.
from PyQt6.QtCore import QEvent as _QEvent, QMimeData as _QMimeData, QPoint as _QPoint, QRect as _QRect
from PyQt6.QtWidgets import QAbstractButton as _QAB, QGraphicsOpacityEffect as _QGOE

_CARD_MIME = "application/x-ncz-library-card"

def get_library_order_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "library_order.json")

def _load_library_order():
    try:
        with open(get_library_order_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}

class _CardReorder(QObject):
    """Press a library card, drag it onto another card and let go to put it in that spot.
    Mouse-driven (a ghost card follows the cursor), so it doesn't depend on OS drag-and-drop."""
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.press = None          # (card, global pos) while the button is held on a movable card
        self.card = None           # card being dragged
        self.ghost = None          # (label, opacity effect)
        self.cursor_set = False
        QApplication.instance().installEventFilter(self)

    def _lists(self):
        return [self.app.cards, getattr(self.app, "fan_lib_list", [])]

    def _fixed(self):
        return getattr(self.app, "_fixed_cards", set())

    def _rect(self, card):
        return _QRect(card.mapTo(self.app.grid_container, _QPoint(0, 0)), card.size())

    def _card_at(self, pos):
        for lst in self._lists():
            for card, _t in lst:
                try:
                    if card.isVisible() and self._rect(card).contains(pos):
                        return card
                except RuntimeError:
                    continue
        return None

    def _card_for(self, widget):
        """The movable card this widget sits in, or None (buttons and the fixed NCZ cards don't start a drag)."""
        cards = {id(c): c for lst in self._lists() for c, _t in lst}
        w = widget
        while w is not None:
            if isinstance(w, _QAB):
                return None
            if id(w) in cards:
                return None if id(w) in self._fixed() else w
            w = w.parentWidget()
        return None

    def _plan(self, pos):
        """(list, from index, to index) for dropping the dragged card at pos, or None if not allowed."""
        src = self.card
        if src is None or id(src) in self._fixed():
            return None
        for lst in self._lists():
            idx = next((i for i, (c, _t) in enumerate(lst) if c is src), None)
            if idx is None:
                continue
            target = self._card_at(pos)
            if target is not None:
                if target is src or id(target) in self._fixed():
                    return None
                j = next((i for i, (c, _t) in enumerate(lst) if c is target), None)
                return (lst, idx, j) if j is not None else None
            # empty space: only counts as "move to the end" right after / below the last card of this list
            shown = [c for c, _t in lst if c.isVisible()]
            if not shown:
                return None
            r = self._rect(shown[-1])
            fs = getattr(self.app, "fan_section", None)
            if lst is self.app.cards and fs is not None and fs.isVisible() and pos.y() >= fs.geometry().top():
                return None
            if pos.y() > r.bottom() or (pos.y() >= r.top() and pos.x() > r.right()):
                return (lst, idx, len(lst) - 1)
            return None
        return None

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t not in (_QEvent.Type.MouseButtonPress, _QEvent.Type.MouseMove, _QEvent.Type.MouseButtonRelease):
            return False
        if not isinstance(obj, QWidget):
            return False
        try:
            return self._handle(obj, ev, t)
        except RuntimeError:           # a card was deleted in the middle of a drag
            self._cleanup()
            return False

    def _handle(self, obj, ev, t):
        gpos = ev.globalPosition().toPoint()
        if t == _QEvent.Type.MouseButtonPress:
            if ev.button() == Qt.MouseButton.LeftButton and self.card is None and self.press is None:
                card = self._card_for(obj)
                if card is not None:
                    self.press = (card, gpos)
            return False
        if t == _QEvent.Type.MouseMove:
            if self.card is None:
                if self.press is None:
                    return False
                if not (ev.buttons() & Qt.MouseButton.LeftButton):
                    self.press = None
                    return False
                if (gpos - self.press[1]).manhattanLength() < QApplication.startDragDistance():
                    return False
                self._begin(self.press[0])
            self._track(gpos)
            return True
        # button released
        if self.card is not None:
            plan = self._plan(self.app.grid_container.mapFromGlobal(gpos))
            self._cleanup()
            if plan:
                lst, i, j = plan
                lst.insert(j, lst.pop(i))
                self.app.apply_filter(self.app.search_edit.text())
                self.save()
            return True
        self.press = None
        return False

    def _begin(self, card):
        self.card = card
        self.press = None
        pm = card.grab().scaledToWidth(130, Qt.TransformationMode.SmoothTransformation)
        ghost = QLabel(self.app)
        ghost.setPixmap(pm)
        ghost.setFixedSize(pm.size())
        ghost.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        eff = _QGOE(ghost)
        ghost.setGraphicsEffect(eff)
        ghost.show()
        ghost.raise_()
        self.ghost = (ghost, eff)
        dim = _QGOE(card)
        dim.setOpacity(0.35)
        card.setGraphicsEffect(dim)
        QApplication.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)
        self.cursor_set = True

    def _track(self, gpos):
        ghost, eff = self.ghost
        p = self.app.mapFromGlobal(gpos)
        ghost.move(p.x() - ghost.width() // 2, p.y() - ghost.height() // 2)
        pos = self.app.grid_container.mapFromGlobal(gpos)
        self._autoscroll(pos)
        eff.setOpacity(0.95 if self._plan(pos) else 0.5)

    def _cleanup(self):
        if self.ghost is not None:
            self.ghost[0].deleteLater()
            self.ghost = None
        if self.cursor_set:
            QApplication.restoreOverrideCursor()
            self.cursor_set = False
        if self.card is not None:
            try:
                self.card.setGraphicsEffect(None)
            except RuntimeError:
                pass
        self.card = None
        self.press = None

    def _autoscroll(self, pos):
        view = self.app.grid_container.parentWidget()
        scroll = view.parentWidget() if view is not None else None
        bar = scroll.verticalScrollBar() if hasattr(scroll, "verticalScrollBar") else None
        if bar is None:
            return
        y = pos.y() - bar.value()          # position inside the visible area
        if y < 50:
            bar.setValue(bar.value() - 24)
        elif y > view.height() - 50:
            bar.setValue(bar.value() + 24)

    def save(self):
        data = {"main": [t for c, t in self.app.cards if id(c) not in self._fixed()],
                "fan": [t for c, t in getattr(self.app, "fan_lib_list", [])]}
        try:
            path = get_library_order_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass


def _apply_saved_order(lst, saved, keep_first=0):
    """Re-sorts lst in place by saved title order; unknown titles keep their place at the end."""
    rank = {t: i for i, t in enumerate(saved)}
    head, tail = lst[:keep_first], lst[keep_first:]
    tail.sort(key=lambda it: rank.get(it[1], len(rank)))      # stable
    lst[:] = head + tail


_reorder_prev_init = AdaptiveApp.__init__

def _reorder_init(self):
    _reorder_prev_init(self)
    # the three NCZ games are always the first three cards
    self._fixed_cards = {id(c) for c, _t in self.cards[:3]}
    saved = _load_library_order()
    _apply_saved_order(self.cards, saved.get("main", []), keep_first=3)
    _apply_saved_order(getattr(self, "fan_lib_list", []), saved.get("fan", []))
    self._card_reorder = _CardReorder(self)
    self.apply_filter()

AdaptiveApp.__init__ = _reorder_init

 
# ================================================================ Auto-Add Desktop Games
# "+" menu -> "Auto-Add Desktop Games": scans the Desktop for game shortcuts / exes / folders, matches them
# against Steam to get covers (SteamGridDB first if you set a key, Steam's own art otherwise, then the exe's
# own icon as a last resort), lets you untick anything that isn't a game, and adds the rest to the library.
from PyQt6.QtCore import QFileInfo as _DeskFileInfo, QStandardPaths as _DeskStdPaths
from PyQt6.QtGui import QTextOption as _DeskTextOption
from PyQt6.QtWidgets import (QFileIconProvider as _DeskIconProvider, QListWidget as _DeskList,
                             QListWidgetItem as _DeskItem)

_DESK_GAME_PATHS = (
    "/steamapps/common/", "/epic games/", "/gog games/", "/gog galaxy/games/", "/riot games/",
    "/ubisoft game launcher/games/", "/electronic arts/", "/ea games/", "/origin games/", "/xboxgames/",
    "/rockstar games/", "/games/", "/itch/apps/", "/battle.net/",
)
_DESK_EXE_IGNORE = re.compile(
    r"(unins|uninst|setup|install|crash|report|redist|vcredist|vc_redist|dxsetup|dotnet|directx|"
    r"helper|updater|cefsubprocess|notification_helper|easyanticheat|battleye|anticheat)", re.I)
_DESK_LAUNCH_EXTS = (".exe", ".bat", ".cmd")
_DESK_URL_RE = re.compile(r"steam://(?:rungameid|run)/(\d+)", re.I)

def _desk_clean_title(name):
    name = re.sub(r"\s*[-\u2013]\s*shortcut$", "", name, flags=re.I)
    name = re.sub(r"\s*\(\d+\)$", "", name)
    name = name.replace("_", " ").replace("\u2122", "").replace("\u00ae", "")
    return re.sub(r"\s+", " ", name).strip()

def _desk_looks_like_game_path(path):
    p = "/" + str(path).replace("\\", "/").lower().lstrip("/")
    return any(m in p for m in _DESK_GAME_PATHS)

def _desk_norm_path(path):
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return str(path)

def _desktop_dirs():
    """Every Desktop folder that exists (user, OneDrive-redirected, public). Call from the GUI thread."""
    dirs = []
    try:
        dirs += _DeskStdPaths.standardLocations(_DeskStdPaths.StandardLocation.DesktopLocation)
    except Exception:
        pass
    if sys.platform.startswith("win"):
        for root in (os.environ.get("USERPROFILE"), os.environ.get("PUBLIC")):
            if root:
                dirs.append(os.path.join(root, "Desktop"))
        for var in ("OneDrive", "OneDriveConsumer"):
            if os.environ.get(var):
                dirs.append(os.path.join(os.environ[var], "Desktop"))
    else:
        found = _linux_desktop_dir()
        if found:
            dirs.append(found)
        dirs.append(os.path.expanduser("~/Desktop"))
    out, seen = [], set()
    for d in dirs:
        try:
            key = os.path.normcase(os.path.realpath(d))
        except Exception:
            continue
        if os.path.isdir(d) and key not in seen:
            seen.add(key)
            out.append(d)
    return out

def _desk_parse_lnk(path):
    """Reads a Windows .lnk file directly (MS-SHLLINK): (target, arguments, working dir), or None if unreadable.
    Target may be '' for shortcuts that only point at a shell item (Store apps etc.)."""
    import struct
    try:
        with open(path, "rb") as f:
            d = f.read()
        if len(d) < 76 or struct.unpack_from("<I", d, 0)[0] != 0x4C:
            return None
        flags = struct.unpack_from("<I", d, 0x14)[0]
        uni = bool(flags & 0x80)
        ansi = "mbcs" if sys.platform.startswith("win") else "cp1252"
        pos, target = 76, ""
        if flags & 0x01:                                   # LinkTargetIDList
            pos += 2 + struct.unpack_from("<H", d, pos)[0]
        if flags & 0x02:                                   # LinkInfo
            base = pos
            size, hdr, li_flags = struct.unpack_from("<III", d, base)
            if li_flags & 0x01:
                def cstr(off, wide):
                    if wide:
                        end = base + off
                        while end + 1 < len(d) and d[end:end + 2] != b"\x00\x00":
                            end += 2
                        return d[base + off:end].decode("utf-16le", "replace")
                    end = d.index(b"\x00", base + off)
                    return d[base + off:end].decode(ansi, "replace")
                lb_off, = struct.unpack_from("<I", d, base + 0x10)
                cs_off, = struct.unpack_from("<I", d, base + 0x18)
                if hdr >= 0x24:
                    lbu, csu = struct.unpack_from("<II", d, base + 0x1C)
                    local = cstr(lbu, True) if lbu else cstr(lb_off, False)
                    suffix = cstr(csu, True) if csu else (cstr(cs_off, False) if cs_off else "")
                else:
                    local = cstr(lb_off, False)
                    suffix = cstr(cs_off, False) if cs_off else ""
                target = os.path.join(local, suffix) if suffix else local
            pos = base + size
        strings = {}
        for bit, key in ((0x04, "name"), (0x08, "rel"), (0x10, "work"), (0x20, "args"), (0x40, "icon")):
            if flags & bit:
                n = struct.unpack_from("<H", d, pos)[0]
                pos += 2
                if uni:
                    strings[key] = d[pos:pos + n * 2].decode("utf-16le", "replace")
                    pos += n * 2
                else:
                    strings[key] = d[pos:pos + n].decode(ansi, "replace")
                    pos += n
        while not target and pos + 8 <= len(d):            # extra data: environment-variable target block
            bsize, sig = struct.unpack_from("<II", d, pos)
            if bsize < 4:
                break
            if sig == 0xA0000001 and bsize >= 0x314:
                raw = d[pos + 268:pos + 268 + 520].decode("utf-16le", "replace")
                target = raw.split("\x00")[0]
            pos += bsize
        if not target and strings.get("rel"):
            target = os.path.normpath(os.path.join(os.path.dirname(path), strings["rel"]))
            if not os.path.exists(target):
                target = ""
        target = os.path.expandvars(target)
        work = os.path.expandvars(strings.get("work", ""))
        return target, strings.get("args", ""), work
    except Exception:
        return None


def _resolve_lnks(paths):
    """{lnk path: (target, arguments, working dir)}. Built-in parser first; PowerShell for any it couldn't resolve."""
    out = {}
    for p in paths:
        info = _desk_parse_lnk(p)
        if info:
            out[p] = info
    missing = [p for p in paths if not (out.get(p) or ("",))[0]]
    out.update(_resolve_lnks_powershell(missing))
    return out

def _resolve_lnks_powershell(paths):
    """Same thing through one PowerShell call (Windows only)."""
    if not paths or not sys.platform.startswith("win"):
        return {}
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    data = []
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(paths, f)
        script = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            f"$paths=@(Get-Content -Raw -Encoding UTF8 -LiteralPath {_ps_quote(tmp)}|ConvertFrom-Json);"
            "$sh=New-Object -ComObject WScript.Shell;$out=@();"
            "foreach($p in $paths){try{$s=$sh.CreateShortcut($p);"
            "$out+=[pscustomobject]@{p=[string]$p;t=[string]$s.TargetPath;a=[string]$s.Arguments;"
            "w=[string]$s.WorkingDirectory}}catch{}};"
            "ConvertTo-Json -InputObject @($out) -Compress")
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, timeout=60, creationflags=0x08000000)
        text = res.stdout.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
        data = json.loads(text) if text else []
    except Exception:
        data = []
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if isinstance(data, dict):
        data = [data]
    return {d["p"]: (d.get("t") or "", d.get("a") or "", d.get("w") or "")
            for d in data if isinstance(d, dict) and d.get("p")}

def _desk_split_args(text):
    text = (text or "").strip()
    if not text:
        return []
    try:
        return [t.strip('"') for t in shlex.split(text, posix=False)]
    except ValueError:
        return [text]

def _desk_read_ini(path):
    """Key/value pairs of a .url / .desktop file (first occurrence of each key wins)."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith(("#", ";", "[")):
                    k, v = line.split("=", 1)
                    out.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return out

def _desk_best_exe(folder, title):
    """Most likely game exe inside a folder: best name match, then biggest file. None if nothing fits."""
    tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
    want = _norm_title(title)
    best, best_key = None, None
    base_depth = folder.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(folder):
        if root.count(os.sep) - base_depth >= 3:
            dirs[:] = []
        for f in files:
            if not f.lower().endswith(".exe") or _DESK_EXE_IGNORE.search(f):
                continue
            stem = os.path.splitext(f)[0].lower()
            score = len(tokens & set(re.findall(r"[a-z0-9]+", stem)))
            n = _norm_title(stem)
            if n and (n in want or want in n):
                score += 5
            full = os.path.join(root, f)
            try:
                key = (score, os.path.getsize(full))
            except OSError:
                continue
            if best_key is None or key > best_key:
                best, best_key = full, key
    return best

def _desk_candidate_from(path, lnk_info):
    """One Desktop entry -> candidate dict, or None when it isn't something we can launch."""
    name = os.path.basename(path)
    low = name.lower()
    stem = os.path.splitext(name)[0] if os.path.isfile(path) else name
    title = _desk_clean_title(stem)
    if not title:
        return None
    cand = dict(title=title, exe_path="", launch_url="", launch_file="", args="", launch_args=[], launch_cmd=[], workdir="",
                appid=0, img_base="", likely=False, source=path)

    if low.endswith(".lnk"):
        target, args, workdir = lnk_info.get(path, ("", "", ""))
        tlow = os.path.basename(target).lower()
        m = _DESK_URL_RE.search(args) or re.search(r"-applaunch\s+(\d+)", args)
        if tlow == "steam.exe" and m:
            cand.update(appid=int(m.group(1)), launch_url=f"steam://rungameid/{m.group(1)}",
                        exe_path=path, likely=True)
            return cand
        exe = ""
        if target and os.path.isdir(target):
            exe = _desk_best_exe(target, title) or ""
        elif target and os.path.isfile(target):
            exe = target
        # The shortcut itself is what gets launched (keeps its arguments, run-as-admin and Store-app targets);
        # the exe is only used for the icon and "Open Game Location". Unresolvable ones still work this way.
        cand.update(exe_path=exe or path, launch_file=path, args=args.strip(),
                    workdir=workdir if workdir and os.path.isdir(workdir) else "")
        cand["likely"] = bool(exe) and _desk_looks_like_game_path(exe)
        return cand

    if low.endswith(".url"):
        info = _desk_read_ini(path)
        url = info.get("URL", "")
        m = _DESK_URL_RE.search(url)
        if m:
            cand.update(appid=int(m.group(1)), launch_url=f"steam://rungameid/{m.group(1)}",
                        exe_path=path, likely=True)
            return cand
        if url and not url.lower().startswith(("http://", "https://", "file:")) and "://" in url:
            # Epic / GOG / Ubisoft ... protocol shortcuts: launched through the OS handler
            cand.update(launch_url=url, exe_path=path,
                        likely=any(k in url.lower() for k in ("epicgames", "gog", "uplay", "ubisoft")))
            return cand
        return None

    if low.endswith(".desktop"):
        info = _desk_read_ini(path)
        if info.get("Type", "Application") != "Application" or info.get("Hidden", "").lower() == "true":
            return None
        title = _desk_clean_title(info.get("Name") or title) or title
        exec_line = re.sub(r"%[fFuUdDnNickvm]", "", info.get("Exec", "")).strip()
        if not exec_line:
            return None
        cand["title"] = title
        m = _DESK_URL_RE.search(exec_line)
        is_game = "game" in info.get("Categories", "").lower()
        if m:
            cand.update(appid=int(m.group(1)), launch_url=f"steam://rungameid/{m.group(1)}",
                        exe_path=path, likely=True)
            return cand
        try:
            cmd = shlex.split(exec_line)
        except ValueError:
            return None
        cand.update(launch_cmd=cmd, exe_path=path, likely=is_game)
        return cand

    if os.path.isfile(path) and low.endswith(".exe"):
        cand.update(exe_path=path, likely=_desk_looks_like_game_path(path))
        return cand

    if os.path.isdir(path):
        exe = _desk_best_exe(path, title)
        if not exe:
            return None
        cand.update(exe_path=exe, workdir=os.path.dirname(exe))
        return cand
    return None

def _desk_steam_lookup(title):
    """(appid, title, image base) for the Steam game whose name matches this shortcut, else None.
    Exact name first, then a close one (one name contains the other, or all its words appear in the Steam title)."""
    games, _total = fetch_steam_games(title, 0, 8)
    want = _norm_title(title)
    for appid, steam_title, base in games:
        if _norm_title(steam_title) == want:
            return appid, steam_title, base
    tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
    for appid, steam_title, base in games:
        n = _norm_title(steam_title)
        if want and n and (want in n or n in want) and min(len(n), len(want)) / max(len(n), len(want)) >= 0.4:
            return appid, steam_title, base
        if len(want) >= 4 and tokens and tokens <= set(re.findall(r"[a-z0-9]+", steam_title.lower())):
            return appid, steam_title, base      # e.g. "FC 26" -> "EA SPORTS FC 26"
    return None

def scan_desktop_games(dirs, known_titles, known_exes, known_urls, known_appids, steam_lookup=_desk_steam_lookup):
    """Background-thread scan. Returns candidate dicts for desktop entries that look launchable and aren't
    in the library yet. Entries whose name matches a Steam game come back with appid set (used for Steam covers); all of them are added by default."""
    entries = []
    for d in dirs:
        try:
            entries += [os.path.join(d, n) for n in sorted(os.listdir(d)) if not n.startswith(".")]
        except OSError:
            continue
    lnks = _resolve_lnks([p for p in entries if p.lower().endswith(".lnk")])
    me = _desk_norm_path(sys.executable)
    found, seen_keys = [], set()
    for p in entries:
        try:
            cand = _desk_candidate_from(p, lnks)
        except Exception:
            cand = None
        if not cand:
            continue
        exe_key = _desk_norm_path(cand["exe_path"]) if cand["exe_path"] else ""
        if exe_key == me:
            continue
        # An exe only identifies a game when the shortcut passes no arguments (several games can share one launcher exe).
        ident_exe = exe_key if (cand["exe_path"] and not cand["launch_url"] and not cand["args"]) else ""
        keys = {k for k in (ident_exe, cand["launch_url"], _norm_title(cand["title"])) if k}
        if keys & seen_keys:
            continue
        seen_keys |= keys
        if (_norm_title(cand["title"]) in known_titles or (ident_exe and ident_exe in known_exes)
                or (cand["launch_url"] and cand["launch_url"] in known_urls)
                or (cand["appid"] and cand["appid"] in known_appids)):
            continue
        found.append(cand)
    found = found[:80]

    def match(cand):
        if cand["appid"]:
            return
        try:
            hit = steam_lookup(cand["title"])
        except Exception:
            hit = None
        if hit:
            cand.update(appid=hit[0], img_base=hit[2], likely=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(match, found))
    found.sort(key=lambda c: (not c["likely"], c["title"].lower()))
    return found

# ---- Qt part
def _desk_icon_cover(icon_source, title, out_path):
    """Last-resort cover: the exe's own icon centred on a dark 2:3 card with the title under it."""
    try:
        pm = _DeskIconProvider().icon(_DeskFileInfo(icon_source)).pixmap(256, 256)
        if pm.isNull():
            return ""
        canvas = QPixmap(600, 900)
        canvas.fill(QColor("#202024"))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scaled = pm.scaled(340, 340, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap((600 - scaled.width()) // 2, 190, scaled)
        font = QFont()
        font.setPointSize(30)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#f5f5f7"))
        opt = _DeskTextOption(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        opt.setWrapMode(_DeskTextOption.WrapMode.WordWrap)
        painter.drawText(QRectF(40, 580, 520, 260), title, opt)
        painter.end()
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        return out_path if canvas.save(out_path, "PNG") else ""
    except Exception:
        return ""

def _desk_sgdb_search(title, key):
    """Search SteamGridDB by the shortcut's name; take the exact match, else its top result, then that game's
    best portrait cover (image bytes or None)."""
    found = _sgdb_get_json("/search/autocomplete/" + urllib.parse.quote(title, safe=""), key)
    games = [g for g in (found or {}).get("data") or [] if isinstance(g, dict) and g.get("id")]
    if not games:
        return None
    want = _norm_title(title)
    pick = next((g for g in games if _norm_title(g.get("name")) == want), games[0])
    return _first_grid_image(_sgdb_get_json(
        f"/grids/game/{pick['id']}?dimensions={SGDB_PORTRAIT_SIZES}&types=static", key))

def _desk_find_cover(cand, key, loader):
    """Network part of the cover lookup (runs on a worker thread). Returns a file path or ''.
    Matched on Steam: SteamGridDB by Steam id, else Steam's CDN art (loader._fetch does both).
    Then, with an API key, a SteamGridDB search by name. Without a key a non-Steam game gets its exe icon."""
    if cand.get("appid"):
        try:
            path = loader._fetch(cand["appid"], cand.get("img_base", ""), key, cand["title"])
            if path:
                return path
        except Exception:
            pass
    if key:
        try:
            data = _desk_sgdb_search(cand["title"], key)
            if data:
                out = os.path.join(get_library_cover_dir(), f"desk-{_cover_key(cand['title'])}.img")
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as f:
                    f.write(data)
                return out
        except Exception:
            pass
    return ""

class DesktopGamesDialog(QDialog):
    cover_ready = pyqtSignal(int, str)

    def __init__(self, cands, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Auto-Add Desktop Games")
        self.resize(560, 640)
        self.cands = cands
        self.chosen = []
        self._covers = {}
        self._pending = len(cands)
        self._key = get_steamgriddb_key()
        self._loader = CoverLoader()
        self._loader.set_key(self._key)
        self._pool = ThreadPoolExecutor(max_workers=4)
        self.cover_ready.connect(self._on_cover)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(10)
        matched = sum(1 for c in cands if c["appid"])
        head = QLabel(f"Found {len(cands)} game{'s' if len(cands) != 1 else ''} on your desktop "
                      f"({matched} matched on Steam, so they get Steam covers). Everything is ticked, "
                      f"so untick anything that isn't a game.")
        head.setWordWrap(True)
        lay.addWidget(head)

        self.list = _DeskList()
        self.list.setIconSize(QSize(60, 90))
        self.list.setSpacing(2)
        for i, c in enumerate(cands):
            if c["appid"]:
                sub = "Matched on Steam"
            else:
                sub = "Custom game"
            item = _DeskItem(f"{c['title']}\n{sub}  \u2022  {os.path.basename(c['source'])}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setSizeHint(QSize(0, 100))
            self.list.addItem(item)
        self.list.itemChanged.connect(lambda _it: self._refresh_buttons())
        lay.addWidget(self.list, 1)

        self.status = QLabel("")
        self.status.setObjectName("RowDesc")
        lay.addWidget(self.status)

        row = QHBoxLayout()
        all_btn = QPushButton("Select All")
        none_btn = QPushButton("Select None")
        all_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        none_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.add_btn = QPushButton("Add Selected")
        self.add_btn.setObjectName("Primary")
        self.add_btn.clicked.connect(self._accept)
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        row.addStretch()
        row.addWidget(cancel)
        row.addWidget(self.add_btn)
        lay.addLayout(row)

        self._refresh_buttons()
        for i, c in enumerate(cands):
            self._pool.submit(self._work, i, c)

    def _work(self, i, cand):
        try:
            path = _desk_find_cover(cand, self._key, self._loader)
        except Exception:
            path = ""
        try:
            self.cover_ready.emit(i, path)
        except RuntimeError:
            pass  # dialog already closed

    def _on_cover(self, i, path):
        cand = self.cands[i]
        if not path:
            source = cand["exe_path"] if os.path.exists(cand["exe_path"]) else cand["source"]
            out = os.path.join(get_library_cover_dir(), f"desk-{_cover_key(cand['title'])}.png")
            path = _desk_icon_cover(source, cand["title"], out)
        self._covers[i] = path
        item = self.list.item(i)
        pix = rounded_cover_pixmap(path, 60, 90, 6) if path else None
        if item is not None and pix:
            item.setIcon(QIcon(pix))
        self._pending -= 1
        self._refresh_buttons()

    def _checked(self):
        return [self.list.item(r).data(Qt.ItemDataRole.UserRole) for r in range(self.list.count())
                if self.list.item(r).checkState() == Qt.CheckState.Checked]

    def _set_all(self, state):
        self.list.blockSignals(True)
        for r in range(self.list.count()):
            self.list.item(r).setCheckState(state)
        self.list.blockSignals(False)
        self._refresh_buttons()

    def _refresh_buttons(self):
        n = len(self._checked())
        self.add_btn.setText(f"Add Selected ({n})")
        loading = self._pending > 0
        self.add_btn.setEnabled(n > 0 and not loading)
        done = len(self.cands) - self._pending
        self.status.setText(f"Loading covers... {done}/{len(self.cands)}" if loading else "Covers ready.")

    def _accept(self):
        self.chosen = [dict(self.cands[i], cover_path=self._covers.get(i, "")) for i in self._checked()]
        self.accept()

    def done(self, result):
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._pool.shutdown(wait=False)
        self._loader.shutdown()
        super().done(result)

def _desk_known(self):
    """What the library already has, so nothing gets added twice."""
    titles = {_norm_title(t) for _c, t in self.cards}
    exes, urls, appids = set(), set(), set()
    for g in load_custom_games():
        titles.add(_norm_title(g.get("title")))
        if g.get("exe_path"):
            exes.add(_desk_norm_path(g["exe_path"]))
        if g.get("launch_url"):
            urls.add(g["launch_url"])
    for g in load_steam_library():
        titles.add(_norm_title(g.get("title")))
        appids.add(g.get("appid"))
        if g.get("exe_path"):
            exes.add(_desk_norm_path(g["exe_path"]))
    try:
        for g in load_fan_library():
            if g.get("exe_path"):
                exes.add(_desk_norm_path(g["exe_path"]))
    except Exception:
        pass
    titles.discard("")
    return titles, exes, urls, appids

def _auto_add_desktop_games(self):
    dirs = _desktop_dirs()
    if not dirs:
        QMessageBox.information(self, "Auto-Add Desktop Games", "Couldn't find your Desktop folder.")
        return
    titles, exes, urls, appids = _desk_known(self)
    ok, cands, error = BusyDialog(
        "Scanning your desktop for games...",
        lambda: scan_desktop_games(dirs, titles, exes, urls, appids), self).run()
    if not ok:
        QMessageBox.warning(self, "Auto-Add Desktop Games", f"The scan failed:\n\n{error}")
        return
    if not cands:
        QMessageBox.information(self, "Auto-Add Desktop Games",
                                "No new games found on your desktop.\n\nShortcuts (.lnk / .url / .desktop), "
                                ".exe files and game folders are checked. Games already in your library are skipped.")
        return
    dlg = DesktopGamesDialog(cands, self)
    if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.chosen:
        return

    games = load_custom_games()
    added = 0
    stamp = int(time.time() * 1000)
    for n, c in enumerate(dlg.chosen):
        gid = f"custom_{stamp}_{n}"
        cover = c.get("cover_path") or ""
        if cover and os.path.exists(cover):
            try:
                os.makedirs(get_library_cover_dir(), exist_ok=True)
                dest = os.path.join(get_library_cover_dir(), f"{gid}.img")
                shutil.copyfile(cover, dest)
                cover = dest
            except Exception:
                pass
        entry = {"id": gid, "title": c["title"], "exe_path": c["exe_path"], "cover_path": cover, "compat_tool": ""}
        if c.get("launch_file"):
            try:
                folder = os.path.join(os.path.dirname(get_launcher_settings_path()), "desktop_shortcuts")
                os.makedirs(folder, exist_ok=True)
                keep = os.path.join(folder, f"{gid}.lnk")
                shutil.copyfile(c["launch_file"], keep)
                c["launch_file"] = keep
            except Exception:
                pass
        for k in ("launch_url", "launch_file", "launch_args", "launch_cmd", "workdir"):
            if c.get(k):
                entry[k] = c[k]
        games.append(entry)
        save_custom_games(games)  # saved per game so a crash half way doesn't lose the earlier ones
        self.create_custom_card(gid, entry["title"], entry["exe_path"], cover, "")
        added += 1
    self.apply_filter()
    QMessageBox.information(self, "Auto-Add Desktop Games",
                            f"Added {added} game{'s' if added != 1 else ''} to your library.")

AdaptiveApp.auto_add_desktop_games = _auto_add_desktop_games

def _desk_launch(win, entry):
    """Launch for entries that aren't a plain exe: steam:// / epic:// links, shortcut arguments, .desktop Exec."""
    try:
        if entry.get("launch_url"):
            QDesktopServices.openUrl(QUrl(entry["launch_url"]))
            return
        if entry.get("launch_file") and os.path.exists(entry["launch_file"]):
            if sys.platform.startswith("win"):
                os.startfile(entry["launch_file"])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(entry["launch_file"]))
            return
        exe = entry.get("exe_path", "")
        cmd = entry.get("launch_cmd") or ([exe] + list(entry.get("launch_args") or []))
        if isinstance(cmd, (list, tuple)) and entry.get("id"):
            cmd = list(cmd) + launch_args_for(f"custom-{entry['id']}")
        cwd = entry.get("workdir") or os.path.dirname(exe) or None
        if cwd and not os.path.isdir(cwd):
            cwd = None
        subprocess.Popen(cmd, cwd=cwd)
    except Exception as e:
        QMessageBox.warning(win, "Launch Game", f"Couldn't launch game: {e}")

_desk_prev_custom_card = AdaptiveApp.create_custom_card

def _desk_create_custom_card(self, game_id, title_text, exe_path, cover_path, compat_tool):
    _desk_prev_custom_card(self, game_id, title_text, exe_path, cover_path, compat_tool)
    entry = next((g for g in load_custom_games() if g.get("id") == game_id), None)
    if not entry or not (entry.get("launch_url") or entry.get("launch_file") or entry.get("launch_args")
                         or entry.get("launch_cmd")):
        return
    card = getattr(self, "custom_cards", {}).get(game_id)
    btn = card.findChild(QPushButton, "Primary") if card is not None else None
    if btn is None:
        return
    try:
        btn.clicked.disconnect()
    except Exception:
        pass
    btn.clicked.connect(lambda _c=False, e=entry: _desk_launch(self, e))

AdaptiveApp.create_custom_card = _desk_create_custom_card

_desk_prev_build_library_page = AdaptiveApp.build_library_page

def _desk_build_library_page(self):
    widget = _desk_prev_build_library_page(self)
    for btn in widget.findChildren(QPushButton):
        if btn.text() == "+":
            menu = btn.findChild(QMenu)
            if menu is not None and not any(a.text().startswith("Auto-Add Desktop") for a in menu.actions()):
                menu.addSeparator()
                menu.addAction("Auto-Add Desktop Games").triggered.connect(
                    lambda _c=False: self.auto_add_desktop_games())
    return widget

AdaptiveApp.build_library_page = _desk_build_library_page


# ================================================================ frameless window: exit button + draggable top bar
from PyQt6.QtCore import QEvent

class _TitleBarDrag(QObject):
    """Makes the top nav bar act as the title bar: drag to move, double-click to maximize / restore."""
    def __init__(self, window, bar):
        super().__init__(bar)
        self._window = window
        self._last_press = 0.0
        bar.installEventFilter(self)

    def _toggle_maximized(self):
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def eventFilter(self, obj, event):
        et = event.type()
        left = hasattr(event, "button") and event.button() == Qt.MouseButton.LeftButton
        if et == QEvent.Type.MouseButtonDblClick and left:
            self._last_press = 0.0
            self._toggle_maximized()
            return True
        if et == QEvent.Type.MouseButtonPress and left:
            now = time.monotonic()
            if now - self._last_press < QApplication.doubleClickInterval() / 1000.0:
                self._last_press = 0.0
                self._toggle_maximized()
                return True
            self._last_press = now
            handle = self._window.windowHandle()
            if handle is not None and not self._window.isMaximized():
                handle.startSystemMove()
            return True
        return False

def _caption_icon(restore):
    """Maximize (one square) / restore down (two squares) glyph, drawn so it doesn't depend on a font."""
    icon = QIcon()
    for mode, color in ((QIcon.Mode.Normal, _theme_state["nav_text"]), (QIcon.Mode.Active, _theme_state["nav_active"])):
        pm = QPixmap(24, 24)
        pm.setDevicePixelRatio(2.0)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(color), 1.2))
        if restore:
            p.drawRect(QRectF(1.5, 3.5, 7, 7))
            p.drawPolyline([QPointF(3.5, 3.5), QPointF(3.5, 1.5), QPointF(10.5, 1.5),
                            QPointF(10.5, 8.5), QPointF(8.5, 8.5)])
        else:
            p.drawRect(QRectF(1.5, 1.5, 9, 9))
        p.end()
        icon.addPixmap(pm, mode)
    return icon

class _WindowStateWatcher(QObject):
    def __init__(self, window, callback):
        super().__init__(window)
        self._callback = callback
        window.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.WindowStateChange:
            self._callback()
        return False

_frameless_prev_init = AdaptiveApp.__init__

def _frameless_init(self):
    _frameless_prev_init(self)
    self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)  # no OS title bar / border

    bar = self.findChild(QFrame, "Sidebar")
    if bar is None or bar.layout() is None:
        return
    _TitleBarDrag(self, bar)

    exit_btn = QPushButton("\u2715")
    exit_btn.setObjectName("ExitButton")
    exit_btn.setToolTip("Close")
    exit_btn.setFixedSize(40, 32)
    exit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    exit_btn.setStyleSheet(
        "QPushButton { background: transparent; border: none; border-radius: 6px;"
        " font-size: 15px; font-weight: bold; padding: 0; }"
        "QPushButton:hover { background: #e5484d; color: #ffffff; }"
        "QPushButton:pressed { background: #c93a3f; color: #ffffff; }")

    def exit_launcher():
        # With "Close button minimizes to tray" on, this hides the launcher to the tray (Quit is in the tray menu);
        # otherwise it really quits.
        tray = getattr(self, "_tray", None)
        if tray is not None and tray.isVisible() and load_launcher_settings().get("close_to_tray", True):
            self.close()
            return
        self._really_quit = True
        if tray is not None:
            tray.hide()
        QApplication.instance().quit()

    exit_btn.clicked.connect(exit_launcher)

    min_btn = QPushButton("-")
    min_btn.setObjectName("MinButton")
    min_btn.setToolTip("Minimize")
    min_btn.setFixedSize(40, 32)
    min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    min_btn.setStyleSheet(
        "QPushButton { background: transparent; border: none; border-radius: 6px;"
        " font-size: 18px; font-weight: bold; padding: 0; }"
        "QPushButton:hover { background: rgba(128, 128, 128, 0.25); }"
        "QPushButton:pressed { background: rgba(128, 128, 128, 0.4); }")
    min_btn.clicked.connect(self.showMinimized)

    max_btn = QPushButton()
    max_btn.setObjectName("MaxButton")
    max_btn.setFixedSize(40, 32)
    max_btn.setIconSize(QSize(12, 12))
    max_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    max_btn.setStyleSheet(
        "QPushButton { background: transparent; border: none; border-radius: 6px; padding: 0; }"
        "QPushButton:hover { background: rgba(128, 128, 128, 0.25); }"
        "QPushButton:pressed { background: rgba(128, 128, 128, 0.4); }")

    def refresh_max_btn():
        maximized = self.isMaximized()
        max_btn.setIcon(_caption_icon(maximized))
        max_btn.setToolTip("Restore Down" if maximized else "Maximize")

    max_btn.clicked.connect(lambda: self.showNormal() if self.isMaximized() else self.showMaximized())
    _WindowStateWatcher(self, refresh_max_btn)
    refresh_max_btn()
    _theme_listeners.append(refresh_max_btn)   # redraw the glyph when the theme changes

    bar.layout().addSpacing(8)
    bar.layout().addWidget(min_btn)
    bar.layout().addWidget(max_btn)
    bar.layout().addWidget(exit_btn)

AdaptiveApp.__init__ = _frameless_init


# ================================================================ in-game overlay (toast + settings)
try:
    from overlay import overlay as _overlay
except Exception:
    _overlay = None
try:        # the toasts draw emoji as Apple pictures from the same cache the chat uses
    import overlay as _overlay_module
    _overlay_module.set_emoji_dir(os.path.dirname(emoji_store()._file("0")))
except Exception:
    pass

OVERLAY_MODES = {
    "window": "Overlay window (default)",
    "inject": "In-game injection (D3D11, 64-bit; Wine/Proton too)",
    "off": "Off",
}

def _overlay_settings_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "overlay_settings.json")

def get_overlay_mode():
    try:
        with open(_overlay_settings_path(), encoding="utf-8") as f:
            mode = json.load(f).get("mode")
        if mode in OVERLAY_MODES:
            return mode
    except Exception:
        pass
    return "window"

def set_overlay_mode(mode):
    try:
        os.makedirs(os.path.dirname(_overlay_settings_path()), exist_ok=True)
        with open(_overlay_settings_path(), "w", encoding="utf-8") as f:
            json.dump({"mode": mode}, f)
    except Exception:
        pass

def _overlay_game_launched(exe):
    """In inject mode, puts the overlay DLL into the game we just started."""
    if _overlay is None or get_overlay_mode() != "inject":
        return
    from overlay import inject_overlay
    ok = inject_overlay(exe, asset_path("overlay_hook.dll"))
    _ov_log(f"in-game overlay in {os.path.basename(exe)}: {'LOADED' if ok else 'not loaded (yet)'}")
    if not ok and sys.platform.startswith("linux"):
        try:
            from overlay import diagnose_linux
            _ov_log("   why: " + diagnose_linux(exe))
        except Exception as e:
            _ov_log(f"   (diagnose failed: {e!r})")

_OV_REG_HEAD = ("Windows Registry Editor Version 5.00\r\n\r\n"
                "[HKEY_LOCAL_MACHINE\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Windows]\r\n")
_OV_DLL_DIR = "ncz"      # C:\\ncz\\overlay_hook.dll inside the prefix (AppInit_DLLs can't take paths with spaces)

def _overlay_wine_target(app, exe, compat_tool):
    """(prefix, argv-prefix, env) for running a command inside the Wine/Proton prefix this game launches in."""
    if not (sys.platform.startswith("linux") and exe.lower().endswith(".exe")):
        return None
    kind = compat_tool_kind(compat_tool)
    if kind == "proton":
        compat = compat_data_dir_for(exe)
        os.makedirs(compat, exist_ok=True)
        return os.path.join(compat, "pfx"), [compat_tool, "run"], _proton_redist_env((compat_tool, compat))
    wine = compat_tool if kind == "wine" else default_wine_path(app)
    if not wine:
        return None
    env = wine_game_env()
    return env.get("WINEPREFIX") or _wine_prefix(), [wine], env

def _overlay_prepare_wine(app, exe, compat_tool, enable):
    """Before a Wine/Proton game starts: registers (enable) or removes the overlay DLL as an AppInit_DLL in its
    prefix. Wine loads AppInit_DLLs into every GUI process as user32 starts, which is the Linux stand-in for the
    Windows remote-thread injection. Returns the prefix when the DLL is set up, else None."""
    import tempfile
    target = _overlay_wine_target(app, exe, compat_tool)
    if target is None:
        return None
    prefix, base_cmd, env = target
    marker = os.path.join(prefix, ".ncz_overlay_appinit")
    src = asset_path("overlay_hook.dll")
    dest_dir = os.path.join(prefix, "drive_c", _OV_DLL_DIR)
    dest = os.path.join(dest_dir, "overlay_hook.dll")

    def regedit(body):
        reg = os.path.join(tempfile.gettempdir(), "ncz_overlay_appinit.reg")
        with open(reg, "w", encoding="ascii", newline="") as f:
            f.write(_OV_REG_HEAD + body)
        win_path = "Z:" + reg.replace("/", "\\")
        r = subprocess.run(base_cmd + ["regedit", "/S", win_path], env=env, cwd=os.path.dirname(exe),
                           capture_output=True, timeout=120)
        return r.returncode == 0

    if not enable:
        if os.path.exists(marker):
            if regedit('"AppInit_DLLs"=-\r\n"LoadAppInit_DLLs"=-\r\n"RequireSignedAppInit_DLLs"=-\r\n'):
                os.remove(marker)
        return None
    if not os.path.isfile(src):
        return None
    same = False
    try:
        with open(src, "rb") as a, open(dest, "rb") as b:
            same = a.read() == b.read()
    except OSError:
        pass
    if not (same and os.path.exists(marker)):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)       # first time per prefix takes a few seconds
        try:
            if not regedit('"AppInit_DLLs"="C:\\\\' + _OV_DLL_DIR + '\\\\overlay_hook.dll"\r\n'
                           '"LoadAppInit_DLLs"=dword:00000001\r\n"RequireSignedAppInit_DLLs"=dword:00000000\r\n'):
                return None
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(src, dest)
            with open(marker, "w", encoding="utf-8") as f:
                f.write("1")
        finally:
            QApplication.restoreOverrideCursor()
    try:
        from overlay import register_wine_prefix
        register_wine_prefix(exe, prefix)
    except Exception:
        pass
    return prefix

def _overlay_classify(text):
    """Launcher friend notification text -> (line-2 prefix, highlighted part)."""
    if text.startswith("is now playing "):
        return "is playing ", text[len("is now playing "):]
    if text.startswith("Game invite: "):
        return "invited you to play ", text[len("Game invite: "):]
    if text == "is now online":
        return "is ", "online"
    if text == "sent you a friend request":
        return "sent you a ", "friend request"
    # anything else from the friends page is a chat message: show the message itself (one line, shortened)
    msg = " ".join(text.split())
    if len(msg) > 32:
        msg = msg[:29] + "..."
    return msg, ""

class _OvImageBridge(QObject):
    done = pyqtSignal(str, str, str, str, str)      # name, highlight, avatar, prefix, picture path ("" = none)

_ov_image_bridge = None

def _overlay_show(name, hl, avatar, prefix, image=None):
    _overlay.set_mode("window")          # no injected game running: use the window toast
    try:
        _overlay.notify_playing(name, hl, avatar, prefix, image=image or None)
    except TypeError:                    # an older overlay.py without picture support
        _overlay.notify_playing(name, hl, avatar, prefix)

class _OvInviteBridge(QObject):
    done = pyqtSignal(str, str, str, str, str)      # sender name, game, avatar path, cover path, join link

_ov_invite_bridge = None

def _overlay_invite_ready(name, game, avatar, cover, link):
    try:
        import overlay as _ovm
        _ovm.invite_accept_hook = _accept_invite_from_overlay
    except Exception:
        pass
    _overlay.set_mode("window")
    _overlay.notify_invite(name, game, link, avatar or None, cover or None)

def _overlay_show_invite(uid, name, inv, avatar):
    """Game invite -> overlay toast with the game's cover and an Accept button (cover is fetched off the GUI thread)."""
    global _ov_invite_bridge
    if _ov_invite_bridge is None:
        _ov_invite_bridge = _OvInviteBridge()
        _ov_invite_bridge.done.connect(_overlay_invite_ready)
    bridge = _ov_invite_bridge
    _invite_games[inv["link"]] = inv.get("game", "")
    _invite_games[inv["link"]] = inv.get("game", "")

    def work():
        cover = invite_cover_path(inv, uid)
        if not cover:
            try:
                cover = _fetch_invite_cover(inv, uid)
            except Exception:
                cover = ""
        bridge.done.emit(name, inv["game"] or "a game", avatar or "", cover or "", inv["link"])
    threading.Thread(target=work, daemon=True).start()

def _overlay_friend_event(page, name, text, uid):
    """Every friend notification (playing / online / friend request / message) -> overlay toast.
    A message with a picture (png / jpg / gif ...) shows the picture in the toast; a video says "sent a video"."""
    global _ov_image_bridge
    if _overlay is None:
        return
    msg = (page.__dict__.get("_ov_msgs") or {}).pop(uid, None)
    mode = get_overlay_mode()
    if mode == "off":
        return
    prefix, hl = _overlay_classify(text)
    inv = parse_invite(str(msg.get("text") or "")) if isinstance(msg, dict) else None
    if inv and hasattr(_overlay, "notify_invite"):
        if mode == "inject":                 # the injected in-game toast is text only, so the clickable one comes on top
            try:
                from overlay import send_to_injected
                send_to_injected(name, hl, prefix)
            except Exception:
                pass
        _overlay_show_invite(uid, name, inv, _friend_avatar_file(uid, ((page.friends.get(uid) or {}).get("photo"))))
        return
    att = None
    if isinstance(msg, dict):
        atts = [a for a in (msg.get("att") if isinstance(msg.get("att"), list) else []) if isinstance(a, dict)]
        if atts and atts[0].get("k") in ("img", "gif", "vid"):
            att = atts[0]
            caption = " ".join(str(msg.get("text") or "").split())
            if att.get("k") == "vid":
                prefix, hl = "sent a video", ""
            elif caption:
                prefix, hl = (caption if len(caption) <= 32 else caption[:29] + "..."), ""
            else:
                prefix, hl = ("sent a GIF" if att.get("k") == "gif" else "sent an image"), ""
    if mode == "inject":
        from overlay import send_to_injected
        if send_to_injected(name, hl, prefix):
            return                       # shown inside the game (text only)
    info = (page.friends.get(uid) or page.requests.get(uid) or {})
    avatar = _friend_avatar_file(uid, info.get("photo"))
    if att is None or att.get("k") == "vid":
        _overlay_show(name, hl, avatar, prefix)
        return
    if _ov_image_bridge is None:         # picture: download it (cached on disk) off the GUI thread, then show the toast
        _ov_image_bridge = _OvImageBridge()
        _ov_image_bridge.done.connect(lambda n, h, a, p, i: _overlay_show(n, h, a, p, i))
    bridge = _ov_image_bridge

    def work():
        try:
            path = cloud_fetch_media(uid, att)
        except Exception:
            path = ""
        bridge.done.emit(name, hl, avatar or "", prefix, path)
    threading.Thread(target=work, daemon=True).start()

_ov_prev_launch_exe = _launch_exe

def _launch_exe(app, exe, compat_tool="", d3d="", args=None):
    linux = sys.platform.startswith("linux")
    if linux and _overlay is not None and exe and os.path.exists(exe):
        try:       # has to happen before the game starts: Wine reads AppInit_DLLs while the process is loading
            pfx = _overlay_prepare_wine(app, exe, compat_tool, get_overlay_mode() == "inject")
            _ov_log(f"overlay mode={get_overlay_mode()}, wine prefix prepared: {pfx}")
        except Exception as e:
            print(f"overlay: couldn't set up the Wine prefix: {e}")
            _ov_log(f"couldn't set up the Wine prefix: {e!r}")
    _ov_prev_launch_exe(app, exe, compat_tool, d3d, args)
    if exe and os.path.exists(exe):
        # on Linux the Wine process takes a while to appear: look again a few times
        for ms in ((2500, 6000, 12000, 25000, 45000, 70000) if linux else (1500,)):
            QTimer.singleShot(ms, lambda: _overlay_game_launched(exe))

_ov_prev_friends_init = FriendsPage.__init__

def _ov_friends_init(self, *a, **k):
    _ov_prev_friends_init(self, *a, **k)
    self.friend_event.connect(lambda n, t, u: _overlay_friend_event(self, n, t, u))

FriendsPage.__init__ = _ov_friends_init

_ov_prev_settings_init = SettingsPage.__init__

def _ov_settings_init(self, *a, **k):
    _ov_prev_settings_init(self, *a, **k)
    layout = self.layout()
    panel = QFrame()
    panel.setObjectName("Panel")
    pl = QVBoxLayout(panel)
    pl.setContentsMargins(22, 18, 22, 18)
    row = QHBoxLayout()
    text = QVBoxLayout()
    text.setSpacing(2)
    name = QLabel("Game Overlay")
    name.setObjectName("RowTitle")
    desc = QLabel("Shows \"FRIEND is playing GAME\" when a friend starts a game. Overlay window works over "
                  "borderless/windowed games; injection (D3D11, 64-bit games) also works in exclusive fullscreen; on Linux it is loaded into Wine/Proton games through AppInit_DLLs. If it fails it falls back to the window. Don't use it with anti-cheat games.")
    desc.setObjectName("RowDesc")
    desc.setWordWrap(True)
    text.addWidget(name)
    text.addWidget(desc)
    combo = QComboBox()
    combo.setFixedWidth(240)
    for key, label in OVERLAY_MODES.items():
        combo.addItem(label, key)
    combo.setCurrentIndex(max(combo.findData(get_overlay_mode()), 0))
    combo.currentIndexChanged.connect(lambda _i: set_overlay_mode(combo.currentData()))
    row.addLayout(text, 1)
    row.addWidget(combo)
    pl.addLayout(row)
    idx = max(layout.count() - 1, 0)   # just before the trailing stretch
    layout.insertSpacing(idx, 12)
    layout.insertWidget(idx + 1, panel)

SettingsPage.__init__ = _ov_settings_init


# ================================================================ second instance with separate data (NCZ_PROFILE=name)
_ncz_profile = re.sub(r"[^A-Za-z0-9_-]", "_", os.environ.get("NCZ_PROFILE", "").strip())
if _ncz_profile:
    _ncz_base_settings_path = get_launcher_settings_path

    def get_launcher_settings_path():
        # settings, account, profile, friends cache, overlay settings... all live next to this file
        base = os.path.dirname(_ncz_base_settings_path())
        return os.path.join(f"{base}_{_ncz_profile}", "launcher.json")


# ================================================================ friends overlay (Shift+Q while in a game)
from PyQt6.QtWidgets import QSizeGrip, QListWidget, QListWidgetItem
from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QPen

def _force_foreground_x11(winid):
    """Asks the window manager to focus our window the way a taskbar/pager would (a plain activateWindow() is
    ignored by focus-stealing prevention when the keypress that opened the panel went to the game)."""
    try:
        from Xlib import display, X, protocol
        d = display.Display()
        root = d.screen().root
        ev = protocol.event.ClientMessage(window=d.create_resource_object("window", int(winid)),
                                          client_type=d.intern_atom("_NET_ACTIVE_WINDOW"),
                                          data=(32, [2, X.CurrentTime, 0, 0, 0]))     # source 2 = pager
        root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        d.flush()
        d.close()
        return
    except Exception:
        pass
    xdotool = shutil.which("xdotool")
    if xdotool:
        try:
            subprocess.Popen([xdotool, "windowactivate", str(int(winid))],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

def _force_foreground(hwnd):
    if sys.platform.startswith("linux"):
        if QApplication.platformName() == "xcb":
            _force_foreground_x11(hwnd)
        return
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        u.keybd_event(0x12, 0, 0, 0)       # tap ALT so Windows lets us take focus from the game
        u.keybd_event(0x12, 0, 2, 0)
        u.SetForegroundWindow(hwnd)
    except Exception:
        pass

class _FriendsDim(QWidget):
    """Full-screen 50% black layer behind the panel; click it to close."""
    clicked = pyqtSignal()
    def __init__(self, geo):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        # (no X11 bypass hint here: a bypass window can't get keyboard focus, so the chat box wouldn't work)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(geo)
    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 128))
        p.end()
    def mousePressEvent(self, _e):
        self.clicked.emit()

def _fo_status(d):
    """-> (text, color, group) group: 0 in game, 1 online, 2 offline."""
    s, _c = _friend_status(d)
    if s.startswith("Playing"):
        return s, "#a97cff", 0
    if s == "Online":
        return s, "#4ade80", 1
    if s == "Idle":
        return s, "#60a5fa", 1
    return s, "#6b6b78", 2

def _fo_avatar(path, letter, size, dot=None):
    """Round profile picture (or initial) with an optional status dot."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    circ = QPainterPath()
    circ.addEllipse(0, 0, size, size)
    p.setClipPath(circ)
    src = QPixmap(path) if path else QPixmap()
    if not src.isNull():
        p.drawPixmap(0, 0, src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                      Qt.TransformationMode.SmoothTransformation))
    else:
        p.fillRect(0, 0, size, size, QColor("#3a2f66"))
        p.setPen(QColor("#d6c5ff"))
        f = QFont()
        f.setPixelSize(int(size * 0.42))
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, letter)
    p.setClipping(False)
    if dot:
        d = max(10, int(size * 0.32))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#1b1824"))                      # ring in the card colour
        p.drawEllipse(size - d, size - d, d, d)
        p.setBrush(QColor(dot))
        p.drawEllipse(size - d + 2, size - d + 2, d - 4, d - 4)
    p.end()
    return pm

def _fo_clear(layout):
    while layout.count():
        it = layout.takeAt(0)
        if it.widget() is not None:
            it.widget().deleteLater()
        elif it.layout() is not None:
            _fo_clear(it.layout())
            it.layout().deleteLater()

def _fo_icon(kind, color="#b8b0cc", size=20):
    """Draws back / close / send icons so they never depend on font glyphs."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 2.0 * size / 20)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.scale(size / 20, size / 20)
    if kind == "back":
        p.drawPolyline([QPointF(12, 4.5), QPointF(6.5, 10), QPointF(12, 15.5)])
    elif kind == "close":
        p.drawLine(QPointF(5, 5), QPointF(15, 15))
        p.drawLine(QPointF(15, 5), QPointF(5, 15))
    elif kind == "game":   # game controller (invite to game)
        body = QPainterPath()
        body.addRoundedRect(QRectF(2.5, 5.5, 15, 9.5), 4.5, 4.5)
        p.drawPath(body)
        p.drawLine(QPointF(5.2, 10.2), QPointF(8.6, 10.2))
        p.drawLine(QPointF(6.9, 8.5), QPointF(6.9, 11.9))
        pen.setWidthF(2.6)
        p.setPen(pen)
        p.drawPoint(QPointF(12.6, 9.2))
        p.drawPoint(QPointF(14.6, 11.2))
    else:  # send (arrow up)
        p.drawLine(QPointF(10, 16), QPointF(10, 5))
        p.drawPolyline([QPointF(5, 9.5), QPointF(10, 4.5), QPointF(15, 9.5)])
    p.end()
    return QIcon(pm)

class _FriendRow(QFrame):
    clicked = pyqtSignal(str)
    def __init__(self, uid):
        super().__init__()
        self.uid = uid
        self.setObjectName("Row")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
    def mousePressEvent(self, _e):
        self.clicked.emit(self.uid)

class _FriendsPanel(QWidget):
    got_friends = pyqtSignal(object)
    got_chat = pyqtSignal(object)
    got_invite = pyqtSignal(object)
    invite_sent = pyqtSignal(object)
    closed = pyqtSignal()

    STYLE = """
    #Card{background:#1b1824;border:1px solid #342d47;border-radius:14px;}
    #Head{background:rgba(169,124,255,0.09);border-top-left-radius:13px;border-top-right-radius:13px;border-bottom:1px solid #2b2540;}
    QLabel{color:#e8e8ee;background:transparent;}
    #Title{font-size:17px;font-weight:700;color:#ffffff;}
    #Sub{font-size:12px;color:#9a93b0;}
    #Group{font-size:11px;font-weight:700;color:#6b6b78;padding:12px 8px 4px 8px;}
    #Name{font-size:14px;font-weight:600;color:#ffffff;}
    #Empty{color:#6b6b78;font-size:13px;}
    #Row{background:transparent;border-radius:10px;}
    #Row:hover{background:#282236;}
    QScrollArea{background:transparent;border:none;}
    #Body{background:transparent;}
    QScrollBar:vertical{background:transparent;width:8px;margin:2px;}
    QScrollBar::handle:vertical{background:#3f3658;border-radius:4px;min-height:24px;}
    QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}
    QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}
    #IconBtn{background:transparent;color:#a0a0ad;border:none;border-radius:8px;font-size:18px;}
    #IconBtn:hover{background:#2d2740;color:#ffffff;}
    #Mine{background:#a97cff;color:#ffffff;border-radius:14px;padding:8px 12px;font-size:13px;}
    #Theirs{background:#2d2740;color:#ececf1;border-radius:14px;padding:8px 12px;font-size:13px;}
    #Bar{background:transparent;border-top:1px solid #2b2540;}
    #Input{background:#282236;color:#ffffff;border:1px solid #342d47;border-radius:18px;padding:8px 14px;font-size:13px;}
    #Input:focus{border:1px solid #a97cff;}
    #Send{background:#a97cff;color:#ffffff;border:none;border-radius:18px;font-size:16px;font-weight:700;}
    #Send:hover{background:#b794ff;}
    #Invite{background:#2d2740;border:none;border-radius:18px;}
    #Invite:hover{background:#3d3458;}
    """

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        # (no X11 bypass hint here: a bypass window can't get keyboard focus, so the chat box wouldn't work)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(320, 400)
        self.resize(420, 620)
        self.setStyleSheet(self.STYLE)
        self.friends, self.uid, self.fname = {}, None, ""
        self._sig, self._bubbles = None, []
        self._inviting = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        outer.addWidget(card)
        root = QVBoxLayout(card)
        root.setContentsMargins(0, 0, 0, 14)      # bottom strip holds the resize grip
        root.setSpacing(0)

        # header
        head = QFrame()
        head.setObjectName("Head")
        head.setFixedHeight(62)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(14, 0, 10, 0)
        hl.setSpacing(10)
        self.back = QPushButton()
        self.back.setIcon(_fo_icon("back"))
        self.back.setIconSize(QSize(20, 20))
        self.back.setObjectName("IconBtn")
        self.back.setFixedSize(32, 32)
        self.back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back.clicked.connect(self._show_list)
        self.back.hide()
        self.h_avatar = QLabel()
        self.h_avatar.setFixedSize(38, 38)
        self.h_avatar.hide()
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        self.title = QLabel("Friends")
        self.title.setObjectName("Title")
        self.sub = QLabel("")
        self.sub.setObjectName("Sub")
        tcol.addStretch()
        tcol.addWidget(self.title)
        tcol.addWidget(self.sub)
        tcol.addStretch()
        x = QPushButton()
        x.setIcon(_fo_icon("close"))
        x.setIconSize(QSize(18, 18))
        x.setObjectName("IconBtn")
        x.setFixedSize(32, 32)
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.clicked.connect(self.closed.emit)
        hl.addWidget(self.back)
        hl.addWidget(self.h_avatar)
        hl.addLayout(tcol, 1)
        ico = asset_path("icon.png")
        if os.path.exists(ico):
            logo = QLabel()
            logo.setFixedSize(30, 30)
            logo.setPixmap(_fo_avatar(ico, "", 30))
            hl.addWidget(logo)
        hl.addWidget(x)
        root.addWidget(head)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # page 0: friends list
        self.list_scroll = QScrollArea()
        self.list_scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("Body")
        self.lv = QVBoxLayout(body)
        self.lv.setContentsMargins(10, 4, 10, 8)
        self.lv.setSpacing(2)
        self.list_scroll.setWidget(body)
        self.stack.addWidget(self.list_scroll)

        # page 1: chat
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        cbody = QWidget()
        cbody.setObjectName("Body")
        self.cv = QVBoxLayout(cbody)
        self.cv.setContentsMargins(14, 10, 14, 10)
        self.cv.setSpacing(6)
        self.chat_scroll.setWidget(cbody)
        bar = QFrame()
        bar.setObjectName("Bar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 10, 12, 0)
        bl.setSpacing(8)
        self.note = QLabel("")                 # short status line ("Finding your game...", "Invite sent.")
        self.note.setObjectName("Sub")
        self.note.setWordWrap(True)
        self.note.setContentsMargins(14, 6, 14, 0)
        self.note.hide()
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.timeout.connect(self.note.hide)
        invite = QPushButton()
        invite.setIcon(_fo_icon("game", "#d6c5ff"))
        invite.setIconSize(QSize(20, 20))
        invite.setObjectName("Invite")
        invite.setFixedSize(36, 36)
        invite.setCursor(Qt.CursorShape.PointingHandCursor)
        invite.setToolTip("Invite to the game you're playing")
        invite.clicked.connect(self._invite)
        self.input = QLineEdit()
        self.input.setObjectName("Input")
        self.input.setPlaceholderText("Message...")
        self.input.setMaxLength(500)
        self.input.returnPressed.connect(self._send)
        send = QPushButton()
        send.setIcon(_fo_icon("send", "#ffffff"))
        send.setIconSize(QSize(20, 20))
        send.setObjectName("Send")
        send.setFixedSize(36, 36)
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.clicked.connect(self._send)
        bl.addWidget(invite)
        bl.addWidget(self.input, 1)
        bl.addWidget(send)
        pl.addWidget(self.chat_scroll, 1)
        pl.addWidget(self.note)
        pl.addWidget(bar)
        self.stack.addWidget(page)

        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(14, 14)
        self.got_friends.connect(self._fill_friends)
        self.got_chat.connect(self._fill_chat)
        self.got_invite.connect(self._on_invite)
        self.invite_sent.connect(self._on_invite_sent)
        self.timer = QTimer(self)             # chat refresh
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._load_chat)
        self.ftimer = QTimer(self)            # friends list refresh
        self.ftimer.setInterval(10000)
        self.ftimer.timeout.connect(lambda: self.uid is None and self.refresh())
        self.ftimer.start()

    def _bg(self, fn, sig):
        def work():
            try:
                res = fn()
            except Exception:
                res = None
            try:
                sig.emit(res)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        self._bg(cloud_fetch_friends, self.got_friends)

    # ---- friends list
    def _fill_friends(self, friends):
        if friends is None:
            return
        self.friends = friends
        _fo_clear(self.lv)
        rows = sorted(((_fo_status(d), uid, d) for uid, d in friends.items()), key=lambda r: r[0][2])
        online = sum(1 for r in rows if r[0][2] < 2)
        if self.uid is None:
            self.sub.setText(f"{online} online" if friends else "")
        if not friends:
            e = QLabel("No friends yet.\nAdd some from the launcher.")
            e.setObjectName("Empty")
            e.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.lv.addStretch()
            self.lv.addWidget(e)
            self.lv.addStretch()
            return
        titles = {0: "IN GAME", 1: "ONLINE", 2: "OFFLINE"}
        shown = set()
        for (status, color, grp), uid, d in rows:
            if grp not in shown:
                shown.add(grp)
                g = QLabel(titles[grp])
                g.setObjectName("Group")
                self.lv.addWidget(g)
            name = d.get("username") or "Friend"
            row = _FriendRow(uid)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(8, 8, 8, 8)
            rl.setSpacing(12)
            av = QLabel()
            av.setFixedSize(40, 40)
            av.setPixmap(_fo_avatar(_friend_avatar_file(uid, d.get("photo")), name[:1].upper(), 40, color))
            col = QVBoxLayout()
            col.setSpacing(1)
            n = QLabel(name)
            n.setObjectName("Name")
            s = QLabel(status)
            s.setStyleSheet(f"color:{color};font-size:12px;")
            col.addWidget(n)
            col.addWidget(s)
            rl.addWidget(av)
            rl.addLayout(col, 1)
            row.clicked.connect(self._open_chat)
            self.lv.addWidget(row)
        self.lv.addStretch()

    def _show_list(self):
        self.timer.stop()
        self.uid, self._sig = None, None
        self.back.hide()
        self.h_avatar.hide()
        self.title.setText("Friends")
        self.sub.setStyleSheet("")
        self._fill_friends(self.friends)
        self.stack.setCurrentIndex(0)

    # ---- chat
    def _open_chat(self, uid):
        d = self.friends.get(uid) or {}
        self.uid, self._sig = uid, None
        self.fname = d.get("username") or "Friend"
        status, color, _g = _fo_status(d)
        self.title.setText(self.fname)
        self.sub.setText(status)
        self.sub.setStyleSheet(f"color:{color};")
        self.h_avatar.setPixmap(_fo_avatar(_friend_avatar_file(uid, d.get("photo")), self.fname[:1].upper(), 38, color))
        self.h_avatar.show()
        self.back.show()
        self._note("")
        _fo_clear(self.cv)
        self._bubbles = []
        self.stack.setCurrentIndex(1)
        self.input.setFocus()
        self._load_chat()
        self.timer.start()

    def _bubble_w(self):
        return max(140, int(self.width() * 0.66))

    def _load_chat(self):
        uid = self.uid
        if uid:
            self._bg(lambda: (uid,) + tuple(cloud_get_messages(uid)[:2]), self.got_chat)

    def _fill_chat(self, res):
        if not res or res[0] != self.uid:
            return
        _other, me, msgs = res
        sig = tuple((m.get("from"), chat_preview(m)) for m in msgs)
        if sig == self._sig:
            return                              # nothing new: don't rebuild (avoids flicker)
        first = self._sig is None
        sb = self.chat_scroll.verticalScrollBar()
        stick = first or sb.value() >= sb.maximum() - 24
        self._sig = sig
        _fo_clear(self.cv)
        self._bubbles = []
        self.cv.addStretch()                    # keeps messages anchored to the bottom
        if not msgs:
            e = QLabel("No messages yet - say hi!")
            e.setObjectName("Empty")
            e.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.cv.addWidget(e)
        for m in msgs:
            mine = m.get("from") == me
            b = QLabel(chat_preview(m))
            b.setObjectName("Mine" if mine else "Theirs")
            b.setWordWrap(True)
            b.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            b.setMaximumWidth(self._bubble_w())
            self._bubbles.append(b)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            if mine:
                row.addStretch()
                row.addWidget(b)
            else:
                row.addWidget(b)
                row.addStretch()
            self.cv.addLayout(row)
        if stick:
            self._scroll_bottom()

    def _scroll_bottom(self):
        sb = self.chat_scroll.verticalScrollBar()
        for ms in (0, 60, 200):               # layout settles a moment after the bubbles are added
            QTimer.singleShot(ms, lambda: sb.setValue(sb.maximum()))

    def _send(self):
        text = self.input.text().strip()
        uid = self.uid
        if not text or not uid:
            return
        self.input.clear()
        def work():
            try:
                cloud_send_message(uid, text)
            except Exception:
                pass
            QTimer.singleShot(0, self._load_chat)
        threading.Thread(target=work, daemon=True).start()

    # ---- game invite: same detection as the Friends page's "Invite to Game" button
    def _note(self, text, ms=0):
        self._note_timer.stop()
        self.note.setText(text)
        self.note.setVisible(bool(text))
        if text and ms:
            self._note_timer.start(ms)

    def _invite(self):
        uid = self.uid
        if not uid or self._inviting:
            return
        self._inviting = True
        clip = normalize_steam_join(QApplication.clipboard().text())      # fallback: a link you just copied
        self._note("Finding your game and join link...")
        self._bg(lambda: (uid, clip, detect_invite_info()), self.got_invite)

    def _on_invite(self, res):
        self._inviting = False
        if not res:
            self._note("Couldn't look up your game. Try again.", 4000)
            return
        uid, clip, info = res
        if uid != self.uid:                       # went back / opened another chat meanwhile
            self._note("")
            return
        info = info or {}
        game, appid, link = info.get("game", ""), info.get("appid", 0), info.get("link", "")
        if game and link:
            self._send_invite(uid, game, link, appid)
            return
        self._note("")
        if not game:
            note = "No game is running right now, so enter the game and its join link."
        elif clip:
            note = f"Found {game}. Used the link from your clipboard; check it, then send."
        else:
            note = f"Found {game}, but Steam has no join link for it right now (it only has one while you're in a joinable lobby or server)."
        dlg = SteamInviteDialog(None, game, link or clip, appid, note)     # no parent: the panel's dark style stays out of it
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        def front():
            try:
                dlg.raise_()
                dlg.activateWindow()
                _force_foreground(int(dlg.winId()))
            except Exception:
                pass
        QTimer.singleShot(0, front)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        g, l, a = dlg.values()
        if uid == self.uid:
            self._send_invite(uid, g, l, a)

    def _send_invite(self, uid, game, link, appid):
        text = make_invite_text(game, link, appid)
        self._note(f"Sending invite to {self.fname}...")
        def send():
            cloud_send_message(uid, text)
            return uid, game
        self._bg(send, self.invite_sent)          # a failure comes back as None

    def _on_invite_sent(self, res):
        if not res:
            self._note("Couldn't send the invite. Try again.", 4000)
            return
        uid, game = res
        self._note(f"Invite to {game} sent.", 3000)
        if uid == self.uid:
            self._load_chat()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.grip.move(self.width() - 20, self.height() - 20)
        self.grip.raise_()
        w = self._bubble_w()
        for b in self._bubbles:
            b.setMaximumWidth(w)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):               # drag the panel by its header
        if e.position().y() < 62 and self.windowHandle() is not None:
            self.windowHandle().startSystemMove()

def _ov_log(msg):
    """Small debug log next to launcher.json (overlay_debug.log) - helps when the hotkey/panel doesn't show."""
    try:
        path = os.path.join(os.path.dirname(get_launcher_settings_path()), "overlay_debug.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 100000:
            os.remove(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass

class _FriendsOverlay(QObject):
    toggle_req = pyqtSignal()
    def __init__(self):
        super().__init__()
        self.dim = self.panel = None
        self.toggle_req.connect(self.open, Qt.ConnectionType.QueuedConnection)

    _verbose = False      # True when asked via `main.py --friends`: then say why nothing opens

    def _refuse(self, why):
        _ov_log(f"friends panel not opened: {why}")
        if self._verbose and _overlay is not None:
            _overlay.notify_playing("Friends panel", why, None, "")
        self._verbose = False

    def open(self):
        _ov_log("friends panel requested")
        if self.panel is not None and self.panel.isVisible():
            self.close()                 # Shift+Q again closes it
            return
        if get_overlay_mode() == "off":
            return self._refuse("the game overlay is set to Off in Settings")
        if not (_now_playing["name"] or _running):
            return self._refuse("no game started from the launcher is running")   # only while a launcher game runs
        if load_account() is None:
            return self._refuse("you are not logged in")
        if get_overlay_mode() == "inject":
            from overlay import has_injected
            if has_injected():
                return self._refuse("the in-game overlay is active - press Shift+Q inside the game")
        self._verbose = False
        try:
            scr = QApplication.primaryScreen().geometry()
            self.dim = _FriendsDim(scr)
            self.panel = _FriendsPanel()
            self.dim.clicked.connect(self.close)
            self.panel.closed.connect(self.close)
            self.dim.show()
            self.panel.move(scr.center() - self.panel.rect().center())
            self.panel.show()
            self.panel.raise_()
            self.panel.activateWindow()
            _force_foreground(int(self.panel.winId()))
            self.panel.refresh()
            _ov_log(f"friends panel shown (platform {QApplication.platformName()})")
        except Exception:
            import traceback
            _ov_log("friends panel failed:\n" + traceback.format_exc())
            self.close()

    def close(self):
        for w in (self.panel, self.dim):
            if w is not None:
                w.close()
        self.panel = self.dim = None

def _start_shift_q_hook(callback):
    """Windows low-level keyboard hook. Only listens (never blocks keys), so Shift+Q still types normally."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    class KBD(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]
    PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    u.SetWindowsHookExW.restype = ctypes.c_void_p
    u.SetWindowsHookExW.argtypes = [ctypes.c_int, PROC, ctypes.c_void_p, wintypes.DWORD]
    u.CallNextHookEx.restype = ctypes.c_ssize_t
    u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    k.GetModuleHandleW.restype = ctypes.c_void_p
    q_down = [False]                     # ignore key auto-repeat while Q is held
    def proc(n, w, l):
        if n >= 0:
            kb = ctypes.cast(l, ctypes.POINTER(KBD)).contents
            if kb.vkCode == 0x51:
                if w in (0x100, 0x104):
                    if not q_down[0] and (u.GetAsyncKeyState(0x10) & 0x8000):
                        callback()
                    q_down[0] = True
                elif w in (0x101, 0x105):
                    q_down[0] = False
        return u.CallNextHookEx(None, n, w, l)
    cb = PROC(proc)
    def loop():
        u.SetWindowsHookExW(13, cb, k.GetModuleHandleW(None), 0)
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
    threading.Thread(target=loop, daemon=True).start()
    return cb

def _start_shift_q_hook_linux(callback):
    """Linux (X11 / XWayland) Shift+Q listener via pynput (pip install pynput). Only listens, never blocks keys."""
    try:
        from pynput import keyboard
    except Exception as e:
        print("Shift+Q overlay hotkey on Linux needs: pip install pynput")
        _ov_log(f"pynput unavailable: {e!r}")
        return None
    state = {"shift": False, "q": False}
    def on_press(k):
        if not state.get("seen"):
            state["seen"] = True
            _ov_log(f"first key event heard: {k!r}")      # proves the listener receives keys at all
        if k in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
            state["shift"] = True
        elif getattr(k, "char", None) and k.char.lower() == "q":
            if state["shift"] and not state["q"]:
                _ov_log("Shift+Q heard")
                callback()
            state["q"] = True
    def on_release(k):
        if k in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
            state["shift"] = False
        elif getattr(k, "char", None) and k.char.lower() == "q":
            state["q"] = False
    try:
        lst = keyboard.Listener(on_press=on_press, on_release=on_release, daemon=True)
        lst.start()
        _ov_log(f"hotkey listener started (platform {QApplication.platformName()}, "
                f"session {os.environ.get('XDG_SESSION_TYPE', '?')}, DISPLAY={os.environ.get('DISPLAY', '')})")
        def health():
            _ov_log(f"hotkey listener running={getattr(lst, 'running', '?')}, keys heard so far={state.get('seen', False)}")
            if not getattr(lst, "running", True):
                try:
                    lst.join(0.2)             # re-raises whatever killed the listener thread
                except Exception as e:
                    _ov_log(f"hotkey listener died: {e!r}")
        threading.Timer(4.0, health).start()
        return lst
    except Exception as e:
        _ov_log(f"hotkey listener failed: {e!r}")
        return None

class _InjectBridge(QObject):
    """Serves the in-game (injected) friends panel: answers its commands with friends / chat data."""
    def __init__(self):
        super().__init__()
        self.last, self.chat_uid, self._chat_t = None, None, 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    @staticmethod
    def _dirs():
        try:
            from overlay import injected_temp_dirs
            return injected_temp_dirs()       # Wine/Proton: the prefix Temp folder(s) of the injected games
        except Exception:
            import tempfile
            return [tempfile.gettempdir()]

    def _atomic_write(self, name, lines):
        for d in self._dirs():
            path = os.path.join(d, name)
            try:
                with open(path + ".tmp", "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                os.replace(path + ".tmp", path)
            except OSError:
                pass

    @staticmethod
    def _clean(s):
        return " ".join(str(s).replace("\t", " ").split())

    def _bg(self, fn):
        def work():
            try:
                fn()
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _write_friends(self):
        import time
        friends = cloud_fetch_friends() or {}
        rows = sorted(((_fo_status(d), uid, d) for uid, d in friends.items()), key=lambda r: r[0][2])
        lines = [str(time.time_ns())]
        for (status, color, grp), uid, d in rows:
            lines.append("\t".join([uid, self._clean(d.get("username") or "Friend"), self._clean(status),
                                    str(grp), color.lstrip("#")]))
        self._atomic_write("ncz_friends.txt", lines)

    def _write_chat(self, uid):
        import time
        res = cloud_get_messages(uid)
        me, msgs = res[0], res[1]
        lines = [str(time.time_ns()), uid]
        for m in msgs:
            lines.append(("1" if m.get("from") == me else "0") + "\t" + self._clean(chat_preview(m)))
        self._atomic_write("ncz_chat.txt", lines)

    def _load_chat(self, uid):
        import time
        self._chat_t = time.time()
        self._bg(lambda: self._write_chat(uid))

    def _handle(self, cmd):
        if cmd == "open":
            self.chat_uid = None
            self._bg(self._write_friends)
        elif cmd == "close":
            self.chat_uid = None
        elif cmd.startswith("chat "):
            self.chat_uid = cmd[5:].strip()
            self._load_chat(self.chat_uid)
        elif cmd.startswith("send "):
            uid, _, text = cmd[5:].partition(" ")
            def work():
                cloud_send_message(uid, text)
                self._write_chat(uid)
            self._bg(work)

    def poll(self):
        import time
        if get_overlay_mode() != "inject":
            return
        cmd = None
        for d in self._dirs():
            try:
                with open(os.path.join(d, "ncz_cmd.txt"), encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            if len(lines) >= 2:
                try:
                    newer = cmd is None or int(lines[0]) > int(cmd[0])
                except ValueError:
                    newer = cmd is None
                if newer:
                    cmd = (lines[0], lines[1])
        if self.last is None:                  # first tick: ignore leftovers from an earlier session
            self.last = cmd[0] if cmd else ""
            return
        if cmd and cmd[0] != self.last:
            self.last = cmd[0]
            self._handle(cmd[1])
        elif self.chat_uid and time.time() - self._chat_t > 1.5:
            self._load_chat(self.chat_uid)     # keep the open chat fresh

_fo_prev_init = AdaptiveApp.__init__

def _fo_init(self):
    _fo_prev_init(self)
    self._friends_overlay = _FriendsOverlay()
    self._inject_bridge = _InjectBridge()
    if sys.platform.startswith("win"):
        self._kb_hook = _start_shift_q_hook(self._friends_overlay.toggle_req.emit)
    elif sys.platform.startswith("linux"):
        self._kb_hook = _start_shift_q_hook_linux(self._friends_overlay.toggle_req.emit)
        if self._kb_hook is None and _overlay is not None and get_overlay_mode() != "off":
            QTimer.singleShot(4000, lambda: _overlay.notify_playing(
                "Shift+Q overlay", "pip install pynput", None, "needs: "))

AdaptiveApp.__init__ = _fo_init


# ================================================================ controller translator (Windows / XInput)
def _controller_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "controller_settings.json")

def get_controller_enabled():
    try:
        with open(_controller_path(), encoding="utf-8") as f:
            return json.load(f).get("enabled") is True
    except Exception:
        return False

def set_controller_enabled(on):
    try:
        os.makedirs(os.path.dirname(_controller_path()), exist_ok=True)
        with open(_controller_path(), "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(on)}, f)
    except Exception:
        pass

from PyQt6.QtWidgets import QDialog, QSlider, QGridLayout

def _controller_profile_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "controller_profile.json")

class _ControllerRemapDialog(QDialog):
    """Pick the key / mouse button each controller input sends. Saves on every change."""
    STYLE = """
    QDialog{background:#1b1824;}
    QLabel{color:#e8e8ee;font-size:13px;}
    #Head{font-size:18px;font-weight:700;color:#ffffff;}
    #Note{color:#9a93b0;font-size:12px;}
    #Section{color:#a97cff;font-size:11px;font-weight:700;padding-top:10px;}
    QComboBox{background:#282236;color:#ffffff;border:1px solid #342d47;border-radius:8px;padding:5px 10px;min-width:130px;}
    QComboBox:hover{border:1px solid #a97cff;}
    QComboBox QAbstractItemView{background:#231f2e;color:#ffffff;selection-background-color:#a97cff;border:1px solid #342d47;}
    QScrollArea{background:transparent;border:none;}
    #Body{background:transparent;}
    QPushButton{background:#a97cff;color:#ffffff;border:none;border-radius:8px;padding:8px 16px;font-weight:600;}
    QPushButton:hover{background:#b794ff;}
    #Ghost{background:#2d2740;}
    #Ghost:hover{background:#3a3252;}
    QSlider::groove:horizontal{background:#342d47;height:6px;border-radius:3px;}
    QSlider::handle:horizontal{background:#a97cff;width:16px;margin:-5px 0;border-radius:8px;}
    QSlider::sub-page:horizontal{background:#a97cff;border-radius:3px;}
    """
    SECTIONS = [("BUTTONS", ["A", "B", "X", "Y", "LB", "RB", "L3", "R3", "Start", "Back"]),
                ("D-PAD", ["DUp", "DDown", "DLeft", "DRight"]),
                ("TRIGGERS", ["LT", "RT"]),
                ("LEFT STICK", ["LSUp", "LSDown", "LSLeft", "LSRight"])]

    def __init__(self, parent=None):
        super().__init__(parent)
        from controller_input import INPUTS, ACTIONS, DEFAULTS, DEFAULT_SPEED, read_profile, write_profile
        self._defaults, self._default_speed, self._write = DEFAULTS, DEFAULT_SPEED, write_profile
        self.path = _controller_profile_path()
        self.prof = read_profile(self.path)
        self.setWindowTitle("Controller Remap")
        self.resize(440, 600)
        self.setStyleSheet(self.STYLE)
        labels = {i: l for i, l, _m in INPUTS}
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        head = QLabel("Controller Remap")
        head.setObjectName("Head")
        note = QLabel("Choose what each controller input sends to the game. The right stick always moves the mouse.")
        note.setObjectName("Note")
        note.setWordWrap(True)
        root.addWidget(head)
        root.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("Body")
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 8, 0)
        grid.setVerticalSpacing(6)
        self.combos = {}
        r = 0
        for title, ids in self.SECTIONS:
            s = QLabel(title)
            s.setObjectName("Section")
            grid.addWidget(s, r, 0, 1, 2)
            r += 1
            for iid in ids:
                cb = QComboBox()
                cb.setMaxVisibleItems(12)
                cb.addItems(list(ACTIONS))
                cb.setCurrentText(self.prof[iid])
                cb.currentTextChanged.connect(lambda txt, k=iid: self._changed(k, txt))
                self.combos[iid] = cb
                grid.addWidget(QLabel(labels[iid]), r, 0)
                grid.addWidget(cb, r, 1)
                r += 1
        s = QLabel("MOUSE")
        s.setObjectName("Section")
        grid.addWidget(s, r, 0, 1, 2)
        r += 1
        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(2, 40)
        self.speed.setValue(int(self.prof["_mouse_speed"]))
        self.speed_lbl = QLabel(str(self.speed.value()))
        self.speed.valueChanged.connect(self._speed_changed)
        grid.addWidget(QLabel("Right stick speed"), r, 0)
        sr = QHBoxLayout()
        sr.addWidget(self.speed, 1)
        sr.addWidget(self.speed_lbl)
        grid.addLayout(sr, r, 1)
        grid.setColumnStretch(0, 1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        row = QHBoxLayout()
        reset = QPushButton("Reset to defaults")
        reset.setObjectName("Ghost")
        reset.clicked.connect(self._reset)
        done = QPushButton("Done")
        done.clicked.connect(self.accept)
        row.addWidget(reset)
        row.addStretch()
        row.addWidget(done)
        root.addLayout(row)

    def _save(self):
        try:
            self._write(self.path, self.prof)
        except Exception:
            pass

    def _changed(self, iid, txt):
        self.prof[iid] = txt
        self._save()

    def _speed_changed(self, v):
        self.speed_lbl.setText(str(v))
        self.prof["_mouse_speed"] = int(v)
        self._save()

    def _reset(self):
        for iid, cb in self.combos.items():
            cb.setCurrentText(self._defaults[iid])      # triggers _changed -> saves
        self.speed.setValue(self._default_speed)

_ct_prev_init = AdaptiveApp.__init__

def _ct_init(self):
    _ct_prev_init(self)
    try:
        from controller_input import ControllerTranslator, AVAILABLE
        if AVAILABLE:
            self._controller = ControllerTranslator(
                get_controller_enabled, lambda: bool(_now_playing["name"] or _running),
                _controller_profile_path())
            self._controller.start()
    except Exception:
        pass

AdaptiveApp.__init__ = _ct_init

_ct_prev_settings_init = SettingsPage.__init__

def _ct_settings_init(self, *a, **k):
    _ct_prev_settings_init(self, *a, **k)
    if not sys.platform.startswith("win"):
        return
    layout = self.layout()
    panel = QFrame()
    panel.setObjectName("Panel")
    pl = QVBoxLayout(panel)
    pl.setContentsMargins(22, 18, 22, 18)
    row = QHBoxLayout()
    text = QVBoxLayout()
    text.setSpacing(2)
    name = QLabel("Controller Translator")
    name.setObjectName("RowTitle")
    desc = QLabel("Turns an Xbox-style controller into keyboard + mouse while a game is running "
                  "(left stick = WASD, right stick = mouse, triggers = clicks, A = Space...). "
                  "Turn it off for games that already support controllers, or they'll get both.")
    desc.setObjectName("RowDesc")
    desc.setWordWrap(True)
    text.addWidget(name)
    text.addWidget(desc)
    combo = QComboBox()
    combo.setFixedWidth(240)
    combo.addItem("Off", False)
    combo.addItem("On (keyboard + mouse)", True)
    combo.setCurrentIndex(1 if get_controller_enabled() else 0)
    combo.currentIndexChanged.connect(lambda _i: set_controller_enabled(combo.currentData()))
    row.addLayout(text, 1)
    remap = QPushButton("Remap buttons")
    remap.clicked.connect(lambda: _ControllerRemapDialog(self).exec())
    btns = QVBoxLayout()
    btns.addWidget(combo)
    btns.addWidget(remap)
    row.addLayout(btns)
    pl.addLayout(row)
    idx = max(layout.count() - 1, 0)
    layout.insertSpacing(idx, 12)
    layout.insertWidget(idx + 1, panel)

SettingsPage.__init__ = _ct_settings_init


# ================================================================ library sort (A to Z) + Discord Rich Presence
# Library sort: "Custom order" is whatever you dragged the cards into; "A to Z" sorts everything alphabetically
# but the three NCZ games always stay on top. Your dragged order is kept and comes back when you switch back.
_lib_sort_cache = {"mode": None}

def get_library_sort():
    if _lib_sort_cache["mode"] is None:
        _lib_sort_cache["mode"] = load_launcher_settings().get("library_sort", "custom")
    return _lib_sort_cache["mode"]

def set_library_sort(mode):
    mode = mode if mode in ("custom", "az") else "custom"
    _lib_sort_cache["mode"] = mode
    try:
        st = load_launcher_settings()
        st["library_sort"] = mode
        save_launcher_settings(st)
    except Exception:
        pass

_sort_prev_apply_filter = AdaptiveApp.apply_filter

def _sort_apply_filter(self, text=""):
    if get_library_sort() != "az":
        return _sort_prev_apply_filter(self, text)
    # sort in place just for this layout pass, then put the custom order back untouched
    fan = getattr(self, "fan_lib_list", None)
    orig_main = list(self.cards)
    orig_fan = list(fan) if fan is not None else None
    try:
        self.cards[3:] = sorted(self.cards[3:], key=lambda it: it[1].casefold())
        if fan is not None:
            fan.sort(key=lambda it: it[1].casefold())
        return _sort_prev_apply_filter(self, text)
    finally:
        self.cards[:] = orig_main
        if fan is not None:
            fan[:] = orig_fan

AdaptiveApp.apply_filter = _sort_apply_filter

# dragging cards makes no sense while they're sorted A to Z
_sort_prev_card_for = _CardReorder._card_for

def _sort_card_for(self, widget):
    if get_library_sort() == "az":
        return None
    return _sort_prev_card_for(self, widget)

_CardReorder._card_for = _sort_card_for

_sort_prev_init = AdaptiveApp.__init__

def _sort_init(self):
    _sort_prev_init(self)
    try:
        header = self.search_edit.parentWidget().layout().itemAt(0).layout()
        idx = header.indexOf(self.search_edit)
        if idx >= 0:
            combo = QComboBox()
            combo.setFixedHeight(36)
            combo.setToolTip("Library order. A to Z keeps the NCZ games on top.")
            combo.addItem("Custom order", "custom")
            combo.addItem("A to Z", "az")
            combo.setCurrentIndex(1 if get_library_sort() == "az" else 0)

            def changed(_i):
                set_library_sort(combo.currentData())
                self.apply_filter(self.search_edit.text())

            combo.currentIndexChanged.connect(changed)
            header.insertWidget(idx, combo)
            header.insertSpacing(idx + 1, 10)
        self.apply_filter(self.search_edit.text())
    except Exception:
        pass

AdaptiveApp.__init__ = _sort_init


# Discord Rich Presence: shows the game you're playing (and for how long). Nothing is shown while no game is
# running. Needs the "pypresence" package (pip install pypresence); without it this quietly does nothing.
DISCORD_APP_ID = "1228748652974047362"

def get_discord_rpc_enabled():
    return bool(load_launcher_settings().get("discord_rpc", True))

def set_discord_rpc_enabled(on):
    try:
        st = load_launcher_settings()
        st["discord_rpc"] = bool(on)
        save_launcher_settings(st)
    except Exception:
        pass
    _discord_rpc.poke()

# Cover art for Discord. Steam games use their public Steam cover (Discord accepts web links for images).
# The two NCZ games have no public link, so they use art assets you upload in the Discord Developer Portal
# (Rich Presence -> Art Assets) under exactly these names; until you do, they just show without a picture.
_RPC_ASSET_KEYS = {AE_GAME_NAME.casefold(): "fnancz_ae", NCZ2_GAME_NAME.casefold(): "fnancz_2"}
_rpc_image_cache = {}

def _rpc_url_exists(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False

def _rpc_image_for(name):
    key = str(name).casefold()
    if key in _rpc_image_cache:
        return _rpc_image_cache[key]
    image = _RPC_ASSET_KEYS.get(key)
    if image is None:
        try:
            for g in load_steam_library():
                if pretty_game_name(g.get("title", "")).casefold() == key and g.get("appid"):
                    base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{g['appid']}/"
                    for fname in ("library_600x900.jpg", "header.jpg"):
                        if _rpc_url_exists(base + fname):
                            image = base + fname
                            break
                    break
        except Exception:
            image = None
    _rpc_image_cache[key] = image
    return image

class _DiscordRPC:
    """All pypresence calls happen on one background thread, so the launcher never waits on Discord."""
    def __init__(self):
        self._wake = threading.Event()
        self._rpc = None
        self._shown = None            # game currently shown on Discord
        self._since = {}              # game -> when we first saw it running
        self._missing = False         # pypresence isn't installed
        self.status = "Starting..."
        threading.Thread(target=self._loop, daemon=True).start()

    def _log(self, msg):
        self.status = msg
        try:
            path = os.path.join(os.path.dirname(get_launcher_settings_path()), "discord_rpc.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except Exception:
            pass

    def poke(self):
        self._wake.set()

    def _loop(self):
        while True:
            self._wake.wait(timeout=15)     # also retries every 15 s (Discord may have been opened later)
            self._wake.clear()
            try:
                self._sync()
            except Exception as e:
                self._drop()
                self._log(f"Could not reach Discord ({type(e).__name__}: {e}). Is the Discord desktop app open?")

    def _drop(self):
        rpc, self._rpc, self._shown = self._rpc, None, None
        if rpc is not None:
            try:
                rpc.close()
            except Exception:
                pass

    def _connect(self):
        if self._rpc is not None:
            return True
        if self._missing:
            return False
        try:
            from pypresence import Presence
        except Exception as e:
            self._missing = True
            self._log(f"pypresence is not installed for this Python ({sys.executable}): {e}. Run: pip install pypresence")
            return False
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        rpc = Presence(DISCORD_APP_ID, loop=loop)
        rpc.connect()
        self._rpc = rpc
        return True

    def _sync(self):
        name = _now_playing["name"] if get_discord_rpc_enabled() else None
        if name == "NCZFront":          # NCZFront is a website: never shown on Discord
            name = next((n for n in _running if n != "NCZFront"), None)
        for g in list(self._since):
            if g != name:
                self._since.pop(g, None)
        if not name:
            self._drop()                    # no game running -> nothing on Discord
            if self.status.startswith(("Starting", "Showing")) or not self.status.startswith(("pypresence", "Could")):
                self.status = "Off" if not get_discord_rpc_enabled() else "Waiting for a game to start"
            return
        start = self._since.setdefault(name, int(time.time()))
        if not self._connect():
            return
        if self._shown != name:
            # "name" (pypresence 4.6+) makes the title the game's name instead of the application's name
            fields = dict(name=name, details="via NCZ Games Launcher", start=start)
            image = _rpc_image_for(name)
            if image:
                fields.update(large_image=image, large_text=name)
            self._rpc.update(**fields)
            self._shown = name
            self._log(f"Showing '{name}' on Discord")

_discord_rpc = _DiscordRPC()
_playing_listeners.append(_discord_rpc.poke)

_rpc_prev_settings_init = SettingsPage.__init__

def _rpc_settings_init(self, *a, **k):
    _rpc_prev_settings_init(self, *a, **k)
    layout = self.layout()
    panel = QFrame()
    panel.setObjectName("Panel")
    pl = QVBoxLayout(panel)
    pl.setContentsMargins(22, 18, 22, 18)
    row = QHBoxLayout()
    text = QVBoxLayout()
    text.setSpacing(2)
    name = QLabel("Discord Rich Presence")
    name.setObjectName("RowTitle")
    desc = QLabel("Shows the game you're playing from the launcher on your Discord profile. "
                  "Needs Discord running, and the pypresence package installed.")
    desc.setObjectName("RowDesc")
    desc.setWordWrap(True)
    text.addWidget(name)
    text.addWidget(desc)
    status = QLabel()
    status.setObjectName("RowDesc")
    status.setWordWrap(True)
    text.addWidget(status)
    status_timer = QTimer(self)
    status_timer.timeout.connect(lambda: status.setText("Status: " + _discord_rpc.status))
    status_timer.start(1500)
    status.setText("Status: " + _discord_rpc.status)
    combo = QComboBox()
    combo.setFixedWidth(240)
    combo.addItem("Off", False)
    combo.addItem("On", True)
    combo.setCurrentIndex(1 if get_discord_rpc_enabled() else 0)
    combo.currentIndexChanged.connect(lambda _i: set_discord_rpc_enabled(combo.currentData()))
    row.addLayout(text, 1)
    row.addWidget(combo)
    pl.addLayout(row)
    idx = max(layout.count() - 1, 0)
    layout.insertSpacing(idx, 12)
    layout.insertWidget(idx + 1, panel)

SettingsPage.__init__ = _rpc_settings_init


# ================================================================ FNaNCZ AE / 2 native launch on Linux
# They're native Linux games made by the conversion script, started through their run.sh. This looks for run.sh
# anywhere inside the game folder (the script may put it in a subfolder), says so when it can't find it, and
# warns with the game's own output if it quits right away. Output also goes to launch-<game>.log.
def _find_run_sh(game_dir):
    for root, dirs, files in os.walk(game_dir):
        if "run.sh" in files:
            return os.path.join(root, "run.sh")
        if root[len(game_dir):].count(os.sep) >= 2:
            dirs[:] = []
    return None

def _native_linux_launch(app, game_dir, game_id, title):
    if not os.path.isdir(game_dir):
        QMessageBox.warning(app, title, f"{title} isn't installed.\n\nLooked in:\n{game_dir}")
        return
    sh = _find_run_sh(game_dir)
    if not sh:
        try:
            listing = ", ".join(sorted(os.listdir(game_dir))[:15]) or "(empty)"
        except OSError:
            listing = "(couldn't read it)"
        QMessageBox.warning(app, title, f"Couldn't find run.sh for {title}.\n\nLooked in:\n{game_dir}\n\n"
                                        f"The folder contains: {listing}\n\n"
                                        "The Linux conversion probably didn't finish. Try reinstalling the game.")
        return
    log_path = os.path.join(os.path.dirname(get_launcher_settings_path()), f"launch-{game_id}.log")
    try:
        os.chmod(sh, 0o755)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.Popen(["bash", sh] + launch_args_for(game_id), cwd=os.path.dirname(sh),
                                    stdout=log, stderr=subprocess.STDOUT)
    except Exception as e:
        QMessageBox.warning(app, title, f"Couldn't start {title}:\n{e}")
        return

    def check():
        code = proc.poll()
        if code is None or code == 0:
            return
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                tail = "".join(f.readlines()[-12:]).strip()
        except OSError:
            tail = ""
        QMessageBox.warning(app, title, f"{title} closed right after starting (exit code {code}).\n\n"
                                        f"{tail or 'It printed nothing.'}\n\nFull output: {log_path}")

    QTimer.singleShot(4000, check)

_native_prev_launch_ae = AdaptiveApp.launch_ae_game
_native_prev_launch_ncz2 = AdaptiveApp.launch_ncz2_game

def _native_launch_ae(self):
    if sys.platform.startswith("linux"):
        return _native_linux_launch(self, installed_game_dir(AE_GAME_NAME), "ae", AE_GAME_NAME)
    return _native_prev_launch_ae(self)

def _native_launch_ncz2(self):
    if sys.platform.startswith("linux"):
        return _native_linux_launch(self, installed_game_dir(NCZ2_GAME_NAME), "ncz2", NCZ2_GAME_NAME)
    return _native_prev_launch_ncz2(self)

AdaptiveApp.launch_ae_game = _native_launch_ae
AdaptiveApp.launch_ncz2_game = _native_launch_ncz2

# ================================================================ library duplicate-card fix + library saved to account
def _drop_steam_card_widget(self, appid):
    """Removes a store card's widget only (library file and cover stay untouched)."""
    card = self.steam_cards.pop(appid, None)
    if card is None:
        return
    self.cards = [(c, t) for c, t in self.cards if c is not card]
    try:
        card.hide()
        card.setParent(None)
        card.deleteLater()
    except RuntimeError:
        pass

# Adding a game as "existing" after it was added from the Store used to build a second card for the same appid and
# overwrite steam_cards[appid], orphaning the first card (remove then hit the wrong card, the orphan was unremovable).
# Every caller goes through here, so there can only ever be one card per appid.
_lib_prev_create_steam_card = AdaptiveApp.create_steam_card

def _lib_create_steam_card(self, appid, title_text, cover_path):
    if appid in self.steam_cards:
        _drop_steam_card_widget(self, appid)
    _lib_prev_create_steam_card(self, appid, title_text, cover_path)

AdaptiveApp.create_steam_card = _lib_create_steam_card

# ---- library <-> account (users/<uid>/library in the cloud)
_lib_lock = threading.RLock()
_lib_timer = None
_lib_suppress = False       # True while a cloud copy is being applied locally, so that doesn't push straight back
_lib_last_sync = 0.0

def _lib_state_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "library_sync.json")

def _lib_load_state():
    try:
        with open(_lib_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}

def _lib_save_state(state):
    try:
        os.makedirs(os.path.dirname(_lib_state_path()), exist_ok=True)
        with open(_lib_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass

def _lib_url(uid, token):
    base = FIREBASE_DB_URL.strip().rstrip("/")
    return (f"{base}/users/{urllib.parse.quote(uid, safe='')}/library.json"
            f"?auth={urllib.parse.quote(token, safe='')}")

def _lib_local_payload():
    """What goes to the cloud: store games and fan games (paths on this PC stay on this PC)."""
    games = {str(g["appid"]): {"title": g["title"]} for g in load_steam_library()}
    fan = {str(g["id"]): {"title": g["title"], "link": g.get("link", ""), "tags": list(g.get("tags") or [])}
           for g in load_fan_library()}
    return games, fan

def _lib_push_now():
    with _lib_lock:
        token, uid = get_id_token()
        state = _lib_load_state()
        seq = state.get("seq", 0)
        games, fan = _lib_local_payload()
        body = {"updated": {".sv": "timestamp"}, "device": platform.system() or "Unknown",
                "games": games, "fan": fan}
        res = _http_json(_lib_url(uid, token), "PUT", body)
        rev = res.get("updated") if isinstance(res, dict) else None
        state = _lib_load_state()
        state.update(uid=uid, rev=rev)
        if state.get("seq", 0) == seq:
            state["dirty"] = False
        _lib_save_state(state)

def _lib_push_bg():
    try:
        _lib_push_now()
    except Exception:
        pass    # stays marked dirty; retried on the next change, launch or login

def _lib_schedule_push(delay=2.0):
    global _lib_timer
    with _lib_lock:
        if _lib_timer is not None:
            _lib_timer.cancel()
        _lib_timer = threading.Timer(delay, _lib_push_bg)
        _lib_timer.daemon = True
        _lib_timer.start()

def _lib_mark_dirty():
    if _lib_suppress or not cloud_configured():
        return
    acct = load_account()
    if not acct:
        return
    with _lib_lock:
        state = _lib_load_state()
        if state.get("uid") != acct["uid"]:
            state = {"uid": acct["uid"], "rev": None}
        state["dirty"] = True
        state["seq"] = state.get("seq", 0) + 1
        _lib_save_state(state)
    _lib_schedule_push()

# Every add/remove in the app saves through these two, so the account copy follows automatically.
_lib_raw_save_steam_library = save_steam_library
_lib_raw_save_fan_library = save_fan_library

def save_steam_library(games):
    _lib_raw_save_steam_library(games)
    _lib_mark_dirty()

def save_fan_library(games):
    _lib_raw_save_fan_library(games)
    _lib_mark_dirty()

def _lib_download_cover(appid):
    dest = os.path.join(get_library_cover_dir(), f"{appid}.img")
    if os.path.exists(dest):
        return dest
    urls = []
    try:
        data = _http_json(f"{FIREBASE_DB_URL.strip().rstrip('/')}/steam_links/{appid}.json", "GET")
        if isinstance(data, dict) and isinstance(data.get("cover_url"), str):
            urls.append(data["cover_url"])
    except Exception:
        pass
    urls.append(f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg")
    urls.append(f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg")
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                img = resp.read()
            if img:
                os.makedirs(get_library_cover_dir(), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(img)
                return dest
        except Exception:
            continue
    return ""

def _lib_as_dict(value):
    return value if isinstance(value, dict) else {}

def _lib_sync_worker():
    """Background: reconcile this PC's library with the account. Returns what to apply locally, or None."""
    token, uid = get_id_token()
    state = _lib_load_state()
    if state.get("uid") != uid:
        state = {"uid": uid, "rev": None, "dirty": False, "seq": 0}
    cloud = _http_json(_lib_url(uid, token))
    local_games, local_fan = _lib_local_payload()
    if not isinstance(cloud, dict):                      # nothing in this account yet: upload what we have
        if local_games or local_fan:
            _lib_save_state(state)
            _lib_push_now()
        return None
    c_games, c_fan = _lib_as_dict(cloud.get("games")), _lib_as_dict(cloud.get("fan"))
    rev = cloud.get("updated")
    if state.get("rev") is None:
        # first time this PC meets this account: keep everything from both sides
        t_games, t_fan = dict(c_games), dict(c_fan)
        t_games.update(local_games)
        t_fan.update(local_fan)
        push = (t_games != c_games) or (t_fan != c_fan)
    elif state.get("dirty"):
        _lib_save_state(state)
        _lib_push_now()                                  # this PC changed while offline: its copy wins
        return None
    elif rev != state.get("rev"):
        t_games, t_fan, push = dict(c_games), dict(c_fan), False     # changed on another PC: take it
        state.update(rev=rev, dirty=False)
    else:
        return None
    _lib_save_state(state)
    for aid in t_games:
        if aid.isdigit() and aid not in local_games:
            _lib_download_cover(int(aid))
    return {"games": t_games, "fan": t_fan, "push": push}

def _lib_apply(self, result):
    global _lib_suppress
    if not result:
        return
    _lib_suppress = True
    try:
        # store games
        local = {g["appid"]: g for g in load_steam_library()}
        target = {}
        for key, info in _lib_as_dict(result["games"]).items():
            if str(key).isdigit() and isinstance(info, dict) and info.get("title"):
                target[int(key)] = info["title"]
        entries = [local[a] for a in local if a in target]
        for aid, title in target.items():
            if aid not in local:
                cover = os.path.join(get_library_cover_dir(), f"{aid}.img")
                entries.append({"appid": aid, "title": title, "cover": cover if os.path.exists(cover) else ""})
        save_steam_library(entries)
        for aid in [a for a in list(self.steam_cards) if a not in target]:
            _drop_steam_card_widget(self, aid)
        for e in entries:
            if e["appid"] not in self.steam_cards:
                self.create_steam_card(e["appid"], e["title"], e.get("cover", ""))

        # fan games
        flocal = {g["id"]: g for g in load_fan_library()}
        ftarget = {}
        for key, info in _lib_as_dict(result["fan"]).items():
            if str(key).isdigit() and isinstance(info, dict) and info.get("title"):
                ftarget[int(key)] = info
        fentries = [flocal[a] for a in flocal if a in ftarget]
        for gid, info in ftarget.items():
            if gid not in flocal:
                fentries.append({"id": gid, "title": info["title"], "link": info.get("link", ""),
                                 "tags": list(info.get("tags") or []), "cover": ""})
        save_fan_library(fentries)
        fan_cards = getattr(self, "fan_lib_cards", None)
        if fan_cards is not None:
            for gid in [g for g in list(fan_cards) if g not in ftarget]:
                card = fan_cards.pop(gid)
                self.fan_lib_list = [(c, t) for c, t in self.fan_lib_list if c is not card]
                card.hide()
                card.setParent(None)
                card.deleteLater()
            for e in fentries:
                if e["id"] not in fan_cards:
                    self._fan_create_library_card(e)
        self.apply_filter()
        try:
            page = self.store_detail_page
            page.set_in_library(page._appid in self.steam_cards)
        except Exception:
            pass
    finally:
        _lib_suppress = False
    if result.get("push"):
        _lib_mark_dirty()

def _lib_sync_async(self):
    global _lib_last_sync
    if not cloud_configured() or load_account() is None:
        return
    worker = getattr(self, "_lib_worker", None)
    if worker is not None and worker.isRunning():
        return
    if time.time() - _lib_last_sync < 20:
        return
    _lib_last_sync = time.time()
    worker = TaskWorker(_lib_sync_worker)
    worker.done.connect(lambda result: _lib_apply(self, result))
    self._lib_worker = worker
    worker.start()

AdaptiveApp.sync_library = _lib_sync_async

_acl_prev_init = AdaptiveApp.__init__

def _acl_init(self):
    _acl_prev_init(self)
    self.account_page.account_changed.connect(self.sync_library)
    QTimer.singleShot(1500, self.sync_library)

AdaptiveApp.__init__ = _acl_init

# ================================================================ playtime counter (saved to the account)
# Each PC only ever writes its own number (users/<uid>/playtime/<game>/dev/<this PC>), so two PCs never overwrite each
# other; a game's total is the sum over all PCs.
_pt_lock = threading.RLock()
_pt_data = None            # {"device": str, "games": {key: {"title", "own", "others", "last"}}}
_pt_active = {}            # key -> time of the last increment of a running session (for the live counter)
_pt_timer = None
_pt_last_sync = 0.0

def _pt_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "playtime.json")

def _pt_key(title):
    return _hashlib.md5(str(title).strip().lower().encode("utf-8")).hexdigest()[:12]

def _pt_load():
    global _pt_data
    with _pt_lock:
        if _pt_data is None:
            data = {}
            try:
                with open(_pt_path(), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
            if not isinstance(data, dict):
                data = {}
            if not isinstance(data.get("games"), dict):
                data["games"] = {}
            if not isinstance(data.get("device"), str) or not data["device"]:
                data["device"] = os.urandom(6).hex()
            _pt_data = data
        return _pt_data

def _pt_save():
    with _pt_lock:
        try:
            os.makedirs(os.path.dirname(_pt_path()), exist_ok=True)
            tmp = _pt_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_pt_load(), f)
            os.replace(tmp, _pt_path())
        except OSError:
            pass

def _pt_add(title, seconds):
    seconds = int(seconds)
    if seconds <= 0:
        return
    with _pt_lock:
        games = _pt_load()["games"]
        g = games.setdefault(_pt_key(title), {"title": title, "own": 0, "others": 0, "last": 0})
        g["title"] = title
        g["own"] = int(g.get("own", 0)) + seconds
        g["last"] = int(time.time() * 1000)
        _pt_save()
    _pt_schedule_push()

def _pt_total(title):
    key = _pt_key(title)
    with _pt_lock:
        g = _pt_load()["games"].get(key)
        total = (int(g.get("own", 0)) + int(g.get("others", 0))) if g else 0
        if key in _pt_active:
            total += max(0, int(time.time() - _pt_active[key]))
    return total

def _pt_fmt(seconds):
    if seconds < 60:
        return "Not played yet" if seconds <= 0 else "Played: under 1 min"
    minutes = seconds // 60
    if minutes < 60:
        return f"Played: {minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"Played: {hours} h {minutes} min" if hours < 100 else f"Played: {hours} h"

def _pt_session(name, proc):
    """Counts the time a launched game's process is alive. Saved every 30 s so a crash/quit loses almost nothing."""
    key = _pt_key(name)
    def run():
        start = last = time.time()
        _pt_active[key] = last
        while True:
            try:
                proc.wait(timeout=30)
                done = True
            except subprocess.TimeoutExpired:
                done = False
            except Exception:
                done = True
            now = time.time()
            if not done or now - start >= 10:     # ignore launcher stubs / scripts that exit straight away
                _pt_add(name, now - last)
                last = now
                _pt_active[key] = now
            if done:
                break
        _pt_active.pop(key, None)
    threading.Thread(target=run, daemon=True).start()

_pt_prev_track_playing = _track_playing

def _track_playing(name, proc):
    _pt_prev_track_playing(name, proc)
    try:
        _pt_session(pretty_game_name(name), proc)
    except Exception:
        pass

def _pt_url(uid, token):
    base = FIREBASE_DB_URL.strip().rstrip("/")
    return (f"{base}/users/{urllib.parse.quote(uid, safe='')}/playtime.json"
            f"?auth={urllib.parse.quote(token, safe='')}")

def _pt_sync_now():
    """Pull the other PCs' totals, then push this PC's. Raises on cloud errors (callers ignore them)."""
    if not cloud_configured() or load_account() is None:
        return
    token, uid = get_id_token()
    cloud = _http_json(_pt_url(uid, token))
    cloud = cloud if isinstance(cloud, dict) else {}
    with _pt_lock:
        data = _pt_load()
        dev = data["device"]
        games = data["games"]
        for g in games.values():
            g["others"] = 0
        for key, val in cloud.items():
            if not isinstance(val, dict) or not isinstance(val.get("title"), str):
                continue
            devs = val.get("dev") if isinstance(val.get("dev"), dict) else {}
            others = sum(int(v) for d, v in devs.items() if d != dev and isinstance(v, (int, float)))
            mine = int(devs.get(dev, 0)) if isinstance(devs.get(dev, 0), (int, float)) else 0
            g = games.setdefault(key, {"title": val["title"], "own": 0, "others": 0, "last": 0})
            g["others"] = others
            g["own"] = max(int(g.get("own", 0)), mine)
            g["last"] = max(int(g.get("last", 0)), int(val.get("last") or 0))
        _pt_save()
        payload = {}
        for key, g in games.items():
            if int(g.get("own", 0)) > 0:
                payload[f"{key}/title"] = g["title"]
                payload[f"{key}/last"] = int(g.get("last", 0))
                payload[f"{key}/dev/{dev}"] = int(g["own"])
    if payload:
        token, uid = get_id_token()
        _http_json(_pt_url(uid, token), "PATCH", payload)

def _pt_sync_bg():
    try:
        _pt_sync_now()
    except Exception:
        pass

def _pt_schedule_push(delay=20.0):
    global _pt_timer
    if not cloud_configured():
        return
    with _pt_lock:
        if _pt_timer is not None and _pt_timer.is_alive():
            return          # a push is already queued; it will send the latest numbers
        _pt_timer = threading.Timer(delay, _pt_sync_bg)
        _pt_timer.daemon = True
        _pt_timer.start()

def _pt_refresh_cards(self):
    """Gives every game card a 'Played: ...' line under its status and keeps it current."""
    pairs = list(getattr(self, "cards", [])) + list(getattr(self, "fan_lib_list", []))
    for card, title in pairs:
        if title == "NCZFront":         # a website: the launcher can't see how long it's open
            continue
        try:
            label = card.findChild(QLabel, "CardPlaytime")
            if label is None:
                layout = card.layout()
                status = card.findChild(QLabel, "CardStatus")
                idx = layout.indexOf(status) if (layout is not None and status is not None) else -1
                if idx < 0:
                    continue
                label = QLabel(card)
                label.setObjectName("CardPlaytime")
                layout.insertWidget(idx + 1, label)
            text = _pt_fmt(_pt_total(title))
            if label.text() != text:
                label.setText(text)
        except RuntimeError:
            continue

def _pt_sync_async(self):
    global _pt_last_sync
    if time.time() - _pt_last_sync < 20:
        return
    _pt_last_sync = time.time()
    threading.Thread(target=_pt_sync_bg, daemon=True).start()

AdaptiveApp.sync_playtime = _pt_sync_async

_pt_prev_init = AdaptiveApp.__init__

def _pt_init(self):
    _pt_prev_init(self)
    self._pt_ui_timer = QTimer(self)
    self._pt_ui_timer.setInterval(3000)
    self._pt_ui_timer.timeout.connect(lambda: _pt_refresh_cards(self))
    self._pt_ui_timer.start()
    QTimer.singleShot(2500, lambda: _pt_refresh_cards(self))
    self.account_page.account_changed.connect(self.sync_playtime)
    QTimer.singleShot(3000, self.sync_playtime)

AdaptiveApp.__init__ = _pt_init

if __name__ == "__main__":
    main()
