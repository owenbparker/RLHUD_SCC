"""The running app: the match (from Rocket League's Stats API, an observer
PC, or the demo), and the web server for the overlay and the clients."""

import base64
import json
import mimetypes
import os
import re
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import statsapi
from .config import DATA_DIR, LOGO_DIR, SPONSOR_DIR, WEB_DIR, web_file
from .demo import DemoMatch
from .game import GameState
from .log import log, since
from .updater import Updater, clean_up_old_update

mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("image/svg+xml", ".svg")

PAGES = {"/": "/start", "/start": "start.html", "/control": "control.html", "/observer": "observer.html",
         "/overlay": "overlay.html"}
SEND_EVERY = 1 / 30   # observer -> GFX PC


class HudApp:
    def __init__(self, config, demo=False):
        self.config = config
        self.state = GameState()
        self.updater = Updater()
        self.server = None
        self.role = None          # "gfx", "observer" or None (the start screen)
        self.overlay_window = None
        self.demo = None
        self.last_source = None
        self.ingest_at = 0.0
        cfg = config.get()
        self.stats = statsapi.StatsClient(self.on_stats, cfg["stats_api"]["host"], cfg["stats_api"]["port"])
        self._stats_started = False
        self._outbox, self._out_lock = [], threading.Lock()
        self._stop = threading.Event()
        self._cond = threading.Condition()
        if demo:
            self.set_demo(True)

    # --- where the match comes from -------------------------------------------------

    def on_stats(self, event, data):
        """A message from Rocket League on this PC: used here (GFX), or sent to
        the GFX PC (observer)."""
        if self.role == "observer":
            with self._out_lock:
                if event == "UpdateState":   # only the latest one is worth sending
                    self._outbox = [m for m in self._outbox if m[0] != "UpdateState"]
                self._outbox.append([event, data])
            return
        if self.demo:
            return
        self.feed(event, data, "game")

    def feed(self, event, data, source):
        self.last_source = source
        self.state.apply(event, data)
        with self._cond:
            self._cond.notify_all()

    def ingest(self, messages, source="observer"):
        """Messages sent here by an observer PC."""
        if self.demo:
            return
        self.ingest_at = time.time()
        for event, data in messages:
            self.feed(event, data, source)

    def set_reading(self, on):
        self.config.update({"stats_api": {"read": bool(on)}})
        self._apply_reading()

    def _apply_reading(self):
        cfg = self.config.get()["stats_api"]
        self.stats.set_address(cfg["host"], int(cfg["port"]))
        if cfg["read"] and not self._stats_started:
            self._stats_started = True
            self.stats.start()
            log("Reading Rocket League (Stats API) on this PC")
        elif not cfg["read"] and self._stats_started:
            self.stats.stop()
            self.stats = statsapi.StatsClient(self.on_stats, cfg["host"], int(cfg["port"]))
            self._stats_started = False
            log("Stopped reading Rocket League on this PC")

    def set_demo(self, on):
        if self.demo:
            self.demo.stop()
            self.demo = None
        self.state.reset()
        if on:
            self.demo = DemoMatch(lambda e, d: self.feed(e, d, "demo")).start()
            log("Demo match started")
        else:
            log("Demo match stopped")
        with self._cond:
            self._cond.notify_all()

    def set_role(self, role):
        if role not in ("gfx", "observer", None):
            raise ValueError(f"unknown client {role!r}")
        self.role = role

    def _send_loop(self):
        """Observer client: sends what Rocket League says to the GFX PC."""
        failed = None
        while not self._stop.wait(SEND_EVERY):
            if self.role != "observer":
                continue
            with self._out_lock:
                out, self._outbox = self._outbox, []
            if not out:
                continue
            url = self.config.get().get("observer_url")
            if not url:
                continue
            try:
                from .overlay_window import gfx_base_url
                req = urllib.request.Request(gfx_base_url(url) + "/api/ingest", json.dumps({"messages": out}).encode(),
                                             {"Content-Type": "application/json"})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                opener.open(req, timeout=3).read()
                if failed:
                    log("Sending the game to the GFX PC again")
                failed = None
            except Exception as e:
                msg = str(getattr(e, "reason", e))
                if msg != failed:
                    log(f"Couldn't send the game to the GFX PC ({msg})", "warn")
                failed = msg

    # --- what the pages see -------------------------------------------------------------

    def info(self):
        cfg = self.config.get()
        port = self.server.port if self.server else cfg["http_port"]
        lan = lan_ip() if cfg.get("allow_network") else None
        source = "demo" if self.demo else ("observer" if time.time() - self.ingest_at < 5 else
                                            "game" if self.stats.connected else None)
        return {"role": self.role, "port": port, "lan_url": f"http://{lan}:{port}" if lan else None,
                "network": bool(cfg.get("allow_network")), "demo": bool(self.demo), "source": source,
                "stats": dict(self.stats.info(), read=cfg["stats_api"]["read"], files=statsapi.config_files()),
                "update": self.updater.info(), "data_dir": DATA_DIR,
                "overlay_window": self.overlay_window.info() if self.overlay_window else {"available": False}}

    def snapshot(self):
        return {"game": self.state.snapshot(), "config": self.config.get(), "app": self.info(), "logos": logos()}

    def overlay_state(self):
        cfg = self.config.get()
        keep = ("left", "right", "best_of", "event", "stage", "scorebug", "names", "sponsors", "sponsor_seconds", "show")
        return {"game": self.state.snapshot(), "config": {k: cfg[k] for k in keep}}

    def wait(self, timeout):
        with self._cond:
            self._cond.wait(timeout)

    def changed(self):
        with self._cond:
            self._cond.notify_all()

    def stop(self):
        self._stop.set()
        self.stats.stop()
        if self.demo:
            self.demo.stop()
        if self.server:
            self.server.stop()

    def host(self):
        cfg = self.config.get()
        return "0.0.0.0" if cfg.get("allow_network") else cfg["http_host"]

    def set_network(self, on):
        self.config.update({"allow_network": bool(on)})
        if self.server:
            self.server.restart(self.host())
        log("Other PCs on your network can now read the HUD" if on else "HUD is now only reachable from this PC")


def logos():
    out = []
    for folder, prefix in ((os.path.join(WEB_DIR, "assets", "logos"), "assets/logos/"), (LOGO_DIR, "logos/")):
        if os.path.isdir(folder):
            out += [prefix + n for n in sorted(os.listdir(folder)) if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg"))]
    return out


def save_upload(folder, prefix, name, data_url):
    m = re.fullmatch(r"data:image/(png|jpeg|webp|svg\+xml);base64,(.+)", data_url or "", re.S)
    if not m:
        raise ValueError("send a PNG, JPG, WebP or SVG image")
    ext = {"png": ".png", "jpeg": ".jpg", "webp": ".webp", "svg+xml": ".svg"}[m.group(1)]
    stem = re.sub(r"[^A-Za-z0-9 _.-]+", "", os.path.splitext(os.path.basename(name or "logo"))[0]).strip() or "logo"
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, stem + ext), "wb") as f:
        f.write(base64.b64decode(m.group(2)))
    return prefix + stem + ext


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def _is_local(address):
    return address in ("127.0.0.1", "::1") or address.startswith("127.")


def check_gfx(text):
    from .overlay_window import gfx_base_url
    try:
        base = gfx_base_url(text)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base + "/api/state", timeout=3) as r:
            ok = "game" in json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": f"Can't reach the HUD at {base} ({getattr(e, 'reason', e)}). Check the address, that "
                                      "Rocket League HUD is running there, and that \"Let other PCs on the network read the HUD\" is ticked."}
    return {"ok": True, "overlay": base + "/overlay"} if ok else {"ok": False, "error": f"{base} isn't a Rocket League HUD"}


def find_gfx(port):
    from .discover import find_huds

    def is_gfx(state):
        return "game" in state and "phase" in (state.get("game") or {}) and (state.get("app") or {}).get("role") != "observer"
    out = []
    for hit in find_huds(port, is_gfx):
        cfg = hit["state"].get("config") or {}
        teams = " vs ".join(t for t in ((cfg.get("left") or {}).get("name"), (cfg.get("right") or {}).get("name")) if t)
        out.append({"ip": hit["ip"], "overlay": hit["url"] + "/overlay", "teams": teams})
    log(f"Looked for the HUD on the network: {len(out)} found" + "".join(f", {h['ip']}" for h in out))
    return out


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, body, ctype="application/json", status=200):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status=200):
            self._send(json.dumps(data), status=status)

        def _stream(self):
            """Server-sent events: the overlay's state, whenever it changes (up to 30 a second)."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            last, last_cfg, sent = None, None, 0.0
            try:
                self.wfile.write(b"retry: 1000\n\n")
                while True:
                    app.wait(1.0)
                    key = (app.state.version, app.config.version)
                    now = time.monotonic()
                    if key == last and now - sent < 1.0:
                        continue
                    if now - sent < 1 / 30:
                        time.sleep(1 / 30 - (now - sent))
                    last, sent = key, time.monotonic()
                    self.wfile.write(b"data: " + json.dumps(app.overlay_state()).encode() + b"\n\n")
                    self.wfile.flush()
            except (ConnectionError, OSError):
                pass

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            if path in PAGES:
                target = PAGES[path]
                if target.startswith("/"):
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
                path = "/" + target
            if path == "/api/state":
                return self._json(app.snapshot())
            if path == "/api/stream":
                return self._stream()
            if path == "/api/log":
                return self._json({"lines": since(int((parse_qs(url.query).get("after") or ["0"])[0]))})
            if path in ("/api/observer/check", "/api/observer/find"):
                if not _is_local(self.client_address[0]):
                    return self._json({"error": "forbidden"}, 403)
                if path.endswith("check"):
                    return self._json(check_gfx((parse_qs(url.query).get("url") or [""])[0]))
                return self._json({"huds": find_gfx(app.config.get()["http_port"])})
            full = web_file(unquote(path))
            if not full:
                return self._json({"error": "not found"}, 404)
            with open(full, "rb") as f:
                self._send(f.read(), mimetypes.guess_type(full)[0] or "application/octet-stream")

        def do_POST(self):
            path = urlparse(self.path).path
            local = _is_local(self.client_address[0])
            # Observer PCs send the game here over the network.
            if not local and not (path == "/api/ingest" and app.config.get().get("allow_network")):
                return self._json({"error": "settings can only be changed on the HUD's own PC"}, 403)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if path == "/api/ingest":
                    app.ingest(body.get("messages") or [])
                    return self._json({"ok": True})
                if path == "/api/config":
                    app.config.update(body)
                    if "stats_api" in body:
                        app._apply_reading()
                    app.changed()
                elif path == "/api/reader":
                    app.set_reading(body.get("enabled"))
                elif path == "/api/statsapi/enable":
                    done = statsapi.enable()
                    if not done:
                        raise ValueError("Rocket League's settings folder wasn't found on this PC. Is Rocket League installed?")
                    log("Turned on Rocket League's Stats API in " + ", ".join(done) + " (restart Rocket League)")
                elif path == "/api/demo":
                    app.set_demo(bool(body.get("on")))
                elif path == "/api/role":
                    app.set_role(body.get("role"))
                elif path == "/api/network":
                    app.set_network(bool(body.get("on")))
                elif path == "/api/logo":
                    logo = save_upload(LOGO_DIR, "logos/", body.get("name"), body.get("data"))
                    if body.get("side") in ("left", "right"):
                        app.config.update({body["side"]: {"logo": logo}})
                    app.changed()
                elif path == "/api/sponsor":
                    sponsor = save_upload(SPONSOR_DIR, "sponsors/", body.get("name"), body.get("data"))
                    app.config.update({"sponsors": app.config.get()["sponsors"] + [sponsor]})
                    app.changed()
                elif path == "/api/overlay-window":
                    if not app.overlay_window:
                        raise ValueError("The HUD window needs the Windows desktop app")
                    app.overlay_window.set(bool(body.get("open")), body.get("screen"), body.get("url"))
                elif path == "/api/update/check":
                    app.updater.check()
                elif path == "/api/update/install":
                    app.updater.install_in_background()
                else:
                    return self._json({"error": "not found"}, 404)
                self._json(app.snapshot())
            except (ValueError, KeyError, TypeError, OSError) as e:
                self._json({"error": str(e)}, 400)

    return Handler


class Server:
    def __init__(self, app, host, port):
        self.app, self.port = app, port
        self._start(host)

    def _start(self, host):
        self.httpd = ThreadingHTTPServer((host, self.port), make_handler(self.app))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.host = host

    def restart(self, host):
        if host != self.host:
            self.httpd.shutdown()
            self.httpd.server_close()
            self._start(host)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def start(app):
    """Start the web server (raises OSError if the port is taken), reading the game if set to."""
    port = app.config.get()["http_port"]
    app.server = Server(app, app.host(), port)
    app._apply_reading()
    threading.Thread(target=app._send_loop, daemon=True).start()
    clean_up_old_update()
    app.updater.check_in_background()
    log(f"Rocket League HUD {app.updater.current} running - control panel http://localhost:{port}/control")
    log(f"Overlay (browser source, 1920x1080): http://localhost:{port}/overlay")
    return app.server
