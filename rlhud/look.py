"""The overlay's look from the SCC GFX Engine ("Add to a HUD").

A graphic made in the GFX Engine's Composer can be added to this HUD: the
engine keeps it (as its HTML export: the graphic, its pictures and fonts,
and the GFX player) for Rocket League, and this HUD, connected to that
engine, takes each new version in and plays it as its overlay with its own
live data (look-live.js), at the game's own speed.

    look/            the export, as it came
    look.json        {"name", "rev", "at", "engine", "from"}
"""

import io
import json
import os
import shutil
import threading
import time
import urllib.request
import zipfile

from .log import log

CHECK_EVERY = 5      # seconds between asking the engine for a new version
MAX_SIZE = 200 << 20


def install(folder, blob, meta):
    """Unpacks an export zip as the look (the old one stays until the new one is in)."""
    if len(blob) > MAX_SIZE:
        raise ValueError("That graphic is too big (200 MB at most)")
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise ValueError("That isn't a GFX Engine graphic (a .zip from Add to a HUD or Export)")
    names = z.namelist()
    if "index.html" not in names or "gfx-data.js" not in names or "runtime.js" not in names:
        raise ValueError("That zip isn't a GFX Engine graphic export (no index.html / gfx-data.js / runtime.js)")
    new = os.path.join(folder, "look.new")
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(new)
    root = os.path.realpath(new)
    for info in z.infolist():
        if info.is_dir():
            continue
        target = os.path.realpath(os.path.join(new, info.filename))
        if not target.startswith(root + os.sep):
            continue   # (a path that climbs out)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with z.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    old = os.path.join(folder, "look")
    gone = os.path.join(folder, "look.old")
    shutil.rmtree(gone, ignore_errors=True)
    if os.path.isdir(old):
        os.replace(old, gone)
    os.replace(new, old)
    shutil.rmtree(gone, ignore_errors=True)
    meta = dict(meta, at=meta.get("at") or time.time(), installed=time.time())
    with open(os.path.join(folder, "look.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return meta


def current(folder):
    """The look this HUD has: its meta, or None."""
    if not os.path.isfile(os.path.join(folder, "look", "index.html")):
        return None
    try:
        with open(os.path.join(folder, "look.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"name": "Graphic", "rev": 0}


def remove(folder):
    shutil.rmtree(os.path.join(folder, "look"), ignore_errors=True)
    try:
        os.remove(os.path.join(folder, "look.json"))
    except OSError:
        pass


def page(folder):
    """The look's page, fed by this HUD (look-live.js goes in before the player)."""
    with open(os.path.join(folder, "look", "index.html"), encoding="utf-8") as f:
        html = f.read()
    tag = '<script src="/look-live.js"></script>\n<script src="gfx-data.js"></script>'
    html = html.replace('<script src="gfx-data.js"></script>', tag, 1) if "gfx-data.js" in html else html
    # (its files are beside it, under /look/)
    return html.replace("<head>", '<head>\n<base href="/look/">', 1)


def _get(url, timeout=10):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return r.read()


class LookSync:
    """Takes each new graphic the connected GFX Engine has for Rocket League."""

    def __init__(self, folder, engine, on_change):
        self.folder, self.engine, self.on_change = folder, engine, on_change
        self.error = ""
        self.waiting = None     # what the engine has, when it differs from ours
        self._stop = threading.Event()
        self._wake = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def info(self):
        have = current(self.folder)
        return {"look": have, "error": self.error, "engine_has": self.waiting}

    def check_now(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _run(self):
        failed = ""
        while not self._stop.is_set():
            self._wake.wait(CHECK_EVERY)
            self._wake.clear()
            eng = self.engine
            if not eng.connected or not eng.url:
                continue
            try:
                reply = json.loads(_get(f"{eng.url}/api/hud-look?game=rl", 5))
                theirs = reply.get("look")
                if not theirs:
                    self.waiting = None
                    continue
                have = current(self.folder) or {}
                same = have.get("rev") == theirs.get("rev") and have.get("engine") == reply.get("id") and have.get("at") == theirs.get("at")
                if same:
                    self.waiting, self.error = None, ""
                    continue
                self.waiting = theirs
                blob = _get(f"{eng.url}/api/hud-look?game=rl&file=1", 60)
                meta = install(self.folder, blob, {"name": theirs.get("name"), "rev": theirs.get("rev"), "at": theirs.get("at"),
                                                   "engine": reply.get("id"), "from": eng.name or eng.url})
                self.waiting, self.error = None, ""
                log(f'Took the overlay "{meta["name"]}" from the GFX Engine {eng.name or eng.url} (version {meta["rev"]})')
                self.on_change()
            except Exception as e:
                msg = str(getattr(e, "reason", e))
                if msg != failed:
                    log(f"Couldn't take the overlay from the GFX Engine: {msg}", "warn")
                failed = self.error = msg
            else:
                failed = ""
