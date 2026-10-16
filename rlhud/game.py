"""The match, as the Stats API (or the demo) tells it: built up from
UpdateState and the events, and handed to the overlay as a snapshot."""

import threading
import time

# Statfeed events shown by a player's name (EventName -> icon). Others are ignored.
EVENT_ICONS = {
    "Goal": "goal", "AerialGoal": "goal", "BicycleGoal": "goal", "LongGoal": "goal", "TurtleGoal": "goal",
    "BackwardsGoal": "goal", "HatTrick": "goal", "OvertimeGoal": "goal", "PoolShot": "goal",
    "Shot": "shot", "Save": "save", "EpicSave": "save", "Savior": "save", "Assist": "assist", "Playmaker": "assist",
    "Demolish": "demo", "Demolition": "demo",
}
EVENT_SECONDS = 6          # how long an event's icon stays by the name
GOAL_SECONDS = 8


def _player_ref(p):
    p = p or {}
    return {"name": p.get("Name") or "", "team": p.get("TeamNum"), "shortcut": p.get("Shortcut")}


class GameState:
    def __init__(self):
        self._lock = threading.Lock()
        self.version = 0
        self.reset()

    def reset(self):
        self.players = []
        self.teams = [{"name": "", "score": 0, "color": ""}, {"name": "", "score": 0, "color": ""}]
        self.clock, self.overtime = 300, False
        self.target = None
        self.replay = False
        self.phase = "none"        # none, countdown, live, goal, replay, paused, ended, podium
        self.winner = None
        self.arena = ""
        self.events = []           # recent statfeed events
        self.goal = None           # the last goal, for a few seconds
        self.ball_speed = 0
        self.updated = 0.0
        self.match_guid = ""
        self._event_id = 0

    def apply(self, event, data, now=None):
        """One Stats API message."""
        now = time.time() if now is None else now
        with self._lock:
            if event == "UpdateState":
                self._update(data)
            elif event == "MatchCreated" or event == "MatchInitialized":
                if event == "MatchCreated":
                    self.reset()
                self.phase = "countdown"
            elif event == "CountdownBegin":
                self.phase, self.replay = "countdown", False
            elif event == "RoundStarted":
                self.phase, self.replay = "live", False
            elif event == "ClockUpdatedSeconds":
                self.clock, self.overtime = int(data.get("TimeSeconds", self.clock)), bool(data.get("bOvertime"))
            elif event == "GoalScored":
                scorer = _player_ref(data.get("Scorer"))
                self.goal = {"scorer": scorer, "assister": _player_ref(data.get("Assister")) if data.get("Assister") else None,
                             "speed": round(float(data.get("GoalSpeed") or 0) * 0.036, 1),   # uu/s -> km/h
                             "team": scorer["team"], "at": now}
                self.phase = "goal"
            elif event == "GoalReplayStart":
                self.phase, self.replay = "replay", True
            elif event in ("GoalReplayEnd", "GoalReplayWillEnd"):
                self.replay = event == "GoalReplayWillEnd"
                if event == "GoalReplayEnd":
                    self.phase = "countdown"
            elif event == "MatchPaused":
                self.phase = "paused"
            elif event == "MatchUnpaused":
                self.phase = "live"
            elif event == "MatchEnded":
                self.phase, self.winner = "ended", data.get("WinnerTeamNum")
            elif event == "PodiumStart":
                self.phase = "podium"
            elif event == "MatchDestroyed":
                self.reset()
            elif event == "StatfeedEvent":
                icon = EVENT_ICONS.get(data.get("EventName") or "")
                if icon:
                    self._event_id += 1
                    self.events.append({"id": self._event_id, "icon": icon, "name": data.get("EventName"),
                                        "label": data.get("Type") or data.get("EventName"),
                                        "player": _player_ref(data.get("MainTarget")),
                                        "other": _player_ref(data.get("SecondaryTarget")) if data.get("SecondaryTarget") else None,
                                        "at": now})
            elif event == "BallHit":
                ball = data.get("Ball") or {}
                self.ball_speed = round(float(ball.get("PostHitSpeed") or 0) * 0.036)
            else:
                return
            self.updated = now
            self.version += 1

    def _update(self, data):
        self.match_guid = data.get("MatchGuid") or self.match_guid
        players = []
        for p in data.get("Players") or []:
            players.append({
                "name": p.get("Name") or "", "id": p.get("PrimaryId") or "", "shortcut": p.get("Shortcut"),
                "team": p.get("TeamNum", 0), "score": p.get("Score", 0), "goals": p.get("Goals", 0),
                "shots": p.get("Shots", 0), "assists": p.get("Assists", 0), "saves": p.get("Saves", 0),
                "touches": p.get("Touches", 0), "demos": p.get("Demos", 0),
                "boost": p.get("Boost"), "speed": round(float(p.get("Speed") or 0) * 0.036),
                "supersonic": bool(p.get("bSupersonic")), "demolished": bool(p.get("bDemolished")),
                "has_car": p.get("bHasCar", True)})
        players.sort(key=lambda p: (p["team"], p["shortcut"] if p["shortcut"] is not None else 99, p["name"].lower()))
        self.players = players
        g = data.get("Game") or {}
        for t in g.get("Teams") or []:
            i = t.get("TeamNum")
            if i in (0, 1):
                self.teams[i] = {"name": t.get("Name") or "", "score": t.get("Score", 0),
                                 "color": t.get("ColorPrimary") or ""}
        if "TimeSeconds" in g:
            self.clock = int(g.get("TimeSeconds") or 0)
        self.overtime = bool(g.get("bOvertime", self.overtime))
        replay = bool(g.get("bReplay"))
        if replay and not self.replay and self.phase not in ("replay",):
            self.phase = "replay"
        elif not replay and self.phase == "replay":
            self.phase = "countdown"
        self.replay = replay
        if g.get("bHasWinner"):
            self.phase = self.phase if self.phase in ("podium",) else "ended"
        self.arena = g.get("Arena") or self.arena
        tgt = g.get("Target") or {}
        self.target = _player_ref(tgt) if g.get("bHasTarget") and tgt.get("Name") else None
        ball = g.get("Ball") or {}
        if ball.get("Speed") is not None:
            self.ball_speed = round(float(ball["Speed"]) * 0.036)
        if self.phase == "none" and players:
            self.phase = "live"

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self.events = [e for e in self.events if now - e["at"] < EVENT_SECONDS]
            goal = self.goal if self.goal and now - self.goal["at"] < GOAL_SECONDS else None
            return {"phase": self.phase, "clock": self.clock, "overtime": self.overtime, "replay": self.replay,
                    "teams": [dict(t) for t in self.teams], "players": [dict(p) for p in self.players],
                    "target": dict(self.target) if self.target else None,
                    "events": [dict(e, age=round(now - e["at"], 2)) for e in self.events],
                    "goal": dict(goal) if goal else None, "winner": self.winner, "arena": self.arena,
                    "ball_speed": self.ball_speed, "age": round(now - self.updated, 1) if self.updated else None,
                    "version": self.version}
