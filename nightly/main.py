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
    QFileDialog, QInputDialog, QTextEdit
)
from PyQt6.QtCore import QSize, Qt, QRectF, QThread, QUrl, QBuffer, QIODevice, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QPainterPath, QPalette, QColor, QIcon, QFont, QDesktopServices, QImage

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
GAMES_DIR = os.path.join(SCRIPT_DIR, "games")
DOWNLOAD_DIR = SCRIPT_DIR + "/games/"
AE_GAME_NAME = "Five Nights at NCZ AE"
NCZ2_GAME_NAME = "Five Nights at NCZ 2"
NCZFRONT_URL = "https://desktopsob7i.tail441aca.ts.net/"

FIREBASE_API_KEY = "AIzaSyBEphV3IipXeUUIpgP6XYrtPG3RrZ-wPt4"
FIREBASE_DB_URL = "https://ncz-games-launcher-default-rtdb.europe-west1.firebasedatabase.app"

def asset_path(filename):
    return os.path.join(ASSETS_DIR, filename)

def installed_game_dir(game_name):
    return os.path.join(GAMES_DIR, game_name)

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
    return os.path.join(GAMES_DIR, f"{game_name} (Wine)")

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

def get_wine_d3d_mode():
    mode = load_launcher_settings().get("wine_d3d", "default")
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

def launch_wine(wine_path, exe, cwd):
    """Starts a Windows exe under wine with the chosen Direct3D renderer.
    For DXVK/VKD3D, the first launch installs them into the prefix with winetricks (if it's installed)."""
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
                subprocess.Popen([wine_path, exe], cwd=cwd, env=env)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()
        return
    subprocess.Popen([wine_path, exe], cwd=cwd, env=env)

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
    settings = {"dark_mode": "system", "steamgriddb_api_key": "", "currency": "EGP", "wine_d3d": "default"}
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
                if loaded.get("currency") in ("EGP", "QAR", "USD", "SAR"):
                    settings["currency"] = loaded["currency"]
                key = loaded.get("steamgriddb_api_key", "")
                if isinstance(key, str):
                    settings["steamgriddb_api_key"] = key.strip()
        except Exception:
            pass
    return settings

def save_launcher_settings(settings):
    path = get_launcher_settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=4)

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

def _http_json(url, method="GET", payload=None, form=None):
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
        with urllib.request.urlopen(req, timeout=CLOUD_TIMEOUT) as resp:
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
    "dark": dict(
        bg="#121214", sidebar="#1a1a1e", panel="#202024", input="#121214", border="#2e2e33",
        text="#f5f5f7", subtext="#9a9aa3", accent="#10eb73", accent_hover="#40ef8f", accent_press="#0dbc5c",
        button="#2c2c31", button_hover="#3a3a40", disabled_bg="#26262a", disabled_text="#66666d",
        handle="#3a3a40", hover="#25252a",
    ),
    "light": dict(
        bg="#f3f3f5", sidebar="#ffffff", panel="#ffffff", input="#f3f3f5", border="#d9d9de",
        text="#18181b", subtext="#6b6b73", accent="#10eb73", accent_hover="#40ef8f", accent_press="#0dbc5c",
        button="#e6e6ea", button_hover="#d9d9df", disabled_bg="#ececef", disabled_text="#a4a4ab",
        handle="#c4c4cb", hover="#ebebef",
    ),
}

STYLE_TEMPLATE = Template("""
QMainWindow, QWidget#Content, QWidget#GridContainer { background: $bg; }
QDialog, QMessageBox { background: $panel; }
QLabel { color: $text; background: transparent; }
QToolTip { background: $panel; color: $text; border: 1px solid $border; padding: 4px 6px; }

QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget#qt_scrollarea_viewport { background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }

/* ---- sidebar ---- */
QFrame#Sidebar { background: $sidebar; border-right: 1px solid $border; }
QLabel#Brand { font-size: 17px; font-weight: bold; color: $text; }
QPushButton#NavButton {
    background: transparent; color: $subtext; border: none; border-left: 3px solid transparent;
    border-radius: 0; text-align: left; padding: 13px 22px; font-size: 14px; font-weight: 600;
}
QPushButton#NavButton:hover { background: $hover; color: $text; }
QPushButton#NavButton:checked { background: $hover; color: $text; border-left: 3px solid $accent; }
QPushButton#ProfileButton { background: transparent; border: none; border-radius: 8px; padding: 0; text-align: left; }
QPushButton#ProfileButton:hover, QPushButton#ProfileButton:checked { background: $hover; }
QLabel#Avatar { background: $accent; color: #ffffff; border-radius: 18px; font-size: 15px; font-weight: bold; }
QLabel#AvatarLarge { background: $accent; color: #ffffff; border-radius: 40px; font-size: 32px; font-weight: bold; }
QLabel#ProfileName { font-size: 13px; font-weight: 600; }
QLabel#ProfileSub { color: $subtext; font-size: 11px; }

/* ---- library page ---- */
QLabel#PageTitle { font-size: 28px; font-weight: bold; }
QLabel#PageCount { color: $subtext; font-size: 13px; padding-left: 12px; padding-bottom: 6px; }
QLabel#EmptyState { color: $subtext; font-size: 14px; padding: 40px; }
QLineEdit#Search { background: $panel; border: 1px solid $border; border-radius: 16px; padding: 0px 16px; font-size: 13px; }
QLineEdit#Search:focus { border: 1px solid $accent; }

/* ---- settings / credits ---- */
QFrame#Panel { background: $panel; border: 1px solid $border; border-radius: 12px; }
QLabel#RowTitle { font-size: 14px; font-weight: 600; }
QLabel#RowDesc { color: $subtext; font-size: 12px; }

/* ---- game cards ---- */
QFrame#GameCard { background: transparent; border: 1px solid transparent; border-radius: 12px; }
QFrame#GameCard:hover { background: $panel; border: 1px solid $border; }
QLabel#CardTitle { font-size: 14px; font-weight: 600; padding-left: 2px; }
QLabel#CardStatus { color: $subtext; font-size: 12px; padding-left: 2px; }
QLabel#CoverPlaceholder { background: $panel; border: 1px dashed $border; border-radius: 8px; color: $subtext; }

/* ---- buttons ---- */
QPushButton {
    background: $button; color: $text; border: none; border-radius: 4px;
    padding: 8px 18px; font-weight: 600;
}
QPushButton:hover, QPushButton:pressed { background: $button_hover; }
QPushButton:disabled { background: $disabled_bg; color: $disabled_text; }
QPushButton:flat { background: transparent; color: $accent; padding: 4px; }
QPushButton:flat:hover { background: transparent; color: $accent_hover; }
QPushButton#Primary { background: $accent; color: #ffffff; }
QPushButton#Primary:hover { background: $accent_hover; }
QPushButton#Primary:pressed { background: $accent_press; }
QPushButton#Primary:disabled { background: $disabled_bg; color: $disabled_text; }
QPushButton#MoreButton { padding: 0; font-size: 11px; }
QPushButton#MoreButton::menu-indicator { image: none; width: 0px; }

/* ---- inputs ---- */
QLineEdit {
    background: $input; color: $text; border: 1px solid $border; border-radius: 4px;
    padding: 8px 10px; selection-background-color: $accent; selection-color: #ffffff;
}
QLineEdit:focus { border: 1px solid $accent; }
QComboBox { background: $input; color: $text; border: 1px solid $border; border-radius: 4px; padding: 6px 10px; }
QComboBox:hover { border: 1px solid $accent; }
QComboBox QAbstractItemView {
    background: $panel; color: $text; border: 1px solid $border; outline: none;
    selection-background-color: $accent; selection-color: #ffffff;
}

/* ---- menus ---- */
QMenu { background: $panel; color: $text; border: 1px solid $border; padding: 6px; }
QMenu::item { padding: 9px 30px 9px 14px; border-radius: 4px; background: transparent; }
QMenu::item:selected { background: $hover; }
QMenu::item:disabled { color: $disabled_text; }
QMenu::separator { height: 1px; background: $border; margin: 6px 8px; }

/* ---- progress + scrollbars ---- */
QProgressBar { background: $button; border: none; border-radius: 3px; min-height: 6px; max-height: 6px; }
QProgressBar::chunk { background: $accent; border-radius: 3px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: $handle; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: $handle; border-radius: 5px; min-width: 30px; }
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

def build_app_font():
    font = QFont()
    font.setFamilies(["Segoe UI", "Noto Sans", "Helvetica Neue", "Arial"])
    font.setPointSize(10)
    return font

def system_prefers_dark():
    app = QApplication.instance()
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        window_color = app.palette().color(QPalette.ColorRole.Window)
        return window_color.lightness() < 128

def apply_dark_mode(mode):
    app = QApplication.instance()
    if app is None:
        return
    app.setStyle("Fusion")
    use_dark = mode == "on" or (mode == "system" and system_prefers_dark())
    theme = THEMES["dark" if use_dark else "light"]
    app.setPalette(build_palette(theme))
    app.setFont(build_app_font())
    app.setStyleSheet(STYLE_TEMPLATE.substitute(theme))

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
                req = urllib.request.Request(self.version_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
                    content = resp.read().decode('utf-8')
                self.remote_version = parse_remote_version(content)

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
                    mf_req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(mf_req, timeout=NET_TIMEOUT) as mf_resp:
                        mf_html = mf_resp.read().decode('utf-8')
                    match = re.search(r'href="(https?://download[^"]+)"', mf_html)
                    if match:
                        download_url = match.group(1)

                dl_req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(dl_req, timeout=NET_TIMEOUT) as response:
                    # The server picks the name; strip path separators so it can't land outside DOWNLOAD_DIR.
                    file_name = safe_filename_from_headers(response, self.game_name)
                    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                    dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
                    stream_download(response, dest_path, self.progress.emit)

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

class SettingsPage(QWidget):
    steamgriddb_key_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._key_worker = None
        QApplication.instance().aboutToQuit.connect(self._wait_for_key_worker)
        self.setObjectName("Content")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 28, 36, 0)
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

    def on_wine_d3d_changed(self):
        settings = load_launcher_settings()
        settings["wine_d3d"] = self.d3d_combo.currentData()
        save_launcher_settings(settings)

    def on_currency_changed(self):
        settings = load_launcher_settings()
        settings["currency"] = self.currency_combo.currentData()
        save_launcher_settings(settings)

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

def fetch_steam_games(term, start, count=STORE_PAGE_SIZE):
    """One page of Steam's own game list (games only, no DLC/software). Returns (games, total_count)."""
    params = {"query": "", "term": term, "start": start, "count": count, "dynamic_data": "",
              "sort_by": "_ASC", "category1": 998, "l": "english", "infinite": 1}
    url = STEAM_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    data = json.loads(_http_get(url).decode("utf-8", errors="replace"))
    return parse_steam_results(data.get("results_html", "")), int(data.get("total_count") or 0)

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
              "USD": "US Dollar (USD)", "SAR": "Saudi Riyal (SAR)"}
FALLBACK_RATES = {"USD": 1.0, "EGP": 48.0, "QAR": 3.64, "SAR": 3.75}
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

def format_price(usd, currency):
    if currency == "USD":
        return f"${usd:,.2f}"
    amount = usd * get_usd_rates().get(currency, FALLBACK_RATES[currency])
    text = f"{amount:,.0f}" if currency == "EGP" else f"{amount:,.2f}"
    return f"~{text} {currency}"

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
    return dict(
        description=html_lib.unescape(re.sub(r"<[^>]+>", " ", text)).strip(),
        developers=", ".join(info.get("developers") or []),
        release=(info.get("release_date") or {}).get("date", ""),
        genres=[g.get("description", "") for g in (info.get("genres") or [])][:5],
        price=price,
        shots=shots,
    )

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

class StoreDetailPage(QWidget):
    back_requested = pyqtSignal()
    add_requested = pyqtSignal(int, str, str)   # appid, title, cover path
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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
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

        back = QPushButton("< Back to Store")
        back.setFlat(True)
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(self.back_requested.emit)
        layout.addWidget(back, 0, Qt.AlignmentFlag.AlignLeft)
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
        col.addWidget(self.price_label)
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

    def show_game(self, appid, title, cover_path):
        self._appid = appid
        self._title = title
        self._cover_path = cover_path or ""
        self.title_label.setText(title)
        self.meta_label.setText("")
        self.price_label.setText("")
        self.desc_label.setText("Loading details...")
        self._clear(self.chips_row)
        self._clear(self.shots_row)
        self._set_extras_visible(False)
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
                return (appid, fetch_steam_details(appid, currency))
            except Exception:
                return (appid, None)

        worker = TaskWorker(work)
        worker.done.connect(self._on_details)
        self._workers = [w for w in self._workers if w.isRunning()] + [worker]
        worker.start()

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
        header.addWidget(self.search_edit)
        layout.addLayout(header)
        layout.addSpacing(10)

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

        def work():
            try:
                games, total = fetch_steam_games(term, start)
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
        self.resize(1040, 640)
        self.setMinimumSize(QSize(760, 640))
        self.setWindowTitle("NCZ Games Launcher")

        icon_path = asset_path("icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.center_on_screen()

        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
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
        root_layout.addWidget(self.pages, 1)

        self.apply_filter()
        self.refresh_profile()
        self.account_page.sync_profile_async()
        self.check_nczfront_status()
        QApplication.instance().aboutToQuit.connect(self._wait_for_version_workers)
        self.check_for_updates()

    # ------------------------------------------------------------ sidebar
    def make_nav_button(self, text, checkable=False):
        btn = QPushButton(text)
        btn.setObjectName("NavButton")
        btn.setCheckable(checkable)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    def build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(230)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 24, 0, 16)
        layout.setSpacing(2)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(22, 0, 22, 0)
        brand_row.setSpacing(10)
        logo_pix = CreditsPage.circular_pixmap(asset_path("icon.png"), 64)
        if logo_pix:
            logo_pix.setDevicePixelRatio(2.0)
            logo = QLabel()
            logo.setFixedSize(32, 32)
            logo.setPixmap(logo_pix)
            brand_row.addWidget(logo)
        brand = QLabel("NCZ Games")
        brand.setObjectName("Brand")
        brand_row.addWidget(brand)
        brand_row.addStretch()
        layout.addLayout(brand_row)
        layout.addSpacing(28)

        self.nav_group = QButtonGroup(self)
        for index, label in ((0, "Library"), (4, "Store"), (1, "Settings"), (2, "Credits")):
            btn = self.make_nav_button(label, checkable=True)
            self.nav_group.addButton(btn, index)
            layout.addWidget(btn)
        self.nav_group.button(0).setChecked(True)
        self.nav_group.idClicked.connect(self.show_page)
        layout.addStretch()

        profile_wrap = QHBoxLayout()
        profile_wrap.setContentsMargins(12, 0, 12, 0)
        profile_wrap.addWidget(self.build_profile_button())
        layout.addLayout(profile_wrap)
        return sidebar

    def build_profile_button(self):
        btn = QPushButton()
        btn.setObjectName("ProfileButton")
        btn.setFixedHeight(56)
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
        # window width minus the sidebar (230) and the library page / grid margins (36 + 24 + 12)
        available = self.width() - 230 - 36 - 24 - 12
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
                subprocess.Popen([exe_path], cwd=game_dir)
        elif sys.platform.startswith("linux"):
            sh_path = os.path.join(game_dir, "run.sh")
            if os.path.exists(sh_path):
                os.chmod(sh_path, 0o755)
                subprocess.Popen(["bash", sh_path], cwd=game_dir)

    def launch_with_wine(self, game_id):
        if not self.wine:
            return
        wine_path, _version = self.wine
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

        try:
            launch_wine(wine_path, exe_path, os.path.dirname(exe_path))
        except Exception as e:
            QMessageBox.warning(self, "Launch using wine", f"Couldn't start wine: {e}")

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
                subprocess.Popen([exe_path], cwd=game_dir)
        elif sys.platform.startswith("linux"):
            sh_path = os.path.join(game_dir, "run.sh")
            if os.path.exists(sh_path):
                os.chmod(sh_path, 0o755)
                subprocess.Popen(["bash", sh_path], cwd=game_dir)

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
            if self.wine:
                btn_wine = menu.addAction(f"Launch using wine ({self.wine[1]})")
                btn_wine.setToolTip("Extract the Windows build (no Linux conversion) and run it with wine")
                btn_wine.triggered.connect(lambda _checked=False, g=game_id: self.launch_with_wine(g))
            btn_open_location = menu.addAction("Open Game Location")
            btn_open_location.triggered.connect(lambda _checked=False, n=game_name: self.open_game_location(n))
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

    def center_on_screen(self):
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

def main():
    import traceback
    sys.excepthook = lambda *a: traceback.print_exception(*a)  # PyQt6 aborts on unhandled slot errors otherwise
    app = QApplication(sys.argv)
    app.setApplicationName("NCZ Games Launcher")

    icon_path = asset_path("icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    apply_dark_mode(load_launcher_settings().get("dark_mode", "system"))

    def on_system_color_scheme_changed(_scheme=None):
        if load_launcher_settings().get("dark_mode") == "system":
            apply_dark_mode("system")

    try:
        app.styleHints().colorSchemeChanged.connect(on_system_color_scheme_changed)
    except Exception:
        pass

    window = AdaptiveApp()
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
        self.remove_steam_game(appid)
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
                            subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
                        except Exception as e:
                            QMessageBox.warning(self, "Launch Game", f"Couldn't launch game: {e}")
                    elif sys.platform.startswith("linux"):
                        if self.wine:
                            wine_path, _ = self.wine
                            try:
                                launch_wine(wine_path, exe_path, os.path.dirname(exe_path))
                            except Exception as e:
                                QMessageBox.warning(self, "Launch using wine", f"Couldn't start wine: {e}")
                        else:
                            QMessageBox.warning(self, "Wine Required", "Wine was not found on your system to run this Windows game.")
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

def fetch_steam_title_and_base(appid):
    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english"
        data = json.loads(_http_get(url).decode("utf-8", errors="replace"))
        info = (data.get(str(appid)) or {}).get("data") or {}
        name = info.get("name")
        header_image = info.get("header_image", "")
        img_base = ""
        if header_image:
            img_base = header_image.split("?")[0].rsplit("/", 1)[0]
        return html_lib.unescape(name) if name else f"Game {appid}", img_base
    except Exception:
        pass
    return f"Game {appid}", ""

def fetch_activated_games():
    if not FIREBASE_DB_URL:
        return []
    base = FIREBASE_DB_URL.strip().rstrip("/")
    url = f"{base}/steam_links.json"
    try:
        data = _http_json(url, "GET")
        if isinstance(data, dict):
            res = []
            for k, v in data.items():
                try:
                    appid = int(k)
                except Exception:
                    continue
                if isinstance(v, dict):
                    link = v.get("link") or v.get("url") or v.get("download_link")
                    if isinstance(link, str) and link.strip():
                        title = v.get("title")
                        cover_url = v.get("cover_url") or ""
                        res.append((appid, title, cover_url))
                elif isinstance(v, str) and v.strip():
                    res.append((appid, None, ""))
            return res
    except Exception:
        pass
    return []

_original_store_page_init = StorePage.__init__

def _patched_store_page_init(self, parent=None):
    _original_store_page_init(self, parent)
    self._showing_activated = False
    main_layout = self.layout()
    if main_layout and main_layout.count() > 0:
        header = main_layout.itemAt(0).layout()
        if isinstance(header, QHBoxLayout):
            self.activated_btn = QPushButton("Activated Games")
            self.activated_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.activated_btn.setFixedHeight(36)
            self.activated_btn.clicked.connect(self._toggle_activated_games)
            header.insertWidget(2, self.activated_btn)

StorePage.__init__ = _patched_store_page_init

def _toggle_activated_games(self):
    self._showing_activated = not self._showing_activated
    if self._showing_activated:
        self.activated_btn.setObjectName("Primary")
        self.activated_btn.style().polish(self.activated_btn)
        self.search_edit.setEnabled(False)
        self._load_activated_games()
    else:
        self.activated_btn.setObjectName("")
        self.activated_btn.style().polish(self.activated_btn)
        self.search_edit.setEnabled(True)
        self._start_search()

StorePage._toggle_activated_games = _toggle_activated_games

def _load_activated_games(self):
    self._request_id += 1
    self.loader.cancel_pending()
    self._clear_cards()
    self._exhausted = True
    self._loading = False
    self.retry_btn.hide()
    self.count_label.setText("")
    self._set_status("Loading activated games...")
    req_id = self._request_id

    def work():
        try:
            raw_list = fetch_activated_games()
            games = []
            for appid, title, cover_url in raw_list:
                img_base = ""
                if not title:
                    title, img_base = fetch_steam_title_and_base(appid)
                
                cover_path = ""
                if cover_url:
                    try:
                        req = urllib.request.Request(cover_url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            img_data = resp.read()
                        os.makedirs(get_library_cover_dir(), exist_ok=True)
                        local_path = os.path.join(get_library_cover_dir(), f"{appid}.img")
                        with open(local_path, 'wb') as f:
                            f.write(img_data)
                        cover_path = local_path
                    except Exception:
                        pass
                games.append((appid, title, cover_path, img_base))
            return (req_id, games, "")
        except Exception as e:
            return (req_id, [], str(e))

    worker = TaskWorker(work)
    worker.done.connect(self._on_activated_loaded)
    self._workers = [w for w in self._workers if w.isRunning()] + [worker]
    worker.start()

StorePage._load_activated_games = _load_activated_games

def _on_activated_loaded(self, result):
    req_id, games, error = result
    if req_id != self._request_id:
        return
    if error:
        self._set_status("Couldn't load activated games.")
        return
    for appid, title, cover_path, img_base in games:
        card = StoreCard(appid, title)
        card.clicked.connect(self.game_selected.emit)
        
        if cover_path and os.path.exists(cover_path):
            card.set_cover(cover_path)
        else:
            self.loader.request(appid, img_base, title)

        index = len(self._cards)
        cols = self._columns()
        self._cols = cols
        self._cards.append(card)
        self._card_by_id[appid] = card
        self.grid.addWidget(card, index // cols, index % cols)
        
    self._set_status("" if self._cards else "No activated games found.")
    self.count_label.setText(f"{len(games)} activated")

StorePage._on_activated_loaded = _on_activated_loaded

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
                subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
            except Exception as e:
                QMessageBox.warning(self, "Launch Game", f"Couldn't launch game: {e}")
        elif sys.platform.startswith("linux"):
            if compat_tool and os.path.exists(compat_tool):
                compat_data_path = os.path.dirname(exe_path) + "_compat_data"
                os.makedirs(compat_data_path, exist_ok=True)
                env = os.environ.copy()
                env["STEAM_COMPAT_DATA_PATH"] = compat_data_path
                env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = os.path.expanduser("~/.local/share/Steam")
                apply_proton_d3d(env)
                try:
                    subprocess.Popen([compat_tool, "run", exe_path], cwd=os.path.dirname(exe_path), env=env)
                except Exception as e:
                    QMessageBox.warning(self, "Launch Error", f"Couldn't start Proton: {e}")
            elif exe_path.lower().endswith(('.sh', '.py')):
                try:
                    subprocess.Popen(["bash" if exe_path.endswith('.sh') else "python3", exe_path], cwd=os.path.dirname(exe_path))
                except Exception as e:
                    QMessageBox.warning(self, "Launch Error", f"Couldn't start script: {e}")
            elif self.wine:
                wine_path, _ = self.wine
                try:
                    launch_wine(wine_path, exe_path, os.path.dirname(exe_path))
                except Exception as e:
                    QMessageBox.warning(self, "Launch using wine", f"Couldn't start wine: {e}")
            else:
                try:
                    subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
                except Exception as e:
                    QMessageBox.warning(self, "Launch Error", f"Couldn't launch executable: {e}")

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

    menu.addAction("Remove from Library").triggered.connect(remove_custom_game)
    menu.addAction("Open Game Location").triggered.connect(lambda: self.open_game_location(os.path.dirname(exe_path)))

    actions.addWidget(btn_play, 1)
    actions.addWidget(more_btn)
    layout.addLayout(actions)

    if not hasattr(self, "custom_cards"):
        self.custom_cards = {}
    self.custom_cards[game_id] = card
    self.cards.append((card, title_text))

AdaptiveApp.create_custom_card = _patched_create_custom_card

class CompatToolDialog(QDialog):
    def __init__(self, current_tool, tools_dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Switch Compatibility Tool")
        self.setFixedSize(350, 150)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        
        layout.addWidget(QLabel("Select Proton / Compatibility Tool:"))
        
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

        self.status_update.emit("Connecting...")
        req = urllib.request.Request(self.download_url, headers={'User-Agent': 'Mozilla/5.0'})
        
        with urllib.request.urlopen(req, timeout=20) as response:
            self._response = response
            if self._is_cancelled:
                self.failed.emit("CANCELLED")
                return

            ext = ".rar" if self.download_url.lower().endswith('.rar') else ".zip"
            file_name = safe_filename_from_headers(response, self.title, ext)

            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
            
            if total_size <= 0:
                total_size = int(response.headers.get('Content-Length', 0))

            downloaded = 0
            block_size = 8192
            start_time = time.time()

            with open(dest_path, 'wb') as f:
                while not self._is_cancelled:
                    buffer = response.read(block_size)
                    if not buffer:
                        break
                    downloaded += len(buffer)
                    f.write(buffer)
                    elapsed = time.time() - start_time
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    self.progress.emit(downloaded, total_size, speed)

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

def pw_game_url(title):
    t = title.lower().replace("'", "")
    plain = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t)).strip()
    # special cases
    if re.search(r"\bdying light (2|two)\b", plain):          # any edition of Dying Light 2
        return PW_BASE_URL.rstrip("/") + "/dying-l-two-stay-human"
    if re.fullmatch(r"dying light( the following)?( enhanced edition)?", plain):
        return PW_BASE_URL.rstrip("/") + "/dying-light-the-following"
    if re.search(r"\brisk of rain (2|two)\b", plain):
        return PW_BASE_URL.rstrip("/") + "/risk-of-rain-two"
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
    state = {"closed": 0, "blocked": []}  # extra tabs closed so far, and the blocked URLs
    def watch(pg):
        if pg is not page:
            pg.on("close", lambda _p=None: state.__setitem__("closed", state["closed"] + 1))
        def on_nav(frame):
            if frame != pg.main_frame or _pw_host_ok(frame.url, allowed):
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
        if pg is not page and not _pw_host_ok(pg.url, allowed):  # popup that was already on a bad site
            state["blocked"].append(pg.url)
            try:
                pg.close()
            except Exception:
                pass
    watch(page)
    context.on("page", watch)
    return state

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
                    if not tab.is_closed():
                        tab.close()
                except RuntimeError:
                    raise
                except Exception:
                    page.wait_for_timeout(1000)
                if any(h in u for u in guard["blocked"][blocked_before:] for h in PW_BLOCKED_HOSTS) or \
                        (tab is not None and any(h in (tab.url or "") for h in PW_BLOCKED_HOSTS)):
                    self.status_update.emit("Got a blocked site, trying a different DOWNLOAD HERE button...")
                    bad.add(idx)
            if not target:
                raise RuntimeError("Couldn't reach a valid download link.")

            self.status_update.emit("Getting file link...")
            if "gofile.io" in target.url or "pixeldrain.com" in target.url:
                btn = target.get_by_role("button", name=re.compile("Download", re.IGNORECASE)).first
            else:
                btn = target.get_by_text("Download File", exact=False).first
            btn.wait_for(state="visible", timeout=30000)
            if "bzzhr.to" in target.url:
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

def _fb_stream(self, url, headers):
    total_size = self.expected_size
    self.status_update.emit("Connecting...")
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        self._response = response
        if self._is_cancelled:
            raise RuntimeError("CANCELLED")
        ext = ".rar" if url.lower().split("?")[0].endswith(".rar") else ".zip"
        file_name = safe_filename_from_headers(response, self.title, ext)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        if total_size <= 0:
            total_size = int(response.headers.get('Content-Length', 0) or 0)
        downloaded, start_time = 0, time.time()
        with open(dest_path, 'wb') as f:
            while not self._is_cancelled:
                buf = response.read(8192)
                if not buf:
                    break
                downloaded += len(buf)
                f.write(buf)
                elapsed = time.time() - start_time
                self.progress.emit(downloaded, total_size, downloaded / elapsed if elapsed > 0 else 0)
    return dest_path

def _fb_pw_run(self):
    if _pw_is_ncz(self.title):
        return _fb_direct_run(self)
    dest_path = self.existing_zip_path if self.existing_zip_path and os.path.exists(self.existing_zip_path) else None
    fresh = False
    try:
        if not dest_path:
            url, headers = _pw_find_link(self, pw_game_url(self.title))
            self.download_url = url  # bridged: the copied link becomes this download's link
            dest_path = _fb_stream(self, url, headers)
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
            self.status_update.emit("Checking version...")
            req = urllib.request.Request(self.version_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=20) as resp:
                content = resp.read().decode('utf-8')
            self.remote_version = parse_remote_version(content)

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
                self.status_update.emit("Resolving MediaFire link...")
                mf_req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(mf_req, timeout=20) as mf_resp:
                    mf_html = mf_resp.read().decode('utf-8')
                match = re.search(r'href="(https?://download[^"]+)"', mf_html)
                if match:
                    download_url = match.group(1)

            if self._is_cancelled:
                self.failed.emit("CANCELLED")
                return

            self.status_update.emit("Connecting...")
            dl_req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
            
            with urllib.request.urlopen(dl_req, timeout=20) as response:
                self._response = response
                if self._is_cancelled:
                    self.failed.emit("CANCELLED")
                    return

                # The server picks the name; strip path separators so it can't land outside DOWNLOAD_DIR.
                file_name = safe_filename_from_headers(response, self.title)
                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
                stream_download(response, dest_path, self.progress.emit, lambda: self._is_cancelled)

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
                        subprocess.Popen([custom_exe], cwd=os.path.dirname(custom_exe))
                    except Exception as e:
                        QMessageBox.warning(self, "Launch Game", f"Couldn't launch game: {e}")
                elif sys.platform.startswith("linux"):
                    compat_config = load_compat_config()
                    proton_bin = compat_config.get(str(appid), "")
                    if proton_bin and os.path.exists(proton_bin):
                        compat_data_path = os.path.dirname(custom_exe) + "_compat_data"
                        os.makedirs(compat_data_path, exist_ok=True)
                        env = os.environ.copy()
                        env["STEAM_COMPAT_DATA_PATH"] = compat_data_path
                        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = os.path.expanduser("~/.local/share/Steam")
                        apply_proton_d3d(env)
                        try:
                            subprocess.Popen([proton_bin, "run", custom_exe], cwd=os.path.dirname(custom_exe), env=env)
                        except Exception as e:
                            QMessageBox.warning(self, "Launch Error", f"Couldn't start Proton: {e}")
                    elif self.wine:
                        wine_path, _ = self.wine
                        try:
                            launch_wine(wine_path, custom_exe, os.path.dirname(custom_exe))
                        except Exception as e:
                            QMessageBox.warning(self, "Launch using wine", f"Couldn't start wine: {e}")
                    else:
                        try:
                            subprocess.Popen([custom_exe], cwd=os.path.dirname(custom_exe))
                        except Exception as e:
                            QMessageBox.warning(self, "Launch Error", f"Couldn't launch executable: {e}")

            btn_play.clicked.connect(launch_existing_game)

        if sys.platform.startswith("linux"):
            more_btn = card.findChild(QPushButton, "MoreButton")
            if more_btn and more_btn.menu():
                menu = more_btn.menu()
                has_compat_action = any("Compatibility Tool" in action.text() for action in menu.actions())
                if not has_compat_action:
                    def open_compat_dialog():
                        tools = get_available_compatibility_tools()
                        if not tools:
                            QMessageBox.information(self, "No Tools Found", "No custom Proton or GE-Proton tools were found in your Steam directories.")
                            return
                        compat_config = load_compat_config()
                        current_tool = compat_config.get(str(appid), "")
                        
                        dlg = CompatToolDialog(current_tool, tools, self)
                        if dlg.exec() == int(QDialog.DialogCode.Accepted):
                            selected = dlg.get_selected()
                            config = load_compat_config()
                            if selected:
                                config[str(appid)] = selected
                            else:
                                config.pop(str(appid), None)
                            save_compat_config(config)
                            QMessageBox.information(self, "Compatibility Tool", "Compatibility tool updated successfully for this game.")

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
    for g in load_steam_library() + load_custom_games():
        if g.get("exe_path") and g.get("title"):
            known[os.path.normcase(os.path.abspath(g["exe_path"]))] = g["title"]
    games_root = os.path.normcase(os.path.abspath(GAMES_DIR))
    for a in args:
        if not isinstance(a, str):
            continue
        full = os.path.abspath(a)
        norm = os.path.normcase(full)
        if norm in known:
            return _canonical_title(known[norm])
        if norm.startswith(games_root + os.sep):
            first = os.path.relpath(full, os.path.abspath(GAMES_DIR)).split(os.sep)[0]
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
        except CloudError:
            pass
        return fid, None
    with ThreadPoolExecutor(max_workers=6) as ex:
        return {f: v for f, v in ex.map(one, friend_ids) if v}

def cloud_send_message(other, text):
    token, uid = get_id_token()
    _http_json(_fdb(f"chats/{_chat_id(uid, other)}", token), "POST",
               {"from": uid, "text": text[:500], "ts": {".sv": "timestamp"}})

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

class ChatView(QScrollArea):
    """Scrollable chat with message bubbles (green = you, grey = them)."""
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
        bar = self.verticalScrollBar()
        # whenever the content grows, stay glued to the newest message while sticking
        bar.rangeChanged.connect(lambda _mn, mx: bar.setValue(mx) if self._stick else None)

    def reset(self):
        """Call when opening a chat so it starts at the newest message."""
        self._force_bottom = True
        self.clear()

    def clear(self):
        while self._lay.count() > 1:
            w = self._lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def set_messages(self, me, msgs):
        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 30
        self.clear()
        if not msgs:
            hint = QLabel("No messages yet. Say hi!")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet("color: #888888;")
            self._lay.insertWidget(0, hint)
        for i, m in enumerate(msgs):
            mine = m.get("from") == me
            bubble = QLabel(str(m.get("text", "")))
            bubble.setTextFormat(Qt.TextFormat.PlainText)
            bubble.setWordWrap(True)
            bubble.setMaximumWidth(380)
            bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if mine:
                bubble.setStyleSheet("background:#10eb73; color:#ffffff; border-radius:12px; padding:8px 12px;")
            else:
                bubble.setStyleSheet("background:rgba(128,128,128,0.28); border-radius:12px; padding:8px 12px;")
            ts = m.get("ts")
            stamp = QLabel(time.strftime("%H:%M", time.localtime(ts / 1000.0))
                           if isinstance(ts, (int, float)) else "")
            stamp.setStyleSheet("color:#888888; font-size:10px;")
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight if mine else Qt.AlignmentFlag.AlignLeft)
            col.addWidget(stamp, 0, Qt.AlignmentFlag.AlignRight if mine else Qt.AlignmentFlag.AlignLeft)
            row = QHBoxLayout()
            if mine:
                row.addStretch()
                row.addLayout(col)
            else:
                row.addLayout(col)
                row.addStretch()
            wrap = QWidget()
            wrap.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            self._lay.insertWidget(i, wrap)
        self._stick = self._force_bottom or at_bottom
        self._force_bottom = False
        if self._stick:
            QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
            QTimer.singleShot(300, lambda: setattr(self, "_stick", False))


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
        self.chat_timer.setInterval(4000)
        self.chat_timer.timeout.connect(self.load_messages)
        self.beat_timer = QTimer(self)
        self.beat_timer.setInterval(60000)
        self.beat_timer.timeout.connect(self.heartbeat)
        self.beat_timer.start()
        self.list_timer.start()  # keeps polling in the background for notifications
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
        self._detect_messages(last)
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
        if self._prev_last is not None:
            for uid, (key, msg) in last.items():
                if key == self._prev_last.get(uid) or msg.get("from") == me:
                    continue
                viewing = (self.current_uid == uid and self.stack.currentIndex() == 1
                           and self.window().isActiveWindow())
                if viewing:
                    continue
                name = self.friends.get(uid, {}).get("username") or "A friend"
                text = str(msg.get("text", ""))
                self.friend_event.emit(name, text if len(text) <= 100 else text[:97] + "...", uid)
        self._prev_last = {uid: key for uid, (key, _m) in last.items()}

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
        lay.addWidget(self.chat_view, 1)
        send_row = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Write a message...")
        self.chat_input.setMaxLength(500)
        self.chat_input.returnPressed.connect(self.send_message)
        send = QPushButton("Send")
        send.setObjectName("Primary")
        send.clicked.connect(self.send_message)
        send_row.addWidget(self.chat_input, 1)
        send_row.addWidget(send)
        lay.addLayout(send_row)
        lay.addSpacing(16)
        return page

    def open_friend(self, uid):
        self.current_uid = uid
        self._last_msg_key = ""
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

    def load_messages(self):
        uid = self.current_uid
        if not uid:
            return
        def done(res, err):
            if err or uid != self.current_uid:
                return
            me, msgs, last_key = res
            if last_key == self._last_msg_key:
                return
            self._last_msg_key = last_key
            self.chat_view.set_messages(me, msgs)
        self._run(lambda: cloud_get_messages(uid), done, key=("messages", uid))

    def send_message(self):
        text = self.chat_input.text().strip()
        uid = self.current_uid
        if not text or not uid:
            return
        self.chat_input.clear()
        self._run(lambda: cloud_send_message(uid, text), lambda r, e: self.load_messages())

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
    if tray is not None and tray.isVisible() and not getattr(self, "_really_quit", False):
        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            tray.showMessage("NCZ Games Launcher", "Still running in the background. "
                             "Right-click the tray icon to quit.", self.windowIcon(), 4000)
    else:
        event.accept()

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
        try:
            if self_update_check():
                self._selfupdate_sig.updated.emit()
        except Exception:
            pass
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
    if not os.path.isdir(GAMES_DIR):
        return None
    try:
        children = sorted(os.listdir(GAMES_DIR))
    except OSError:
        return None
    for name in children:
        if "garrysmod" not in re.sub(r"[^a-z0-9]", "", name.lower()):
            continue
        base = os.path.join(GAMES_DIR, name)
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
QPushButton#GmodTab:checked {{ background: {t['accent']}; color: #0b0b0d; border: 1px solid {t['accent']}; }}
QFrame#GmodRow {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QFrame#GmodRow:hover {{ border: 1px solid {t['accent']}; }}
QPushButton#GmodToggle {{ background: {t['button']}; color: {t['subtext']}; border-radius: 13px;
    padding: 0 14px; min-width: 70px; font-size: 12px; }}
QPushButton#GmodToggle:hover {{ background: {t['button_hover']}; }}
QPushButton#GmodToggle:checked {{ background: {t['accent']}; color: #0b0b0d; }}
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
    self.gmod_btn.setStyleSheet("QPushButton#NavButton { padding: 13px 8px 13px 22px; font-size: 13px; }")
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
QPushButton#WsTab:checked {{ background: {t['accent']}; color: #0b0b0d; border: 1px solid {t['accent']}; }}
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
        for w in self.game_cards:
            w.setParent(None)
            w.deleteLater()
        self.game_cards = []
        games = _ws_installed_games()
        for g in games:
            self.game_cards.append(self._make_game_card(g))
        self.games_empty.setVisible(not games)
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
    dirs = [os.path.dirname(os.path.abspath(__file__))]
    for getter in (_bundled_dir, _writable_dir):
        try:
            dirs.append(getter())
        except Exception:
            pass
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

def fetch_fan_overview(gid):
    """Game Jolt's own site api (the same one its pages use). Returns the page data as one dict."""
    data = json.loads(_http_get(f"https://gamejolt.com/site-api/web/discover/games/overview/{gid}?ignore", timeout=15))
    payload = data.get("payload") or {}
    merged = dict(payload)
    for src in (data.get("game"), payload.get("game")):
        if isinstance(src, dict):
            merged.update(src)
    return merged

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
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        if text:
            return text[:1200]
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

class FanDetailPage(StoreDetailPage):
    """Same details page as the Store, for Game Jolt fan games."""
    DETAIL_W, DETAIL_H = 480, 270   # horizontal cover

    def __init__(self, parent=None):
        super().__init__(parent)
        self._link = ""
        for b in self.findChildren(QPushButton):
            if b.text().startswith("< Back"):
                b.setText("< Back to Fan Games")
        self.price_label.hide()
        self.install_cb = None  # set by the app: (id, title, cover, link)
        self.steam_btn.setText("Install")
        self.steam_btn.clicked.disconnect()
        self.steam_btn.clicked.connect(
            lambda: self.install_cb and self.install_cb(self._appid, self._title, self._cover_path, self._link))

    def show_game(self, appid, title, cover_path, link="", tags=()):
        self._appid = appid
        self._title = title
        self._link = link
        self._cover_path = cover_path or ""
        self.title_label.setText(title)
        self.meta_label.setText("Fan game  \u2022  Game Jolt")
        self.desc_label.setText("Loading details...")
        self._clear(self.chips_row)
        self._clear(self.shots_row)
        self._set_extras_visible(False)
        for t in sorted(tags):
            chip = QLabel(_FAN_TAG_NAMES.get(t, t.title()))
            chip.setStyleSheet(self.CHIP_STYLE)
            self.chips_row.addWidget(chip)
        self.chips_row.addStretch()
        pix = rounded_cover_pixmap(cover_path, self.DETAIL_W, self.DETAIL_H, 12) if cover_path else None
        if pix:
            self.cover.setText("")
            self.cover.setPixmap(pix)
        else:
            self.cover.setPixmap(QPixmap())
            self.cover.setText("No\ncover")

        def work():
            desc, dev = "", ""
            try:
                game = fetch_fan_overview(appid)
                desc, dev = fan_description(game), fan_developer(game)
            except Exception:
                pass
            if not desc:
                try:
                    page = _http_get(link, timeout=15).decode("utf-8", errors="replace")
                    for rx in _OGD_RES:
                        m = rx.search(page)
                        if m and m.group(1).strip():
                            desc = html_lib.unescape(m.group(1)).strip()
                            break
                except Exception:
                    pass
            return (appid, desc, dev)

        worker = TaskWorker(work)
        worker.done.connect(self._on_fan_details)
        self._workers = [w for w in self._workers if w.isRunning()] + [worker]
        worker.start()

    def _on_fan_details(self, result):
        appid, desc, dev = result
        if appid != self._appid:
            return
        if dev:
            self.meta_label.setText(f"by {dev}  \u2022  Game Jolt fan game")
        self.desc_label.setText(desc or "No description available. Open the game on Game Jolt for more.")

FAN_COVER_W, FAN_COVER_H = 220, 124   # horizontal covers
FAN_CARD_W = FAN_COVER_W + 18

class FanGamesPage(StorePage):
    fan_selected = pyqtSignal(int, str, str)  # id, title, cover path

    def __init__(self, parent=None):
        super().__init__(parent)
        try:  # this page has no "Activated Games" view
            self.activated_btn.hide()
        except Exception:
            pass
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

            # 1) ask Game Jolt's own api (from inside the browser) for the Windows build's download url
            if gid:
                self.status_update.emit("Looking for the Windows build...")
                try:
                    api_url = page.evaluate(GJ_API_JS, gid)
                except Exception:
                    api_url = None
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
            clicked_main = False
            for _ in range(60):  # up to ~15s, checked every 250ms so it reacts as soon as the button shows up
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
            end, clicked, saw_list = time.time() + 300, False, False
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
                            if not clicked:
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


def _gj_stream(self, url, headers):
    """Like _fb_stream, but keeps the file's real name/extension (.exe, .zip, .rar, .7z...)."""
    self.status_update.emit("Connecting...")
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        self._response = response
        if self._is_cancelled:
            raise RuntimeError("CANCELLED")
        name = ""
        cd = response.headers.get("Content-Disposition") or ""
        m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
        if m:
            name = urllib.parse.unquote(m.group(1).strip().strip('"'))
        if not name:
            name = os.path.basename(urllib.parse.unquote(response.geturl().split("?")[0]))
        name = re.sub(r'[<>:"/\\|?*]', "_", name).strip() or sanitize_folder_name(self.title) + ".bin"
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, name))
        total = int(response.headers.get("Content-Length", 0) or 0)
        downloaded, start = 0, time.time()
        with open(dest_path, "wb") as f:
            while not self._is_cancelled:
                buf = response.read(8192)
                if not buf:
                    break
                downloaded += len(buf)
                f.write(buf)
                el = time.time() - start
                self.progress.emit(downloaded, total, downloaded / el if el > 0 else 0)
    return dest_path

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

class GameJoltDownloadWorker(FirebaseDownloadWorker):
    def __init__(self, title, game_url, gid=None, existing_zip_path=None):
        super().__init__(title, game_url, 0)
        self.game_url = game_url
        self.gid = gid
        self.existing_zip_path = existing_zip_path

    def run(self):
        dest_path, fresh = None, False
        try:
            if self.existing_zip_path and os.path.exists(self.existing_zip_path):
                dest_path = self.existing_zip_path   # user chose to reuse the file that's already downloaded
            else:
                url, headers = _gj_find_link(self, self.game_url, self.gid)
                self.download_url = url
                dest_path = _gj_stream(self, url, headers)
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
        _launch_exe(self, exe, load_compat_config().get(f"fan-{gid}", ""))
        return
    if not link:
        QMessageBox.warning(self, "Install", "This game has no Game Jolt link.")
        return
    cancelled, existing = _ask_existing_archive(self, title)
    if cancelled:
        return
    worker = GameJoltDownloadWorker(title, link, gid, existing)
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
        if compat_tool and os.path.exists(compat_tool):
            return ([compat_tool, "run", exe], cwd,
                    {"STEAM_COMPAT_DATA_PATH": cwd + "_compat_data",
                     "STEAM_COMPAT_CLIENT_INSTALL_PATH": os.path.expanduser("~/.local/share/Steam")})
        if exe.lower().endswith(".sh"):
            return (["bash", exe], cwd, {})
        if exe.lower().endswith(".exe") and getattr(app, "wine", None):
            return ([app.wine[0], exe], cwd, {})
    return ([exe], cwd, {})

def _launch_exe(app, exe, compat_tool=""):
    if not exe or not os.path.exists(exe):
        QMessageBox.warning(app, "Launch Error", f"Executable not found:\n{exe}")
        return
    argv, cwd, extra = _launch_spec(app, exe, compat_tool)
    try:
        env = os.environ.copy()
        if extra:
            os.makedirs(extra["STEAM_COMPAT_DATA_PATH"], exist_ok=True)
            env.update(extra)
            apply_proton_d3d(env)
        elif sys.platform.startswith("linux") and argv[0] != exe:
            env = wine_game_env()
        subprocess.Popen(argv, cwd=cwd, env=env)
    except Exception as e:
        QMessageBox.warning(app, "Launch Error", f"Couldn't launch the game: {e}")

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

def create_game_shortcut(app, title, exe, compat_tool="", fallback_icon=""):
    import shlex
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
                  f"$s.IconLocation='{q(exe)},0';$s.Save()")
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
            cmd = " ".join(shlex.quote(a) for a in argv)
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

def _add_shortcut_action(app, card, title, exe_fn, tool_fn=lambda: "", cover=""):
    more = card.findChild(QPushButton, "MoreButton") if card is not None else None
    if not more or not more.menu():
        return
    more.menu().addAction("Create Desktop Shortcut").triggered.connect(
        lambda _c=False: create_game_shortcut(app, title, exe_fn(), tool_fn(), cover))

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
        _add_shortcut_action(self, self.cards[-1][0], title_text, exe_fn, cover=image_filename)
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
                         lambda: load_compat_config().get(str(appid), ""), cover_path)
AdaptiveApp.create_steam_card = _sc_create_steam_card

_sc_prev_custom_card = AdaptiveApp.create_custom_card
def _sc_create_custom_card(self, game_id, title_text, exe_path, cover_path, compat_tool):
    _sc_prev_custom_card(self, game_id, title_text, exe_path, cover_path, compat_tool)
    _add_shortcut_action(self, getattr(self, "custom_cards", {}).get(game_id), title_text,
                         lambda: exe_path, lambda: compat_tool, cover_path)
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
                         lambda: load_compat_config().get(f"fan-{gid}", ""), g.get("cover", ""))
AdaptiveApp._fan_create_library_card = _sc_fan_card

def _fan_compat_dialog(self, gid):
    tools = get_available_compatibility_tools()
    if not tools:
        QMessageBox.information(self, "No Tools Found", "No custom Proton or GE-Proton tools were found in your Steam directories.")
        return
    key = f"fan-{gid}"
    dlg = CompatToolDialog(load_compat_config().get(key, ""), tools, self)
    if dlg.exec() == int(QDialog.DialogCode.Accepted):
        selected = dlg.get_selected()
        config = load_compat_config()
        if selected:
            config[key] = selected
        else:
            config.pop(key, None)
        save_compat_config(config)
        QMessageBox.information(self, "Compatibility Tool", "Compatibility tool updated successfully for this game.")

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

 
if __name__ == "__main__":
    main()
