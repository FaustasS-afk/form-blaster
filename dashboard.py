"""
dashboard.py — live browser dashboard for the probe and the blast.

  python dashboard.py            opens http://localhost:8765

Read-only: reads data/formblaster.db (read-only mode) and the newest
*_probe.csv, so it is safe to leave open while anything runs.
"""

import csv
import json
import sqlite3
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "formblaster.db"
PORT = 8765

_hist = {"probe": [], "blast": []}   # (time, done) samples for rate


def _rate(key, done):
    """Leads per minute over the last ~2 minutes of samples."""
    now = time.time()
    h = _hist[key]
    if not h or h[-1][1] != done or now - h[-1][0] > 5:
        h.append((now, done))
    while h and now - h[0][0] > 120:
        h.pop(0)
    if len(h) < 2 or h[-1][0] - h[0][0] < 3:
        return 0.0
    return (h[-1][1] - h[0][1]) / (h[-1][0] - h[0][0]) * 60


def probe_state():
    files = sorted(HERE.glob("*_probe.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    f = files[-1]
    src = f.with_name(f.name[:-len("_probe.csv")] + ".txt")
    total = 0
    if src.exists():
        with open(src, encoding="utf-8") as fh:
            total = len({l.strip() for l in fh if l.strip()})
    with open(f, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    done = len(rows)
    age = time.time() - f.stat().st_mtime
    rate = _rate("probe", done)
    return dict(
        file=src.name, total=max(total, done), done=done, counts=counts,
        running=age < 20 and done < total, idle_secs=int(age),
        rate=round(rate, 1),
        eta_min=round((total - done) / rate, 1) if rate and total > done else None,
        recent=[dict(url=r["url"], status=r["status"], page=r["form_page"])
                for r in rows[-25:][::-1]],
    )


def blast_state():
    if not DB.exists():
        return None
    c = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        counts = dict(c.execute("SELECT status, COUNT(*) FROM sites GROUP BY status"))
        recent = c.execute(
            "SELECT url, status, COALESCE(NULLIF(detail,''), error), last_try "
            "FROM sites WHERE last_try IS NOT NULL ORDER BY last_try DESC LIMIT 25"
        ).fetchall()
    finally:
        c.close()
    total = sum(counts.values())
    pending = counts.get("pending", 0)
    done = total - pending
    last = recent[0][3] if recent else None
    age = None
    if last:
        try:
            age = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            pass
    rate = _rate("blast", done)
    return dict(
        total=total, done=done, pending=pending, counts=counts,
        running=age is not None and age < 30, idle_secs=int(age) if age else None,
        rate=round(rate, 1),
        eta_min=round(pending / rate, 1) if rate and pending else None,
        recent=[dict(url=u, status=s, detail=(d or "")[:90], at=(t or "")[11:19])
                for u, s, d, t in recent],
    )


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api"):
            body = json.dumps(dict(probe=probe_state(), blast=blast_state(),
                                   now=datetime.now().strftime("%H:%M:%S"))).encode()
            ctype = "application/json"
        else:
            body = PAGE.encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Form Blaster Live</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--mute:#6b7280;--line:#e5e7eb;
--ok:#16a34a;--maybe:#d97706;--form:#2563eb;--none:#9ca3af;--block:#64748b;--fail:#dc2626;--bar:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b22;--ink:#e6edf3;--mute:#8b949e;
--line:#30363d;--ok:#3fb950;--maybe:#d29922;--form:#58a6ff;--none:#6e7681;--block:#8b949e;--fail:#f85149;--bar:#58a6ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:20px 16px 40px}
header{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:16px}
h1{font-size:20px;margin:0}.clock{color:var(--mute);font-variant-numeric:tabular-nums}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
.ph{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.ph h2{font-size:16px;margin:0}.sub{color:var(--mute)}
.pill{font-size:12px;font-weight:600;padding:2px 9px;border-radius:99px;border:1px solid var(--line);color:var(--mute)}
.pill.on{color:var(--ok);border-color:var(--ok)}
.pill.on::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ok);
margin-right:6px;animation:p 1.2s infinite}@keyframes p{50%{opacity:.25}}
.bar{height:10px;background:var(--line);border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--bar);transition:width .6s}
.nums{display:flex;justify-content:space-between;color:var(--mute);margin:6px 0 14px;font-variant-numeric:tabular-nums;flex-wrap:wrap;gap:8px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:14px}
.tile{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.tile span{color:var(--mute);font-size:12px}
.feed{width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed}
.feed td{padding:5px 6px;border-top:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.feed td.t{width:70px;color:var(--mute);font-variant-numeric:tabular-nums}.feed td.s{width:118px}
.st{font-weight:600}
.s-submitted,.s-form_found{color:var(--ok)}.s-needs_manual{color:var(--maybe)}
.s-no_form{color:var(--none)}.s-blocked{color:var(--block)}.s-failed{color:var(--fail)}
.feed tr.new{animation:f 1.5s}@keyframes f{from{background:color-mix(in srgb,var(--bar) 18%,transparent)}}
.empty{color:var(--mute)}
@media (max-width:600px){.feed td.t{display:none}.feed td.s{width:96px}}
</style></head><body><div class="wrap">
<header><h1>Form Blaster Live</h1><span class="clock" id="clock">connecting…</span></header>
<section class="panel" id="probe"></section>
<section class="panel" id="blast"></section>
</div>
<script>
const LABEL={submitted:"Sent",needs_manual:"Sent, unconfirmed",form_found:"Form found",
no_form:"No form",blocked:"Blocked",failed:"Failed",pending:"Waiting"};
const seen={probe:new Set(),blast:new Set()};
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const short=u=>String(u).replace(/^https?:\/\/(www\.)?/,"").replace(/\/$/,"");
const fmt=n=>Number(n||0).toLocaleString();
function eta(m){if(m==null)return"";return m<60?`~${Math.ceil(m)} min left`:`~${(m/60).toFixed(1)} h left`}
function state(s){return s.running?'<span class="pill on">Running</span>':
 `<span class="pill">Idle${s.idle_secs!=null?` (last activity ${s.idle_secs<120?s.idle_secs+"s":Math.round(s.idle_secs/60)+" min"} ago)`:""}</span>`}
function tiles(order,counts){return '<div class="tiles">'+order.filter(k=>counts[k]!=null)
 .map(k=>`<div class="tile"><b class="s-${k}">${fmt(counts[k])}</b><span>${LABEL[k]||k}</span></div>`).join("")+"</div>"}
function feed(key,rows,cell){
 if(!rows.length)return '<p class="empty">Nothing yet.</p>';
 const out=rows.map(r=>{const id=r.url+r.status;const isNew=seen[key].size&&!seen[key].has(id);
  return `<tr class="${isNew?"new":""}">${cell(r)}</tr>`}).join("");
 seen[key]=new Set(rows.map(r=>r.url+r.status));
 return `<table class="feed">${out}</table>`}
function render(d){
 document.getElementById("clock").textContent="Updated "+d.now;
 const p=d.probe,P=document.getElementById("probe");
 if(!p)P.innerHTML='<div class="ph"><h2>Form check</h2></div><p class="empty">No probe run yet.</p>';
 else{const pct=p.total?p.done/p.total*100:0;
  P.innerHTML=`<div class="ph"><h2>Form check</h2><span class="sub">${esc(p.file)} · sends nothing</span>${state(p)}</div>
  <div class="bar"><i style="width:${pct}%"></i></div>
  <div class="nums"><span>${fmt(p.done)} / ${fmt(p.total)} checked (${pct.toFixed(1)}%)</span>
  <span>${p.rate?p.rate+" / min":""} ${eta(p.eta_min)}</span></div>
  ${tiles(["form_found","no_form","blocked","failed"],p.counts)}
  ${feed("probe",p.recent,r=>`<td class="s st s-${r.status}">${LABEL[r.status]||r.status}</td>
   <td title="${esc(r.url)}">${esc(short(r.url))}</td><td class="sub" title="${esc(r.page)}">${esc(r.page?short(r.page):"")}</td>`)}`}
 const b=d.blast,B=document.getElementById("blast");
 if(!b)B.innerHTML='<div class="ph"><h2>Sending</h2></div><p class="empty">No database yet.</p>';
 else{const pct=b.total?b.done/b.total*100:0;
  B.innerHTML=`<div class="ph"><h2>Sending</h2><span class="sub">data/formblaster.db</span>${state(b)}</div>
  <div class="bar"><i style="width:${pct}%"></i></div>
  <div class="nums"><span>${fmt(b.done)} / ${fmt(b.total)} processed (${pct.toFixed(1)}%)</span>
  <span>${b.rate?b.rate+" / min":""} ${eta(b.eta_min)}</span></div>
  ${tiles(["submitted","needs_manual","no_form","blocked","failed","pending"],b.counts)}
  ${feed("blast",b.recent,r=>`<td class="t">${esc(r.at)}</td><td class="s st s-${r.status}">${LABEL[r.status]||r.status}</td>
   <td title="${esc(r.url)}">${esc(short(r.url))}</td><td class="sub" title="${esc(r.detail)}">${esc(r.detail)}</td>`)}`}}
async function tick(){try{const r=await fetch("/api",{cache:"no-store"});render(await r.json())}
 catch(e){document.getElementById("clock").textContent="Dashboard server stopped"}}
tick();setInterval(tick,1500);
</script></body></html>"""


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://localhost:{PORT}"
    print(f"Dashboard: {url}   (Ctrl+C to stop)")
    webbrowser.open(url)
    srv.serve_forever()
