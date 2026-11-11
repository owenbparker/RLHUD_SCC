"""Player webcams on the HUD.

Each player can run the SCC Player Cam app on their PC, pick Rocket League
and enter their in-game name. Their cam connects straight to the overlay
over the local network (WebRTC, peer to peer); this app only passes the
connection messages between the cam and the overlays.

A player's cam can also be a link (a VDO.Ninja view link, say) typed in the
control panel beside their in-game name; a link takes over from their
Player Cam.

A cam is matched to a player in the match by name (the game's own name, or
the name the HUD shows them as), and the overlay shows the cam of the
player the observer is spectating.

(The name travels as "riot_id", the field Player Cam's VALORANT mode uses.)
"""

import json
import re
import threading
import time
import unicodedata
from urllib.parse import urlparse

from .log import log

LONG_POLL = 20          # seconds a poll waits for a message
GONE_AFTER = LONG_POLL + 15


def normalise(text):
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def clean_cam_name(text):
    """A cam's name: the player's in-game name; raises ValueError when empty."""
    name = re.sub(r"\s+", " ", str(text or "")).strip()
    if not name or len(name) > 32:
        raise ValueError("Enter your in-game name")
    return name


def match_players(cam_names, players):
    """{cam name: {"team", "slot", "name"}} for the cams that are a player in
    the match. players: [{"team": 0|1, "slot": 1.., "name", "also": [names]}]."""
    out, taken = {}, set()
    for cam in cam_names:
        key = normalise(cam)
        if not key:
            continue
        for p in players:
            k = (p.get("team"), p.get("slot"))
            if k in taken:
                continue
            if any(normalise(n) == key for n in [p.get("name")] + list(p.get("also") or []) if n):
                out[cam] = {"team": p["team"], "slot": p["slot"], "name": p.get("name")}
                taken.add(k)
                break
    return out


def clean_link(text):
    """A cam link as typed ("vdo.ninja/?view=abc" or a full address) ->
    "https://vdo.ninja/?view=abc"; "" when it isn't a web address."""
    text = str(text or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        u = urlparse(text)
    except ValueError:
        return ""
    return text if u.scheme in ("http", "https") and u.hostname and "." in u.hostname else ""


def cam_links(entries, players=()):
    """The cam links typed in the control panel, each beside a player's
    in-game name (config cams.links): [{"name", "link", "player"}]."""
    rows = []
    for e in entries or ():
        if not isinstance(e, dict):
            continue
        try:
            name = clean_cam_name(e.get("name"))
        except ValueError:
            continue
        rows.append({"name": name, "link": clean_link(e.get("link"))})
    matched = match_players([r["name"] for r in rows], players)
    for r in rows:
        r["player"] = matched.get(r["name"])
    return rows


class CamHub:
    """Passes WebRTC connection messages between player cams (publishers)
    and the overlays showing them (viewers), and keeps track of whose cam is
    whose."""

    def __init__(self):
        self._cond = threading.Condition()
        self._inbox = {}       # peer id -> [messages]
        self._seen = {}        # peer id -> last poll
        self._name = {}        # publisher id -> the player's in-game name
        self._label = {}       # publisher id -> camera name

    def poll(self, peer, name=None, label=None, wait=LONG_POLL):
        """A cam or overlay checking in; waits up to `wait` seconds for
        messages. A cam passes its player's name ("" when it leaves)."""
        if not peer or len(peer) > 64:
            raise ValueError("Bad peer id")
        deadline = time.time() + max(0.0, min(float(wait or 0), LONG_POLL))
        with self._cond:
            self._touch(peer)
            if name is not None:
                who = clean_cam_name(name) if name else ""
                if who:
                    for other, n in list(self._name.items()):
                        if other != peer and normalise(n) == normalise(who):
                            self._name.pop(other)   # one cam per player: the newest wins
                            self._inbox.setdefault(other, []).append({"from": "hub", "type": "replaced"})
                if self._name.get(peer, "") != who:
                    log(f"Player cam {'connected' if who else 'left'}: {who or self._name.get(peer, '')}"
                        + (f" ({label})" if label and who else ""))
                if who:
                    self._name[peer] = who
                else:
                    self._name.pop(peer, None)
                if label is not None:
                    self._label[peer] = str(label)[:60]
                elif not who:
                    self._label.pop(peer, None)   # (a cam leaving)
            while not self._inbox.get(peer) and time.time() < deadline:
                self._cond.wait(deadline - time.time())
                self._touch(peer)
            return {"riot_id": self._name.get(peer), "messages": self._inbox.pop(peer, [])}

    def send(self, to, sender, data, ip=None):
        """A message for a peer. ip: where the sender is, as this app sees it
        (WebRTC hides a PC's own address behind a ".local" name that doesn't
        reach across networks: the other end puts this address in its place)."""
        if not isinstance(data, dict) or len(json.dumps(data)) > 64 * 1024:
            raise ValueError("Bad message")
        with self._cond:
            if to not in self._seen:
                raise ValueError("That cam or overlay isn't connected")
            box = self._inbox.setdefault(to, [])
            box.append({**data, "from": sender, **({"ip": ip} if ip else {})})
            del box[:-50]
            self._cond.notify_all()

    def _touch(self, peer):
        now = time.time()
        self._seen[peer] = now
        for p, t in list(self._seen.items()):
            if now - t > GONE_AFTER:
                for d in (self._seen, self._inbox, self._label):
                    d.pop(p, None)
                who = self._name.pop(p, None)
                if who:
                    log(f"Player cam disconnected: {who}")

    def all_cams(self, players=()):
        """Every cam connected, with or without a name yet (the GFX Engine's
        Camera Control Room): [{"id", "riot_id", "label", "player"}]."""
        with self._cond:
            now = time.time()
            live = [(p, self._name.get(p, ""), label) for p, label in self._label.items()
                    if now - self._seen.get(p, 0) < GONE_AFTER]
        matched = match_players([n for _, n, _ in live if n], players)
        return [{"id": p, "riot_id": n, "label": label, "player": matched.get(n)} for p, n, label in live]

    def publishers(self, players=()):
        """[{"id", "riot_id", "label", "player": {"team", "slot", "name"} or None}]."""
        with self._cond:
            now = time.time()
            live = [(p, n) for p, n in self._name.items() if now - self._seen.get(p, 0) < GONE_AFTER]
            labels = dict(self._label)
        matched = match_players([n for _, n in live], players)
        return [{"id": p, "riot_id": n, "label": labels.get(p, ""), "player": matched.get(n)} for p, n in live]
