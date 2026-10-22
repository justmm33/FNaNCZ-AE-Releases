"""Controller Bridge (PC side).

Receives gamepad state from the NCZ mobile app over the local network (WebSocket)
and replays it as a virtual Xbox 360 controller on this PC.

  Windows : pip install vgamepad          (installs the ViGEmBus driver)
  Linux   : pip install evdev             (needs write access to /dev/uinput)

Only the standard library is needed for the network part.
"""
import base64
import collections
import hashlib
import json
import os
import socket
import struct
import threading
import time
import traceback

PORT = 8765
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
IDLE_TIMEOUT = 30   # seconds with no frame at all (not even a pong) before a phone is treated as dead
PING_EVERY = 5      # server -> phone ping interval; an idle (untouched) controller must not look "silent"

# ------------------------------------------------------------------ logging
# Every connection step is written to the console, to controller_bridge.log (next to this file) and to an
# in-memory buffer (get_log()) so the launcher can show it.
_LOG_LINES = collections.deque(maxlen=500)
_LOG_LOCK = threading.Lock()
_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "controller_bridge.log")
_LOG_MAX_BYTES = 512 * 1024


def log(msg, *args):
    if args:
        msg = msg % args
    line = "%s [bridge] %s" % (time.strftime("%H:%M:%S"), msg)
    with _LOG_LOCK:
        _LOG_LINES.append(line)
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            if os.path.exists(_LOG_FILE) and os.path.getsize(_LOG_FILE) > _LOG_MAX_BYTES:
                os.replace(_LOG_FILE, _LOG_FILE + ".old")
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def get_log():
    with _LOG_LOCK:
        return "\n".join(_LOG_LINES)


def log_path():
    return _LOG_FILE


# Standard Gamepad API button order (what browsers report on the phone):
# 0 A, 1 B, 2 X, 3 Y, 4 LB, 5 RB, 6 LT, 7 RT, 8 Back, 9 Start, 10 LS, 11 RS,
# 12 Up, 13 Down, 14 Left, 15 Right, 16 Home


# ------------------------------------------------------------------ virtual pads
class _WinPad:
    def __init__(self):
        import vgamepad as vg
        self.vg = vg
        self.pad = vg.VX360Gamepad()
        B = vg.XUSB_BUTTON
        self.map = {0: B.XUSB_GAMEPAD_A, 1: B.XUSB_GAMEPAD_B, 2: B.XUSB_GAMEPAD_X, 3: B.XUSB_GAMEPAD_Y,
                    4: B.XUSB_GAMEPAD_LEFT_SHOULDER, 5: B.XUSB_GAMEPAD_RIGHT_SHOULDER,
                    8: B.XUSB_GAMEPAD_BACK, 9: B.XUSB_GAMEPAD_START,
                    10: B.XUSB_GAMEPAD_LEFT_THUMB, 11: B.XUSB_GAMEPAD_RIGHT_THUMB,
                    12: B.XUSB_GAMEPAD_DPAD_UP, 13: B.XUSB_GAMEPAD_DPAD_DOWN,
                    14: B.XUSB_GAMEPAD_DPAD_LEFT, 15: B.XUSB_GAMEPAD_DPAD_RIGHT,
                    16: B.XUSB_GAMEPAD_GUIDE}
        self.down = set()

    def update(self, mask, axes, lt, rt):
        for i, btn in self.map.items():
            on = bool(mask >> i & 1)
            if on and i not in self.down:
                self.pad.press_button(button=btn)
                self.down.add(i)
            elif not on and i in self.down:
                self.pad.release_button(button=btn)
                self.down.discard(i)
        self.pad.left_joystick_float(x_value_float=axes[0], y_value_float=-axes[1])
        self.pad.right_joystick_float(x_value_float=axes[2], y_value_float=-axes[3])
        self.pad.left_trigger_float(value_float=lt)
        self.pad.right_trigger_float(value_float=rt)
        self.pad.update()

    def close(self):
        """Neutralize all inputs but keep the virtual pad plugged in (no connect/disconnect popup)."""
        try:
            self.update(0, [0, 0, 0, 0], 0, 0)
        except Exception:
            pass

    def destroy(self):
        """Really unplug the virtual pad (only when the bridge is stopped)."""
        self.close()
        self.pad = None          # vgamepad unplugs the device when the object is freed


class _LinuxPad:
    def __init__(self):
        from evdev import UInput, AbsInfo, ecodes as e
        self.e = e
        stick = AbsInfo(0, -32768, 32767, 16, 128, 0)
        trig = AbsInfo(0, 0, 255, 0, 0, 0)
        hat = AbsInfo(0, -1, 1, 0, 0, 0)
        caps = {
            e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST, e.BTN_TL, e.BTN_TR,
                       e.BTN_SELECT, e.BTN_START, e.BTN_MODE, e.BTN_THUMBL, e.BTN_THUMBR],
            e.EV_ABS: [(e.ABS_X, stick), (e.ABS_Y, stick), (e.ABS_RX, stick), (e.ABS_RY, stick),
                       (e.ABS_Z, trig), (e.ABS_RZ, trig), (e.ABS_HAT0X, hat), (e.ABS_HAT0Y, hat)],
        }
        self.ui = UInput(caps, name="Microsoft X-Box 360 pad (NCZ Bridge)", vendor=0x045e, product=0x028e)
        self.map = {0: e.BTN_SOUTH, 1: e.BTN_EAST, 2: e.BTN_WEST, 3: e.BTN_NORTH, 4: e.BTN_TL, 5: e.BTN_TR,
                    8: e.BTN_SELECT, 9: e.BTN_START, 10: e.BTN_THUMBL, 11: e.BTN_THUMBR, 16: e.BTN_MODE}

    def update(self, mask, axes, lt, rt):
        e, ui = self.e, self.ui
        for i, code in self.map.items():
            ui.write(e.EV_KEY, code, mask >> i & 1)
        ui.write(e.EV_ABS, e.ABS_HAT0X, (mask >> 15 & 1) - (mask >> 14 & 1))
        ui.write(e.EV_ABS, e.ABS_HAT0Y, (mask >> 13 & 1) - (mask >> 12 & 1))
        for code, v in ((e.ABS_X, axes[0]), (e.ABS_Y, axes[1]), (e.ABS_RX, axes[2]), (e.ABS_RY, axes[3])):
            ui.write(e.EV_ABS, code, int(max(-1, min(1, v)) * 32767))
        ui.write(e.EV_ABS, e.ABS_Z, int(lt * 255))
        ui.write(e.EV_ABS, e.ABS_RZ, int(rt * 255))
        ui.syn()

    def close(self):
        """Neutralize all inputs but keep the virtual device alive."""
        try:
            self.update(0, [0, 0, 0, 0], 0, 0)
        except Exception:
            pass

    def destroy(self):
        self.close()
        try:
            self.ui.close()
        except Exception:
            pass


def _make_pad():
    import sys
    return _WinPad() if sys.platform.startswith("win") else _LinuxPad()


# ------------------------------------------------------------------ minimal WebSocket
def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("phone closed the connection")
        buf += chunk
    return buf


def _read_frame(sock):
    b1, b2 = _recv_exact(sock, 2)
    op, ln = b1 & 0x0F, b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    if ln > 65536:
        raise ConnectionError("frame too large (%d bytes)" % ln)
    mask = _recv_exact(sock, 4) if b2 & 0x80 else None
    data = _recv_exact(sock, ln) if ln else b""
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return op, data


def _send_frame(sock, op, data=b""):
    n = len(data)
    if n < 126:
        head = bytes([0x80 | op, n])
    elif n < 65536:
        head = bytes([0x80 | op, 126]) + struct.pack(">H", n)
    else:
        head = bytes([0x80 | op, 127]) + struct.pack(">Q", n)
    sock.sendall(head + data)


def _handshake(sock):
    req = b""
    while b"\r\n\r\n" not in req:
        chunk = sock.recv(1024)
        if not chunk:
            log("handshake: phone closed the connection before sending a full HTTP request (got %d bytes)", len(req))
            return False
        req += chunk
        if req[:2] == b"\x16\x03":     # TLS ClientHello: never contains \r\n\r\n, so check right away
            log("handshake FAILED: the phone is speaking TLS (wss:// / https://). The bridge only accepts plain ws://. "
                "Use ws://<pc-ip>:%d, and note an https page can't open ws:// (mixed content).", PORT)
            return False
        if len(req) > 8192:
            log("handshake: request too large, dropping")
            return False
    lines = req.decode("latin-1").split("\r\n")
    log("handshake: request line: %s", lines[0][:120])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    for k in ("host", "origin", "upgrade", "connection", "sec-websocket-version", "sec-websocket-protocol", "user-agent"):
        if k in headers:
            log("handshake:   %s: %s", k, headers[k][:120])
    key = headers.get("sec-websocket-key")
    if not key:
        log("handshake FAILED: no Sec-WebSocket-Key header (this was a plain HTTP request, not a WebSocket)")
        if lines[0].startswith("GET"):      # opening http://<pc-ip>:8765 in a browser = a quick reachability test
            body = b"NCZ Controller Bridge is running. Connect with a WebSocket (ws://), not a browser page."
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n" % len(body) + body)
        return False
    accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
    reply = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
             "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n" % accept)
    proto = headers.get("sec-websocket-protocol")
    if proto:           # a browser aborts the connection if it asked for a subprotocol and the server doesn't pick one
        chosen = proto.split(",")[0].strip()
        reply += "Sec-WebSocket-Protocol: %s\r\n" % chosen
        log("handshake: client asked for subprotocol(s) '%s', selecting '%s'", proto, chosen)
    sock.sendall((reply + "\r\n").encode())
    log("handshake OK, waiting for the hello message")
    return True


# ------------------------------------------------------------------ server
class ControllerBridge:
    def __init__(self, pin_getter, port=PORT):
        self.pin_getter = pin_getter
        self.port = port
        self.srv = None
        self.running = False
        self.client = None          # address string of the connected phone
        self.error = ""
        self.pad = None             # ONE virtual pad for the whole session (created on first auth, never per connection)
        self.pad_lock = threading.Lock()
        self.active_sock = None     # the phone connection that currently owns the pad

    def _get_pad(self):
        with self.pad_lock:
            if self.pad is None:
                self.pad = _make_pad()
                log("virtual gamepad created (once; it stays plugged in until the bridge is stopped)")
            return self.pad

    def start(self):
        if self.running:
            log("start() called but the bridge is already running")
            return
        log("starting: binding 0.0.0.0:%d ...", self.port)
        try:
            self.srv = socket.socket()
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.srv.bind(("0.0.0.0", self.port))
            self.srv.listen(2)
        except OSError as ex:
            self.error = str(ex)
            log("START FAILED: %s  (is another program or a second launcher already using port %d?)", ex, self.port)
            return
        self.error = ""
        self.running = True
        log("listening on %s:%d (TCP / WebSocket). Phone should connect to ws://%s:%d", local_ip(), self.port,
            local_ip(), self.port)
        log("if 'connection from' never appears below: wrong IP, different network / guest Wi-Fi (client isolation), "
            "or the PC firewall is blocking TCP %d", self.port)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        was = self.running
        self.running = False
        try:
            self.srv.close()
        except Exception:
            pass
        with self.pad_lock:
            act, self.active_sock = self.active_sock, None
            pad, self.pad = self.pad, None
        if act is not None:
            try:
                act.close()
            except Exception:
                pass
        if pad is not None:
            pad.destroy()
        if was:
            log("stopped")

    def _accept_loop(self):
        while self.running:
            try:
                conn, addr = self.srv.accept()
            except OSError as ex:
                if self.running:
                    log("accept error: %s", ex)
                break
            log("connection from %s:%d", addr[0], addr[1])
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True).start()

    def _serve(self, sock, addr):
        pad = None
        who = "%s:%d" % (addr[0], addr[1])
        ignored = 0
        count, window = 0, time.time()
        slock = threading.Lock()        # the heartbeat thread and this thread both write to the socket

        def send(op, data=b""):
            with slock:
                _send_frame(sock, op, data)

        def heartbeat():
            # Ping the phone regularly. A browser answers with a pong automatically, so an idle controller
            # (phone only sends a state message when something changes) never looks dead to the idle timeout.
            while self.running and self.active_sock is sock:
                time.sleep(PING_EVERY)
                if self.active_sock is not sock:
                    break
                try:
                    send(9, b"")
                except Exception:
                    break

        try:
            sock.settimeout(IDLE_TIMEOUT)
            if not _handshake(sock):
                return
            authed = False
            while self.running:
                op, data = _read_frame(sock)
                if op == 8:
                    log("%s: phone sent a close frame", who)
                    break
                if op == 9:
                    send(10, data)
                    continue
                if op == 10:            # pong answering our heartbeat ping
                    continue
                if op != 1:
                    ignored += 1
                    if ignored <= 5:
                        log("%s: ignoring frame with opcode %d (%d bytes). The bridge only reads TEXT (JSON) frames; "
                            "a binary hello would leave the phone stuck on 'connecting'", who, op, len(data))
                    continue
                try:
                    msg = json.loads(data.decode("utf-8", "ignore"))
                except ValueError:
                    log("%s: message is not valid JSON: %r", who, data[:80])
                    continue
                if not authed:
                    t = msg.get("t")
                    got = str(msg.get("pin"))
                    ok = t == "hello" and got == str(self.pin_getter())
                    log("%s: first message type=%r, pin length=%d, pin %s", who, t, len(got),
                        "MATCHES" if ok else "does NOT match")
                    if ok:
                        authed = True
                        try:
                            pad = self._get_pad()
                        except Exception as ex:
                            log("%s: could not create the virtual gamepad: %r\n%s", who, ex, traceback.format_exc())
                            send(1, json.dumps({"t": "err", "m": "No virtual gamepad driver: %s" % ex}).encode())
                            break
                        with self.pad_lock:
                            old, self.active_sock = self.active_sock, sock
                        if old is not None:
                            log("%s: replacing the previous phone connection (same pad is reused)", who)
                            try:
                                old.shutdown(socket.SHUT_RDWR)
                            except Exception:
                                pass
                            try:
                                old.close()
                            except Exception:
                                pass
                        self.client = addr[0]
                        send(1, b'{"t":"ok"}')
                        log("%s: authenticated, sent ok. Streaming controller input", who)
                        threading.Thread(target=heartbeat, daemon=True).start()
                        continue
                    send(1, b'{"t":"err","m":"Wrong PIN"}')
                    break
                if msg.get("t") == "s":
                    if self.active_sock is not sock:
                        break           # a newer connection took over the pad
                    a = (list(msg.get("a", [])) + [0, 0, 0, 0])[:4]
                    pad.update(int(msg.get("b", 0)), [float(x) for x in a],
                               float(msg.get("lt", 0)), float(msg.get("rt", 0)))
                    count += 1
                    if time.time() - window >= 5:
                        log("%s: streaming, %d state messages in the last 5s", who, count)
                        count, window = 0, time.time()
        except socket.timeout:
            log("%s: no data or pong for %ds, treating the phone as gone", who, IDLE_TIMEOUT)
        except ConnectionError as ex:
            log("%s: connection ended: %s", who, ex)
        except Exception as ex:
            log("%s: ERROR %r\n%s", who, ex, traceback.format_exc())
        finally:
            with self.pad_lock:
                mine = self.active_sock is sock
                if mine:
                    self.active_sock = None
                    self.client = None
            if pad and mine:
                pad.close()             # release all buttons/sticks, but the virtual pad stays plugged in
            try:
                sock.close()
            except Exception:
                pass
            log("%s: disconnected", who)


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
