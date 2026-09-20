"""
Genera dashboard_estatico.html en el escritorio del usuario.
HTML puro con todos los datos embebidos — funciona en cualquier navegador.
Sin servidor, sin JS, sin dependencias.
"""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

env_path = ROOT / "installer" / "dist" / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.strip().partition("=")
            os.environ[k] = v

import pandas as pd
from sqlalchemy import text
from config.database import engine

RESOLVED = ("win","loss","push","half_win","half_loss")

def _mx(dt):
    try: return pd.Timestamp(dt).tz_convert("America/Mexico_City").strftime("%d %b %H:%M")
    except Exception: return str(dt)[:10]

def _pill(res):
    r = str(res).strip()
    m = {"win":"Ganada","loss":"Perdida","push":"Nula","pending":"Pendiente",
         "stale":"Sin fuente","unresolved":"Sin datos","half_win":"Media G","half_loss":"Media P"}
    cls = {"win":"win","loss":"loss","pending":"pending","push":"push",
           "stale":"stale","unresolved":"stale","half_win":"win","half_loss":"loss"}.get(r,r)
    return f'<span class="pill {cls}">{m.get(r,r)}</span>'

# KPIs
df = pd.read_sql(text("SELECT result, stake, odds, probability, clv, profit FROM bets_history WHERE match_date >= NOW() - INTERVAL '90 days'"), engine)
resolved = df[df["result"].isin(RESOLVED)]
pending_n = int((df["result"]=="pending").sum())
wr = round(resolved["result"].isin(["win","half_win"]).mean()*100, 1) if len(resolved) else None
profit = round(float(resolved["profit"].sum()), 2) if len(resolved) else 0
staked = float(resolved["stake"].sum()) if len(resolved) else 0
roi = round(profit/staked*100, 1) if staked > 0 else None

# By league (180d)
bl = pd.read_sql(text("""
    SELECT league, COUNT(*) n, ROUND(SUM(profit)::numeric,2) profit
    FROM bets_history WHERE result IN ('win','loss','push','half_win','half_loss')
      AND match_date >= NOW() - INTERVAL '180 days'
    GROUP BY league ORDER BY profit DESC LIMIT 15
"""), engine)

# By market
bm = pd.read_sql(text("""
    SELECT market, COUNT(*) n, ROUND(SUM(profit)::numeric,2) profit
    FROM bets_history WHERE result IN ('win','loss','push','half_win','half_loss')
      AND match_date >= NOW() - INTERVAL '180 days'
    GROUP BY market ORDER BY profit DESC LIMIT 10
"""), engine)

# Bets table
bets = pd.read_sql(text("""
    SELECT match_date, match, league, market, probability, odds, stake, result, profit
    FROM bets_history WHERE match_date >= NOW() - INTERVAL '7 days'
    ORDER BY match_date DESC LIMIT 100
"""), engine)

lg_rows = ""
for _, r in bl.iterrows():
    cls = "neg" if float(r["profit"] or 0) < 0 else "pos"
    lg_rows += f"<tr><td>{str(r['league']).replace('soccer_','')}</td><td>{int(r['n'])}</td><td class='{cls}'>{float(r['profit'] or 0):+.2f}u</td></tr>"

mkt_rows = ""
for _, r in bm.iterrows():
    cls = "neg" if float(r["profit"] or 0) < 0 else "pos"
    mkt_rows += f"<tr><td>{r['market']}</td><td>{int(r['n'])}</td><td class='{cls}'>{float(r['profit'] or 0):+.2f}u</td></tr>"

bet_rows = ""
for _, b in bets.iterrows():
    r = str(b["result"]).strip()
    m = {"win":"Ganada","loss":"Perdida","push":"Nula","pending":"Pendiente","stale":"Sin fuente","unresolved":"Sin datos"}
    cls = {"win":"win","loss":"loss","pending":"pending","push":"push","stale":"stale","unresolved":"stale"}.get(r,r)
    bet_rows += (f"<tr><td>{_mx(pd.Timestamp(b['match_date']))}</td><td>{b['match']}</td>"
                 f"<td>{str(b['league']).replace('soccer_','')}</td><td>{b['market']}</td>"
                 f"<td>{float(b['probability'])*100:.0f}%</td><td>{float(b['odds']):.2f}</td>"
                 f"<td>{float(b['stake']):.2f}u</td>"
                 f"<td><span class='pill {cls}'>{m.get(r,r)}</span></td><td>{float(b['profit'] or 0):+.2f}u</td></tr>")

html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<title>Betting Dashboard</title><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{ --bg:#0f1420; --card:#1a2233; --text:#e2e8f0; --muted:#8b98ad; --green:#22c55e; --red:#ef4444; }}
* {{ box-sizing:border-box; margin:0; padding:0; font-family:'Segoe UI',system-ui,sans-serif; }}
body {{ background:var(--bg); color:var(--text); padding:20px; }}
h1 {{ font-size:1.3rem; margin-bottom:14px; }}
h2 {{ font-size:1.05rem; margin:20px 0 10px; }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:20px; }}
.card {{ background:var(--card); border-radius:10px; padding:14px; }}
.lbl {{ color:var(--muted); font-size:.75rem; text-transform:uppercase; }}
.val {{ font-size:1.3rem; font-weight:700; margin-top:4px; }}
.pos {{ color:var(--green); }} .neg {{ color:var(--red); }}
table {{ width:100%; border-collapse:collapse; font-size:.82rem; background:var(--card); border-radius:10px; }}
th {{ text-align:left; padding:8px 10px; color:var(--muted); font-weight:600; border-bottom:2px solid #2a3550; }}
td {{ padding:7px 10px; border-bottom:1px solid #232d45; }}
.pill {{ padding:2px 8px; border-radius:99px; font-size:.72rem; font-weight:600; }}
.pill.win{{background:#14351f;color:var(--green)}} .pill.loss{{background:#3a1a1a;color:var(--red)}}
.pill.pending{{background:#2a2a14;color:#eab308}} .pill.stale{{background:#1e2a3a;color:#93c5fd}}
.pill.push{{background:#1e2a3a;color:#93c5fd}}
</style></head><body>
<h1>⚽ Betting Dashboard</h1>
<div class="kpis">
<div class="card"><div class="lbl">Bets totales</div><div class="val">{len(df)}</div></div>
<div class="card"><div class="lbl">Resueltas 90d</div><div class="val">{len(resolved)}</div></div>
<div class="card"><div class="lbl">Pendientes</div><div class="val">{pending_n}</div></div>
<div class="card"><div class="lbl">Win rate</div><div class="val">{wr or '—'}%</div></div>
<div class="card"><div class="lbl">Profit</div><div class="val {'pos' if profit >= 0 else 'neg'}">{profit:+.2f}u</div></div>
</div>
<h2>📊 Por mercado (180d)</h2>
<table><thead><tr><th>Mercado</th><th>Bets</th><th>Profit</th></tr></thead><tbody>{mkt_rows}</tbody></table>
<h2>🌍 Por liga (180d)</h2>
<table><thead><tr><th>Liga</th><th>Bets</th><th>Profit</th></tr></thead><tbody>{lg_rows}</tbody></table>
<h2>📋 Apuestas recientes (7 días)</h2>
<table><thead><tr><th>Fecha</th><th>Partido</th><th>Liga</th><th>Mercado</th><th>Prob</th><th>Odd</th><th>Stake</th><th>Estado</th><th>Profit</th></tr></thead><tbody>{bet_rows}</tbody></table>
</body></html>"""

out = ROOT / "dashboard_estatico.html"
out.write_text(html, encoding="utf-8")
print(f"OK: {out} ({len(html)} bytes)")
