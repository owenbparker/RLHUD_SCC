"""A made-up 3v3, sent as the Stats API would send it (UpdateState many times
a second, and events: shots, saves, demos, goals with their replays), so
the HUD can be tried without the game."""

import random
import threading
import time

BLUE = ["DRALII", "KIILEERRZ", "RW9"]
ORANGE = ["ATOMIC", "BEASTMODE", "DANIEL"]


class DemoMatch:
    def __init__(self, send, rate=30, seed=None, speed=1.0):
        self.send = send
        self.rate, self.speed = rate, speed
        self.rng = random.Random(seed)
        self._stop = threading.Event()
        self._thread = None
        self.players = [{"Name": n, "PrimaryId": f"Epic|{i}|0", "Shortcut": i + 1, "TeamNum": 0 if i < 3 else 1,
                         "Score": 0, "Goals": 0, "Shots": 0, "Assists": 0, "Saves": 0, "Touches": 0, "CarTouches": 0,
                         "Demos": 0, "Boost": 33, "Speed": 0, "bBoosting": False, "bOnGround": True, "bOnWall": False,
                         "bPowersliding": False, "bDemolished": False, "bSupersonic": False, "bHasCar": True}
                        for i, n in enumerate(BLUE + ORANGE)]
        self.score = [0, 0]
        self.clock = 300.0
        self.target = 0
        self.state = "countdown"
        self.state_until = 3.0
        self.replay = False
        self.demolished_until = {}

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="demo")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def ref(self, i):
        p = self.players[i]
        return {"Name": p["Name"], "Shortcut": p["Shortcut"], "TeamNum": p["TeamNum"]}

    def event(self, name, data=None):
        self.send(name, dict({"MatchGuid": "DEMO"}, **(data or {})))

    def update_state(self):
        players = []
        for i, p in enumerate(self.players):
            players.append(dict(p, bDemolished=i in self.demolished_until))
        self.send("UpdateState", {"MatchGuid": "DEMO", "Players": players, "Game": {
            "Teams": [{"Name": "BLUE", "TeamNum": 0, "Score": self.score[0], "ColorPrimary": "1873FF", "ColorSecondary": "0A3C8C"},
                      {"Name": "ORANGE", "TeamNum": 1, "Score": self.score[1], "ColorPrimary": "C26418", "ColorSecondary": "7A3A0A"}],
            "TimeSeconds": int(round(self.clock)), "bOvertime": False, "Ball": {"Speed": self.rng.uniform(300, 2500), "TeamNum": 0},
            "bReplay": self.replay, "bHasWinner": False, "Winner": "", "Arena": "Stadium_P",
            "bHasTarget": True, "Target": self.ref(self.target)}})

    def _run(self):
        self.event("MatchCreated")
        self.event("MatchInitialized")
        self.event("CountdownBegin")
        t_prev = time.monotonic()
        since_switch = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            dt = (now - t_prev) * self.speed
            t_prev = now
            self.step(dt)
            since_switch += dt
            if since_switch > self.rng.uniform(5, 9) and not self.replay:
                since_switch = 0
                self.target = self.rng.randrange(6)
            self.update_state()
            self._stop.wait(1 / self.rate)

    def step(self, dt):
        rng = self.rng
        self.state_until -= dt
        for i, p in enumerate(self.players):
            if i in self.demolished_until:
                self.demolished_until[i] -= dt
                if self.demolished_until[i] <= 0:
                    del self.demolished_until[i]
                    p["Boost"] = 33
                continue
            p["Boost"] = int(max(0, min(100, p["Boost"] + rng.uniform(-40, 34) * dt)))
            if rng.random() < 0.15 * dt:
                p["Boost"] = 100
            p["Speed"] = rng.uniform(800, 2300)
            p["bSupersonic"] = p["Speed"] > 2200
        if self.state == "countdown":
            if self.state_until <= 0:
                self.state, self.replay = "live", False
                self.event("RoundStarted")
            return
        if self.state == "replay":
            if self.state_until <= 0:
                self.event("GoalReplayEnd")
                self.state, self.state_until, self.replay = "countdown", 3.0, False
                self.event("CountdownBegin")
            return
        if self.state == "goal":
            if self.state_until <= 0:
                self.state, self.state_until, self.replay = "replay", 6.0, True
                self.event("GoalReplayStart")
            return
        # Live: the clock runs, and things happen.
        before = int(self.clock)
        self.clock = max(0.0, self.clock - dt)
        if int(self.clock) != before:
            self.event("ClockUpdatedSeconds", {"TimeSeconds": int(self.clock), "bOvertime": False})
        if self.clock <= 0:
            self.event("MatchEnded", {"WinnerTeamNum": 0 if self.score[0] >= self.score[1] else 1})
            self.clock, self.score = 300.0, [0, 0]
            for p in self.players:
                for k in ("Score", "Goals", "Shots", "Assists", "Saves", "Demos"):
                    p[k] = 0
            self.state, self.state_until = "countdown", 4.0
            self.event("CountdownBegin")
            return
        if rng.random() < 0.25 * dt:
            self.shot(rng.randrange(6))
        if rng.random() < 0.06 * dt:
            a = rng.randrange(6)
            victims = [i for i in range(6) if self.players[i]["TeamNum"] != self.players[a]["TeamNum"]
                       and i not in self.demolished_until]
            if victims:
                v = rng.choice(victims)
                self.players[a]["Demos"] += 1
                self.players[a]["Score"] += 25
                self.demolished_until[v] = 3.0
                self.event("StatfeedEvent", {"EventName": "Demolish", "Type": "Demolition",
                                             "MainTarget": self.ref(a), "SecondaryTarget": self.ref(v)})

    def shot(self, i):
        rng, p = self.rng, self.players[i]
        p["Shots"] += 1
        p["Score"] += 20
        self.event("StatfeedEvent", {"EventName": "Shot", "Type": "Shot on Goal", "MainTarget": self.ref(i)})
        team = p["TeamNum"]
        defenders = [k for k in range(6) if self.players[k]["TeamNum"] != team]
        if rng.random() < 0.55:
            k = rng.choice(defenders)
            self.players[k]["Saves"] += 1
            self.players[k]["Score"] += 50
            self.event("StatfeedEvent", {"EventName": "Save", "Type": "Save", "MainTarget": self.ref(k)})
            return
        # Goal.
        p["Goals"] += 1
        p["Score"] += 100
        self.score[team] += 1
        mates = [k for k in range(6) if self.players[k]["TeamNum"] == team and k != i]
        assister = rng.choice(mates) if rng.random() < 0.6 else None
        data = {"GoalSpeed": rng.uniform(1800, 3200), "GoalTime": 300 - self.clock, "ImpactLocation": {"X": 0, "Y": 5120, "Z": 300},
                "Scorer": self.ref(i), "BallLastTouch": {"Player": self.ref(i), "Speed": 2400}}
        if assister is not None:
            self.players[assister]["Assists"] += 1
            self.players[assister]["Score"] += 50
            data["Assister"] = self.ref(assister)
        self.event("StatfeedEvent", {"EventName": "Goal", "Type": "Goal", "MainTarget": self.ref(i)})
        if assister is not None:
            self.event("StatfeedEvent", {"EventName": "Assist", "Type": "Assist", "MainTarget": self.ref(assister)})
        self.event("GoalScored", data)
        self.target = i
        self.state, self.state_until = "goal", 2.5
