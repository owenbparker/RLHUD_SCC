"""Rocket League's Stats API: the game's own feed of the match.

With it turned on (PacketSendRate > 0 in TAGame\\Config\\DefaultStatsAPI.ini,
and TAStatsAPI.ini in Documents\\My Games where the game keeps one), the
game opens a socket on this PC (port 49123) and sends JSON messages:
{"Event": "UpdateState", "Data": {...}} many times a second, and an event
({"Event": "GoalScored", ...}) as things happen. Some builds send it as a
plain TCP stream, newer ones also as a WebSocket (port 49124); this reads
either. It's read-only, from the game itself, so Easy Anti-Cheat has no
part in it.
"""

import base64
import json
import os
import re
import socket
import threading
import time

from .log import log

DEFAULT_PORT = 49123


def messages(buffer):
    """The complete JSON messages at the start of buffer (however they're
    run together: back to back, or one per line), and what's left over."""
    decoder = json.JSONDecoder()
    out = []
    i, n = 0, len(buffer)
    while i < n:
        while i < n and buffer[i] in " \t\r\n\x00,":
            i += 1
        if i >= n:
            break
        if buffer[i] != "{":
            # Not JSON here (a stray byte): skip to the next message.
            nxt = buffer.find("{", i + 1)
            if nxt < 0:
                return out, ""
            i = nxt
            continue
        try:
            obj, end = decoder.raw_decode(buffer, i)
        except json.JSONDecodeError:
            break   # not all here yet
        out.append(obj)
        i = end
    return out, buffer[i:]


def unwrap(msg):
    """(event, data) of a message. Data may come as a JSON string."""
    event = msg.get("Event") or msg.get("event") or ""
    data = msg.get("Data", msg.get("data"))
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    return event, data if isinstance(data, dict) else {}


class StatsClient:
    """Keeps a connection to the Stats API, calling on_message(event, data)
    for each message. Reconnects by itself."""

    def __init__(self, on_message, host="127.0.0.1", port=DEFAULT_PORT):
        self.on_message = on_message
        self.host, self.port = host, port
        self.connected = False
        self.mode = None          # "tcp" or "websocket"
        self.last_message = 0.0
        self.messages = 0
        self.error = None
        self._stop = threading.Event()
        self._thread = None
        self._sock = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="statsapi")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._close()

    def set_address(self, host, port):
        if (host, int(port)) != (self.host, self.port):
            self.host, self.port = host, int(port)
            self._close()   # reconnects to the new address

    def info(self):
        age = round(time.time() - self.last_message, 1) if self.last_message else None
        return {"connected": self.connected, "mode": self.mode, "host": self.host, "port": self.port,
                "messages": self.messages, "age": age, "error": self.error}

    def _close(self):
        s, self._sock = self._sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass

    def _run(self):
        logged = None
        while not self._stop.is_set():
            try:
                self._connect_and_read()
            except (OSError, ValueError) as e:
                self.error = str(e) or type(e).__name__
            if self.connected:
                log("Lost the connection to Rocket League's Stats API", "warn")
            self.connected, self.mode = False, None
            self._close()
            if self.error != logged:
                logged = self.error
                if self.error:
                    log(f"Waiting for Rocket League's Stats API on {self.host}:{self.port} ({self.error})")
            self._stop.wait(2)

    def _connect_and_read(self):
        sock = socket.create_connection((self.host, self.port), timeout=3)
        self._sock = sock
        # A plain TCP feed starts talking by itself; a WebSocket waits to be asked.
        sock.settimeout(1.5)
        try:
            first = sock.recv(65536)
        except socket.timeout:
            first = None
        if first is None:
            self._websocket(sock)
            return
        if not first:
            raise ConnectionError("closed")
        self._mark("tcp")
        sock.settimeout(5)
        buffer = first.decode("utf-8", "replace")
        while not self._stop.is_set():
            msgs, buffer = messages(buffer)
            for m in msgs:
                self._deliver(m)
            if len(buffer) > 4_000_000:
                buffer = ""
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue   # quiet between matches
            if not chunk:
                raise ConnectionError("Rocket League closed the connection")
            buffer += chunk.decode("utf-8", "replace")

    def _websocket(self, sock):
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        sock.settimeout(5)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during the WebSocket handshake")
            head += chunk
        head, rest = head.split(b"\r\n\r\n", 1)
        if not re.match(rb"HTTP/1\.[01] 101", head):
            raise ConnectionError("not the Stats API (no WebSocket upgrade)")
        self._mark("websocket")
        data, parts = rest, []
        while not self._stop.is_set():
            frame, data = _ws_frame(data)
            if frame is None:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    raise ConnectionError("Rocket League closed the connection")
                data += chunk
                continue
            fin, opcode, payload = frame
            if opcode == 8:
                raise ConnectionError("Rocket League closed the connection")
            if opcode == 9:   # ping -> pong (masked, as a client must)
                sock.sendall(_ws_encode(10, payload))
                continue
            if opcode in (0, 1, 2):
                parts.append(payload)
                if fin:
                    text = b"".join(parts).decode("utf-8", "replace")
                    parts = []
                    msgs, _ = messages(text)
                    for m in msgs:
                        self._deliver(m)

    def _mark(self, mode):
        self.connected, self.mode, self.error = True, mode, None
        log(f"Connected to Rocket League's Stats API ({mode}, {self.host}:{self.port})")

    def _deliver(self, msg):
        self.messages += 1
        self.last_message = time.time()
        try:
            self.on_message(*unwrap(msg))
        except Exception as e:   # a bad message mustn't drop the connection
            log(f"Couldn't use a Stats API message: {e!r}", "warn")


def _ws_frame(data):
    """((fin, opcode, payload), rest) for the first whole frame, or (None, data)."""
    if len(data) < 2:
        return None, data
    b0, b1 = data[0], data[1]
    n, i = b1 & 0x7F, 2
    if n == 126:
        if len(data) < 4:
            return None, data
        n, i = int.from_bytes(data[2:4], "big"), 4
    elif n == 127:
        if len(data) < 10:
            return None, data
        n, i = int.from_bytes(data[2:10], "big"), 10
    mask = None
    if b1 & 0x80:
        if len(data) < i + 4:
            return None, data
        mask, i = data[i:i + 4], i + 4
    if len(data) < i + n:
        return None, data
    payload = data[i:i + n]
    if mask:
        payload = bytes(b ^ mask[k % 4] for k, b in enumerate(payload))
    return (bool(b0 & 0x80), b0 & 0x0F, payload), data[i + n:]


def _ws_encode(opcode, payload):
    mask = os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
    else:
        head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
    return head + mask + bytes(b ^ mask[k % 4] for k, b in enumerate(payload))


# --- turning it on ---------------------------------------------------------------

def config_files():
    """The Stats API settings files on this PC (that exist, or whose folder does)."""
    found = []
    docs = os.path.join(os.path.expanduser("~"), "Documents", "My Games", "Rocket League", "TAGame", "Config")
    found.append(os.path.join(docs, "TAStatsAPI.ini"))
    for root in install_dirs():
        found.append(os.path.join(root, "TAGame", "Config", "DefaultStatsAPI.ini"))
    return [p for p in found if os.path.isdir(os.path.dirname(p))]


def install_dirs():
    """Rocket League install folders (Epic and Steam) on this PC."""
    dirs = []
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    manifests = os.path.join(program_data, "Epic", "EpicGamesLauncher", "Data", "Manifests")
    if os.path.isdir(manifests):
        for name in os.listdir(manifests):
            if name.endswith(".item"):
                try:
                    with open(os.path.join(manifests, name), encoding="utf-8") as f:
                        item = json.load(f)
                except (OSError, ValueError):
                    continue
                if "rocket league" in (item.get("DisplayName") or "").lower() and item.get("InstallLocation"):
                    dirs.append(item["InstallLocation"])
    for steam in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        libs = [steam]
        try:
            with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8") as f:
                libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
        except OSError:
            pass
        for lib in libs:
            d = os.path.join(lib, "steamapps", "common", "rocketleague")
            if os.path.isdir(d):
                dirs.append(d)
    for d in (r"C:\Program Files\Epic Games\rocketleague", r"D:\Epic Games\rocketleague"):
        if os.path.isdir(d):
            dirs.append(d)
    seen, out = set(), []
    for d in dirs:
        key = os.path.normcase(os.path.normpath(d))
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            out.append(d)
    return out


def set_ini(path, rate=30, port=DEFAULT_PORT):
    """Writes PacketSendRate and Port into a Stats API ini (keeping anything else)."""
    text = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    section = "[TAGame.MatchStatsExporter_TA]"
    if section.lower() not in text.lower():
        text = text.rstrip() + ("\n\n" if text.strip() else "") + section + "\n"
    for key, value in (("PacketSendRate", rate), ("Port", port)):
        if re.search(rf"^\s*{key}\s*=", text, re.M | re.I):
            text = re.sub(rf"^(\s*{key}\s*=).*$", rf"\g<1>{value}", text, flags=re.M | re.I)
        else:
            text = re.sub(re.escape(section), section + f"\n{key}={value}", text, count=1, flags=re.I)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def enable(rate=30, port=DEFAULT_PORT):
    """Turns the Stats API on in every settings file found. [paths written]"""
    done = []
    for path in config_files():
        set_ini(path, rate, port)
        done.append(path)
    return done
