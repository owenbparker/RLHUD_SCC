"""Settings for the Rocket League HUD, saved to config.json next to the
project (or next to RocketLeagueHUD.exe in the desktop build). Everything
here can be changed from the control panel while the app runs."""

import copy
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROZEN = getattr(sys, "frozen", False)
RESOURCE_DIR = getattr(sys, "_MEIPASS", ROOT) if FROZEN else ROOT
DATA_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else ROOT
WEB_DIR = os.path.join(RESOURCE_DIR, "web")
LOGO_DIR = os.path.join(DATA_DIR, "logos")         # uploaded team logos (served as logos/...)
SPONSOR_DIR = os.path.join(DATA_DIR, "sponsors")   # uploaded sponsor logos (served as sponsors/...)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    # Not the League (8777, 8779), VALORANT (8778) or CoD (8780) HUD's ports.
    "http_host": "127.0.0.1",
    "http_port": 8781,
    "allow_network": False,
    # Rocket League's Stats API: read the game on this PC (GFX), at this address.
    "stats_api": {"read": False, "host": "127.0.0.1", "port": 49123},
    # Observer client: the GFX PC whose HUD to show and send the game to.
    "observer_url": "",
    "overlay_screen": 0,

    # The teams: blue (left) and orange (right), as the game has them.
    "left": {"name": "TEAM FALCONS", "tricode": "FLCN", "region": "MENA2", "logo": "", "color": "#1d5fd6", "wins": 2},
    "right": {"name": "NRG", "tricode": "NRG", "region": "NA1", "logo": "", "color": "#d1892b", "wins": 1},
    "best_of": 7,
    # The scorebug's header: the event (shown with the team names) and the stage.
    "event": "ROCKET LEAGUE 2026 WORLD CHAMPIONSHIP",
    "stage": "PLAYOFFS | QUARTERFINAL",
    # The scorebug: "auto" (team names out around goals, replays and kickoffs),
    # "compact" or "expanded".
    "scorebug": "auto",
    # Player names to show instead of the in-game ones: {"in-game name": "shown name"}.
    "names": {},
    # Sponsor logos under the blue team's boost list, in turn (paths like
    # "sponsors/x.png"), each for this many seconds.
    "sponsors": [],
    "sponsor_seconds": 8,
    "show": {"players": True, "sponsors": True, "target": True, "boost": True, "events": True},
}

_lock = threading.Lock()


def _merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "names":
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.data = copy.deepcopy(DEFAULTS)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = _merge(DEFAULTS, json.load(f))
        self.version = 0

    def get(self):
        with _lock:
            return copy.deepcopy(self.data)

    def update(self, changes):
        with _lock:
            self.data = _merge(self.data, changes)
            self.version += 1
            snapshot = copy.deepcopy(self.data)
        self.save(snapshot)
        return snapshot

    def save(self, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.get()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, self.path)


def web_file(rel):
    """Path on disk for a URL path: uploaded logos (logos/...) and sponsors
    (sponsors/...), else the web folder. Paths that climb out are refused."""
    rel = rel.replace("\\", "/").lstrip("/")
    base = WEB_DIR
    for prefix, folder in (("logos/", LOGO_DIR), ("sponsors/", SPONSOR_DIR)):
        if rel.startswith(prefix):
            base, rel = folder, rel[len(prefix):]
    path = os.path.normpath(os.path.join(base, rel))
    if not path.startswith(os.path.normpath(base) + os.sep) or not os.path.isfile(path):
        return None
    return path
