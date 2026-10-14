from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .hooks import evaluate_current
from .runway import estimate_runway
from .state import StateStore


def status_payload(store: StateStore) -> dict[str, Any]:
    current = evaluate_current(store)
    if not current:
        return {"active": False, "message": "No active task with a usable quota snapshot."}
    session, decision = current
    runway = estimate_runway(session, decision, store.load_history())
    spent = max(0.0, session.started_remaining_percent - decision.remaining_percent)
    return {
        "active": True,
        "task": session.task,
        "provider": session.provider,
        "window": session.window,
        "state": decision.state.value,
        "starting_remaining_percent": round(session.started_remaining_percent, 2),
        "remaining_percent": round(decision.remaining_percent, 2),
        "spent_percent": round(spent, 2),
        "floor_percent": round(decision.floor_percent, 2),
        "landing_reserve_percent": round(decision.reserve_percent, 2),
        "development_floor_percent": round(decision.development_floor_percent, 2),
        "safe_development_headroom_percent": round(
            max(0.0, decision.remaining_percent - decision.development_floor_percent), 2
        ),
        "runway": runway.to_dict(),
        "invariant": "The quota floor is a limit, never a spending target.",
    }


DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quota Floor</title>
<style>
:root { font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
body { margin: 0; background: #0b0d10; color: #f2f4f7; }
main { max-width: 980px; margin: 0 auto; padding: 32px 20px 64px; }
header { display:flex; justify-content:space-between; align-items:flex-start; gap:24px; margin-bottom:24px; }
h1 { margin:0; font-size:28px; letter-spacing:-.03em; }
.sub { color:#aab2bf; margin-top:6px; }
.badge { border:1px solid #343a46; padding:7px 11px; border-radius:999px; font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
.grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }
.card { background:#14171c; border:1px solid #262b34; border-radius:14px; padding:16px; }
.label { color:#8f98a8; font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
.value { font-size:28px; margin-top:8px; font-variant-numeric:tabular-nums; }
.wide { grid-column:1/-1; }
.bar { height:14px; background:#242a33; border-radius:999px; overflow:hidden; margin-top:14px; position:relative; }
.fill { height:100%; background:#e9edf3; width:0%; transition:width .25s ease; }
.floor { position:absolute; top:-4px; bottom:-4px; width:2px; background:#ffb454; }
.meta { display:flex; flex-wrap:wrap; gap:18px; margin-top:12px; color:#aab2bf; font-size:13px; }
.runway { font-size:22px; margin-top:8px; }
.note { color:#9da7b6; line-height:1.5; }
.task { font-size:16px; line-height:1.5; white-space:pre-wrap; }
@media(max-width:720px){.grid{grid-template-columns:1fr 1fr}.card.wide{grid-column:1/-1}}
</style>
</head>
<body><main>
<header><div><h1>Quota Floor</h1><div class="sub">Per-task quota budget + graceful handoff</div></div><div id="state" class="badge">loading</div></header>
<div class="grid">
  <section class="card"><div class="label">Current</div><div id="current" class="value">—</div></section>
  <section class="card"><div class="label">Floor</div><div id="floorValue" class="value">—</div></section>
  <section class="card"><div class="label">Landing reserve</div><div id="reserve" class="value">—</div></section>
  <section class="card"><div class="label">Safe headroom</div><div id="headroom" class="value">—</div></section>
  <section class="card wide">
    <div class="label">Quota budget</div>
    <div class="bar"><div id="fill" class="fill"></div><div id="floorMark" class="floor"></div></div>
    <div class="meta"><span id="provider">—</span><span id="window">—</span><span id="spent">—</span><span id="devfloor">—</span></div>
  </section>
  <section class="card wide">
    <div class="label">Observed burn-rate runway</div>
    <div id="runway" class="runway">Learning…</div>
    <p id="burn" class="note">Runway is calculated from real quota movement; it never controls the hard floor.</p>
  </section>
  <section class="card wide"><div class="label">Task</div><div id="task" class="task">—</div></section>
  <section class="card wide"><div class="label">Invariant</div><p class="note">A quota floor is a limit, never a spending target. If the task finishes early, work stops immediately.</p></section>
</div>
</main>
<script>
const pct = x => (x == null ? '—' : Number(x).toFixed(1) + '%');
const hrs = x => {
  if(x == null) return '—';
  const n=Number(x); if(n < 1) return Math.round(n*60)+'m';
  return n.toFixed(n < 10 ? 1 : 0)+'h';
};
async function refresh(){
  const r = await fetch('/api/status', {cache:'no-store'}); const d = await r.json();
  if(!d.active){ document.getElementById('state').textContent='inactive'; return; }
  state.textContent=d.state; current.textContent=pct(d.remaining_percent); floorValue.textContent=pct(d.floor_percent);
  reserve.textContent=pct(d.landing_reserve_percent); headroom.textContent=pct(d.safe_development_headroom_percent);
  fill.style.width=Math.max(0,Math.min(100,d.remaining_percent))+'%'; floorMark.style.left=Math.max(0,Math.min(100,d.floor_percent))+'%';
  provider.textContent=d.provider; window.textContent=d.window; spent.textContent='Spent '+pct(d.spent_percent); devfloor.textContent='Development stops near '+pct(d.development_floor_percent);
  task.textContent=d.task;
  const f=d.runway;
  if(f && f.available && f.recent_runway_hours != null){
    runway.textContent='Recent '+hrs(f.recent_runway_hours)+' · typical '+hrs(f.typical_runway_hours)+' · heavy '+hrs(f.heavy_runway_hours);
    burn.textContent='Burn: recent '+pct(f.recent_burn_percent_per_hour)+'/h · typical '+pct(f.typical_burn_percent_per_hour)+'/h · heavy '+pct(f.heavy_burn_percent_per_hour)+'/h · '+f.confidence+' confidence · trend '+f.trend;
  } else {
    runway.textContent='Learning — waiting for real quota movement';
    burn.textContent='No task-cost guess. Runway appears only after observed quota burn samples.';
  }
}
refresh(); setInterval(refresh, 2000);
</script></body></html>'''


def serve_dashboard(store: StateStore, host: str = "127.0.0.1", port: int = 8765) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/status":
                raw = json.dumps(status_payload(store)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers(); self.wfile.write(raw); return
            if self.path in ("/", "/index.html"):
                raw = DASHBOARD_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers(); self.wfile.write(raw); return
            self.send_error(404)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Quota Floor dashboard: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
