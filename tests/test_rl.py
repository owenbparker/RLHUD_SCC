import base64
import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from rlhud import statsapi
from rlhud.config import Config
from rlhud.demo import DemoMatch
from rlhud.game import GameState

UPDATE = {"Event": "UpdateState", "Data": {
    "MatchGuid": "X", "Players": [
        {"Name": "ATOMIC", "Shortcut": 4, "TeamNum": 1, "Goals": 1, "Shots": 2, "Assists": 0, "Saves": 1, "Demos": 0, "Boost": 45},
        {"Name": "DRALII", "Shortcut": 1, "TeamNum": 0, "Goals": 0, "Shots": 1, "Assists": 1, "Saves": 0, "Demos": 2, "Boost": 100}],
    "Game": {"Teams": [{"Name": "BLUE", "TeamNum": 0, "Score": 4, "ColorPrimary": "1873FF"},
                       {"Name": "ORANGE", "TeamNum": 1, "Score": 1, "ColorPrimary": "C26418"}],
             "TimeSeconds": 74, "bOvertime": False, "bReplay": False, "bHasTarget": True,
             "Target": {"Name": "ATOMIC", "Shortcut": 4, "TeamNum": 1}, "Ball": {"Speed": 1000}, "Arena": "Stadium_P"}}}


class FramingTests(unittest.TestCase):
    def test_messages_back_to_back_lines_and_split(self):
        a, b = json.dumps(UPDATE), json.dumps({"Event": "RoundStarted", "Data": {}})
        msgs, rest = statsapi.messages(a + b + "\n" + a[:20])
        self.assertEqual([m["Event"] for m in msgs], ["UpdateState", "RoundStarted"])
        self.assertEqual(rest, a[:20])
        msgs, rest = statsapi.messages(rest + a[20:])
        self.assertEqual(len(msgs), 1)
        self.assertEqual(rest, "")

    def test_data_as_a_string(self):
        event, data = statsapi.unwrap({"Event": "GoalScored", "Data": json.dumps({"GoalSpeed": 100})})
        self.assertEqual((event, data["GoalSpeed"]), ("GoalScored", 100))

    def test_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "DefaultStatsAPI.ini")
            with open(path, "w") as f:
                f.write("[TAGame.MatchStatsExporter_TA]\nPacketSendRate=0\nOther=1\n")
            statsapi.set_ini(path)
            with open(path) as f:
                text = f.read()
            self.assertIn("PacketSendRate=30", text)
            self.assertIn("Port=49123", text)
            self.assertIn("Other=1", text)
            fresh = os.path.join(tmp, "TAStatsAPI.ini")
            statsapi.set_ini(fresh)
            with open(fresh) as f:
                self.assertEqual(f.read().splitlines()[0], "[TAGame.MatchStatsExporter_TA]")


def serve_once(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        conn, _ = srv.accept()
        try:
            handler(conn)
        finally:
            time.sleep(0.5)
            conn.close()
            srv.close()
    threading.Thread(target=run, daemon=True).start()
    return port


class ClientTests(unittest.TestCase):
    def _collect(self, port, n):
        got = []
        client = statsapi.StatsClient(lambda e, d: got.append((e, d)), "127.0.0.1", port).start()
        deadline = time.time() + 6
        while len(got) < n and time.time() < deadline:
            time.sleep(0.05)
        mode = client.mode
        client.stop()
        return got, mode

    def test_tcp_stream(self):
        payload = (json.dumps(UPDATE) + json.dumps({"Event": "GoalScored", "Data": {"GoalSpeed": 90}})).encode()
        port = serve_once(lambda c: (c.sendall(payload[:50]), time.sleep(0.1), c.sendall(payload[50:])))
        got, mode = self._collect(port, 2)
        self.assertEqual(mode, "tcp")
        self.assertEqual([e for e, _ in got], ["UpdateState", "GoalScored"])

    def test_websocket(self):
        def ws(conn):
            req = b""
            while b"\r\n\r\n" not in req:
                req += conn.recv(1024)
            key = [l.split(b": ")[1] for l in req.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            body = json.dumps(UPDATE).encode()
            conn.sendall(bytes([0x81, 126]) + len(body).to_bytes(2, "big") + body)
        port = serve_once(ws)
        got, mode = self._collect(port, 1)
        self.assertEqual(mode, "websocket")
        self.assertEqual(got[0][0], "UpdateState")


class GameTests(unittest.TestCase):
    def test_state_and_events(self):
        g = GameState()
        g.apply("UpdateState", UPDATE["Data"], now=100)
        s = g.snapshot(now=100)
        self.assertEqual([p["name"] for p in s["players"]], ["DRALII", "ATOMIC"])   # blue first
        self.assertEqual((s["teams"][0]["score"], s["teams"][1]["score"], s["clock"]), (4, 1, 74))
        self.assertEqual(s["target"]["name"], "ATOMIC")
        self.assertEqual(s["phase"], "live")
        g.apply("StatfeedEvent", {"EventName": "Save", "Type": "Save", "MainTarget": {"Name": "ATOMIC", "TeamNum": 1}}, now=101)
        g.apply("StatfeedEvent", {"EventName": "Unknown", "MainTarget": {"Name": "ATOMIC", "TeamNum": 1}}, now=101)
        self.assertEqual([e["icon"] for e in g.snapshot(now=102)["events"]], ["save"])
        self.assertEqual(g.snapshot(now=110)["events"], [])     # gone after a few seconds
        g.apply("GoalScored", {"GoalSpeed": 2500, "Scorer": {"Name": "DRALII", "TeamNum": 0}}, now=120)
        self.assertEqual(g.snapshot(now=121)["phase"], "goal")
        g.apply("GoalReplayStart", {}, now=123)
        self.assertEqual(g.snapshot(now=123)["phase"], "replay")
        g.apply("GoalReplayEnd", {}, now=128)
        g.apply("RoundStarted", {}, now=131)
        self.assertEqual(g.snapshot(now=131)["phase"], "live")

    def test_demo_match_plays(self):
        g = GameState()
        demo = DemoMatch(lambda e, d: g.apply(e, d), seed=3, speed=40).start()
        try:
            deadline = time.time() + 8
            while time.time() < deadline and not g.snapshot()["events"]:
                time.sleep(0.05)
        finally:
            demo.stop()
        s = g.snapshot()
        self.assertEqual(len(s["players"]), 6)
        self.assertTrue(s["events"])


class ServerTests(unittest.TestCase):
    def test_pages_stream_and_ingest(self):
        from rlhud.app import HudApp, start
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "config.json"))
            cfg.update({"http_port": 8794})
            app = HudApp(cfg)
            app.updater.check_in_background = lambda: None
            start(app)
            base = "http://127.0.0.1:8794"
            try:
                def post(path, body):
                    req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
                    return json.loads(urllib.request.urlopen(req, timeout=5).read())

                for path in ("/start", "/control", "/observer", "/overlay", "/update.js", "/enginelink.js", "/fonts/Saira-ExtraBoldItalic.ttf"):
                    with urllib.request.urlopen(base + path, timeout=5) as r:
                        self.assertEqual(r.status, 200, path)
                        r.read()
                # An observer PC sends the game.
                post("/api/ingest", {"messages": [["UpdateState", UPDATE["Data"]]]})
                state = json.loads(urllib.request.urlopen(base + "/api/state", timeout=5).read())
                self.assertEqual(state["game"]["target"]["name"], "ATOMIC")
                self.assertEqual(state["app"]["source"], "observer")
                stream = urllib.request.urlopen(base + "/api/stream", timeout=5)
                line = b""
                while not line.startswith(b"data:"):
                    line = stream.readline()
                msg = json.loads(line[5:])
                self.assertEqual(msg["game"]["clock"], 74)
                self.assertEqual(msg["config"]["best_of"], 7)
                stream.close()
                post("/api/config", {"left": {"name": "SAINTS"}, "names": {"ATOMIC": "Atomic!"}})
                self.assertEqual(cfg.get()["left"]["name"], "SAINTS")
                self.assertEqual(cfg.get()["left"]["tricode"], "FLCN")
                post("/api/demo", {"on": True})
                time.sleep(0.5)
                state = json.loads(urllib.request.urlopen(base + "/api/state", timeout=5).read())
                self.assertEqual(state["app"]["source"], "demo")
                self.assertEqual(len(state["game"]["players"]), 6)
                with self.assertRaises(urllib.error.HTTPError):
                    post("/api/role", {"role": "nobody"})
                engine = json.loads(urllib.request.urlopen(base + "/api/engine", timeout=5).read())
                self.assertEqual((engine["source"], engine["connected"]), ("Rocket League HUD", False))
                with self.assertRaises(urllib.error.HTTPError) as err:
                    post("/api/engine/nothing", {})
                self.assertEqual(err.exception.code, 404)
                with self.assertRaises(urllib.error.HTTPError) as err:
                    post("/api/engine/use-global", {})
                self.assertEqual(err.exception.code, 400)
            finally:
                app.stop()


class EngineLinkTests(unittest.TestCase):
    def test_sends_to_and_takes_from_a_gfx_engine(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from unittest import mock
        from rlhud import app as app_module
        from rlhud.app import HudApp

        ingests = []
        logo = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        engine_port = 8798

        class Engine(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body, ctype="application/json"):
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/hello":
                    return self._send({"app": "scc-gfx-engine", "name": "Test"})
                if self.path == "/api/global":
                    base = f"http://127.0.0.1:{engine_port}"
                    return self._send({"global": {"left": {"name": "KARMINE CORP", "short": "KC", "logo_url": base + "/logo.png"},
                                                  "right": {"name": "VITALITY", "short": "VIT"}}})
                if self.path == "/logo.png":
                    return self._send(logo, "image/png")
                self.send_error(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/ingest":
                    ingests.append(body)
                    return self._send({"ok": True, "global": {}})
                self.send_error(404)

        server = ThreadingHTTPServer(("127.0.0.1", engine_port), Engine)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as tmp:
            logo_dir = os.path.join(tmp, "logos")
            cfg = Config(os.path.join(tmp, "config.json"))
            with mock.patch.object(app_module, "LOGO_DIR", logo_dir):
                app = HudApp(cfg)
                try:
                    app.state.apply("UpdateState", UPDATE["Data"])
                    app.engine.connect(f"http://127.0.0.1:{engine_port}")
                    deadline = time.time() + 8
                    while time.time() < deadline and not any(b.get("series") and b.get("fields") for b in ingests):
                        time.sleep(0.05)
                    sent = [b for b in ingests if b.get("series") and b.get("fields")][0]
                    self.assertEqual((sent["source"], sent["game"]), ("Rocket League HUD", "rl"))
                    self.assertEqual(sent["series"]["left"], {"name": "TEAM FALCONS", "short": "FLCN", "score": 2, "color": "#1d5fd6"})
                    self.assertEqual(sent["series"]["best_of"], 7)
                    self.assertEqual(sent["fields"]["left_goals"], 4)
                    self.assertEqual(sent["fields"]["right_p1_name"], "ATOMIC")
                    self.assertEqual(sent["fields"]["clock"], "1:14")
                    self.assertTrue(cfg.get()["gfx_engine"]["connected"])

                    reply = app.engine.use_global()
                    self.assertIn("KARMINE CORP", reply["summary"])
                    c = cfg.get()
                    self.assertEqual((c["left"]["name"], c["left"]["tricode"]), ("KARMINE CORP", "KC"))
                    self.assertEqual((c["right"]["name"], c["right"]["tricode"]), ("VITALITY", "VIT"))
                    self.assertEqual(c["left"]["logo"], "logos/engine-KC.png")
                    with open(os.path.join(logo_dir, "engine-KC.png"), "rb") as f:
                        self.assertEqual(f.read(), logo)
                finally:
                    app.stop()
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
