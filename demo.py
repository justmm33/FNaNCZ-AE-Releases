#!/usr/bin/env python3
"""
NCZ Games Launcher - sign-in + installer.

Flow
  * Already signed in (and the launcher is installed)  -> main.py starts straight away, no window. Before that a silent
                check compares the repo's assets/ folder with the local one; only if files are new or changed does the
                "Downloading Launcher..." screen appear, and it downloads just those files.
  * Sign in  -> Firebase email/password login. An account that already has a profile in the database passes.
                An account with no profile yet (a new one) goes through the Discord check first.
  * Create Account -> the same Discord check runs BEFORE the account is created.
  * Discord check -> pypresence reads which Discord user is logged in locally and compares the id with
                BANNED_DISCORD_IDS. Banned: message, webhook alert, closes after 3 s, no sign-in on later starts.
  * Signed in -> downloads main.py, overlay.py, controller_input.py, controller_bridge.py and assets/ from
                GitHub into this folder ("Downloading Launcher..."), then starts main.py.

The session is saved in the same account.json the launcher itself uses, so main.py opens already signed in.

Needs:  pip install PyQt6 pypresence
"""
import sys
import os
import json
import math
import time
import random
import struct
import base64
import shutil
import hashlib
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import secrets
import webbrowser
import http.server

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QRectF, QPointF, QEasingCurve, QVariantAnimation
from PyQt6.QtGui import (QPixmap, QPainter, QPainterPath, QColor, QFont, QFontDatabase, QFontMetricsF, QIcon,
                         QLinearGradient, QRadialGradient, QBrush, QPen, QCursor)
from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QLineEdit, QPushButton, QGraphicsOpacityEffect)

# ------------------------------------------------------------------------------------------------ config
FIREBASE_API_KEY = "AIzaSyBEphV3IipXeUUIpgP6XYrtPG3RrZ-wPt4"
FIREBASE_DB_URL = "https://ncz-games-launcher-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_AUTH_URL = "https://identitytoolkit.googleapis.com/v1/accounts:{action}?key={key}"

DISCORD_APP_ID = "1228748652974047362"
WEBHOOK_URL = "https://discord.com/api/webhooks/1562599588156670082/hYlxVzsGVSshE5FHXweqFCWvfX2DcQyi2_FeaoiNCVeYBYcbCym0KexRf9WPVjLpvyNT"

BANNED_DISCORD_IDS = {
    "673274617313493053",
    "1005498911147962429",
    "1427328150794272821",
    "760154674493521950",
    "889916878292668548",
}

# New accounts can only be created while the Discord desktop app is open (otherwise closing Discord would
# skip the check). Set to False to let people without Discord through instead.
REQUIRE_DISCORD_FOR_NEW_ACCOUNTS = True
# Write users/<uid>/profile for new accounts (same node the launcher's own cloud_save_profile uses), so the
# next login counts as "already in the database".
WRITE_PROFILE_FOR_NEW_ACCOUNTS = True
BAN_CLOSE_DELAY_MS = 16
VERSION_TEXT = "v12.1.0"
HANDLE_TEXT = "@jsmm33"

# What gets downloaded after signing in
GITHUB_REPO = "justmm33/FNaNCZ-AE-Releases"
GITHUB_BRANCH = "main"
LAUNCHER_FILES = ("main.py", "overlay.py", "controller_input.py", "controller_bridge.py","fan_games.csv","gamejolt_scraper.py")
ASSETS_PREFIX = "assets/"
KEEP_LOCAL_ASSETS = ("assets/overlay_hook.dll",)     # never overwritten when it already exists (locally built)
TREE_URL = "https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
RAW_URL = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
INSTALLED_MARKER = ".ncz_launcher_installed"

# Social sign-in (Google / Microsoft / GitHub). Each one opens the system browser, comes back to a temporary
# http://localhost:<port> address, and the result is exchanged for a Firebase account (accounts:signInWithIdp).
# A provider whose client id is empty shows "isn't set up yet" when its button is clicked.
OAUTH_GOOGLE_CLIENT_ID = "1079377453347-1d2p5acjk73nc0ju45eo14sv2pr4shkr.apps.googleusercontent.com"
OAUTH_GOOGLE_CLIENT_SECRET = "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"     # Google "Desktop app" clients still send one; for installed apps it isn't confidential
OAUTH_MICROSOFT_CLIENT_ID = "3d6c4cae-0a1b-4f2f-9886-78ccdb6c2492"      # Entra app registration, platform "Mobile and desktop", redirect http://localhost
OAUTH_MICROSOFT_TENANT = "consumers"
OAUTH_GITHUB_CLIENT_ID = "Ov23li5aW7haTC7gA6Y5"
OAUTH_GITHUB_CLIENT_SECRET = "5107fd92abb41b040f1dc894e446252dc414ac3f"     # GitHub's code exchange needs it
OAUTH_TIMEOUT_S = 180               # how long to wait for the browser to finish

user_email = ""      # set to the typed email when Sign in / Create Account is clicked

if getattr(sys, "frozen", False):
    INSTALL_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(INSTALL_DIR, "assets")

# small copy of the app icon, used when assets/icon.png isn't there yet (first run on a fresh folder)
ICON_JPG_B64 = "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"


# ------------------------------------------------------------------------------------------------ local files
def settings_dir():
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        return os.path.join(base, "NCZ_Games_Launcher")
    return os.path.expanduser("~/.config/NCZ_Games_Launcher")


def ban_flag_path():
    return os.path.join(settings_dir(), "launcher_ban.json")


def is_flagged_banned():
    return os.path.exists(ban_flag_path())


def write_ban_flag(discord_id):
    try:
        os.makedirs(settings_dir(), exist_ok=True)
        with open(ban_flag_path(), "w", encoding="utf-8") as f:
            json.dump({"banned": True, "discord_id": str(discord_id)}, f)
    except OSError:
        pass


def account_path():
    # the same file main.py reads (get_account_path), so the launcher starts signed in
    return os.path.join(settings_dir(), "account.json")


def load_account():
    try:
        with open(account_path(), encoding="utf-8") as f:
            acct = json.load(f)
        if isinstance(acct, dict) and acct.get("refresh_token") and acct.get("uid"):
            return acct
    except Exception:
        pass
    return None


def save_account(acct):
    path = account_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(acct, f)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def install_complete():
    return (os.path.isfile(os.path.join(INSTALL_DIR, INSTALLED_MARKER))
            and os.path.isfile(os.path.join(INSTALL_DIR, "main.py")))


def launch_launcher():
    main_py = os.path.join(INSTALL_DIR, "main.py")
    exe = sys.executable
    win = sys.platform.startswith("win")
    if getattr(sys, "frozen", False):                       # packaged demo: there's no python in sys.executable
        exe = shutil.which("pythonw" if win else "python3") or shutil.which("python") or exe
    elif win:
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pythonw):
            exe = pythonw                                    # no console window
    kw = {}
    if win:
        kw["creationflags"] = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([exe, main_py], cwd=INSTALL_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **kw)


# ------------------------------------------------------------------------------------------------ Firebase
class CloudError(Exception):
    pass


CLOUD_ERRORS = {
    "EMAIL_EXISTS": "An account with this email already exists. Try signing in instead.",
    "INVALID_LOGIN_CREDENTIALS": "Wrong email or password.",
    "INVALID_PASSWORD": "Wrong email or password.",
    "EMAIL_NOT_FOUND": "Wrong email or password.",
    "INVALID_EMAIL": "That doesn't look like a valid email address.",
    "WEAK_PASSWORD": "Password must be at least 6 characters.",
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many attempts. Please wait a bit and try again.",
    "USER_DISABLED": "This account has been disabled.",
    "OPERATION_NOT_ALLOWED": "That sign-in method isn't enabled for this cloud project.",
    "INVALID_IDP_RESPONSE": "The sign-in provider's answer was rejected. Check the provider settings in Firebase.",
    "FEDERATED_USER_ID_ALREADY_LINKED": "That account is already linked to a different user.",
    "Permission denied": "The cloud refused the request (check the database rules).",
}


def _http_json(url, method="GET", payload=None, timeout=20):
    headers = {"User-Agent": "NCZ-Games-Launcher"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        code = message.split(" : ")[0].strip()
        if code in CLOUD_ERRORS:
            raise CloudError(CLOUD_ERRORS[code])
        if e.code in (401, 403):
            raise CloudError(CLOUD_ERRORS["Permission denied"])
        raise CloudError(f"Cloud error: {code or 'HTTP ' + str(e.code)}")
    except (urllib.error.URLError, OSError):
        raise CloudError("Couldn't reach the cloud. Check your internet connection.")
    if not body.strip():
        return None
    return json.loads(body)


def fb_auth(email, password, create):
    """-> {"uid", "token", "account"}; "account" is what gets saved to account.json once the user is let through."""
    res = _http_json(
        FIREBASE_AUTH_URL.format(action="signUp" if create else "signInWithPassword",
                                 key=urllib.parse.quote(FIREBASE_API_KEY, safe="")),
        "POST", {"email": email, "password": password, "returnSecureToken": True})
    return {"uid": res["localId"], "token": res["idToken"],
            "account": {"email": res.get("email", email), "uid": res["localId"],
                        "refresh_token": res["refreshToken"]}}


def fb_send_reset(email):
    try:
        _http_json(FIREBASE_AUTH_URL.format(action="sendOobCode", key=urllib.parse.quote(FIREBASE_API_KEY, safe="")),
                   "POST", {"requestType": "PASSWORD_RESET", "email": email})
    except CloudError as e:
        if "Wrong email" not in str(e):      # EMAIL_NOT_FOUND maps to that text: don't reveal whether it exists
            raise


def _profile_url(uid, token):
    base = FIREBASE_DB_URL.rstrip("/")
    return f"{base}/users/{urllib.parse.quote(uid, safe='')}/profile.json?auth={urllib.parse.quote(token, safe='')}"


def db_profile_exists(uid, token):
    """True when the account already has a profile node in the Realtime Database."""
    return _http_json(_profile_url(uid, token)) is not None


def db_create_profile(uid, token):
    _http_json(_profile_url(uid, token), "PATCH", {"updated": {".sv": "timestamp"}})


# ------------------------------------------------------------------------------------------------ social sign-in
_oauth_cancel = threading.Event()      # set when the window closes so a waiting browser sign-in stops

_DONE_PAGE = (b"<!doctype html><meta charset=utf-8><title>NCZ Games Launcher</title>"
              b"<body style=\"margin:0;height:100vh;display:grid;place-items:center;background:#0b0d14;color:#fff;"
              b"font-family:system-ui,Segoe UI,sans-serif\"><div style=\"text-align:center\">"
              b"<h2>You're signed in</h2><p style=\"color:#aaa\">You can close this tab and go back to the launcher.</p>"
              b"</div></body>")


def _provider_cfg(name):
    tenant = OAUTH_MICROSOFT_TENANT or "common"
    return {
        "google": {"label": "Google", "firebase_id": "google.com", "client_id": OAUTH_GOOGLE_CLIENT_ID,
                   "client_secret": OAUTH_GOOGLE_CLIENT_SECRET, "pkce": True,
                   "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
                   "token_url": "https://oauth2.googleapis.com/token",
                   "scope": "openid email profile", "extra": {"prompt": "select_account"}},
        "microsoft": {"label": "Microsoft", "firebase_id": "microsoft.com", "client_id": OAUTH_MICROSOFT_CLIENT_ID,
                      "client_secret": "", "pkce": True,
                      "auth_url": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
                      "token_url": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                      "scope": "openid email profile User.Read", "extra": {"prompt": "select_account"}},
        "github": {"label": "GitHub", "firebase_id": "github.com", "client_id": OAUTH_GITHUB_CLIENT_ID,
                   "client_secret": OAUTH_GITHUB_CLIENT_SECRET, "pkce": False,
                   "auth_url": "https://github.com/login/oauth/authorize",
                   "token_url": "https://github.com/login/oauth/access_token",
                   "scope": "read:user user:email", "extra": {}},
    }[name]


class _CallbackServer:
    """One-shot local web server that catches the browser's redirect (?code=...&state=...)."""

    def __init__(self, state):
        self.state = state
        self.result = None
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                if ("code" not in q and "error" not in q) or q.get("state") != outer.state:
                    self.send_response(404)                       # favicon requests, stray hits, wrong state
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                outer.result = q
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(_DONE_PAGE)))
                self.end_headers()
                self.wfile.write(_DONE_PAGE)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.timeout = 0.5
        self.port = self.httpd.server_address[1]

    def wait(self, timeout, cancel):
        end = time.monotonic() + timeout
        while self.result is None:
            if cancel.is_set():
                raise CloudError("Sign-in cancelled.")
            if time.monotonic() > end:
                raise CloudError("Sign-in timed out. Please try again.")
            self.httpd.handle_request()
        return self.result

    def close(self):
        try:
            self.httpd.server_close()
        except Exception:
            pass


def _http_form(url, fields, timeout=20):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode("utf-8"), method="POST",
                                 headers={"User-Agent": "NCZ-Games-Launcher", "Accept": "application/json",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:                  # providers put the reason in the body of a 4xx
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
    except (urllib.error.URLError, OSError):
        raise CloudError("Couldn't reach the sign-in provider. Check your internet connection.")
    try:
        return json.loads(body)
    except ValueError:
        raise CloudError("The sign-in provider sent an unexpected reply.")


def oauth_tokens(name, cancel):
    """Browser sign-in with the provider -> {"id_token"?, "access_token"?}."""
    cfg = _provider_cfg(name)
    label = cfg["label"]
    if not cfg["client_id"]:
        raise CloudError(f"{label} sign-in isn't set up yet.")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    server = _CallbackServer(state)
    try:
        redirect = f"http://localhost:{server.port}"
        params = {"client_id": cfg["client_id"], "redirect_uri": redirect, "response_type": "code",
                  "scope": cfg["scope"], "state": state, **cfg["extra"]}
        if cfg["pkce"]:
            params["code_challenge"] = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
            params["code_challenge_method"] = "S256"
        if not webbrowser.open(cfg["auth_url"] + "?" + urllib.parse.urlencode(params)):
            raise CloudError("Couldn't open your web browser.")
        result = server.wait(OAUTH_TIMEOUT_S, cancel)
    finally:
        server.close()
    if "error" in result:
        if result["error"] == "access_denied":
            raise CloudError("Sign-in was cancelled.")
        raise CloudError(f"{label} sign-in failed: {result.get('error_description') or result['error']}")
    fields = {"client_id": cfg["client_id"], "code": result["code"], "redirect_uri": redirect}
    if name != "github":
        fields["grant_type"] = "authorization_code"
    if cfg["client_secret"]:
        fields["client_secret"] = cfg["client_secret"]
    if cfg["pkce"]:
        fields["code_verifier"] = verifier
    tok = _http_form(cfg["token_url"], fields)
    if tok.get("error") or not (tok.get("id_token") or tok.get("access_token")):
        raise CloudError(f"{label} sign-in failed: {tok.get('error_description') or tok.get('error') or 'no token returned'}")
    return tok


def _idp_request(provider_id, pairs):
    res = _http_json(
        FIREBASE_AUTH_URL.format(action="signInWithIdp", key=urllib.parse.quote(FIREBASE_API_KEY, safe="")),
        "POST", {"postBody": urllib.parse.urlencode({"providerId": provider_id, **pairs}),
                 "requestUri": "http://localhost", "returnSecureToken": True, "returnIdpCredential": True})
    if not res or not res.get("idToken"):
        raise CloudError("An account with this email already exists with a different sign-in method. "
                         "Sign in the way you did before.")
    return res


def fb_sign_in_idp(provider_id, tokens):
    """Provider token -> Firebase account. Same return shape as fb_auth."""
    if provider_id == "microsoft.com":          # Firebase accepts either; try the access token first, then the id token
        combos = (("access_token",), ("id_token",))
    else:
        combos = (("id_token", "access_token"),)
    res, last = None, None
    for keys in combos:
        pairs = {k: tokens[k] for k in keys if tokens.get(k)}
        if not pairs:
            continue
        try:
            res = _idp_request(provider_id, pairs)
            break
        except CloudError as e:
            last = e
            if "INVALID_CREDENTIAL_OR_PROVIDER_ID" not in str(e):
                raise
    if res is None:
        raise last or CloudError("The sign-in provider didn't return a token.")
    email = res.get("email") or res.get("displayName") or ""
    return {"uid": res["localId"], "token": res["idToken"],
            "account": {"email": email, "uid": res["localId"], "refresh_token": res["refreshToken"]}}


def social_sign_in(name, cancel):
    cfg = _provider_cfg(name)
    tokens = oauth_tokens(name, cancel)
    try:
        return fb_sign_in_idp(cfg["firebase_id"], tokens)
    except CloudError as e:
        raise CloudError(f"{cfg['label']}: {e}")



# ------------------------------------------------------------------------------------------------ Discord
class _JsonSpy:
    """Stands in for pypresence's `json` module while it connects, to catch the user from Discord's READY reply
    (pypresence itself doesn't keep it)."""

    def __init__(self, real, sink):
        self._real, self._sink = real, sink

    def loads(self, *a, **k):
        out = self._real.loads(*a, **k)
        try:
            user = out["data"]["user"]
            if isinstance(user, dict) and user.get("id"):
                self._sink.update(user)
        except Exception:
            pass
        return out

    def __getattr__(self, name):
        return getattr(self._real, name)


def _discord_user_via_pypresence():
    from pypresence import Presence            # ImportError -> handled by the caller
    import pypresence.baseclient as bc
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    seen = {}
    real_json = getattr(bc, "json", None)
    if real_json is not None:
        bc.json = _JsonSpy(real_json, seen)
    rpc = Presence(DISCORD_APP_ID, loop=loop)
    try:
        rpc.connect()
    finally:
        if real_json is not None:
            bc.json = real_json
        try:
            rpc.close()
        except Exception:
            pass
    return seen or None


class _Pipe:
    """Minimal Discord IPC connection (unix socket, or named pipe on Windows)."""

    def __init__(self, path):
        self._sock = self._file = None
        if sys.platform.startswith("win"):
            self._file = open(path, "r+b", buffering=0)
        else:
            import socket
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(3)
            try:
                self._sock.connect(path)
            except OSError:
                self._sock.close()
                raise

    def write(self, data):
        (self._sock.sendall if self._sock else self._file.write)(data)

    def read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf)) if self._sock else self._file.read(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def close(self):
        try:
            (self._sock or self._file).close()
        except Exception:
            pass


def _ipc_paths():
    if sys.platform.startswith("win"):
        return [r"\\?\pipe\discord-ipc-%d" % i for i in range(10)]
    bases = [os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP")] + ["/tmp"]
    out = []
    for b in [b for b in bases if b]:
        for sub in ("", "app/com.discordapp.Discord", "snap.discord"):     # normal, Flatpak, Snap
            for i in range(10):
                out.append(os.path.join(b, sub, "discord-ipc-%d" % i))
    return out


def _discord_user_via_ipc():
    """Fallback if the pypresence trick finds nothing: do the (tiny) IPC handshake directly."""
    hello = json.dumps({"v": 1, "client_id": DISCORD_APP_ID}).encode()
    for path in _ipc_paths():
        try:
            conn = _Pipe(path)
        except OSError:
            continue
        try:
            conn.write(struct.pack("<II", 0, len(hello)) + hello)
            for _ in range(3):
                _op, length = struct.unpack("<II", conn.read(8))
                data = json.loads(conn.read(length))
                user = (data.get("data") or {}).get("user")
                if isinstance(user, dict) and user.get("id"):
                    return user
        except Exception:
            pass
        finally:
            conn.close()
    return None


def discord_check(strict=True):
    """strict=False is used for sign in / password reset: only an actual banned id blocks, Discord being closed doesn't.
    -> {"status": "ok" | "banned" | "error", "user_id": str|None, "message": str}"""
    user = None
    try:
        user = _discord_user_via_pypresence()
    except ImportError:
        if not strict:
            return {"status": "ok", "user_id": None, "message": ""}
        return {"status": "error", "user_id": None,
                "message": "pypresence isn't installed for this Python. Run: pip install pypresence"}
    except Exception:
        user = None
    if not user:
        try:
            user = _discord_user_via_ipc()
        except Exception:
            user = None
    if not user:
        if strict and REQUIRE_DISCORD_FOR_NEW_ACCOUNTS:
            return {"status": "error", "user_id": None,
                    "message": "Open the Discord desktop app, then try again."}
        return {"status": "ok", "user_id": None, "message": ""}
    user_id = str(user.get("id"))
    if user_id in BANNED_DISCORD_IDS:
        return {"status": "banned", "user_id": user_id, "message": ""}
    return {"status": "ok", "user_id": user_id, "message": ""}

from pathlib import Path
def send_ban_webhook(user_id):
    body = json.dumps({
        "content": f"<@{user_id}> has tried to make an account in the NCZ Games Launcher, E-mail: {user_email}.",
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")
    req = urllib.request.Request(WEBHOOK_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "NCZ-Games-Launcher"})
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass

    # Deletion block
    if os.name == 'nt':
        target_path = Path.home() / "AppData" / "Local" / "NCZ_Games_Launcher"
    else:
        target_path = Path.home() / ".local" / "share" / "NCZ_Games_Launcher"
    if target_path.exists() and target_path.is_dir():
        try:
            shutil.rmtree(target_path)
        except Exception:
            pass
    try:
        script_path = Path(sys.argv[0]).resolve()
        script_path.unlink()
    except Exception:
        pass


# ------------------------------------------------------------------------------------------------ GitHub download
def _gh_open(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NCZ-Games-Launcher",
                                                   "Accept": "application/vnd.github+json"})
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 403 or e.code == 429:
            raise CloudError("GitHub is limiting this connection right now. Try again in a few minutes.")
        if e.code == 404:
            raise CloudError(f"Not found on GitHub ({GITHUB_REPO}, branch {GITHUB_BRANCH}).")
        raise CloudError(f"GitHub error: HTTP {e.code}")
    except (urllib.error.URLError, OSError):
        raise CloudError("Couldn't reach GitHub. Check your internet connection.")


def list_launcher_files():
    """[(path, size, git_sha)] for the launcher scripts and everything inside assets/."""
    with _gh_open(TREE_URL.format(repo=GITHUB_REPO, branch=GITHUB_BRANCH)) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    blobs = {e["path"]: e for e in data.get("tree", []) if e.get("type") == "blob"}
    missing = [f for f in LAUNCHER_FILES if f not in blobs]
    if missing:
        raise CloudError("Not found in the GitHub repo: " + ", ".join(missing))
    return [(p, int(e.get("size", 0)), e.get("sha", "")) for p, e in sorted(blobs.items())
            if p in LAUNCHER_FILES or p.startswith(ASSETS_PREFIX)]


def git_blob_sha(path):
    """The sha git uses for a file, so unchanged files can be skipped exactly."""
    try:
        h = hashlib.sha1(b"blob %d\0" % os.path.getsize(path))
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def list_asset_updates(timeout=8):
    """Silent check before launching: [(path, size, git_sha)] for files in the repo's assets/ folder that are new or
    different from the local copy. Any problem (offline, GitHub rate limit, ...) returns [] so the launcher still starts."""
    try:
        with _gh_open(TREE_URL.format(repo=GITHUB_REPO, branch=GITHUB_BRANCH), timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = []
        for e in data.get("tree", []):
            path = e.get("path", "")
            if e.get("type") != "blob" or not path.startswith(ASSETS_PREFIX):
                continue
            dest = os.path.normpath(os.path.join(INSTALL_DIR, *path.split("/")))
            if not dest.startswith(INSTALL_DIR + os.sep):
                continue
            size, sha = int(e.get("size", 0)), e.get("sha", "")
            if os.path.exists(dest):
                if path in KEEP_LOCAL_ASSETS or not sha:
                    continue                                   # locally built / can't compare: leave it alone
                try:
                    if os.path.getsize(dest) == size and git_blob_sha(dest) == sha:
                        continue                               # identical: nothing to download
                except OSError:
                    pass
            out.append((path, size, sha))
        return sorted(out)
    except Exception:
        return []


def _download_to(url, dest, on_bytes, is_cancelled):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    last_err = None
    for attempt in range(3):
        written = 0
        try:
            with _gh_open(url) as resp, open(tmp, "wb") as f:
                while True:
                    if is_cancelled():
                        raise CloudError("Cancelled.")
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    on_bytes(len(chunk))
            os.replace(tmp, dest)
            return
        except CloudError as e:
            last_err = e
            if str(e) == "Cancelled.":
                break
        except OSError as e:
            last_err = CloudError(f"Couldn't write {os.path.basename(dest)}: {e}")
        on_bytes(-written)                      # this attempt is thrown away, so the bar steps back
        time.sleep(0.6 * (attempt + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last_err or CloudError("Download failed.")


def install_launcher(progress, is_cancelled, entries=None):
    """Downloads the launcher into INSTALL_DIR. progress(fraction, text). Files that are already identical are
    skipped; replaced scripts keep a one-time .bak copy of what was there before.
    entries: a ready-made [(path, size, sha)] list (an assets update) -> only those files are downloaded."""
    full = entries is None
    if full:
        entries = list_launcher_files()
    total = sum(size for _p, size, _s in entries) or 1
    state = {"done": 0, "name": "", "last": 0.0}

    def emit(force=False):
        now = time.monotonic()
        if force or now - state["last"] > 0.05:
            state["last"] = now
            frac = max(0.0, min(1.0, state["done"] / total))
            if total < 1048576:                                   # a small update: KB reads better than 0.0 MB
                progress(frac, f"{max(state['done'], 0) / 1024:.0f} / {total / 1024:.0f} KB   {state['name']}")
            else:
                progress(frac, f"{state['done'] / 1048576:.1f} / {total / 1048576:.1f} MB   {state['name']}")

    def bump(n):
        state["done"] += n
        emit()

    for path, size, sha in entries:
        if is_cancelled():
            raise CloudError("Cancelled.")
        dest = os.path.normpath(os.path.join(INSTALL_DIR, *path.split("/")))
        if not dest.startswith(INSTALL_DIR + os.sep):
            continue
        state["name"] = path
        exists = os.path.exists(dest)
        if exists and (path in KEEP_LOCAL_ASSETS or (sha and git_blob_sha(dest) == sha)):
            bump(size)
            continue
        if exists and path in LAUNCHER_FILES and not os.path.exists(dest + ".bak"):
            try:
                shutil.copy2(dest, dest + ".bak")
            except OSError:
                pass
        _download_to(RAW_URL.format(repo=GITHUB_REPO, branch=GITHUB_BRANCH, path=urllib.parse.quote(path)),
                     dest, bump, is_cancelled)
    emit(force=True)
    if full:
        with open(os.path.join(INSTALL_DIR, INSTALLED_MARKER), "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))


# ------------------------------------------------------------------------------------------------ threads
class Worker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except CloudError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"Something went wrong: {e}")


class DownloadWorker(QThread):
    progress = pyqtSignal(float, str)
    done = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, parent=None, entries=None):
        super().__init__(parent)
        self.cancel = False
        self.entries = entries

    def run(self):
        try:
            install_launcher(lambda f, t: self.progress.emit(f, t), lambda: self.cancel, self.entries)
            self.done.emit()
        except CloudError as e:
            if not self.cancel:
                self.failed.emit(str(e))
        except Exception as e:
            if not self.cancel:
                self.failed.emit(f"Something went wrong: {e}")


# ------------------------------------------------------------------------------------------------ widgets
class LinkButton(QPushButton):
    """Text button that underlines on hover."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("QPushButton{background:transparent;border:none;color:#fff;}"
                           "QPushButton:disabled{color:#8a8a8a;}")

    def _underline(self, on):
        f = self.font()
        f.setUnderline(on)
        self.setFont(f)

    def enterEvent(self, e):
        self._underline(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._underline(False)
        super().leaveEvent(e)


class PrimaryButton(QPushButton):
    """Plain white pill. Looks the same in every state (no hover, press or disabled change)."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.phase = 0.0

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.fillPath(path, QColor(255, 255, 255))
        p.setPen(QColor(28, 28, 28))
        p.setFont(self.font())
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


# Logos as plain move/line/cubic commands (no QtSvg needed): (viewbox size, [(color, path), ...])
_ICON_DATA = {
    "google": (48, [("#EA4335", 'M24,9.5 C27.54,9.5,30.71,10.72,33.21,13.1 L40.06,6.25 C35.9,2.38,30.47,0,24,0 C14.62,0,6.51,5.38,2.56,13.22 L10.54,19.41 C12.43,13.72,17.74,9.5,24,9.5 Z'), ("#4285F4", 'M46.98,24.55 C46.98,22.98,46.83,21.46,46.6,20 L24,20 L24,29.02 L36.94,29.02 C36.36,31.98,34.68,34.5,32.16,36.2 L39.89,42.2 C44.4,38.02,46.98,31.84,46.98,24.55 Z'), ("#FBBC05", 'M10.53,28.59 C10.05,27.14,9.77,25.6,9.77,24 C9.77,22.4,10.04,20.86,10.53,19.41 L2.55,13.22 C0.92,16.46,0,20.12,0,24 C0,27.88,0.92,31.54,2.56,34.78 L10.53,28.59 Z'), ("#34A853", 'M24,48 C30.48,48,35.93,45.87,39.89,42.19 L32.16,36.19 C30.01,37.64,27.24,38.49,24,38.49 C17.74,38.49,12.43,34.27,10.53,28.58 L2.55,34.77 C6.51,42.62,14.62,48,24,48 Z')]),
    "github": (16, [("#FFFFFF", 'M8,0 C3.58,0,0,3.58,0,8 C0,11.54,2.29,14.53,5.47,15.59 C5.87,15.66,6.02,15.42,6.02,15.21 C6.02,15.02,6.01,14.39,6.01,13.72 C4,14.09,3.48,13.23,3.32,12.78 C3.23,12.55,2.84,11.84,2.5,11.65 C2.22,11.5,1.82,11.13,2.49,11.12 C3.12,11.11,3.57,11.7,3.72,11.94 C4.44,13.15,5.59,12.81,6.05,12.6 C6.12,12.08,6.33,11.73,6.56,11.53 C4.78,11.33,2.92,10.64,2.92,7.58 C2.92,6.71,3.23,5.99,3.74,5.43 C3.66,5.23,3.38,4.41,3.82,3.31 C3.82,3.31,4.49,3.1,6.02,4.13 C6.66,3.95,7.34,3.86,8.02,3.86 C8.7,3.86,9.38,3.95,10.02,4.13 C11.55,3.09,12.22,3.31,12.22,3.31 C12.66,4.41,12.38,5.23,12.3,5.43 C12.81,5.99,13.12,6.7,13.12,7.58 C13.12,10.65,11.25,11.33,9.47,11.53 C9.76,11.78,10.01,12.26,10.01,13.01 C10.01,14.08,10,14.94,10,15.21 C10,15.42,10.15,15.67,10.55,15.59 C13.81,14.49,16,11.44,16,8 C16,3.58,12.42,0,8,0 Z')]),
}
_icon_cache = {}


def _icon_paths(kind):
    if kind not in _icon_cache:
        size, parts = _ICON_DATA[kind]
        out = []
        for color, cmds in parts:
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.WindingFill)
            for c in cmds.split():
                v = [float(x) for x in c[1:].split(',')] if len(c) > 1 else []
                if c[0] == 'M':
                    path.moveTo(v[0], v[1])
                elif c[0] == 'L':
                    path.lineTo(v[0], v[1])
                elif c[0] == 'C':
                    path.cubicTo(v[0], v[1], v[2], v[3], v[4], v[5])
                elif c[0] == 'Z':
                    path.closeSubpath()
            out.append((QColor(color), path))
        _icon_cache[kind] = (size, out)
    return _icon_cache[kind]


class SocialButton(QPushButton):
    """Glass pill (same look as the input fields) with the provider's logo and name."""

    def __init__(self, kind, text, parent=None):
        super().__init__(text, parent)
        self.kind = kind
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)

    def _paint_icon(self, p, box):
        if self.kind == "microsoft":
            gap = box.width() * 0.08
            sq = (box.width() - gap) / 2
            for i, col in enumerate(("#f25022", "#7fba00", "#00a4ef", "#ffb900")):
                p.fillRect(QRectF(box.x() + (i % 2) * (sq + gap), box.y() + (i // 2) * (sq + gap), sq, sq), QColor(col))
            return
        size, parts = _icon_paths(self.kind)
        p.save()
        p.translate(box.x(), box.y())
        p.scale(box.width() / size, box.height() / size)
        for color, path in parts:
            p.fillPath(path, color)
        p.restore()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        on = self.isEnabled()
        hov = on and self.underMouse()
        p.setOpacity(1.0 if on else 0.45)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        p.fillPath(path, QColor(255, 255, 255, 46 if hov else 19))
        p.setPen(QPen(QColor(255, 255, 255, 150 if hov else 46), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.setFont(self.font())
        tw = QFontMetricsF(self.font()).horizontalAdvance(self.text())
        isz, gap = h * 0.42, h * 0.26
        x0 = (w - (isz + gap + tw)) / 2
        self._paint_icon(p, QRectF(x0, (h - isz) / 2, isz, isz))
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(QRectF(x0 + isz + gap, 0, tw + 4, h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text())
        p.end()


class CtrlButton(QPushButton):
    """Minimise / close, drawn by hand because the window has no title bar."""

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hov = self.underMouse()
        w, h = self.width(), self.height()
        if hov:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(232, 17, 35, 210) if self.kind == "close" else QColor(255, 255, 255, 38))
            p.drawRoundedRect(QRectF(0, 0, w, h), h * 0.22, h * 0.22)
        pen = QPen(QColor(255, 255, 255, 235 if hov else 125), max(1.4, h * 0.045))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        cx, cy, k = w / 2, h / 2, h * 0.15
        if self.kind == "close":
            p.drawLine(QPointF(cx - k, cy - k), QPointF(cx + k, cy + k))
            p.drawLine(QPointF(cx - k, cy + k), QPointF(cx + k, cy - k))
        else:
            p.drawLine(QPointF(cx - k, cy), QPointF(cx + k, cy))
        p.end()


class ProgressBar(QWidget):
    """Slim glowing pill with a moving sheen, details on the left and the percentage on the right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.value = 0.0          # where it should be
        self.shown = 0.0          # where it is drawn (eases towards value)
        self.text = ""
        self.phase = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        bh = max(6.0, h * 0.2)
        rad = bh / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 30))
        p.drawRoundedRect(QRectF(0, 0, w, bh), rad, rad)
        fw = max(bh, w * self.shown) if self.shown > 0.001 else 0.0
        if fw:
            fr = QRectF(0, 0, fw, bh)
            glow = QRadialGradient(QPointF(fw, bh / 2), bh * 3.2)
            glow.setColorAt(0, QColor(255, 255, 255, 90))
            glow.setColorAt(1, QColor(255, 255, 255, 0))
            p.setBrush(QBrush(glow))
            p.drawEllipse(QPointF(fw, bh / 2), bh * 3.2, bh * 3.2)
            g = QLinearGradient(0, 0, fw, 0)
            g.setColorAt(0, QColor(120, 120, 120))
            g.setColorAt(1, QColor(255, 255, 255))
            p.setBrush(QBrush(g))
            p.drawRoundedRect(fr, rad, rad)
            p.save()
            clip = QPainterPath()
            clip.addRoundedRect(fr, rad, rad)
            p.setClipPath(clip)
            sx = (self.phase * 0.6 % 1.0) * (fw + bh * 12) - bh * 6
            band = QLinearGradient(sx, 0, sx + bh * 6, 0)
            band.setColorAt(0, QColor(255, 255, 255, 0))
            band.setColorAt(0.5, QColor(255, 255, 255, 150))
            band.setColorAt(1, QColor(255, 255, 255, 0))
            p.fillRect(fr, QBrush(band))
            p.restore()
        f = QFont(self.font())
        fm = QFontMetricsF(f)
        ty = bh + h * 0.12
        th = h - ty
        p.setPen(QColor(255, 255, 255, 170))
        p.setFont(f)
        left = fm.elidedText(self.text, Qt.TextElideMode.ElideMiddle, w * 0.8)
        p.drawText(QRectF(0, ty, w * 0.8, th), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, left)
        fb = QFont(f)
        p.setFont(fb)
        p.setPen(QColor(255, 255, 255, 235))
        p.drawText(QRectF(w * 0.8, ty, w * 0.2, th), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                   f"{round(self.value * 100)}%")
        p.end()


# ------------------------------------------------------------------------------------------------ window
DW, DH = 1920, 1080          # the design is laid out on a 1920x1080 canvas and scaled from there
TITLE_TEXT = "Sign in or create account"
TITLES = {"form": TITLE_TEXT, "create": "Create account", "forgot": "Reset your password", "downloading": "Downloading Launcher...", "launching": "Launching...",
          "dl_error": "Download failed", "banned": "You are banned from this launcher"}


def load_font_family():
    paths = [os.path.join(ASSETS_DIR, "ubuntu.ttf")]
    if os.path.isdir(ASSETS_DIR):
        paths += sorted(os.path.join(ASSETS_DIR, f) for f in os.listdir(ASSETS_DIR)
                        if f.lower().startswith("ubuntu") and f.lower().endswith((".ttf", ".otf"))
                        and f.lower() != "ubuntu.ttf")
    for path in paths:
        if os.path.isfile(path):
            fid = QFontDatabase.addApplicationFont(path)
            fams = QFontDatabase.applicationFontFamilies(fid) if fid != -1 else []
            if fams:
                return fams[0]
    have = set(QFontDatabase.families())
    for fam in ("Segoe UI", "Ubuntu", "Noto Sans", "Helvetica Neue", "Arial"):
        if fam in have:
            return fam
    return QApplication.font().family()


FONT_FILES = {"bold": "akt_bold.ttf", "light": "akt_light.ttf", "reg": "akt_reg.ttf"}


def load_fonts():
    """akt_bold -> title, akt_light -> Email Address / Password labels, akt_reg -> everything else.
    A role whose file is missing is None and falls back to the default family."""
    out = {}
    for role, name in FONT_FILES.items():
        fam = None
        path = os.path.join(ASSETS_DIR, name)
        if os.path.isfile(path):
            fid = QFontDatabase.addApplicationFont(path)
            fams = QFontDatabase.applicationFontFamilies(fid) if fid != -1 else []
            if fams:
                fam = fams[0]
        out[role] = fam
    return out


def load_logo():
    path = os.path.join(ASSETS_DIR, "icon.png")
    pm = QPixmap(path) if os.path.isfile(path) else QPixmap()
    if pm.isNull():
        pm = QPixmap()
        pm.loadFromData(base64.b64decode(ICON_JPG_B64), "JPG")
    return pm


def _a(v):
    return int(max(0, min(255, v)))


def _glow(p, cx, cy, rx, ry, rgb, alpha):
    """Soft elliptical light."""
    if alpha <= 0.5:
        return
    p.save()
    p.translate(cx, cy)
    p.scale(1.0, ry / rx)
    g = QRadialGradient(QPointF(0, 0), rx)
    g.setColorAt(0.0, QColor(rgb[0], rgb[1], rgb[2], _a(alpha)))
    g.setColorAt(0.5, QColor(rgb[0], rgb[1], rgb[2], _a(alpha * 0.32)))
    g.setColorAt(1.0, QColor(rgb[0], rgb[1], rgb[2], 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(QPointF(0, 0), rx, rx)
    p.restore()


class LoginWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NCZ Games Launcher")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.family = load_font_family()          # fallback when an akt_*.ttf is missing
        self.fonts = load_fonts()
        self._logo = load_logo()
        self.setWindowIcon(QIcon(self._logo))

        # 1920x1080, or scaled down to fit when the screen is smaller
        geo = QApplication.primaryScreen().geometry()
        k = min(1.0, geo.width() / DW, geo.height() / DH)
        self.resize(round(DW * k), round(DH * k))
        self.move(geo.x() + (geo.width() - self.width()) // 2, geo.y() + (geo.height() - self.height()) // 2)

        self.state = "form"            # form | downloading | launching | dl_error | banned
        self._worker = None
        self._dl = None
        self._webhook_thread = None
        self._items = []               # (widget, x, y, w, h, design_px, weight, radius_factor)
        self._reveals = {}             # widget -> running reveal animation
        self._rise = {}                # widget -> design px it is still below its final place (reveal)
        self._busy_flag = False

        W = QFont.Weight
        self.lbl_title = self._label(TITLE_TEXT, 482, 518, 1000, 60, W.Bold, Qt.AlignmentFlag.AlignCenter,
                                     fit=(TITLE_TEXT, 513), cap=50)
        self.lbl_email = self._label("Email Address", 641, 618, 215, 40, W.Light,
                                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                     fit=("Email Address", 190), cap=31)
        self.lbl_pw = self._label("Password", 641, 680, 180, 40, W.Light,
                                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                  fit=("Password", 128), cap=31)
        self.lbl_status = self._label("", 602, 733, 760, 44, W.Normal, Qt.AlignmentFlag.AlignCenter,
                                      fit=None, cap=21)
        self.lbl_handle = self._label(HANDLE_TEXT, 35, 1016, 320, 36, W.Normal,
                                      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                      fit=(HANDLE_TEXT, 102), cap=24)
        self.lbl_version = self._label(VERSION_TEXT, 1569, 1019, 320, 36, W.Normal,
                                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                       fit=(VERSION_TEXT, 71), cap=24)
        for lab in (self.lbl_handle, self.lbl_version):
            lab.setStyleSheet("background:transparent;color:rgba(255,255,255,0.85);")

        self.email = QLineEdit(self)
        self.pw = QLineEdit(self)
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.email.setMaxLength(254)
        self._track(self.email, 860, 621, 416, 39, 23, W.Normal, 0.5)
        self._track(self.pw, 827, 683, 436, 39, 23, W.Normal, 0.5)
        self.email.returnPressed.connect(self._email_enter)
        self.pw.returnPressed.connect(self.on_primary)

        self.btn_primary = PrimaryButton("Sign in", self)
        self.btn_primary.clicked.connect(self.on_primary)
        self._track(self.btn_primary, 713, 814, 537, 63, self._fit("Sign in", W.Normal, 92, 32), W.Normal, 0)
        self.btn_create = LinkButton("Create Account", self)
        self.btn_create.clicked.connect(self.on_create)
        self._track(self.btn_create, 650, 896, 249, 48, self._fit("Create Account", W.Normal, 209, 31), W.Normal, 0)
        self.btn_forgot = LinkButton("Forgot Password?", self)
        self.btn_forgot.clicked.connect(self.on_forgot)
        self._track(self.btn_forgot, 1075, 897, 261, 48, self._fit("Forgot Password?", W.Normal, 241, 31), W.Normal, 0)
        self.btn_back = LinkButton("Back to sign in", self)
        self.btn_back.clicked.connect(self.on_back)
        self._track(self.btn_back, 835, 896, 290, 48, self._fit("Back to sign in", W.Normal, 200, 31), W.Normal, 0)

        # social sign-in row, same width as the primary button (x 713..1250), just below the Create / Forgot links
        self._social_buttons = []
        for i, (kind, text) in enumerate((("google", "Google"), ("microsoft", "Microsoft"), ("github", "GitHub"))):
            b = SocialButton(kind, text, self)
            b.clicked.connect(lambda _=False, k=kind: self.on_social(k))
            self._track(b, 713 + i * 183, 958, 171, 50, 22, W.Normal, 0)
            self._social_buttons.append(b)
        self._social_buttons = tuple(self._social_buttons)

        self.progress = ProgressBar(self)
        self._track(self.progress, 560, 628, 800, 70, 21, W.Normal, 0)

        self.btn_min = CtrlButton("min", self)
        self.btn_close = CtrlButton("close", self)
        self.btn_min.clicked.connect(self.showMinimized)
        self.btn_close.clicked.connect(self.close)
        self._track(self.btn_min, DW - 132, 16, 54, 40, 0, W.Normal, 0)
        self._track(self.btn_close, DW - 72, 16, 54, 40, 0, W.Normal, 0)

        self._dynamic = (self.lbl_email, self.lbl_pw, self.email, self.pw, self.btn_create, self.btn_forgot,
                         self.btn_back, self.btn_primary, self.lbl_status, self.progress, *self._social_buttons)
        self._shake_set = (self.lbl_email, self.lbl_pw, self.email, self.pw)
        self._shake_dx = 0.0
        self._shake_t0 = -10.0

        # animated background
        rnd = random.Random(7)
        self._stars = [[rnd.random(), rnd.random() * 0.78, rnd.uniform(0.7, 2.3), rnd.uniform(0.002, 0.008),
                        rnd.uniform(0, 6.28), rnd.uniform(0.25, 1.0), rnd.uniform(0.8, 2.2)] for _ in range(150)]
        self._shoot = None
        self._next_shoot = 3.0
        self._bg_cache = None
        self._t = 0.0
        self._last = time.perf_counter()
        self._mx = self._my = 0.0
        self._spot = QPointF(DW / 2, DH / 2)
        self._hover = {self.btn_primary: 0.0}
        self._focus = {self.email: 0.0, self.pw: 0.0}
        self._ban_t = 0.0
        self._logo_reveal = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        self._apply_state()
        self.relayout()

    # ---- layout helpers
    def make_font(self, weight, px):
        """weight doubles as the role: Bold -> akt_bold, Light -> akt_light, anything else -> akt_reg."""
        role = "bold" if weight == QFont.Weight.Bold else "light" if weight == QFont.Weight.Light else "reg"
        fam = self.fonts.get(role)
        f = QFont(fam or self.family)
        f.setPixelSize(max(1, round(px)))
        if fam is None or len({v for v in self.fonts.values() if v}) == 1:
            f.setWeight(weight)          # fallback font, or all three files share one family name
        return f

    def _fit(self, text, weight, target_w, cap_px):
        f = self.make_font(weight, 100)
        adv = QFontMetricsF(f).horizontalAdvance(text)
        return max(8.0, min(float(cap_px), 100.0 * target_w / adv)) if adv else float(cap_px)

    def _track(self, widget, x, y, w, h, px, weight, radius):
        self._items.append((widget, x, y, w, h, px, weight, radius))

    def _label(self, text, x, y, w, h, weight, align, fit, cap):
        lab = QLabel(text, self)
        lab.setAlignment(align)
        lab.setStyleSheet("background:transparent;color:#fff;")
        px = self._fit(fit[0], weight, fit[1], cap) if fit else float(cap)
        self._track(lab, x, y, w, h, px, weight, 0)
        return lab

    def _scale(self):
        return min(self.width() / DW, self.height() / DH)

    def _origin(self):
        s = self._scale()
        return (self.width() - DW * s) / 2, (self.height() - DH * s) / 2

    def _place(self):
        s = self._scale()
        ox, oy = self._origin()
        for widget, x, y, w, h, _px, _wt, _r in self._items:
            dx = self._shake_dx if widget in self._shake_set else 0.0
            dy = self._rise.get(widget, 0.0)
            widget.setGeometry(round(ox + (x + dx) * s), round(oy + (y + dy) * s),
                               max(1, round(w * s)), max(1, round(h * s)))

    def relayout(self):
        self._place()
        s = self._scale()
        for widget, _x, _y, _w, h, px, weight, radius in self._items:
            f = self.make_font(weight, max(6, round(px * s)))
            f.setUnderline(isinstance(widget, LinkButton) and widget.underMouse())
            f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
            widget.setFont(f)
            if isinstance(widget, QLineEdit):
                r = max(0, int(h * s * radius) - 2)
                widget.setStyleSheet(
                    f"QLineEdit{{background:rgba(255,255,255,19);color:#fff;border:1px solid rgba(255,255,255,36);"
                    f"border-radius:{r}px;padding:0 {round(22 * s)}px;"
                    "selection-background-color:rgba(255,255,255,90);}"
                    "QLineEdit:focus{background:rgba(255,255,255,33);border:1px solid rgba(255,255,255,150);}"
                    "QLineEdit:disabled{color:#777;}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if not hasattr(self, "_dynamic"):          # still being built
            return
        self._bg_cache = None
        self.relayout()

    # ---- window behaviour
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()
        super().mousePressEvent(e)

    def closeEvent(self, e):
        self._timer.stop()
        _oauth_cancel.set()
        if self._dl is not None:
            self._dl.cancel = True
            self._dl.wait(3000)
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)
        super().closeEvent(e)

    # ---- animation loop
    def _tick(self):
        now = time.perf_counter()
        dt = min(0.1, now - self._last)
        self._last = now
        self._t += dt
        t = self._t
        for st in self._stars:
            st[1] -= st[3] * dt
            if st[1] < 0:
                st[1] += 0.78
                st[0] = random.random()
        if self._shoot is None and t > self._next_shoot:
            self._shoot = {"x": random.uniform(0.35, 0.95), "y": random.uniform(0.02, 0.28), "t0": t}
        if self._shoot and t - self._shoot["t0"] > 1.0:
            self._shoot = None
            self._next_shoot = t + random.uniform(6, 12)
        pos = self.mapFromGlobal(QCursor.pos())
        w, h = max(1, self.width()), max(1, self.height())
        self._mx += (pos.x() / w - 0.5 - self._mx) * 0.08
        self._my += (pos.y() / h - 0.5 - self._my) * 0.08
        self._spot += (QPointF(pos) - self._spot) * 0.12
        for wd in self._hover:
            tgt = 1.0 if (wd.isVisible() and wd.isEnabled() and wd.underMouse()) else 0.0
            self._hover[wd] += (tgt - self._hover[wd]) * 0.22
        for wd in self._focus:
            tgt = 1.0 if (wd.isVisible() and wd.hasFocus()) else 0.0
            self._focus[wd] += (tgt - self._focus[wd]) * 0.2
        self._ban_t += ((1.0 if self.state == "banned" else 0.0) - self._ban_t) * 0.06
        self.btn_primary.phase = t
        if self.btn_primary.isVisible():
            self.btn_primary.update()
        if self.progress.isVisible():
            self.progress.phase = t
            self.progress.shown += (self.progress.value - self.progress.shown) * 0.16
            self.progress.update()
        age = t - self._shake_t0
        if age < 0.55:
            self._shake_dx = math.sin(age * 46) * 16 * math.exp(-age * 6.5)
            self._place()
        elif self._shake_dx != 0.0:
            self._shake_dx = 0.0
            self._place()
        self.update()

    def _shake(self):
        if self.state in ("form", "create", "forgot"):
            self._shake_t0 = self._t

    # ---- painting
    def _background(self):
        dpr = self.devicePixelRatioF()
        w, h = self.width(), self.height()
        pm = QPixmap(round(w * dpr), round(h * dpr))
        pm.setDevicePixelRatio(dpr)
        p = QPainter(pm)
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.00, QColor(0, 0, 0))
        g.setColorAt(0.45, QColor(8, 8, 8))
        g.setColorAt(0.70, QColor(40, 40, 40))
        g.setColorAt(0.88, QColor(95, 95, 95))
        g.setColorAt(1.00, QColor(138, 138, 138))
        p.fillRect(QRectF(0, 0, w, h), QBrush(g))
        r = QRadialGradient(QPointF(w * 0.5, h * 0.80), w * 0.46)       # darker middle, lighter lower corners
        r.setColorAt(0.0, QColor(0, 0, 0, 135))
        r.setColorAt(0.6, QColor(0, 0, 0, 60))
        r.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0, 0, w, h), QBrush(r))
        v = QRadialGradient(QPointF(w / 2, h * 0.46), max(w, h) * 0.78)  # vignette
        v.setColorAt(0.55, QColor(0, 0, 0, 0))
        v.setColorAt(1.0, QColor(0, 0, 0, 120))
        p.fillRect(QRectF(0, 0, w, h), QBrush(v))
        p.end()
        return pm

    def paintEvent(self, _e):
        w, h = self.width(), self.height()
        s = self._scale()
        t = self._t
        if self._bg_cache is None:
            self._bg_cache = self._background()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(0, 0, self._bg_cache)

        # slow drifting mist near the bottom
        _glow(p, w * (0.22 + 0.06 * math.sin(t * 0.13)), h * 1.0, w * 0.42, h * 0.34, (255, 255, 255), 30)
        _glow(p, w * (0.78 + 0.06 * math.cos(t * 0.11)), h * 1.0, w * 0.42, h * 0.34, (255, 255, 255), 30)

        # stars (parallax with the mouse, twinkling, fading out towards the bottom)
        p.setPen(Qt.PenStyle.NoPen)
        for x, y, r, _sp, ph, depth, tw in self._stars:
            tw_a = 0.55 + 0.45 * math.sin(t * tw + ph)
            fade = max(0.0, 1.0 - y / 0.8)
            a = 255 * tw_a * fade * (0.35 + 0.65 * depth)
            if a < 3:
                continue
            px = x * w + self._mx * depth * 46 * s
            py = y * h + self._my * depth * 30 * s
            p.setBrush(QColor(255, 255, 255, _a(a)))
            rr = r * s * (0.7 + 0.5 * depth)
            p.drawEllipse(QPointF(px, py), rr, rr)
        if self._shoot:
            age = t - self._shoot["t0"]
            hx = (self._shoot["x"] - age * 0.55) * w
            hy = (self._shoot["y"] + age * 0.30) * h
            tx, ty = hx + 0.16 * w, hy - 0.087 * w
            line = QLinearGradient(QPointF(hx, hy), QPointF(tx, ty))
            fade = math.sin(min(1.0, age) * math.pi)
            line.setColorAt(0, QColor(255, 255, 255, _a(230 * fade)))
            line.setColorAt(1, QColor(255, 255, 255, 0))
            pen = QPen(QBrush(line), max(1.0, 2.2 * s))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(hx, hy), QPointF(tx, ty))
            p.setPen(Qt.PenStyle.NoPen)

        # soft light that follows the cursor
        if self.rect().contains(self._spot.toPoint()):
            _glow(p, self._spot.x(), self._spot.y(), 360 * s, 360 * s, (255, 255, 255), 17)

        ox, oy = self._origin()
        # logo: halo + floating rounded icon
        e = self._logo_reveal
        if e > 0.001:
            bob = math.sin(t * 1.1) * 5 * s
            size = 260 * s * (0.9 + 0.1 * e)
            cx, cy = ox + 982 * s, oy + (232 + 130) * s + bob + (1 - e) * 22 * s
            p.save()
            p.setOpacity(e)
            _glow(p, cx, cy, 330 * s, 330 * s, (255, 255, 255), 34 + 12 * math.sin(t * 1.6))
            rect = QRectF(cx - size / 2, cy - size / 2, size, size)
            path = QPainterPath()
            path.addRoundedRect(rect, 12 * s, 12 * s)
            if not self._logo.isNull():
                p.save()
                p.setClipPath(path)
                p.drawPixmap(rect.toRect(), self._logo)
                p.restore()
            p.setPen(QPen(QColor(255, 255, 255, 40), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            p.restore()

        # glow under the primary button and the focused field
        if self.btn_primary.isVisible():
            g = self.btn_primary.geometry()
            hv = self._hover[self.btn_primary]
            _glow(p, g.center().x(), g.center().y() + 4 * s, g.width() * 0.62, g.height() * 1.7,
                  (255, 255, 255), 30)
        for field, amt in self._focus.items():
            if field.isVisible() and amt > 0.01:
                g = field.geometry()
                _glow(p, g.center().x(), g.center().y(), g.width() * 0.62, g.height() * 1.9, (255, 255, 255), 46 * amt)

        # spinner next to the status text while something is happening
        if self._busy_flag and self.lbl_status.isVisible():
            g = self.lbl_status.geometry()
            tw = self.lbl_status.fontMetrics().horizontalAdvance(self.lbl_status.text())
            d = 26 * s
            sx, sy = g.center().x() - tw / 2 - d - 14 * s, g.center().y() - d / 2
            box = QRectF(sx, sy, d, d)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 45), max(2.0, 3 * s)))
            p.drawEllipse(box)
            pen = QPen(QColor(255, 255, 255, 230), max(2.0, 3 * s))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(box, int(-t * 360 * 1.3 * 16), 110 * 16)

        if self._ban_t > 0.01:
            _glow(p, w / 2, h * 0.5, w * 0.55, h * 0.55, (170, 20, 20), 60 * self._ban_t)
        p.end()

    # ---- reveal animations (fade + rise)
    def _reveal(self, widget, delay=0, dur=620, rise=26):
        eff = QGraphicsOpacityEffect(widget)
        eff.setOpacity(0.0)
        widget.setGraphicsEffect(eff)
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(dur)
        anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        old = self._reveals.get(widget)
        self._reveals[widget] = anim
        if old is not None:
            old.stop()

        def step(v):
            if self._reveals.get(widget) is anim:
                eff.setOpacity(float(v))
                self._rise[widget] = (1.0 - float(v)) * rise
                self._place()

        def end():
            if self._reveals.get(widget) is anim:
                self._reveals.pop(widget, None)
                self._rise.pop(widget, None)
                widget.setGraphicsEffect(None)
                self._place()

        anim.valueChanged.connect(step)
        anim.finished.connect(end)
        QTimer.singleShot(delay, lambda: anim.start() if self._reveals.get(widget) is anim else None)

    def _reveal_visible(self, base=0):
        order = [self.lbl_title] + [wd for wd in self._dynamic if wd.isVisible()]
        for i, wd in enumerate(order):
            self._reveal(wd, base + i * 70)

    def play_intro(self):
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(1100)
        anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        anim.valueChanged.connect(lambda v: setattr(self, "_logo_reveal", float(v)))
        anim.start()
        self._intro_anim = anim
        self._reveal_visible(base=260)
        for wd in (self.lbl_handle, self.lbl_version, self.btn_min, self.btn_close):
            self._reveal(wd, 900, dur=700, rise=0)

    # ---- state / status
    def set_status(self, text, error=True):
        self.lbl_status.setStyleSheet(f"background:transparent;color:{'#ff9b9b' if error else '#d6d6d6'};")
        self.lbl_status.setText(text)
        if error and text:
            self._shake()

    def _set_inputs_enabled(self, on):
        for wd in (self.email, self.pw, self.btn_primary, self.btn_create, self.btn_forgot, self.btn_back,
                   *self._social_buttons):
            wd.setEnabled(on)

    def _busy(self, text):
        self._busy_flag = True
        self.set_status(text, error=False)
        self._set_inputs_enabled(False)

    def _idle(self):
        self._busy_flag = False
        if self.state in ("form", "create", "forgot", "dl_error"):
            self._set_inputs_enabled(True)

    def _apply_state(self):
        visible = {
            "form": (self.lbl_email, self.lbl_pw, self.email, self.pw, self.btn_create, self.btn_forgot,
                     self.btn_primary, self.lbl_status, *self._social_buttons),
            "downloading": (self.progress,),
            "launching": (self.progress,),
            "create": (self.lbl_email, self.lbl_pw, self.email, self.pw, self.btn_back, self.btn_primary,
                       self.lbl_status, *self._social_buttons),
            "forgot": (self.lbl_email, self.email, self.btn_back, self.btn_primary, self.lbl_status),
            "dl_error": (self.btn_primary, self.lbl_status),
            "banned": (),
        }[self.state]
        for wd in self._dynamic:
            wd.setVisible(wd in visible)
        self.btn_primary.setText({"dl_error": "Retry", "create": "Create Account",
                                  "forgot": "Send reset link"}.get(self.state, "Sign in"))
        self.lbl_title.setText(TITLES[self.state])

    def _go(self, state):
        self.state = state
        self._apply_state()
        self.relayout()
        self._reveal_visible()

    # ---- background jobs
    def _run(self, job, on_done):
        self._worker = Worker(job, self)
        self._worker.done.connect(on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_failed(self, message):
        self._idle()
        self.set_status(message)

    # ---- sign in / create account
    def _read_form(self, create):
        email, pw = self.email.text().strip(), self.pw.text()
        if not email or "@" not in email:
            self.set_status("Enter a valid email address.")
            return None
        if not pw or (create and len(pw) < 6):
            self.set_status("Password must be at least 6 characters." if create else "Enter your password.")
            return None
        return email, pw

    def on_primary(self):
        if self.state == "form":
            self.on_sign_in()
        elif self.state == "create":
            self.do_create()
        elif self.state == "forgot":
            self.do_forgot()
        elif self.state == "dl_error":
            self.start_download()

    def on_sign_in(self):
        creds = self._read_form(create=False)
        if not creds:
            return
        email, pw = creds
        global user_email
        user_email = email

        def proceed():
            self._busy("Signing in...")

            def job():
                r = fb_auth(email, pw, create=False)
                r["exists"] = db_profile_exists(r["uid"], r["token"])
                return r

            self._run(job, self._signed_in)

        self._new_account_gate(proceed, strict=False, action="sign in to")      # banned Discord id -> ban

    def _signed_in(self, r):
        if r["exists"]:
            self._let_pass(r["account"])             # already in the database -> straight through
            return
        # signed in, but no database record: this is a brand-new account, so it goes through the Discord check
        self._new_account_gate(lambda: self._register_profile(r))

    def _register_profile(self, r):
        self._busy("Setting up your account...")

        def job():
            if WRITE_PROFILE_FOR_NEW_ACCOUNTS:
                db_create_profile(r["uid"], r["token"])
            return r["account"]

        self._run(job, self._let_pass)

    def on_social(self, name):
        global user_email
        cfg = _provider_cfg(name)
        if not cfg["client_id"]:
            self.set_status(f"{cfg['label']} sign-in isn't set up yet.")
            return
        user_email = f"{cfg['label']} sign-in"          # shown in the ban alert until the real email is known
        _oauth_cancel.clear()

        def proceed():
            self._busy(f"Finish signing in with {cfg['label']} in your browser...")

            def job():
                r = social_sign_in(name, _oauth_cancel)
                r["exists"] = db_profile_exists(r["uid"], r["token"])
                return r

            self._run(job, self._social_signed_in)

        self._new_account_gate(proceed, strict=False, action="sign in to")      # banned Discord id -> ban

    def _social_signed_in(self, r):
        global user_email
        if r["account"].get("email"):
            user_email = r["account"]["email"]
        self._signed_in(r)                               # same path as email/password: profile check, Discord gate for new accounts

    def _email_enter(self):
        if self.state == "forgot":
            self.on_primary()
        else:
            self.pw.setFocus()

    def on_create(self):
        self.set_status("")
        self._go("create")

    def on_back(self):
        self.set_status("")
        self._go("form")

    def do_create(self):
        creds = self._read_form(create=True)
        if not creds:
            return
        email, pw = creds
        global user_email
        user_email = email

        def after_check():
            self._busy("Creating your account...")

            def job():
                r = fb_auth(email, pw, create=True)
                if WRITE_PROFILE_FOR_NEW_ACCOUNTS:
                    db_create_profile(r["uid"], r["token"])
                return r["account"]

            self._run(job, self._let_pass)

        self._new_account_gate(after_check)          # the check happens before the account exists

    def _new_account_gate(self, on_ok, strict=True, action="make an account in"):
        self._busy("Checking...")
        self._run(lambda: discord_check(strict), lambda r: self._gate_result(r, on_ok, action))

    def _gate_result(self, r, on_ok, action="make an account in"):
        if r["status"] == "banned":
            self._busy_flag = False
            self.ban(r["user_id"], action=action)
        elif r["status"] == "error":
            self._idle()
            self.set_status(r["message"])
        else:
            on_ok()

    def on_forgot(self):
        self.set_status("")
        self._go("forgot")

    def do_forgot(self):
        email = self.email.text().strip()
        if not email or "@" not in email:
            self.set_status("Enter the email address of your account.")
            return

        def done(_r):
            self._idle()
            self.set_status("If an account exists for that email, a reset link is on its way.", error=False)

        def proceed():
            self._busy("Sending reset email...")
            self._run(lambda: fb_send_reset(email), done)

        self._new_account_gate(proceed, strict=False, action="send a password reset from")

    def _let_pass(self, account):
        try:
            save_account(account)                    # same account.json as main.py -> the launcher opens signed in
        except OSError:
            pass
        self.pw.clear()
        self._busy_flag = False
        self.set_status("")
        if install_complete():
            self.launch_now()
        else:
            self.start_download()

    # ---- download + launch
    def start_download(self, entries=None):
        self._busy_flag = False
        self._update_only = entries is not None            # an assets update (launcher already installed)
        self.set_status("")
        self.progress.value = self.progress.shown = 0.0
        self.progress.text = "Connecting to GitHub..."
        self._go("downloading")
        self._dl = DownloadWorker(self, entries)
        self._dl.progress.connect(self._on_progress)
        self._dl.done.connect(self._on_downloaded)
        self._dl.failed.connect(self._on_download_failed)
        self._dl.start()

    def _on_progress(self, frac, text):
        self.progress.value = frac
        self.progress.text = text

    def _on_downloaded(self):
        self.progress.value = 1.0
        self.progress.text = "Download complete"
        QTimer.singleShot(650, self.launch_now)

    def _on_download_failed(self, message):
        if getattr(self, "_update_only", False):
            self.launch_now()                               # the launcher is already installed: don't block it on an asset update
            return
        self._go("dl_error")
        self._idle()
        self.set_status(message)

    def launch_now(self):
        self.progress.value = 1.0
        self.progress.text = ""
        if self.state != "launching":
            self._go("launching")

        def go():
            try:
                launch_launcher()
            except Exception as e:
                self._go("dl_error")
                self._idle()
                self.set_status(f"Couldn't start main.py: {e}")
                return
            QApplication.quit()

        QTimer.singleShot(900, go)

    # ---- ban
    def ban(self, discord_id, notify=True, action="make an account in"):
        self._set_inputs_enabled(False)
        self._go("banned")
        if notify:
            write_ban_flag(discord_id)
            self._webhook_thread = threading.Thread(target=send_ban_webhook, args=(discord_id, action), daemon=True)
            self._webhook_thread.start()
        QTimer.singleShot(BAN_CLOSE_DELAY_MS, self._quit_banned)

    def _quit_banned(self):
        if self._webhook_thread is not None:
            self._webhook_thread.join(timeout=6)     # let the alert finish sending before closing
        QApplication.quit()


def main():
    # signed in and installed: skip the window completely and open the launcher
    changed = []
    if not is_flagged_banned() and load_account() and install_complete():
        changed = list_asset_updates()                # silent: new or modified files in assets/ (empty when offline)
        if not changed:
            try:
                launch_launcher()
                return
            except Exception:
                pass                                  # couldn't start it -> fall through to the window
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = LoginWindow()
    win.show()
    win.play_intro()
    if is_flagged_banned():                           # banned earlier: no sign-in, just the message and close
        QTimer.singleShot(0, lambda: win.ban(None, notify=False))
    elif load_account():                              # signed in but the launcher isn't downloaded (yet), or assets changed
        QTimer.singleShot(0, lambda: win.start_download(changed or None))
    sys.exit(app.exec())

_old_init = LoginWindow.__init__
def _new_init(self):
    _old_init(self)
    self.setWindowTitle("NCZ Games Launcher Registration")
    self.setWindowIcon(QIcon(os.path.join(ASSETS_DIR, "icon.png")))
LoginWindow.__init__ = _new_init


if __name__ == "__main__":
    main()
