# SCC Rocket League HUD

A broadcast HUD for Rocket League, after the RLCS 2026 HUD, fed by the game's
own **Stats API** (read-only, from the game itself, so Easy Anti-Cheat
doesn't come into it):

- The **scorebug**: logos, scores, clock and the series. Around goals,
  replays and kickoffs it opens out to the team names and the event.
- Each team's **players and their boost**, the spectated one picked out, and
  what they just did (shot, save, goal, assist, demo) by their name.
- **Sponsors** under the blue team, in turn.
- The **spectated player**: name, goals, shots, assists, saves and demos,
  and the boost dial.

## Install

Install it with the [SCC HUD Manager](https://github.com/owenbparker/SCC-HUD-Manager),
or on its own with **SCC-RL-HUD-Setup.exe** from the
[latest release](https://github.com/owenbparker/RLHUD_SCC/releases/latest)
(installs to `C:\SCC RL HUD`). It updates itself.

## Use

1. **Turn on the Stats API** (once per PC that runs Rocket League): the
   control panel's *Turn on Rocket League's Stats API* button sets
   `PacketSendRate` in the game's `DefaultStatsAPI.ini` / `TAStatsAPI.ini`.
   Restart Rocket League afterwards.
2. Pick the client:
   - **GFX Controller** (the PC with vMix / OBS): add the browser source
     `http://localhost:8781/overlay` (1920×1080). If Rocket League runs on this PC,
     press **Read Rocket League on this PC**.
   - **Observer Client** (a PC spectating in Rocket League): **Connect to HUD**
     finds the GFX PC on the network; **Send Rocket League to the HUD** sends
     the match there. (On the GFX PC, tick *Let other PCs on the network read
     the HUD*.)
3. Set the teams, the series and the scorebug's text in the control panel.
   **Demo match** plays a made-up 3v3 to try it all without the game.

## From source

```
pip install -r requirements-windows.txt
python -m rlhud.gui           # the desktop app
python -m rlhud --demo        # just the server, with a made-up match
python -m unittest discover -s tests
```

Fonts: Saira (SIL Open Font License, `web/fonts/Saira-OFL.txt`).
