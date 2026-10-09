import os
import sys
import re
import time
import json
import shlex
import shutil
import zipfile
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import platform
import threading
import webbrowser
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QGraphicsOpacityEffect,
    QDialog, QProgressBar, QFormLayout, QLineEdit, QScrollArea, QComboBox, QMessageBox
)
from PyQt6.QtCore import QSize, Qt, QRectF, QPropertyAnimation, QSequentialAnimationGroup, QPoint, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QPainterPath, QPalette, QColor, QIcon

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

FIREBASE_API_KEY = "AIzaSyBEphV3IipXeUUIpgP6XYrtPG3RrZ-wPt4"
FIREBASE_DB_URL = "https://ncz-games-launcher-default-rtdb.europe-west1.firebasedatabase.app"

def asset_path(filename):
    return os.path.join(ASSETS_DIR, filename)

def installed_game_dir(game_name):
    return os.path.join(GAMES_DIR, game_name)

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
    settings = {"dark_mode": "system"}
    path = get_launcher_settings_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                mode = str(loaded.get("dark_mode", "system")).lower()
                if mode in ("off", "on", "system"):
                    settings["dark_mode"] = mode
                opts = loaded.get("launch_options", {})
                if isinstance(opts, dict):
                    settings["launch_options"] = {
                        str(k): str(v) for k, v in opts.items() if isinstance(v, str)
                    }
        except Exception:
            pass
    settings.setdefault("launch_options", {})
    return settings

def save_launcher_settings(settings):
    path = get_launcher_settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=4)

def get_launch_options(game_id):
    return load_launcher_settings().get("launch_options", {}).get(game_id, "")

def set_launch_options(game_id, text):
    settings = load_launcher_settings()
    options = settings.setdefault("launch_options", {})
    text = text.strip()
    if text:
        options[game_id] = text
    else:
        options.pop(game_id, None)
    save_launcher_settings(settings)

def parse_launch_options(text):
    text = (text or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text, posix=not sys.platform.startswith("win"))
    except ValueError:
        return text.split()

CLOUD_TIMEOUT = 20
FIREBASE_AUTH_URL = "https://identitytoolkit.googleapis.com/v1/accounts:{action}?key={key}"
FIREBASE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token?key={key}"

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
    try:
        os.remove(get_account_path())
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

def build_dark_palette():
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(45, 45, 45))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(230, 230, 230))
    palette.setColor(QPalette.ColorRole.Base, QColor(32, 32, 32))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(45, 45, 45))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(45, 45, 45))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(230, 230, 230))
    palette.setColor(QPalette.ColorRole.Text, QColor(230, 230, 230))
    palette.setColor(QPalette.ColorRole.Button, QColor(58, 58, 58))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(230, 230, 230))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 80, 80))
    palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(140, 140, 140))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(130, 130, 130))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(130, 130, 130))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(130, 130, 130))
    return palette

def build_light_palette():
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(240, 240, 240))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(20, 20, 20))
    palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(245, 245, 245))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(20, 20, 20))
    palette.setColor(QPalette.ColorRole.Text, QColor(20, 20, 20))
    palette.setColor(QPalette.ColorRole.Button, QColor(240, 240, 240))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(20, 20, 20))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(200, 0, 0))
    palette.setColor(QPalette.ColorRole.Link, QColor(0, 100, 200))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(120, 120, 120))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(140, 140, 140))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(140, 140, 140))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(140, 140, 140))
    return palette

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
    app.setPalette(build_dark_palette() if use_dark else build_light_palette())

class JsonEditorDialog(QDialog):
    def __init__(self, file_path, title="Edit Save File", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedSize(360, 420)
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
        btn_save.setFixedHeight(32)
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

class LaunchOptionsDialog(QDialog):
    def __init__(self, game_id, game_name, parent=None):
        super().__init__(parent)
        self.game_id = game_id
        self.setWindowTitle(f"Launch Options - {game_name}")
        self.setFixedSize(420, 170)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 15)
        layout.setSpacing(10)

        label = QLabel("Command-line arguments passed to the game when you press Play:")
        label.setWordWrap(True)
        layout.addWidget(label)

        self.options_edit = QLineEdit(get_launch_options(game_id))
        self.options_edit.setPlaceholderText("e.g. --windowed --fps 60")
        self.options_edit.setClearButtonEnabled(True)
        self.options_edit.returnPressed.connect(self.save_and_close)
        layout.addWidget(self.options_edit)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_cancel = QPushButton("Cancel")
        btn_save = QPushButton("Save")
        btn_save.setDefault(True)
        btn_cancel.clicked.connect(self.reject)
        btn_save.clicked.connect(self.save_and_close)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        layout.addLayout(btn_row)

    def save_and_close(self):
        try:
            set_launch_options(self.game_id, self.options_edit.text())
        except Exception as e:
            QMessageBox.warning(self, "Launch Options", f"Couldn't save launch options: {e}")
            return
        self.accept()

class ExistingFileDialog(QDialog):
    def __init__(self, filename, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Existing File Found")
        self.setFixedSize(380, 130)
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

    def __init__(self, version_url, game_name, linux_cmd, existing_zip_path=None):
        super().__init__()
        self.version_url = version_url
        self.game_name = game_name
        self.linux_cmd = linux_cmd
        self.existing_zip_path = existing_zip_path

    def run(self):
        try:
            if self.existing_zip_path and os.path.exists(self.existing_zip_path):
                dest_path = self.existing_zip_path
            else:
                req = urllib.request.Request(self.version_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as resp:
                    content = resp.read().decode('utf-8')

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

            if sys.platform.startswith("linux"):
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

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setFixedSize(320, 120)
        self.setModal(True)

        layout = QFormLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        self.dark_mode_combo = QComboBox()
        self.dark_mode_combo.addItem("Off", "off")
        self.dark_mode_combo.addItem("On", "on")
        self.dark_mode_combo.addItem("System", "system")

        current_mode = load_launcher_settings().get("dark_mode", "system")
        index = self.dark_mode_combo.findData(current_mode)
        self.dark_mode_combo.setCurrentIndex(index if index >= 0 else self.dark_mode_combo.findData("system"))
        self.dark_mode_combo.currentIndexChanged.connect(self.on_dark_mode_changed)

        layout.addRow(QLabel("Dark Mode"), self.dark_mode_combo)

    def on_dark_mode_changed(self):
        mode = self.dark_mode_combo.currentData()
        settings = load_launcher_settings()
        settings["dark_mode"] = mode
        save_launcher_settings(settings)
        apply_dark_mode(mode)

class CreditsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Credits")
        self.setFixedSize(400, 160)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Credits")
        title_font = title.font()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        jsmm33 = QLabel("jsmm33 - Development of the Tool")
        jsmm33.setWordWrap(True)
        jsmm33.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(jsmm33)

        pigeon = QLabel("mr.fancypigeon - Linux Conversion Script and Ideas")
        pigeon.setWordWrap(True)
        pigeon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(pigeon)

        layout.addStretch()

class OtherStuffDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Other Stuff")
        self.setFixedSize(220, 175)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        btn_account = QPushButton("Account")
        btn_settings = QPushButton("Settings")
        btn_credits = QPushButton("Credits")
        btn_account.setFixedHeight(32)
        btn_settings.setFixedHeight(32)
        btn_credits.setFixedHeight(32)
        btn_account.clicked.connect(self.open_account)
        btn_settings.clicked.connect(self.open_settings)
        btn_credits.clicked.connect(self.open_credits)

        layout.addWidget(btn_account)
        layout.addWidget(btn_settings)
        layout.addWidget(btn_credits)

    def open_account(self):
        dialog = AccountDialog(self)
        dialog.exec()

    def open_settings(self):
        dialog = SettingsDialog(self)
        dialog.exec()

    def open_credits(self):
        dialog = CreditsDialog(self)
        dialog.exec()

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

class AccountDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Account")
        self.setModal(True)
        self.signup_mode = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

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
        form.addRow(QLabel("Email"), self.email_edit)
        form.addRow(QLabel("Password"), self.password_edit)
        form.addRow(self.confirm_label, self.confirm_edit)
        layout.addWidget(self.form_widget)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #d9534f;")
        layout.addWidget(self.status_label)

        self.primary_btn = QPushButton()
        self.primary_btn.setFixedHeight(32)
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

        self.logout_btn = QPushButton("Log Out")
        self.logout_btn.setFixedHeight(32)
        self.logout_btn.clicked.connect(self.log_out)
        layout.addWidget(self.logout_btn)

        layout.addStretch()

        self.password_edit.returnPressed.connect(self.submit)
        self.confirm_edit.returnPressed.connect(self.submit)
        self.refresh_view()

    def refresh_view(self):
        self.status_label.setText("")
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

        if not configured:
            self.title_label.setText("Account")
            self.info_label.setText("Cloud sync isn't set up in this build of the launcher yet.")
            self.setFixedSize(340, 150)
        elif logged_in:
            self.title_label.setText("Account")
            self.info_label.setText(
                f"Signed in as\n{acct.get('email', 'your account')}\n\n"
                "Use Save Data and Sync Data under each game (Manage) to move your progress between devices.")
            self.setFixedSize(340, 225)
        elif self.signup_mode:
            self.title_label.setText("Create Account")
            self.info_label.setText("Create an account to sync your saves and settings across devices.")
            self.primary_btn.setText("Create Account")
            self.toggle_btn.setText("Already have an account? Log in")
            self.setFixedSize(340, 370)
        else:
            self.title_label.setText("Log In")
            self.info_label.setText("Log in to sync your saves and settings across devices.")
            self.primary_btn.setText("Log In")
            self.toggle_btn.setText("Need an account? Create one")
            self.setFixedSize(340, 345)

    def toggle_mode(self):
        self.signup_mode = not self.signup_mode
        self.refresh_view()

    def submit(self):
        email = self.email_edit.text().strip()
        password = self.password_edit.text()
        if not email or "@" not in email:
            self.status_label.setText("Enter a valid email address.")
            return
        if len(password) < 6:
            self.status_label.setText("Password must be at least 6 characters.")
            return
        if self.signup_mode and password != self.confirm_edit.text():
            self.status_label.setText("Passwords don't match.")
            return

        create = self.signup_mode
        busy = BusyDialog("Creating your account..." if create else "Logging in...",
                          lambda: cloud_sign_in(email, password, create=create), self)
        ok, _value, error = busy.run()
        if ok:
            self.password_edit.clear()
            self.confirm_edit.clear()
            self.accept()
        else:
            self.status_label.setText(error)

    def forgot_password(self):
        email = self.email_edit.text().strip()
        if not email or "@" not in email:
            self.status_label.setText("Type your email above first, then click Forgot password.")
            return
        busy = BusyDialog("Sending reset email...", lambda: cloud_send_password_reset(email), self)
        ok, _value, error = busy.run()
        if ok:
            QMessageBox.information(self, "Password Reset",
                                    "If an account exists for that email, a reset link is on its way.")
        else:
            self.status_label.setText(error)

    def log_out(self):
        clear_account()
        self.signup_mode = False
        self.refresh_view()

class DownloadDialog(QDialog):
    def __init__(self, version_url, game_name, linux_cmd, parent=None, existing_zip_path=None):
        super().__init__(parent)
        self.setWindowTitle("Downloading Game")
        self.setFixedSize(380, 140)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 20, 20, 20)

        self.status_label = QLabel("Connecting...")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)

        self.info_label = QLabel("Speed: 0 KB/s | Downloaded: 0 MB")
        layout.addWidget(self.info_label)

        self.worker = DownloadWorker(version_url, game_name, linux_cmd, existing_zip_path)
        self.worker.progress.connect(self.on_progress)
        self.worker.status_update.connect(self.on_status_update)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_status_update(self, message):
        self.status_label.setText(message)
        self.progress_bar.setRange(0, 0)
        self.info_label.setText("Please check terminal / wait...")

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
        self.accept()

    def on_failed(self, error_msg):
        self.status_label.setText("Download failed!")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet("QProgressBar::chunk { background-color: red; }")
        self.info_label.setText("Error details copied to clipboard.")
        QApplication.clipboard().setText(error_msg)

class AdaptiveApp(QMainWindow):
    def __init__(self):
        super().__init__()

        self.btn_ae_action = None
        self.btn_ae_uninstall = None
        self.btn_ae_save = None
        self.btn_ae_settings = None

        self.btn_ncz2_action = None
        self.btn_ncz2_uninstall = None
        self.btn_ncz2_save = None
        self.btn_ncz2_settings = None

        self.sync_buttons = {}

        self.setFixedSize(QSize(800, 720))
        self.setWindowTitle("NCZ Games Launcher")

        icon_path = asset_path("icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.center_on_screen()

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(30, 25, 30, 25)

        main_layout.addSpacing(15)

        welcome_label = QLabel("Welcome to NCZ Games Launcher! Please select a game to get started.")
        font = welcome_label.font()
        font.setPointSize(12)
        font.setBold(True)
        welcome_label.setFont(font)
        welcome_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(welcome_label)

        main_layout.addSpacing(25)

        top_games_container = QHBoxLayout()
        top_games_container.setSpacing(40)

        game1_layout = self.create_game_column("Five Nights<br>at NCZ AE<br>", asset_path("fnanczaecover.png"), game_id="ae")
        game2_layout = self.create_game_column("Five Nights<br>at NCZ<br>2", asset_path("fnancz2cover.png"), game_id="ncz2")

        top_games_container.addLayout(game1_layout)
        top_games_container.addLayout(game2_layout)

        main_layout.addLayout(top_games_container)
        main_layout.addSpacing(20)

        bottom_games_container = QHBoxLayout()
        game3_layout = self.create_game_column("NCZFront", asset_path("nczfront-cover.png"), game_id="nczfront")
        bottom_games_container.addLayout(game3_layout)
        bottom_games_container.setAlignment(Qt.AlignmentFlag.AlignCenter)

        main_layout.addLayout(bottom_games_container)

        main_layout.addStretch()

        other_stuff_btn = QPushButton("Other Stuff")
        other_stuff_btn.setFixedWidth(160)
        other_stuff_btn.clicked.connect(self.open_other_stuff)
        main_layout.addWidget(other_stuff_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        main_layout.addSpacing(10)

        self.check_nczfront_status()

    def check_nczfront_status(self):
        if hasattr(self, 'nczfront_status_label'):
            self.nczfront_status_label.setText("Checking...")
            self.nczfront_status_label.setStyleSheet("color: gray; font-weight: bold;")
        self.ping_thread = PingWorker("https://desktopsob7i.tail441aca.ts.net/")
        self.ping_thread.result.connect(self.on_nczfront_ping_result)
        self.ping_thread.start()

    def on_nczfront_ping_result(self, is_hosted):
        if hasattr(self, 'nczfront_status_label'):
            if is_hosted:
                self.nczfront_status_label.setText("Hosted")
                self.nczfront_status_label.setStyleSheet("color: #2ee650; font-weight: bold;")
            else:
                self.nczfront_status_label.setText("Not Hosted")
                self.nczfront_status_label.setStyleSheet("color: #f44336; font-weight: bold;")

    def update_ae_button_state(self):
        if not self.btn_ae_action:
            return
        game_dir = installed_game_dir(AE_GAME_NAME)

        try:
            self.btn_ae_action.clicked.disconnect()
        except Exception:
            pass

        if os.path.exists(game_dir):
            self.btn_ae_action.setText("Play")
            self.btn_ae_action.clicked.connect(self.launch_ae_game)
            if self.btn_ae_uninstall:
                self.btn_ae_uninstall.show()
        else:
            self.btn_ae_action.setText("Install")
            self.btn_ae_action.clicked.connect(self.start_ae_install)
            if self.btn_ae_uninstall:
                self.btn_ae_uninstall.hide()

        save_path = get_ae_save_path()
        if self.btn_ae_save:
            self.btn_ae_save.setEnabled(os.path.exists(save_path))

        settings_path = get_ae_settings_path()
        if self.btn_ae_settings:
            self.btn_ae_settings.setEnabled(os.path.exists(settings_path))

        self.refresh_sync_buttons("ae")

    def launch_ae_game(self):
        game_dir = installed_game_dir(AE_GAME_NAME)
        if sys.platform.startswith("win"):
            exe_path = os.path.join(game_dir, "FNaNCZ AE.exe")
            if os.path.exists(exe_path):
                subprocess.Popen([exe_path] + parse_launch_options(get_launch_options("ae")), cwd=game_dir)
        elif sys.platform.startswith("linux"):
            sh_path = os.path.join(game_dir, "run.sh")
            if os.path.exists(sh_path):
                os.chmod(sh_path, 0o755)
                subprocess.Popen(["bash", sh_path] + parse_launch_options(get_launch_options("ae")), cwd=game_dir)

    def uninstall_ae_game(self):
        game_dir = installed_game_dir(AE_GAME_NAME)
        if os.path.exists(game_dir):
            shutil.rmtree(game_dir, ignore_errors=True)
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
        version_url = "https://raw.githubusercontent.com/justmm33/FNaNCZ-AE-Releases/refs/heads/main/version.txt"
        linux_cmd = "rm -f fnancz-installer2.sh >/dev/null 2>&1 && wget https://raw.githubusercontent.com/AmrThePigeon/FNANCZAE1_Script_Builder/refs/heads/main/fnancz-installer2.sh --no-cache && chmod +x fnancz-installer2.sh && ./fnancz-installer2.sh"

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
        game_dir = installed_game_dir(NCZ2_GAME_NAME)

        try:
            self.btn_ncz2_action.clicked.disconnect()
        except Exception:
            pass

        if os.path.exists(game_dir):
            self.btn_ncz2_action.setText("Play")
            self.btn_ncz2_action.clicked.connect(self.launch_ncz2_game)
            if self.btn_ncz2_uninstall:
                self.btn_ncz2_uninstall.show()
        else:
            self.btn_ncz2_action.setText("Install")
            self.btn_ncz2_action.clicked.connect(self.start_ncz2_install)
            if self.btn_ncz2_uninstall:
                self.btn_ncz2_uninstall.hide()

        save_path = get_ncz2_save_path()
        if self.btn_ncz2_save:
            self.btn_ncz2_save.setEnabled(os.path.exists(save_path))

        settings_path = get_ncz2_settings_path()
        if self.btn_ncz2_settings:
            self.btn_ncz2_settings.setEnabled(os.path.exists(settings_path))

        self.refresh_sync_buttons("ncz2")

    def launch_ncz2_game(self):
        game_dir = installed_game_dir(NCZ2_GAME_NAME)
        if sys.platform.startswith("win"):
            exe_path = os.path.join(game_dir, "FNANCZ 2.exe")
            if os.path.exists(exe_path):
                subprocess.Popen([exe_path] + parse_launch_options(get_launch_options("ncz2")), cwd=game_dir)
        elif sys.platform.startswith("linux"):
            sh_path = os.path.join(game_dir, "run.sh")
            if os.path.exists(sh_path):
                os.chmod(sh_path, 0o755)
                subprocess.Popen(["bash", sh_path] + parse_launch_options(get_launch_options("ncz2")), cwd=game_dir)

    def uninstall_ncz2_game(self):
        game_dir = installed_game_dir(NCZ2_GAME_NAME)
        if os.path.exists(game_dir):
            shutil.rmtree(game_dir, ignore_errors=True)
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
        version_url = "https://raw.githubusercontent.com/justmm33/FNaNCZ-AE-Releases/refs/heads/main/version-2.txt"
        linux_cmd = "rm -f fnancz2-installer1.sh >/dev/null 2>&1 && wget https://raw.githubusercontent.com/AmrThePigeon/FNANCZAE2_Script_Builder/refs/heads/main/fnancz2-installer1.sh --no-cache && chmod +x fnancz2-installer1.sh && ./fnancz2-installer1.sh"

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

    def open_launch_options(self, game_id):
        name = {"ae": AE_GAME_NAME, "ncz2": NCZ2_GAME_NAME}.get(game_id, game_id)
        LaunchOptionsDialog(game_id, name, self).exec()

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
            AccountDialog(self).exec()
        return load_account() is not None

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

    def create_game_column(self, html_title_text, image_filename, game_id):
        column_layout = QVBoxLayout()
        column_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        row_layout = QHBoxLayout()
        row_layout.setSpacing(15)
        row_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        cover_label = QLabel()
        cover_label.setFixedSize(120, 180)

        if os.path.exists(image_filename):
            rounded_cover = self.get_rounded_pixmap(image_filename, 120, 180, 12)
            if rounded_cover:
                cover_label.setPixmap(rounded_cover)
            else:
                cover_label.setText("[ Error\nLoading ]")
                cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cover_label.setStyleSheet("border: 1px dashed red; color: red;")
        else:
            cover_label.setText("[ Missing\nCover ]")
            cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cover_label.setStyleSheet("border: 1px dashed gray; border-radius: 12px; color: gray;")

        details_layout = QVBoxLayout()
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        title_header_layout = QHBoxLayout()
        title_header_layout.setContentsMargins(0, 0, 0, 0)

        title_label = QLabel()
        title_label.setText(html_title_text)
        title_font = title_label.font()
        title_font.setPointSize(11)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setWordWrap(True)
        title_label.setFixedSize(130, 75 if game_id != "nczfront" else 30)
        title_header_layout.addWidget(title_label)

        back_button = QPushButton("✕")
        back_button.setFixedSize(22, 22)
        back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        back_button.setStyleSheet("""
            QPushButton {
                border: none; border-radius: 11px; background-color: transparent;
                font-weight: bold; color: gray;
            }
            QPushButton:hover { background-color: rgba(0,0,0,0.1); color: red; }
        """)
        back_button.hide()
        title_header_layout.addWidget(back_button, alignment=Qt.AlignmentFlag.AlignTop)

        details_layout.addLayout(title_header_layout)

        if game_id == "nczfront":
            self.nczfront_status_label = QLabel("Checking...")
            status_font = self.nczfront_status_label.font()
            status_font.setBold(True)
            self.nczfront_status_label.setFont(status_font)
            self.nczfront_status_label.setStyleSheet("color: gray;")
            details_layout.addWidget(self.nczfront_status_label)

        details_layout.addSpacing(8)

        manage_container = QWidget()
        manage_container.setFixedSize(160, 247 if game_id != "nczfront" else 100)

        manage_button = QPushButton("Manage", manage_container)
        manage_button.setFixedWidth(100)
        manage_button.move(0, 0)

        actions_widget = QWidget(manage_container)
        actions_widget.hide()
        actions_layout = QVBoxLayout(actions_widget)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(4)

        if game_id == "nczfront":
            btn_play = QPushButton("Play")
            btn_refresh = QPushButton("Refresh")
            btn_play.setFixedWidth(150)
            btn_refresh.setFixedWidth(150)
            btn_play.clicked.connect(lambda: webbrowser.open("https://desktopsob7i.tail441aca.ts.net/"))
            btn_refresh.clicked.connect(self.check_nczfront_status)
            actions_layout.addWidget(btn_play)
            actions_layout.addWidget(btn_refresh)
        else:
            btn_action = QPushButton("Install")
            btn_uninstall = QPushButton("Uninstall")
            btn_save = QPushButton("Edit Current Save")
            btn_settings = QPushButton("Edit Current Settings")
            btn_launch_opts = QPushButton("Launch Options")
            btn_launch_opts.setToolTip("Command-line arguments passed to the game when it starts")
            btn_launch_opts.clicked.connect(lambda _checked=False, g=game_id: self.open_launch_options(g))
            btn_cloud_save = QPushButton("Save Data")
            btn_cloud_sync = QPushButton("Sync Data")
            btn_cloud_save.setToolTip("Upload this device's save and settings to your account")
            btn_cloud_sync.setToolTip("Download your account's save and settings to this device")
            btn_cloud_save.clicked.connect(lambda _checked=False, g=game_id: self.cloud_save_data(g))
            btn_cloud_sync.clicked.connect(lambda _checked=False, g=game_id: self.cloud_sync_data(g))
            self.sync_buttons[game_id] = (btn_cloud_save, btn_cloud_sync)

            if game_id == "ae":
                self.btn_ae_action = btn_action
                self.btn_ae_uninstall = btn_uninstall
                self.btn_ae_save = btn_save
                self.btn_ae_settings = btn_settings
                btn_uninstall.clicked.connect(self.uninstall_ae_game)
                btn_save.clicked.connect(self.open_ae_save_editor)
                btn_settings.clicked.connect(self.open_ae_settings_editor)
                self.update_ae_button_state()
            elif game_id == "ncz2":
                self.btn_ncz2_action = btn_action
                self.btn_ncz2_uninstall = btn_uninstall
                self.btn_ncz2_save = btn_save
                self.btn_ncz2_settings = btn_settings
                btn_uninstall.clicked.connect(self.uninstall_ncz2_game)
                btn_save.clicked.connect(self.open_ncz2_save_editor)
                btn_settings.clicked.connect(self.open_ncz2_settings_editor)
                self.update_ncz2_button_state()

            for btn in [btn_action, btn_uninstall, btn_launch_opts, btn_save, btn_settings, btn_cloud_save, btn_cloud_sync]:
                btn.setFixedWidth(150)
                actions_layout.addWidget(btn)

        self.register_toggle_events(manage_button, actions_widget, back_button)

        details_layout.addWidget(manage_container)

        row_layout.addWidget(cover_label)
        row_layout.addLayout(details_layout)
        column_layout.addLayout(row_layout)

        return column_layout

    def register_toggle_events(self, manage_btn, actions_frame, back_btn):
        def run_forward_animation():
            manage_btn.setEnabled(False)
            opacity = QGraphicsOpacityEffect(manage_btn)
            manage_btn.setGraphicsEffect(opacity)

            slide = QPropertyAnimation(manage_btn, b"pos")
            slide.setDuration(220)
            slide.setStartValue(QPoint(0, 0))
            slide.setEndValue(QPoint(0, -15))

            fade = QPropertyAnimation(opacity, b"opacity")
            fade.setDuration(180)
            fade.setStartValue(1.0)
            fade.setEndValue(0.0)

            manage_btn._group = QSequentialAnimationGroup()
            manage_btn._group.addAnimation(slide)
            manage_btn._group.addAnimation(fade)

            def complete():
                manage_btn.hide()
                actions_frame.show()
                back_btn.show()

            manage_btn._group.finished.connect(complete)
            manage_btn._group.start()

        def run_reverse_animation():
            back_btn.hide()
            actions_frame.hide()
            manage_btn.show()

            opacity = QGraphicsOpacityEffect(manage_btn)
            manage_btn.setGraphicsEffect(opacity)

            slide = QPropertyAnimation(manage_btn, b"pos")
            slide.setDuration(220)
            slide.setStartValue(QPoint(0, -15))
            slide.setEndValue(QPoint(0, 0))

            fade = QPropertyAnimation(opacity, b"opacity")
            fade.setDuration(180)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)

            manage_btn._reverse_group = QSequentialAnimationGroup()
            manage_btn._reverse_group.addAnimation(slide)
            manage_btn._reverse_group.addAnimation(fade)

            def complete_return():
                manage_btn.setEnabled(True)
                manage_btn.setGraphicsEffect(None)

            manage_btn._reverse_group.finished.connect(complete_return)
            manage_btn._reverse_group.start()

        manage_btn.clicked.connect(run_forward_animation)
        back_btn.clicked.connect(run_reverse_animation)

    def open_other_stuff(self):
        dialog = OtherStuffDialog(self)
        dialog.exec()

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
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
