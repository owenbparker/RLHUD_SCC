"""The running app: the match (from Rocket League's Stats API, an observer
PC, or the demo), and the web server for the overlay and the clients."""

import base64
import json
import mimetypes
import os
import re
import socket
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import look, statsapi
from .cams import CamHub, cam_links
from .config import DATA_DIR, LOGO_DIR, SPONSOR_DIR, WEB_DIR, web_file
from .demo import DemoMatch
from .enginelink import EngineLink, save_logo_bytes, save_sponsors
from .game import GameState
from .log import log, since
from .updater import Updater, clean_up_old_update

mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("image/svg+xml", ".svg")

PAGES = {"/": "/start", "/start": "start.html", "/control": "control.html", "/observer": "observer.html",
         "/overlay": "overlay.html", "/overlay-classic": "overlay.html", "/spectated-cam": "spectated-cam.html"}
CAM_PATHS = ("/api/cam/poll", "/api/cam/send", "/api/cam/list")
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
        # The SCC GFX Engine: the match and series go to it; its teams can come here.
        self.engine = EngineLink("Rocket League HUD", "rl", self._engine_snapshot, self._engine_apply_global,
                                 load=lambda: self.config.get().get("gfx_engine"),
                                 save=lambda d: self.config.update({"gfx_engine": d}), log=log,
                                 swap=lambda: self.swap_sides(engine=False))
        # (a few times a second while the ball is in play, so the engine's boost and clock keep up)
        self.engine.every = lambda: 0.25 if self.state.phase in ("live", "countdown", "goal") else 1.0
        self.cams = CamHub()   # player webcams (SCC Player Cam, Rocket League mode)
        # The overlay made in the GFX Engine's Composer ("Add to a HUD").
        self.look = look.LookSync(DATA_DIR, self.engine, self.changed)
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
        https = self.server.https_port if self.server and self.server.httpsd else None
        return {"role": self.role, "port": port, "lan_url": f"http://{lan}:{port}" if lan else None,
                "https_port": https, "https_url": f"https://{lan}:{https}" if lan and https else None,
                "look": self.look.info(),
                "network": bool(cfg.get("allow_network")), "demo": bool(self.demo), "source": source,
                "stats": dict(self.stats.info(), read=cfg["stats_api"]["read"], files=statsapi.config_files()),
                "update": self.updater.info(), "data_dir": DATA_DIR,
                "overlay_window": self.overlay_window.info() if self.overlay_window else {"available": False}}

    def snapshot(self):
        game = self.state.snapshot()
        return {"game": game, "config": self.config.get(), "app": self.info(), "logos": logos(),
                "cams": dict(self.cams_state(game), connected=self.cams.all_cams(self.cam_players(game)))}

    def overlay_state(self):
        cfg = self.config.get()
        keep = ("left", "right", "best_of", "event", "stage", "scorebug", "names", "sponsors", "sponsor_seconds", "show")
        game = self.state.snapshot()
        cams = self.cams_state(game)
        return {"game": game, "config": {k: cfg[k] for k in keep},
                # (the GFX Engine overlay's data, and who has a cam)
                "fields": self._engine_snapshot(here=True)["fields"], "cams": cams,
                "target_cam": self.target_cam(game, cams), "now": time.time(),
                # (the engine's Global, when connected: the teams' colours, the event)
                "global": self._global_bits()}

    def _global_bits(self):
        g = self.engine.engine_global if self.engine.connected else None
        if not isinstance(g, dict):
            return None
        out = {k: g.get(k) for k in ("event", "stage") if isinstance(g.get(k), str)}
        for side in ("left", "right"):
            t = g.get(side) if isinstance(g.get(side), dict) else {}
            out[side] = {"color": t.get("color") or ""}
        return out

    # --- the SCC GFX Engine -------------------------------------------------------------

    # --- player cams -------------------------------------------------------------------

    def cam_players(self, game=None):
        """The players in the match, as cams are matched to them:
        [{"team": 0|1, "slot": 1.., "name", "also": [the name the HUD shows]}]."""
        game = game or self.state.snapshot()
        names = self.config.get().get("names") or {}
        out = []
        for team in (0, 1):
            for slot, p in enumerate([p for p in game["players"] if p["team"] == team], 1):
                out.append({"team": team, "slot": slot, "name": p["name"], "also": [names.get(p["name"]) or ""]})
        return out

    def cams_state(self, game=None):
        """Who has a cam: {"publishers": [...], "links": [...]} (cams.hidden left out)."""
        cc = self.config.get().get("cams") or {}
        players = self.cam_players(game)
        hidden = {str(n).lower() for n in cc.get("hidden") or []}
        pubs = [p for p in self.cams.publishers(players) if p["riot_id"].lower() not in hidden]
        links = [l for l in cam_links(cc.get("links"), players) if l["link"] and l["name"].lower() not in hidden]
        return {"publishers": pubs, "links": links, "on": cc.get("on", True) is not False}

    def target_cam(self, game, cams=None):
        """The spectated player's cam: {"link"} or {"id"} (a Player Cam), or None."""
        t = game.get("target")
        if not t or t.get("team") not in (0, 1):
            return None
        cams = cams or self.cams_state(game)
        if not cams["on"]:
            return None
        slot = next((p["slot"] for p in self.cam_players(game) if p["team"] == t["team"] and p["name"] == t["name"]), None)
        for l in cams["links"]:
            if l["player"] and l["player"]["team"] == t["team"] and l["player"]["slot"] == slot:
                return {"link": l["link"], "name": l["name"]}
        for p in cams["publishers"]:
            if p["player"] and p["player"]["team"] == t["team"] and p["player"]["slot"] == slot:
                return {"id": p["id"], "name": p["riot_id"]}
        if self.demo:   # (the demo match: everyone has a stand-in cam, to see the box work)
            return {"demo": True, "name": t["name"]}
        return None

    def cam_post(self, path, body, client=None):
        """Webcam messages (also from other PCs on the network)."""
        if path == "/api/cam/poll":
            result = self.cams.poll(str(body.get("id") or ""), body.get("riot_id"), body.get("label"), body.get("wait", 20))
            if result.get("riot_id"):
                result["player"] = next((p["player"] for p in self.cams.publishers(self.cam_players())
                                         if p["id"] == body.get("id")), None)
            return result
        if path == "/api/cam/send":
            self.cams.send(str(body.get("to") or ""), str(body.get("from") or ""), body.get("data"), client)
            return {"ok": True}
        if path == "/api/cam/list":
            # The cams connected now, for the GFX Engine's Camera Control Room.
            cams = []
            for p in self.cams.all_cams(self.cam_players()):
                who = p["player"]
                cams.append({"id": p["id"], "label": p["label"], "riot_id": p["riot_id"],
                             "player": who["name"] if who else "", "team": ("left", "right")[who["team"]] if who else ""})
            return {"game": "rl", "cams": cams}
        raise KeyError(path)

    def _engine_snapshot(self, here=False):
        """What the GFX Engine gets: the match as flat fields, and the series.
        here: for this HUD's own overlay (pictures and the cam at this address)."""
        cfg = self.config.get()
        game = self.state.snapshot()
        names = cfg.get("names") or {}
        target = game.get("target") or {}
        f = {}
        # Pictures and pages as the engine's outputs (on any PC) reach them.
        port = self.server.port if self.server else cfg["http_port"]
        ip = lan_ip() if cfg.get("allow_network") else None
        base = "" if here else f"http://{ip or '127.0.0.1'}:{port}"
        pic = lambda rel: (f"{base}/{rel}" if rel and not re.match(r"https?://", rel) else rel or "")
        team_size = 0
        for i, side in enumerate(("left", "right")):
            team = cfg[side]
            f[f"{side}_name"] = team["name"]
            f[f"{side}_short"] = team["tricode"]
            f[f"{side}_goals"] = game["teams"][i]["score"]
            f[f"{side}_series"] = int(team["wins"] or 0)
            players = [p for p in game["players"] if p["team"] == i]
            team_size = max(team_size, len(players))
            for n in range(1, 5):
                key = f"{side}_p{n}"
                p = players[n - 1] if n <= len(players) else None
                if not p:
                    if n <= 3:   # (an empty row: a 2v2 or 1v1, or no match yet)
                        for k in ("name", "ign", "goals", "assists", "saves", "shots", "score", "demos", "touches", "boost",
                                  "spectated", "demolished", "here"):
                            f[f"{key}_{k}"] = ""
                    continue
                f[key + "_name"] = names.get(p["name"]) or p["name"]
                f[key + "_ign"] = p["name"]   # (the game's own name: what GFX Engine links its roster by)
                for stat in ("goals", "assists", "saves", "shots", "score", "demos", "touches"):
                    f[f"{key}_{stat}"] = p[stat]
                f[key + "_boost"] = p["boost"] if p["boost"] is not None else ""
                f[key + "_spectated"] = 1 if target and target.get("name") == p["name"] and target.get("team") == i else ""
                f[key + "_demolished"] = 1 if p.get("demolished") else ""
                f[key + "_here"] = 1
        clock = int(game["clock"] or 0)
        f["clock"] = ("+" if game["overtime"] else "") + f"{clock // 60}:{clock % 60:02d}"
        f["clock_seconds"] = clock
        f["overtime"] = 1 if game["overtime"] else ""
        f["phase"] = game["phase"]
        f["live"] = 1 if game["phase"] == "live" else ""
        f["replay"] = 1 if game["replay"] or game["phase"] == "replay" else ""
        f["team_size"] = team_size
        f["best_of"] = int(cfg.get("best_of") or 7)
        f["event"] = cfg.get("event") or ""
        f["stage"] = cfg.get("stage") or ""
        f["target"] = names.get(target.get("name")) or target.get("name") or ""
        # The spectated player: their stats, their team, their cam.
        tp = next((p for p in game["players"] if target and p["name"] == target.get("name") and p["team"] == target.get("team")), None)
        side = ("left", "right")[target["team"]] if target and target.get("team") in (0, 1) else ""
        f["target_side"] = side
        for stat in ("goals", "assists", "saves", "shots", "score", "demos", "touches", "speed"):
            f["target_" + stat] = tp[stat] if tp else ""
        f["target_boost"] = tp["boost"] if tp and tp["boost"] is not None else ""
        f["target_logo"] = pic(cfg[side]["logo"]) if side else ""
        gb = self._global_bits() or {}
        f["target_color"] = ((gb.get(side) or {}).get("color") or cfg[side]["color"]) if side else ""
        cam = self.target_cam(game) if tp else None
        f["target_has_cam"] = 1 if cam else ""
        # (one page for whoever is spectated: it switches cams itself, never reloading)
        https = self.server.https_port if self.server and self.server.httpsd else None
        f["target_cam"] = "/spectated-cam" if here else (f"https://{ip or '127.0.0.1'}:{https}/spectated-cam" if https and ip
                                                          else f"{base}/spectated-cam")
        goal = game.get("goal") or {}
        scorer, assister = goal.get("scorer") or {}, goal.get("assister") or {}
        f["goal_scorer"] = (names.get(scorer.get("name")) or scorer.get("name") or "") if goal else ""
        f["goal_assister"] = (names.get(assister.get("name")) or assister.get("name") or "") if goal else ""
        f["goal_speed"] = goal.get("speed", "") if goal else ""
        f["goal_side"] = ("left", "right")[goal["team"]] if goal and goal.get("team") in (0, 1) else ""
        f["ball_speed"] = game["ball_speed"]
        f["arena"] = game["arena"]
        f["look_rev"] = (look.current(DATA_DIR) or {}).get("rev", "")
        series = {side: {"name": cfg[side]["name"], "short": cfg[side]["tricode"], "score": int(cfg[side]["wins"] or 0),
                         "color": cfg[side]["color"]} for side in ("left", "right")}
        series["best_of"] = f["best_of"]
        return {"fields": f, "series": series}

    def _engine_apply_global(self, g, fetch):
        """The GFX Engine's teams (names, short names, logos) and sponsors into the HUD."""
        changes, done = {}, []
        for side in ("left", "right"):
            t = g.get(side) or {}
            team = {}
            if t.get("name"):
                team["name"] = t["name"]
            if t.get("short"):
                team["tricode"] = t["short"][:6]
            if t.get("logo_url"):
                try:
                    blob = fetch(t["logo_url"])
                    name = save_logo_bytes(LOGO_DIR, f"engine-{t.get('short') or t.get('name') or side}", blob)
                    team["logo"] = f"logos/{name}"
                except Exception as e:
                    log(f"Couldn't fetch the {side} logo from the GFX Engine: {e}", "warn")
            if str(t.get("score", "")).isdigit():   # (the series score: Global's is the one shown)
                team["wins"] = int(t["score"])
            if team:
                changes[side] = team
                done.append(f"{side}: {team.get('name') or team.get('tricode') or 'logo'}")
        # Its sponsors, in the HUD's sponsor rotation.
        sponsors, secs = save_sponsors(g, fetch, SPONSOR_DIR, "sponsors/", log)
        if sponsors:
            changes["sponsors"] = sponsors
            if secs:
                changes["sponsor_seconds"] = secs
            done.append(f"{len(sponsors)} sponsor{'s' if len(sponsors) != 1 else ''}")
        if not changes:
            raise ValueError("The engine's Global configuration has no team names, logos or sponsors yet")
        self.config.update(changes)
        self.changed()
        return ", ".join(done)

    def wait(self, timeout):
        with self._cond:
            self._cond.wait(timeout)

    def swap_sides(self, engine=True):
        """The teams change sides (blue and orange): their names, tricodes,
        regions, logos and games won. The colours stay with the side."""
        cfg = self.config.get()
        keep = lambda side: {"color": cfg[side].get("color")}
        self.config.update({"left": dict(cfg["right"], **keep("left")), "right": dict(cfg["left"], **keep("right"))})
        log(f"Sides swapped: {cfg['right']['name'] or 'orange team'} on blue")
        self.changed()
        if engine:   # (connected: the GFX Engine's Global teams swap too, as the HUD follows them)
            self.engine.swap_global()

    def changed(self):
        with self._cond:
            self._cond.notify_all()

    def stop(self):
        self._stop.set()
        self.stats.stop()
        self.engine.stop()
        self.look.stop()
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
            # The overlay: the graphic from the GFX Engine when there is one (and it's on).
            if path == "/overlay" and look.current(DATA_DIR) and (app.config.get().get("look") or {}).get("use", True):
                try:
                    return self._send(look.page(DATA_DIR), "text/html; charset=utf-8")
                except OSError:
                    pass
            if path.startswith("/look/"):
                full = look_file(unquote(path[len("/look/"):]))
                if not full:
                    return self._json({"error": "not found"}, 404)
                if full.endswith("index.html"):
                    return self._send(look.page(DATA_DIR), "text/html; charset=utf-8")
                with open(full, "rb") as f:
                    return self._send(f.read(), mimetypes.guess_type(full)[0] or "application/octet-stream")
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
            if path == "/api/engine":
                return self._json(app.engine.status())
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
            if path in CAM_PATHS:
                # Player cams, from any PC on the network (when it's let in).
                if not local and not app.config.get().get("allow_network"):
                    return self._json({"error": "Tick \"Let other PCs on the network read the HUD\" on the HUD's PC"}, 403)
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > 256 * 1024:
                        raise ValueError("Too big")
                    body = json.loads(self.rfile.read(length) or b"{}")
                    return self._json(app.cam_post(path, body, self.client_address[0]))
                except (ValueError, KeyError, TypeError) as e:
                    return self._json({"error": str(e)}, 400)
            # Observer PCs send the game here over the network.
            if not local and not (path == "/api/ingest" and app.config.get().get("allow_network")):
                return self._json({"error": "settings can only be changed on the HUD's own PC"}, 403)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if path == "/api/ingest":
                    app.ingest(body.get("messages") or [])
                    return self._json({"ok": True})
                if path.startswith("/api/engine/"):
                    # Connect to GFX Engine (see enginelink.py).
                    reply = app.engine.handle("POST", path, body)
                    if reply is None:
                        return self._json({"error": "not found"}, 404)
                    return self._json(reply)
                if path == "/api/config":
                    app.config.update(body)
                    if "stats_api" in body:
                        app._apply_reading()
                    app.changed()
                elif path == "/api/swap":
                    app.swap_sides()
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
                elif path == "/api/look/check":
                    app.look.check_now()
                elif path == "/api/look/upload":
                    # A graphic exported from the GFX Engine (Composer > Add to a HUD, or Export), as a zip.
                    m = re.fullmatch(r"data:[^;,]*;base64,(.+)", body.get("data") or "", re.S)
                    if not m:
                        raise ValueError("Send the .zip the GFX Engine exported")
                    meta = look.install(DATA_DIR, base64.b64decode(m.group(1)),
                                        {"name": os.path.splitext(os.path.basename(body.get("name") or "Graphic"))[0], "rev": 0,
                                         "at": time.time(), "engine": "", "from": "a file"})
                    log(f'Overlay is now "{meta["name"]}" (from a file)')
                    app.config.update({"look": {"use": True}})
                    app.changed()
                elif path == "/api/look/remove":
                    look.remove(DATA_DIR)
                    log("Overlay back to the built-in one")
                    app.changed()
                elif path == "/api/cams":
                    # {"links": [{"name", "link"}], "hidden": [...], "on": bool}
                    changes = {k: body[k] for k in ("links", "hidden", "on") if k in body}
                    if "links" in changes:
                        changes["links"] = [{"name": str(e.get("name") or "")[:32], "link": str(e.get("link") or "")[:500]}
                                            for e in changes["links"] if isinstance(e, dict)][:16]
                    app.config.update({"cams": changes})
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


def look_file(rel):
    """A file of the GFX Engine overlay (DATA_DIR/look/...), or None."""
    base = os.path.realpath(os.path.join(DATA_DIR, "look"))
    path = os.path.realpath(os.path.join(base, rel.replace("\\", "/").lstrip("/") or "index.html"))
    return path if path.startswith(base + os.sep) and os.path.isfile(path) else None


class TlsServer(ThreadingHTTPServer):
    """The same server over https (for VDO.Ninja cam links on another PC's
    HUD window or browser). The handshake happens in each connection's own
    thread, so a slow one holds nothing up."""

    daemon_threads = True

    def __init__(self, address, handler, context):
        self.context = context
        super().__init__(address, handler)

    def finish_request(self, request, client_address):
        try:
            request = self.context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return  # e.g. a browser turning down the certificate
        super().finish_request(request, client_address)


class Server:
    """The HTTP server (and its https twin) in background threads; can be
    restarted on a new host."""

    def __init__(self, app, host, port, https_port=None):
        self.app, self.port, self.https_port = app, port, https_port
        self.httpd = self.httpsd = None
        self._start(host)

    def _start(self, host):
        self.httpd = ThreadingHTTPServer((host, self.port), make_handler(self.app))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.host = host
        self.httpsd = None
        if self.https_port:
            try:
                from .tls import server_context
                context = server_context(os.path.join(DATA_DIR, "certs"), ["localhost", "127.0.0.1", lan_ip()])
                if context:
                    self.httpsd = TlsServer((host, self.https_port), make_handler(self.app), context)
                    threading.Thread(target=self.httpsd.serve_forever, daemon=True).start()
            except Exception as e:  # the HUD works without it; only cam links on other PCs need it
                log(f"Couldn't start https on port {self.https_port} ({e}); cam links won't show on other PCs", "warn")

    def restart(self, host):
        if host == self.host:
            return
        self.stop()
        self._start(host)

    def stop(self):
        for server in (self.httpd, self.httpsd):
            if server:
                server.shutdown()
                server.server_close()


def start(app):
    """Start the web server (raises OSError if the port is taken), reading the game if set to."""
    cfg = app.config.get()
    port = cfg["http_port"]
    app.server = Server(app, app.host(), port, cfg.get("https_port") or port + 10)
    app._apply_reading()
    threading.Thread(target=app._send_loop, daemon=True).start()
    clean_up_old_update()
    app.updater.check_in_background()
    log(f"Rocket League HUD {app.updater.current} running - control panel http://localhost:{port}/control")
    log(f"Overlay (browser source, 1920x1080): http://localhost:{port}/overlay")
    return app.server
