// The GFX Engine overlay's live data (see rlhud/look.py): this HUD's match,
// its teams and its series, many times a second, into the graphic as the
// tables it reads - "Rocket League HUD" (what the engine gets from this HUD)
// and "Global" (the teams, logos, colours, series score and the series boxes,
// as this HUD has them: it follows the engine's Global).
(function () {
  "use strict";
  const RL = "Rocket League HUD";
  const pic = p => (!p ? "" : /^(https?:|data:|\/)/.test(p) ? p : "/" + p);
  const won = (score, n) => (+score || 0) >= n ? "1" : "";

  function tables(s, base) {
    const f = Object.assign({}, s.fields || {});
    const cfg = s.config || {};
    const rl = [["Field", "Value"]].concat(Object.keys(f).map(k => [k, f[k] == null ? "" : String(f[k])]));
    // Global: the exported one, with the match's teams and series on top.
    const g = new Map((base.Global || []).slice(1).map(r => [String(r[0]), r[1]]));
    for (const side of ["left", "right"]) {
      const t = cfg[side] || {}, S = side === "left" ? "Left" : "Right";
      if (t.name) g.set(`${S} name`, t.name);
      if (t.tricode) g.set(`${S} short`, t.tricode);
      if (t.logo) g.set(`${S} logo`, pic(t.logo));
      const gc = ((s.global || {})[side] || {}).color;
      if (gc || t.color) g.set(`${S} color`, gc || t.color);
      g.set(`${S} score`, String(+t.wins || 0));
      for (let n = 1; n <= 4; n++) g.set(`${S} won ${n}`, won(t.wins, n));
    }
    const bo = +cfg.best_of || +g.get("Best of") || 7;
    g.set("Best of", String(bo));
    for (let n = 1; n <= 4; n++) g.set(`Game box ${n}`, n <= (bo + 1) / 2 ? "1" : "");
    g.set("Game", String((+(cfg.left || {}).wins || 0) + (+(cfg.right || {}).wins || 0) + 1));
    const sg = s.global || {};
    if (sg.event != null) g.set("Event", sg.event); else if (cfg.event && !g.get("Event")) g.set("Event", cfg.event);
    if (sg.stage != null) g.set("Stage", sg.stage); else if (cfg.stage && !g.get("Stage")) g.set("Stage", cfg.stage);
    // The HUD's sponsors (the engine's, when it follows Global), in turn.
    const sp = (cfg.sponsors || []).filter(Boolean);
    if (sp.length) g.set("Sponsor logo", pic(sp[0]));
    const out = { [RL]: rl, Global: [["Field", "Value"]].concat([...g].map(([k, v]) => [k, v])) };
    if (sp.length) out.Sponsors = [["Logo", "Seconds", "Epoch", "White"]].concat(sp.map(x => [pic(x), String(+cfg.sponsor_seconds || 8), "0", "1"]));
    return out;
  }

  window.GFX_FEED = function (setData) {
    const base = (window.GFX_EXPORT && window.GFX_EXPORT.tables) || {};
    let last = "";
    function apply(s) {
      const t = tables(s, base);
      const key = JSON.stringify(t);
      if (key === last) return;
      last = key;
      setData(t, s.now);
    }
    function connect() {
      const es = new EventSource("/api/stream");
      es.onmessage = e => { try { apply(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    }
    connect();
  };
})();
