import os
import sys
import re
import time
import json
import shutil
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
    QFileDialog, QInputDialog
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
    settings = {"dark_mode": "system", "steamgriddb_api_key": "", "currency": "EGP"}
    path = get_launcher_settings_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                mode = str(loaded.get("dark_mode", "system")).lower()
                if mode in ("off", "on", "system"):
                    settings["dark_mode"] = mode
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
        with open(get_installed_versions_path(), 'r', encoding='utf-8') as f:
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
        with open(get_account_path(), 'r', encoding='utf-8') as f:
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
        with open(get_profile_cache_path(), 'r', encoding='utf-8') as f:
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
            if orig_type == bool:
                val = text_val.lower() in ('true', '1', 'yes')
            elif orig_type == int:
                try:
                    val = int(text_val)
                except ValueError:
                    val = text_val
            elif orig_type == float:
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

class DownloadWorker(QThread):
    progress = pyqtSignal(int, int, float)
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
                with urllib.request.urlopen(req) as resp:
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
                    with urllib.request.urlopen(mf_req) as mf_resp:
                        mf_html = mf_resp.read().decode('utf-8')
                    match = re.search(r'href="(https?://download[^"]+)"', mf_html)
                    if match:
                        download_url = match.group(1)

                dl_req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(dl_req) as response:
                    file_name = f"{self.game_name}.zip"
                    cd = response.headers.get('Content-Disposition')
                    if cd and 'filename=' in cd:
                        file_name = cd.split('filename=')[-1].strip('"\'')
                    else:
                        url_path = urllib.parse.unquote(response.geturl().split('?')[0])
                        possible_name = os.path.basename(url_path)
                        if possible_name.lower().endswith('.zip'):
                            file_name = possible_name

                    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                    dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
                    total_size = int(response.headers.get('Content-Length', 0))
                    downloaded = 0
                    block_size = 8192
                    start_time = time.time()

                    with open(dest_path, 'wb') as f:
                        while True:
                            buffer = response.read(block_size)
                            if not buffer:
                                break
                            downloaded += len(buffer)
                            f.write(buffer)
                            elapsed = time.time() - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            self.progress.emit(downloaded, total_size, speed)

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

                project_root = os.path.abspath(DOWNLOAD_DIR)
                os.makedirs(GAMES_DIR, exist_ok=True)
                script_name = self.linux_cmd.rsplit("./", 1)[-1].strip() if "./" in self.linux_cmd else "installer.sh"

                full_cmd = self.linux_cmd.replace(f"./{script_name}", f'echo "{GAMES_DIR}" | bash ./{script_name}')
                inner_cmd = (
                    f'cd "{project_root}" && {full_cmd}; '
                    f'rm -f "{dest_path}"; '
                    f'echo "Press ENTER to exit..."; read'
                )

                if shutil.which("x-terminal-emulator"):
                    term_cmd = f'x-terminal-emulator -e bash -c \'{inner_cmd}\''
                elif shutil.which("gnome-terminal"):
                    term_cmd = f'gnome-terminal -- bash -c \'{inner_cmd}\''
                elif shutil.which("konsole"):
                    term_cmd = f'konsole -e bash -c \'{inner_cmd}\''
                elif shutil.which("xfce4-terminal"):
                    term_cmd = f'xfce4-terminal -e "bash -c \'{inner_cmd}\'"'
                elif shutil.which("xterm"):
                    term_cmd = f'xterm -e bash -c \'{inner_cmd}\''
                else:
                    term_cmd = f'bash -c \'{inner_cmd}\''

                proc = subprocess.Popen(term_cmd, shell=True, cwd=project_root)
                proc.wait()
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
        ("mr.fancypigeon", "Linux Script for FNaNCZ 1 and 2, and ideas", "pigeon.png"),
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
            self.info_label.setText(f"Speed: {speed_str} | Progress: {dl_mb:.1f} MB / {total_mb:.1f} MB")
        else:
            self.progress_bar.setRange(0, 0)
            self.status_label.setText("Downloading...")
            self.info_label.setText(f"Speed: {speed_str} | Downloaded: {dl_mb:.1f} MB")

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
        with open(get_steam_library_path(), "r", encoding="utf-8") as f:
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
            subprocess.Popen([wine_path, exe_path], cwd=os.path.dirname(exe_path), env=system_env())
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
        layout.addWidget(title)
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

    def add_download_card(self, card):
        self.empty_label.hide()
        self.container_layout.insertWidget(self.container_layout.count() - 1, card)

class DownloadCard(QFrame):
    CARD_W, CARD_H = 60, 90

    def __init__(self, title, download_url, expected_size=0, cover_path="", parent=None):
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
        
        self.action_btn = QPushButton("Cancel")
        self.action_btn.setFixedSize(80, 28)
        self.action_btn.clicked.connect(self.cancel_download)
        bottom_row.addWidget(self.action_btn)
        layout.addLayout(bottom_row)

        main_layout.addLayout(layout, 1)

        self.worker = FirebaseDownloadWorker(title, download_url, expected_size)
        self.worker.progress.connect(self.on_progress)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def cancel_download(self):
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
            self.info_label.setText(f"Speed: {speed_str} | {dl_mb:.1f} MB / {total_mb:.1f} MB")
        else:
            self.progress_bar.setRange(0, 0)
            self.status_label.setText("Downloading...")
            self.info_label.setText(f"Speed: {speed_str} | Downloaded: {dl_mb:.1f} MB")

    def on_finished(self, zip_path):
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
        self.status_label.setText("Failed")
        self.status_label.setStyleSheet(f"color: {RED};")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.action_btn.setText("Copy Error")
        try:
            self.action_btn.clicked.disconnect()
        except Exception:
            pass
        self.action_btn.clicked.connect(lambda: QApplication.clipboard().setText(error_msg))

class FirebaseDownloadWorker(QThread):
    progress = pyqtSignal(int, int, float)
    status_update = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, title, download_url, expected_size=0):
        super().__init__()
        self.title = title
        self.download_url = download_url
        self.expected_size = expected_size
        self._is_cancelled = False
        self._response = None

    def cancel(self):
        self._is_cancelled = True
        if self._response:
            try:
                self._response.close()
            except Exception:
                pass

    def run(self):
        dest_path = None
        try:
            total_size = self.expected_size
            if total_size <= 0 and not self._is_cancelled:
                try:
                    head_req = urllib.request.Request(self.download_url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(head_req, timeout=3) as head_resp:
                        total_size = int(head_resp.headers.get('Content-Length', 0))
                except Exception:
                    pass

            if self._is_cancelled:
                self.failed.emit("CANCELLED")
                return

            self.status_update.emit("Connecting...")
            req = urllib.request.Request(self.download_url, headers={'User-Agent': 'Mozilla/5.0'})
            
            with urllib.request.urlopen(req, timeout=15) as response:
                self._response = response
                if self._is_cancelled:
                    self.failed.emit("CANCELLED")
                    return

                file_name = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', self.title)}.zip"
                cd = response.headers.get('Content-Disposition')
                if cd and 'filename=' in cd:
                    file_name = cd.split('filename=')[-1].strip('"\'')
                else:
                    url_path = urllib.parse.unquote(response.geturl().split('?')[0])
                    possible_name = os.path.basename(url_path)
                    if possible_name.lower().endswith('.zip'):
                        file_name = possible_name

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
            extract_dir = installed_game_dir(self.title)
            os.makedirs(extract_dir, exist_ok=True)
            if zipfile.is_zipfile(dest_path):
                with zipfile.ZipFile(dest_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)
            else:
                raise ValueError("Downloaded file is not a valid ZIP archive.")

            if self._is_cancelled:
                self.failed.emit("CANCELLED")
                return

            self.finished.emit(dest_path)
        except Exception as e:
            if self._is_cancelled:
                if dest_path and os.path.exists(dest_path):
                    try:
                        os.remove(dest_path)
                    except Exception:
                        pass
                self.failed.emit("CANCELLED")
            else:
                self.failed.emit(str(e))

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

# Safely override FirebaseDownloadWorker run to use sanitized extraction paths
_original_firebase_worker_run = FirebaseDownloadWorker.run

def _patched_firebase_worker_run(self):
    try:
        safe_title = sanitize_folder_name(self.title)
        extract_dir = installed_game_dir(safe_title)
        os.makedirs(extract_dir, exist_ok=True)
    except Exception:
        pass
    _original_firebase_worker_run(self)

FirebaseDownloadWorker.run = _patched_firebase_worker_run

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
                                subprocess.Popen([wine_path, exe_path], cwd=os.path.dirname(exe_path), env=system_env())
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
                    link, expected_size = get_steam_download_link(a)
                    if not link:
                        QMessageBox.information(self, "Link Unavailable", "Contact MM33 to give this game a link.")
                        return
                    card_widget = DownloadCard(t, link, expected_size, p, self)
                    
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
            with open(COMPAT_CONFIG_FILE, 'r') as f:
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
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_selected(self):
        return self.combo.currentData()

if hasattr(AdaptiveApp, "_original_create_steam_card_compat"):
    AdaptiveApp.create_steam_card = AdaptiveApp._original_create_steam_card_compat

AdaptiveApp._original_create_steam_card_compat = AdaptiveApp.create_steam_card

def _patched_create_steam_card_compat(self, appid, title_text, cover_path):
    AdaptiveApp._original_create_steam_card_compat(self, appid, title_text, cover_path)
    card = self.steam_cards.get(appid)
    if card:
        btn_play = card.findChild(QPushButton, "Primary")
        
        def update_compat_launch():
            game_dir = _safe_installed_game_dir(title_text)
            exe_path = find_best_game_exe(game_dir, title_text)
            installed = bool(exe_path and os.path.exists(exe_path))
            
            try:
                btn_play.clicked.disconnect()
            except Exception:
                pass

            if installed:
                def launch_game_compat():
                    if sys.platform.startswith("win"):
                        try:
                            subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
                        except Exception as e:
                            QMessageBox.warning(self, "Launch Game", f"Couldn't launch game: {e}")
                    elif sys.platform.startswith("linux"):
                        compat_config = load_compat_config()
                        proton_bin = compat_config.get(str(appid), "")
                        
                        if proton_bin and os.path.exists(proton_bin):
                            compat_data_path = os.path.join(game_dir, "compat_data")
                            os.makedirs(compat_data_path, exist_ok=True)
                            env = os.environ.copy()
                            env["STEAM_COMPAT_DATA_PATH"] = compat_data_path
                            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = os.path.expanduser("~/.local/share/Steam")
                            try:
                                subprocess.Popen([proton_bin, "run", exe_path], cwd=os.path.dirname(exe_path), env=env)
                            except Exception as e:
                                QMessageBox.warning(self, "Launch Error", f"Couldn't start Proton: {e}")
                        elif self.wine:
                            wine_path, _ = self.wine
                            try:
                                subprocess.Popen([wine_path, exe_path], cwd=os.path.dirname(exe_path), env=system_env())
                            except Exception as e:
                                QMessageBox.warning(self, "Launch using wine", f"Couldn't start wine: {e}")
                        else:
                            QMessageBox.warning(self, "Compatibility Tool Required", "No valid compatibility tool or Wine found.")
                    else:
                        QMessageBox.warning(self, "Platform", "Launching Windows games is not supported on this platform.")

                btn_play.clicked.connect(launch_game_compat)

        update_compat_launch()

        more_btn = card.findChild(QPushButton, "MoreButton")
        if more_btn and more_btn.menu():
            menu = more_btn.menu()
            
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

AdaptiveApp.create_steam_card = _patched_create_steam_card_compat

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

if hasattr(AdaptiveApp, "_original_create_steam_card_compat"):
    AdaptiveApp.create_steam_card = AdaptiveApp._original_create_steam_card_compat

AdaptiveApp._original_create_steam_card_compat = AdaptiveApp.create_steam_card

def _patched_create_steam_card_compat(self, appid, title_text, cover_path):
    AdaptiveApp._original_create_steam_card_compat(self, appid, title_text, cover_path)
    card = self.steam_cards.get(appid)
    if card:
        btn_play = card.findChild(QPushButton, "Primary")
        
        def update_compat_launch():
            game_dir = _safe_installed_game_dir(title_text)
            exe_path = find_best_game_exe(game_dir, title_text)
            installed = bool(exe_path and os.path.exists(exe_path))
            
            try:
                btn_play.clicked.disconnect()
            except Exception:
                pass

            if installed:
                def launch_game_compat():
                    if sys.platform.startswith("win"):
                        try:
                            subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
                        except Exception as e:
                            QMessageBox.warning(self, "Launch Game", f"Couldn't launch game: {e}")
                    elif sys.platform.startswith("linux"):
                        compat_config = load_compat_config()
                        proton_bin = compat_config.get(str(appid), "")
                        
                        if proton_bin and os.path.exists(proton_bin):
                            compat_data_path = os.path.join(game_dir, "compat_data")
                            os.makedirs(compat_data_path, exist_ok=True)
                            env = os.environ.copy()
                            env["STEAM_COMPAT_DATA_PATH"] = compat_data_path
                            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = os.path.expanduser("~/.local/share/Steam")
                            try:
                                subprocess.Popen([proton_bin, "run", exe_path], cwd=os.path.dirname(exe_path), env=env)
                            except Exception as e:
                                QMessageBox.warning(self, "Launch Error", f"Couldn't start Proton: {e}")
                        elif self.wine:
                            wine_path, _ = self.wine
                            try:
                                subprocess.Popen([wine_path, exe_path], cwd=os.path.dirname(exe_path), env=system_env())
                            except Exception as e:
                                QMessageBox.warning(self, "Launch using wine", f"Couldn't start wine: {e}")
                        else:
                            QMessageBox.warning(self, "Compatibility Tool Required", "No valid compatibility tool or Wine found.")
                    else:
                        QMessageBox.warning(self, "Platform", "Launching Windows games is not supported on this platform.")

                btn_play.clicked.connect(launch_game_compat)

        update_compat_launch()

        more_btn = card.findChild(QPushButton, "MoreButton")
        if more_btn and more_btn.menu():
            menu = more_btn.menu()
            
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

AdaptiveApp.create_steam_card = _patched_create_steam_card_compat

class GameDownloadWorker(QThread):
    progress = pyqtSignal(int, int, float)
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

    def run(self):
        dest_path = self.existing_zip_path
        try:
            downloaded_here = False
            if not dest_path or not os.path.exists(dest_path):
                downloaded_here = True
                self.status_update.emit("Checking version...")
                req = urllib.request.Request(self.version_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=15) as resp:
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
                    with urllib.request.urlopen(mf_req, timeout=15) as mf_resp:
                        mf_html = mf_resp.read().decode('utf-8')
                    match = re.search(r'href="(https?://download[^"]+)"', mf_html)
                    if match:
                        download_url = match.group(1)

                if self._is_cancelled:
                    self.failed.emit("CANCELLED")
                    return

                self.status_update.emit("Connecting...")
                dl_req = urllib.request.Request(download_url, headers={'User-Agent': 'Mozilla/5.0'})
                
                with urllib.request.urlopen(dl_req, timeout=15) as response:
                    self._response = response
                    if self._is_cancelled:
                        self.failed.emit("CANCELLED")
                        return

                    file_name = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', self.title)}.zip"
                    cd = response.headers.get('Content-Disposition')
                    if cd and 'filename=' in cd:
                        file_name = cd.split('filename=')[-1].strip('"\'')
                    else:
                        url_path = urllib.parse.unquote(response.geturl().split('?')[0])
                        possible_name = os.path.basename(url_path)
                        if possible_name.lower().endswith('.zip'):
                            file_name = possible_name

                    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                    dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
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
                self.status_update.emit("Running Linux conversion script...")
                project_root = os.path.abspath(DOWNLOAD_DIR)
                os.makedirs(GAMES_DIR, exist_ok=True)
                script_name = self.linux_cmd.rsplit("./", 1)[-1].strip() if "./" in self.linux_cmd else "installer.sh"

                full_cmd = self.linux_cmd.replace(f"./{script_name}", f'echo "{GAMES_DIR}" | bash ./{script_name}')
                inner_cmd = (
                    f'cd "{project_root}" && {full_cmd}; '
                    f'rm -f "{dest_path}"; '
                    f'echo "Press ENTER to exit..."; read'
                )

                if shutil.which("x-terminal-emulator"):
                    term_cmd = f'x-terminal-emulator -e bash -c \'{inner_cmd}\''
                elif shutil.which("gnome-terminal"):
                    term_cmd = f'gnome-terminal -- bash -c \'{inner_cmd}\''
                elif shutil.which("konsole"):
                    term_cmd = f'konsole -e bash -c \'{inner_cmd}\''
                elif shutil.which("xfce4-terminal"):
                    term_cmd = f'xfce4-terminal -e "bash -c \'{inner_cmd}\'"'
                elif shutil.which("xterm"):
                    term_cmd = f'xterm -e bash -c \'{inner_cmd}\''
                else:
                    term_cmd = f'bash -c \'{inner_cmd}\''

                proc = subprocess.Popen(term_cmd, shell=True, cwd=project_root)
                proc.wait()
            else:
                self.status_update.emit("Extracting game files...")
                extract_dir = installed_game_dir(self.title)
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(dest_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)

            if downloaded_here and self.remote_version and not self.wine_extract_dir:
                record_installed_version(self.title, self.remote_version)
            
            self.finished.emit(dest_path)
        except Exception as e:
            if self._is_cancelled:
                self.failed.emit("CANCELLED")
            else:
                self.failed.emit(str(e))

class GameDownloadCard(DownloadCard):
    def __init__(self, game_id, title, version_url, linux_cmd, expected_size=0, cover_path="", parent=None, existing_zip_path=None, wine_extract_dir=None):
        super().__init__(title, version_url, expected_size, cover_path, parent)
        # Disconnect parent worker and replace with GameDownloadWorker
        self.worker.cancel()
        self.worker = GameDownloadWorker(game_id, title, version_url, linux_cmd, existing_zip_path, wine_extract_dir)
        self.worker.progress.connect(self.on_progress)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

# Override installation methods for AE and NCZ2 to use downloads tab
def _patched_start_ae_install(self):
    existing_zips = [
        f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
        if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz ae", "fnancz_ae", "fnanczae"))
    ]
    version_url = GAME_INFO["ae"]["version_url"]
    linux_cmd = GAME_INFO["ae"]["linux_cmd"]
    cover_path = asset_path("fnanczaecover.png")

    existing_zip_path = None
    if existing_zips:
        file_name = existing_zips[0]
        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = full_path
            elif dlg.choice == "overwrite":
                try:
                    os.remove(full_path)
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
    existing_zips = [
        f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
        if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz 2", "fnancz_2", "fnancz 2", "fnancz2"))
    ]
    version_url = GAME_INFO["ncz2"]["version_url"]
    linux_cmd = GAME_INFO["ncz2"]["linux_cmd"]
    cover_path = asset_path("fnancz2cover.png")

    existing_zip_path = None
    if existing_zips:
        file_name = existing_zips[0]
        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = full_path
            elif dlg.choice == "overwrite":
                try:
                    os.remove(full_path)
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

class GameDownloadCard(DownloadCard):
    def __init__(self, game_id, title, version_url, linux_cmd, expected_size=0, cover_path="", parent=None, existing_zip_path=None, wine_extract_dir=None):
        super().__init__(title, version_url, expected_size, cover_path, parent)
        self.worker.cancel()
        self.worker = GameDownloadWorker(game_id, title, version_url, linux_cmd, existing_zip_path, wine_extract_dir)
        self.worker.progress.connect(self.on_progress)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

def _patched_start_ae_install(self):
    existing_zips = [
        f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
        if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz ae", "fnancz_ae", "fnanczae"))
    ]
    version_url = GAME_INFO["ae"]["version_url"]
    linux_cmd = GAME_INFO["ae"]["linux_cmd"]
    cover_path = asset_path("fnanczaecover.png")

    existing_zip_path = None
    if existing_zips:
        file_name = existing_zips[0]
        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = full_path
            elif dlg.choice == "overwrite":
                try:
                    os.remove(full_path)
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
    existing_zips = [
        f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
        if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz 2", "fnancz_2", "fnancz 2", "fnancz2"))
    ]
    version_url = GAME_INFO["ncz2"]["version_url"]
    linux_cmd = GAME_INFO["ncz2"]["linux_cmd"]
    cover_path = asset_path("fnancz2cover.png")

    existing_zip_path = None
    if existing_zips:
        file_name = existing_zips[0]
        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = full_path
            elif dlg.choice == "overwrite":
                try:
                    os.remove(full_path)
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

class GameDownloadCard(DownloadCard):
    def __init__(self, game_id, title, version_url, linux_cmd, expected_size=0, cover_path="", parent=None, existing_zip_path=None, wine_extract_dir=None):
        super().__init__(title, "", expected_size, cover_path, parent)
        self.worker.cancel()
        self.worker = GameDownloadWorker(game_id, title, version_url, linux_cmd, existing_zip_path, wine_extract_dir)
        self.worker.progress.connect(self.on_progress)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

def _patched_start_ae_install(self):
    existing_zips = [
        f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
        if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz ae", "fnancz_ae", "fnanczae"))
    ]
    version_url = GAME_INFO["ae"]["version_url"]
    linux_cmd = GAME_INFO["ae"]["linux_cmd"]
    cover_path = asset_path("fnanczaecover.png")

    existing_zip_path = None
    if existing_zips:
        file_name = existing_zips[0]
        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = full_path
            elif dlg.choice == "overwrite":
                try:
                    os.remove(full_path)
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
    existing_zips = [
        f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
        if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz 2", "fnancz_2", "fnancz 2", "fnancz2"))
    ]
    version_url = GAME_INFO["ncz2"]["version_url"]
    linux_cmd = GAME_INFO["ncz2"]["linux_cmd"]
    cover_path = asset_path("fnancz2cover.png")

    existing_zip_path = None
    if existing_zips:
        file_name = existing_zips[0]
        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = full_path
            elif dlg.choice == "overwrite":
                try:
                    os.remove(full_path)
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

class DownloadCard(QFrame):
    CARD_W, CARD_H = 60, 90

    def __init__(self, title, download_url, expected_size=0, cover_path="", parent=None, start_worker=True):
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
        
        self.action_btn = QPushButton("Cancel")
        self.action_btn.setFixedSize(80, 28)
        self.action_btn.clicked.connect(self.cancel_download)
        bottom_row.addWidget(self.action_btn)
        layout.addLayout(bottom_row)

        main_layout.addLayout(layout, 1)

        self.worker = FirebaseDownloadWorker(title, download_url, expected_size)
        self.worker.progress.connect(self.on_progress)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        if start_worker:
            self.worker.start()

class GameDownloadCard(DownloadCard):
    def __init__(self, game_id, title, version_url, linux_cmd, expected_size=0, cover_path="", parent=None, existing_zip_path=None, wine_extract_dir=None):
        super().__init__(title, version_url, expected_size, cover_path, parent, start_worker=False)
        self.worker = GameDownloadWorker(game_id, title, version_url, linux_cmd, existing_zip_path, wine_extract_dir)
        self.worker.progress.connect(self.on_progress)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

def _patched_start_ae_install(self):
    existing_zips = [
        f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
        if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz ae", "fnancz_ae", "fnanczae"))
    ]
    version_url = GAME_INFO["ae"]["version_url"]
    linux_cmd = GAME_INFO["ae"]["linux_cmd"]
    cover_path = asset_path("fnanczaecover.png")

    existing_zip_path = None
    if existing_zips:
        file_name = existing_zips[0]
        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = full_path
            elif dlg.choice == "overwrite":
                try:
                    os.remove(full_path)
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
    existing_zips = [
        f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
        if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz 2", "fnancz_2", "fnancz 2", "fnancz2"))
    ]
    version_url = GAME_INFO["ncz2"]["version_url"]
    linux_cmd = GAME_INFO["ncz2"]["linux_cmd"]
    cover_path = asset_path("fnancz2cover.png")

    existing_zip_path = None
    if existing_zips:
        file_name = existing_zips[0]
        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = full_path
            elif dlg.choice == "overwrite":
                try:
                    os.remove(full_path)
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

class DownloadCard(QFrame):
    CARD_W, CARD_H = 60, 90

    def __init__(self, title, download_url, expected_size=0, cover_path="", parent=None, worker=None):
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
        
        self.action_btn = QPushButton("Cancel")
        self.action_btn.setFixedSize(80, 28)
        self.action_btn.clicked.connect(self.cancel_download)
        bottom_row.addWidget(self.action_btn)
        layout.addLayout(bottom_row)

        main_layout.addLayout(layout, 1)

        if worker is not None:
            self.worker = worker
        else:
            self.worker = FirebaseDownloadWorker(title, download_url, expected_size)
            
        self.worker.progress.connect(self.on_progress)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

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
            self.info_label.setText(f"Speed: {speed_str} | {dl_mb:.1f} MB / {total_mb:.1f} MB")
        else:
            self.progress_bar.setRange(0, 0)
            self.status_label.setText("Downloading...")
            self.info_label.setText(f"Speed: {speed_str} | Downloaded: {dl_mb:.1f} MB")

    def on_finished(self, zip_path):
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
        self.status_label.setText("Failed")
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

def _patched_start_ae_install(self):
    existing_zips = [
        f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
        if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz ae", "fnancz_ae", "fnanczae"))
    ]
    version_url = GAME_INFO["ae"]["version_url"]
    linux_cmd = GAME_INFO["ae"]["linux_cmd"]
    cover_path = asset_path("fnanczaecover.png")

    existing_zip_path = None
    if existing_zips:
        file_name = existing_zips[0]
        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = full_path
            elif dlg.choice == "overwrite":
                try:
                    os.remove(full_path)
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
    existing_zips = [
        f for f in (os.listdir(DOWNLOAD_DIR) if os.path.isdir(DOWNLOAD_DIR) else [])
        if f.lower().endswith(".zip") and f.lower().startswith(("five nights at ncz 2", "fnancz_2", "fnancz 2", "fnancz2"))
    ]
    version_url = GAME_INFO["ncz2"]["version_url"]
    linux_cmd = GAME_INFO["ncz2"]["linux_cmd"]
    cover_path = asset_path("fnancz2cover.png")

    existing_zip_path = None
    if existing_zips:
        file_name = existing_zips[0]
        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
        dlg = ExistingFileDialog(file_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.choice == "use":
                existing_zip_path = full_path
            elif dlg.choice == "overwrite":
                try:
                    os.remove(full_path)
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

def get_custom_games_path():
    return os.path.join(os.path.dirname(get_launcher_settings_path()), "custom_games.json")

def load_custom_games():
    try:
        with open(get_custom_games_path(), "r", encoding="utf-8") as f:
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
                    subprocess.Popen([wine_path, exe_path], cwd=os.path.dirname(exe_path), env=system_env())
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

_original_build_library_page_header_custom = AdaptiveApp.build_library_page

def _patched_build_library_page_header_custom(self):
    widget = _original_build_library_page_header_custom(self)
    
    main_layout = widget.layout()
    if main_layout and main_layout.count() > 0:
        header_item = main_layout.itemAt(0)
        if header_item and header_item.layout():
            header_layout = header_item.layout()
            
            add_btn = QPushButton("+")
            add_btn.setFixedSize(28, 28)
            add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            add_btn.setToolTip("Add custom game")
            add_btn.setStyleSheet("font-size: 18px; font-weight: bold; border-radius: 14px; padding: 0px;")
            add_btn.clicked.connect(self.open_add_custom_game_dialog)
            
            header_layout.insertWidget(2, add_btn)

    for game in load_custom_games():
        self.create_custom_card(game.get("id"), game.get("title"), game.get("exe_path"), game.get("cover_path"), game.get("compat_tool"))
        
    self.apply_filter()
    return widget

AdaptiveApp.build_library_page = _patched_build_library_page_header_custom

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

if hasattr(AdaptiveApp, "_original_create_steam_card_compat"):
    AdaptiveApp.create_steam_card = AdaptiveApp._original_create_steam_card_compat

AdaptiveApp._original_create_steam_card_compat = AdaptiveApp.create_steam_card

def _patched_create_steam_card_compat(self, appid, title_text, cover_path):
    AdaptiveApp._original_create_steam_card_compat(self, appid, title_text, cover_path)
    card = self.steam_cards.get(appid)
    if card:
        btn_play = card.findChild(QPushButton, "Primary")
        status_label = card.findChild(QLabel, "CardStatus")
        
        def update_compat_launch():
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

                def launch_game_compat():
                    if sys.platform.startswith("win"):
                        try:
                            subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
                        except Exception as e:
                            QMessageBox.warning(self, "Launch Game", f"Couldn't launch game: {e}")
                    elif sys.platform.startswith("linux"):
                        compat_config = load_compat_config()
                        proton_bin = compat_config.get(str(appid), "")
                        
                        if proton_bin and os.path.exists(proton_bin):
                            compat_data_path = os.path.join(game_dir, "compat_data")
                            os.makedirs(compat_data_path, exist_ok=True)
                            env = os.environ.copy()
                            env["STEAM_COMPAT_DATA_PATH"] = compat_data_path
                            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = os.path.expanduser("~/.local/share/Steam")
                            try:
                                subprocess.Popen([proton_bin, "run", exe_path], cwd=os.path.dirname(exe_path), env=env)
                            except Exception as e:
                                QMessageBox.warning(self, "Launch Error", f"Couldn't start Proton: {e}")
                        elif self.wine:
                            wine_path, _ = self.wine
                            try:
                                subprocess.Popen([wine_path, exe_path], cwd=os.path.dirname(exe_path), env=system_env())
                            except Exception as e:
                                QMessageBox.warning(self, "Launch using wine", f"Couldn't start wine: {e}")
                        else:
                            QMessageBox.warning(self, "Compatibility Tool Required", "No valid compatibility tool or Wine found.")
                    else:
                        QMessageBox.warning(self, "Platform", "Launching Windows games is not supported on this platform.")

                btn_play.clicked.connect(launch_game_compat)
            else:
                btn_play.setText("Install")
                if status_label:
                    status_label.setText("Steam game")
                    status_label.setStyleSheet("")

                def on_steam_install(_c=False, a=appid, t=title_text, p=cover_path):
                    link, expected_size = get_steam_download_link(a)
                    if not link:
                        QMessageBox.information(self, "Link Unavailable", "Contact MM33 to give this game a link.")
                        return
                    card_widget = DownloadCard(t, link, expected_size, p, self)
                    
                    def on_dl_finished(_path):
                        update_compat_launch()
                    card_widget.worker.finished.connect(on_dl_finished)
                    
                    self.downloads_page.add_download_card(card_widget)
                    self.nav_group.button(6).setChecked(True)
                    self.pages.setCurrentWidget(self.downloads_page)

                btn_play.clicked.connect(on_steam_install)

        update_compat_launch()

        more_btn = card.findChild(QPushButton, "MoreButton")
        if more_btn and more_btn.menu():
            menu = more_btn.menu()
            
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

            # Avoid adding duplicate menu actions if patched multiple times
            has_compat_action = any("Compatibility Tool" in action.text() for action in menu.actions())
            if not has_compat_action:
                menu.addAction("Switch Compatibility Tool").triggered.connect(open_compat_dialog)

AdaptiveApp.create_steam_card = _patched_create_steam_card_compat

def find_existing_archive(prefixes):
    if not os.path.isdir(DOWNLOAD_DIR):
        return None
    for f in sorted(os.listdir(DOWNLOAD_DIR)):
        if f.lower().endswith((".zip", ".rar")) and f.lower().startswith(prefixes):
            return os.path.join(DOWNLOAD_DIR, f)
    return None

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
        for tool in ['7z', '7za', 'unrar']:
            if shutil.which(tool):
                try:
                    if tool == 'unrar':
                        cmd = [tool, 'x', '-y', dest_path, extract_dir + os.sep]
                    else:
                        cmd = [tool, 'x', '-y', f'-o{extract_dir}', dest_path]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    if res.returncode == 0:
                        return
                except Exception:
                    continue
        try:
            import rarfile
            with rarfile.RarFile(dest_path) as rf:
                rf.extractall(extract_dir)
            return
        except Exception:
            pass
        raise ValueError("RAR archive detected, but no extraction tool ('7z' or 'unrar') or 'rarfile' module is available.")
    
    raise ValueError("Downloaded file is neither a valid ZIP nor a supported RAR archive.")

# --- Update FirebaseDownloadWorker extraction ---
_original_firebase_run = FirebaseDownloadWorker.run

def _patched_firebase_run(self):
    # Override zip check in worker run by intercepting extraction
    pass  # We handle extraction cleanly via extract_archive in custom workers

# Patch FirebaseDownloadWorker to support archives
def _new_firebase_worker_run(self):
    dest_path = None
    try:
        total_size = self.expected_size
        if total_size <= 0 and not self._is_cancelled:
            try:
                head_req = urllib.request.Request(self.download_url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(head_req, timeout=5) as head_resp:
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

            ext = ".zip"
            cd = response.headers.get('Content-Disposition')
            if cd and 'filename=' in cd:
                file_name = cd.split('filename=')[-1].strip('"\'')
            else:
                url_path = urllib.parse.unquote(response.geturl().split('?')[0])
                possible_name = os.path.basename(url_path)
                if possible_name.lower().endswith(('.zip', '.rar')):
                    file_name = possible_name
                    ext = os.path.splitext(possible_name)[1]
                else:
                    file_name = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', self.title)}{ext}"

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
            if dest_path and os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except Exception:
                    pass
            self.failed.emit("CANCELLED")
        else:
            self.failed.emit(str(e))

FirebaseDownloadWorker.run = _new_firebase_worker_run

# --- Update GameDownloadWorker extraction for AE / NCZ2 ---
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

                ext = ".zip"
                cd = response.headers.get('Content-Disposition')
                if cd and 'filename=' in cd:
                    file_name = cd.split('filename=')[-1].strip('"\'')
                    if file_name.lower().endswith('.rar'):
                        ext = '.rar'
                else:
                    url_path = urllib.parse.unquote(response.geturl().split('?')[0])
                    possible_name = os.path.basename(url_path)
                    if possible_name.lower().endswith(('.zip', '.rar')):
                        file_name = possible_name
                        ext = os.path.splitext(possible_name)[1]
                    else:
                        file_name = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', self.title)}{ext}"

                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
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
        elif sys.platform.startswith("linux"):
            # If it's a rar file on Linux and no bash script is designed for it, extract directly or run conversion
            self.status_update.emit("Extracting / converting game archive...")
            extract_archive(dest_path, installed_game_dir(self.title))
            if downloaded_here:
                try:
                    os.remove(dest_path)
                except Exception:
                    pass
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
    progress = pyqtSignal(int, int, float)
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

    def run(self):
        dest_path = self.existing_zip_path
        try:
            downloaded_here = False
            if not dest_path or not os.path.exists(dest_path):
                downloaded_here = True
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

                    ext = ".zip"
                    cd = response.headers.get('Content-Disposition')
                    if cd and 'filename=' in cd:
                        file_name = cd.split('filename=')[-1].strip('"\'')
                        if file_name.lower().endswith('.rar'):
                            ext = '.rar'
                    else:
                        url_path = urllib.parse.unquote(response.geturl().split('?')[0])
                        possible_name = os.path.basename(url_path)
                        if possible_name.lower().endswith(('.zip', '.rar')):
                            file_name = possible_name
                            ext = os.path.splitext(possible_name)[1]
                        else:
                            file_name = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', self.title)}{ext}"

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
                if downloaded_here and dest_path and os.path.exists(dest_path):
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
                if dest_path and os.path.exists(dest_path):
                    try:
                        os.remove(dest_path)
                    except Exception:
                        pass
                self.failed.emit("CANCELLED")
            else:
                self.failed.emit(str(e))

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
            
        self.worker.progress.connect(self.on_progress)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

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
            self.info_label.setText(f"Speed: {speed_str} | {dl_mb:.1f} MB / {total_mb:.1f} MB")
        else:
            self.progress_bar.setRange(0, 0)
            self.status_label.setText("Downloading...")
            self.info_label.setText(f"Speed: {speed_str} | Downloaded: {dl_mb:.1f} MB")

    def on_finished(self, zip_path):
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
        self.status_label.setText("Failed")
        self.status_label.setStyleSheet(f"color: {RED};")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.action_btn.setText("Copy Error")
        try:
            self.action_btn.clicked.disconnect()
        except Exception:
            pass
        self.action_btn.clicked.connect(lambda: QApplication.clipboard().setText(error_msg))

if hasattr(AdaptiveApp, "_original_create_steam_card_compat"):
    AdaptiveApp.create_steam_card = AdaptiveApp._original_create_steam_card_compat

AdaptiveApp._original_create_steam_card_compat = AdaptiveApp.create_steam_card

def _patched_create_steam_card_compat(self, appid, title_text, cover_path):
    AdaptiveApp._original_create_steam_card_compat(self, appid, title_text, cover_path)
    card = self.steam_cards.get(appid)
    if card:
        btn_play = card.findChild(QPushButton, "Primary")
        status_label = card.findChild(QLabel, "CardStatus")
        
        def update_compat_launch():
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

                def launch_game_compat():
                    if sys.platform.startswith("win"):
                        try:
                            subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
                        except Exception as e:
                            QMessageBox.warning(self, "Launch Game", f"Couldn't launch game: {e}")
                    elif sys.platform.startswith("linux"):
                        compat_config = load_compat_config()
                        proton_bin = compat_config.get(str(appid), "")
                        
                        if proton_bin and os.path.exists(proton_bin):
                            compat_data_path = os.path.join(game_dir, "compat_data")
                            os.makedirs(compat_data_path, exist_ok=True)
                            env = os.environ.copy()
                            env["STEAM_COMPAT_DATA_PATH"] = compat_data_path
                            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = os.path.expanduser("~/.local/share/Steam")
                            try:
                                subprocess.Popen([proton_bin, "run", exe_path], cwd=os.path.dirname(exe_path), env=env)
                            except Exception as e:
                                QMessageBox.warning(self, "Launch Error", f"Couldn't start Proton: {e}")
                        elif self.wine:
                            wine_path, _ = self.wine
                            try:
                                subprocess.Popen([wine_path, exe_path], cwd=os.path.dirname(exe_path), env=system_env())
                            except Exception as e:
                                QMessageBox.warning(self, "Launch using wine", f"Couldn't start wine: {e}")
                        else:
                            QMessageBox.warning(self, "Compatibility Tool Required", "No valid compatibility tool or Wine found.")
                    else:
                        QMessageBox.warning(self, "Platform", "Launching Windows games is not supported on this platform.")

                btn_play.clicked.connect(launch_game_compat)
            else:
                btn_play.setText("Install")
                if status_label:
                    status_label.setText("Steam game")
                    status_label.setStyleSheet("")

                def on_steam_install(_c=False, a=appid, t=title_text, p=cover_path):
                    link, expected_size = get_steam_download_link(a)
                    if not link:
                        QMessageBox.information(self, "Link Unavailable", "Contact MM33 to give this game a link.")
                        return
                    
                    safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', t)
                    existing_file = None
                    if os.path.isdir(DOWNLOAD_DIR):
                        for f in os.listdir(DOWNLOAD_DIR):
                            if f.lower().startswith(safe_title.lower()) and f.lower().endswith(('.zip', '.rar')):
                                existing_file = f
                                break

                    existing_zip_path = None
                    if existing_file:
                        full_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, existing_file))
                        dlg = ExistingFileDialog(existing_file, self)
                        if dlg.exec() == QDialog.DialogCode.Accepted:
                            if dlg.choice == "use":
                                existing_zip_path = full_path
                            elif dlg.choice == "overwrite":
                                try:
                                    os.remove(full_path)
                                except Exception:
                                    pass
                        else:
                            return

                    card_widget = DownloadCard(t, link, expected_size, p, self, existing_zip_path=existing_zip_path)
                    
                    def on_dl_finished(_path):
                        update_compat_launch()
                    card_widget.worker.finished.connect(on_dl_finished)
                    
                    self.downloads_page.add_download_card(card_widget)
                    self.nav_group.button(6).setChecked(True)
                    self.pages.setCurrentWidget(self.downloads_page)

                btn_play.clicked.connect(on_steam_install)

        update_compat_launch()

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

AdaptiveApp.create_steam_card = _patched_create_steam_card_compat

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

# --- Patch FirebaseDownloadWorker filename parsing ---
_original_firebase_run_fn = FirebaseDownloadWorker.run

def _patched_firebase_run_with_safe_name(self):
    # We override the filename resolution step inside the run method via monkeypatch or wrapper
    pass

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


# --- Patch GameDownloadWorker filename parsing for AE / NCZ2 ---
def _new_game_run(self):
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

                file_name = safe_filename_from_headers(response, self.title, ".zip")

                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
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
            project_root = os.path.abspath(DOWNLOAD_DIR)
            os.makedirs(GAMES_DIR, exist_ok=True)
            script_name = self.linux_cmd.rsplit("./", 1)[-1].strip() if "./" in self.linux_cmd else "installer.sh"

            full_cmd = self.linux_cmd.replace(f"./{script_name}", f'echo "{GAMES_DIR}" | bash ./{script_name}')
            inner_cmd = (
                f'cd "{project_root}" && {full_cmd}; '
                f'rm -f "{dest_path}"; '
                f'echo "Press ENTER to exit..."; read'
            )

            term_cmd = None
            for term, args in [
                ("kitty", f"bash -c {json.dumps(inner_cmd)}"),
                ("alacritty", f"-e bash -c {json.dumps(inner_cmd)}"),
                ("foot", f"bash -c {json.dumps(inner_cmd)}"),
                ("gnome-terminal", f"-- bash -c {json.dumps(inner_cmd)}"),
                ("konsole", f"-e bash -c {json.dumps(inner_cmd)}"),
                ("xfce4-terminal", f'-e "bash -c {json.dumps(inner_cmd)}"'),
                ("tilix", f"-e bash -c {json.dumps(inner_cmd)}"),
                ("xterm", f"-e bash -c {json.dumps(inner_cmd)}"),
                ("x-terminal-emulator", f"-e bash -c {json.dumps(inner_cmd)}"),
            ]:
                if shutil.which(term):
                    term_cmd = f"{term} {args}"
                    break

            if not term_cmd:
                term_cmd = f"bash -c {json.dumps(inner_cmd)}"

            proc = subprocess.Popen(term_cmd, shell=True, cwd=project_root)
            proc.wait()
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

GameDownloadWorker.run = _new_game_run
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

                ext = ".zip"
                cd = response.headers.get('Content-Disposition')
                if cd and 'filename=' in cd:
                    file_name = cd.split('filename=')[-1].strip('"\'')
                    if file_name.lower().endswith('.rar'):
                        ext = '.rar'
                else:
                    url_path = urllib.parse.unquote(response.geturl().split('?')[0])
                    possible_name = os.path.basename(url_path)
                    if possible_name.lower().endswith(('.zip', '.rar')):
                        file_name = possible_name
                        ext = os.path.splitext(possible_name)[1]
                    else:
                        file_name = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', self.title)}{ext}"

                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                dest_path = os.path.abspath(os.path.join(DOWNLOAD_DIR, file_name))
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
            project_root = os.path.abspath(DOWNLOAD_DIR)
            os.makedirs(GAMES_DIR, exist_ok=True)
            script_name = self.linux_cmd.rsplit("./", 1)[-1].strip() if "./" in self.linux_cmd else "installer.sh"

            full_cmd = self.linux_cmd.replace(f"./{script_name}", f'echo "{GAMES_DIR}" | bash ./{script_name}')
            inner_cmd = (
                f'cd "{project_root}" && {full_cmd}; '
                f'rm -f "{dest_path}"; '
                f'echo "Press ENTER to exit..."; read'
            )

            term_cmd = None
            for term, args in [
                ("kitty", f"bash -c {json.dumps(inner_cmd)}"),
                ("alacritty", f"-e bash -c {json.dumps(inner_cmd)}"),
                ("foot", f"bash -c {json.dumps(inner_cmd)}"),
                ("gnome-terminal", f"-- bash -c {json.dumps(inner_cmd)}"),
                ("konsole", f"-e bash -c {json.dumps(inner_cmd)}"),
                ("xfce4-terminal", f'-e "bash -c {json.dumps(inner_cmd)}"'),
                ("tilix", f"-e bash -c {json.dumps(inner_cmd)}"),
                ("xterm", f"-e bash -c {json.dumps(inner_cmd)}"),
                ("x-terminal-emulator", f"-e bash -c {json.dumps(inner_cmd)}"),
            ]:
                if shutil.which(term):
                    term_cmd = f"{term} {args}"
                    break

            if not term_cmd:
                term_cmd = f"bash -c {json.dumps(inner_cmd)}"

            proc = subprocess.Popen(term_cmd, shell=True, cwd=project_root)
            proc.wait()
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

if __name__ == "__main__":
    main()
