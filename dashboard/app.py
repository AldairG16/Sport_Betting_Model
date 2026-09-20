"""Dashboard Web Local — 100% server-side, cero JS obligatorio."""
import json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
from flask import Flask, redirect
from sqlalchemy import text

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from config.database import engine
from dashboard.display import league_name, market_name, match_name, result_label

app = Flask(__name__)
RESOLVED = ("win","loss","push","half_win","half_loss")
WIN_LIKE = ("win","half_win")
PF = {"win":1.0,"half_win":0.5,"loss":-1.0,"half_loss":-0.5,"push":0.0}
GH_REPO = "AldairG16/Sport_Betting_Model"
MX_TZ = "America/Mexico_City"

def _q(sql):
    try:
        return pd.read_sql(text(sql), engine)
    except Exception as e:
        app.logger.warning(str(e)[:100])
        return pd.DataFrame()

def _pf(df):
    if df.empty: return pd.Series(dtype=float)
    f = df["result"].map(PF).fillna(0.0)
    return (df["odds"]-1) * df["stake"] * f

def _av(s):
    return "—" if pd.isna(s) else str(s)

def _fmt_h(dt):
    try: return pd.Timestamp(dt).tz_convert(MX_TZ).strftime("%d %b, %H:%M")
    except Exception: return str(dt)[:16]

def _ver():
    cands = [Path(__file__).parent.parent / "VERSION"]
    if getattr(sys,"frozen",False): cands.insert(0, Path(sys.executable).parent / "VERSION")
    for c in cands:
        try:
            if c.exists(): return c.read_bytes().decode("utf-8","ignore").replace(chr(0),"").strip() or "dev"
        except OSError: pass
    return "dev"

def _pill(res):
    r = str(res).strip().lower()
    m = {"win":"Ganada","loss":"Perdida","push":"Nula","half_win":"Media G",
         "half_loss":"Media P","pending":"Pendiente","stale":"Sin fuente","unresolved":"Sin datos"}
    cls = {"win":"win","loss":"loss","pending":"pending","push":"push","stale":"stale",
           "unresolved":"stale","half_win":"win","half_loss":"loss"}.get(r,"push")
    lbl = m.get(r, r)
    return f'<span class="pill {cls}">{lbl}</span>'

def _bar(roi):
    p = max(0, min(int(abs(roi)), 60)); n = max(0, min(-int(roi), 60))
    return '<span style="color:var(--blue);font-family:monospace">' + "\u2593"*int(p/5) + "\u2591"*int(n/5) + "</span>"

def _esc(s):
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

# ── Queries ──
def _kpis():
    df = _q("SELECT result, stake, odds, probability, clv FROM bets_history WHERE match_date >= NOW() - INTERVAL '90 days'")
    window = "90d"
    if df.empty or not df["result"].isin(RESOLVED).any():
        df = _q("SELECT result, stake, odds, probability, clv FROM bets_history"); window = "hist\u00f3rico"
    o = {"window":window,"bets":len(df),"pending":0,"resolved":0,"wr":None,"profit":None,"roi":None,"brier":None,"clv":None,"clv_n":0,"bank":None}
    bank = _q("SELECT bankroll FROM bankroll ORDER BY updated_at DESC LIMIT 1")
    o["bank"] = float(bank.iloc[0]["bankroll"]) if not bank.empty else None
    if df.empty: return o
    o["pending"] = int((df["result"]=="pending").sum())
    res = df[df["result"].isin(RESOLVED)]
    o["resolved"] = len(res)
    if res.empty: return o
    o["wr"] = round(float(res["result"].isin(["win","half_win"]).mean()), 3)
    pf = float(_profit(res).sum()); sk = float(res["stake"].sum())
    o["profit"] = round(pf, 2); o["roi"] = round(pf/sk, 3) if sk > 0 else None
    bi = res[res["result"] != "push"]
    if len(bi):
        real = bi["result"].isin(["win","half_win"]).astype(float)
        o["brier"] = round(float(((bi["probability"] - real)**2).mean()), 4)
    clv = df["clv"].dropna()
    o["clv"] = round(float(clv.mean()), 4) if len(clv) else None
    o["clv_n"] = len(clv)
    return o

def _by(dim):
    df = _q(f"SELECT {dim}, result, stake, odds FROM bets_history WHERE result IN ('win','loss','push','half_win','half_loss') AND match_date >= NOW() - INTERVAL '180 days'")
    if df.empty: return []
    df["profit"] = _profit(df)
    g = df.groupby(dim).agg(n=("profit","size"), profit=("profit","sum"), staked=("stake","sum"))
    g["roi"] = (g["profit"]/g["staked"]*100).round(1)
    g = g[g["n"] >= 3].sort_values("roi", ascending=False)
    return [(str(i), int(r["n"]), float(r["roi"]), float(r["profit"])) for i, r in g.iterrows()]

def _bets(status="all"):
    w = {"all":"result IS DISTINCT FROM 'stale'","pending":"result='pending'",
         "resolved":"result IN ('win','loss','push','half_win','half_loss')","stale":"result='stale'"}.get(status, "result IS DISTINCT FROM 'stale'")
    return _q(f"SELECT match_date, match, league, market, probability, odds, stake, result, profit, closing_odds, clv FROM bets_history WHERE {w} ORDER BY match_date DESC LIMIT 300")

def _scorers():
    return _q("SELECT match_date, match, player, team, probability, fair_odds, result FROM goalscorer_picks WHERE match_date >= NOW() - INTERVAL '7 days' ORDER BY match_date DESC LIMIT 30")

def _narr():
    return _q("SELECT narrative, engine, created_at FROM weekly_narrative ORDER BY created_at DESC LIMIT 1")

def _gh_runs():
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GH_REPO}/actions/runs?per_page=8",
            headers={"Authorization": f"Bearer {os.environ.get('GH_TOKEN','')}",
                     "Accept": "application/vnd.github+json", "User-Agent": "bd"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        return [{"name":r["name"],"status":r["status"],"conclusion":r["conclusion"],
                 "created":r["created_at"][:16].replace("T"," "),"url":r["html_url"]}
                for r in data.get("workflow_runs",[])]
    except Exception:
        return []

def _gh_dispatch(wf_file):
    if not os.environ.get("GH_TOKEN"): return False, "Falta GH_TOKEN"
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{wf_file}/dispatches",
            headers={"Authorization":f"Bearer {os.environ.get('GH_TOKEN','')}",
                     "Accept":"application/vnd.github+json","User-Agent":"bd"},
            method="POST", data=b'{"ref":"master"}')
        with urllib.request.urlopen(req, timeout=10) as r:
            return True, "OK"
    except Exception as e:
        return False, str(e)[:120]


# ── RENDER SERVER-SIDE ──
def _kpi_cards(k):
    bank = "—" if k["bank"] is None else f"{k['bank']:.1f}u"
    profit = "—" if k["profit"] is None else f"{k['profit']:+.2f}u"
    roi = "—" if k["roi"] is None else f"{k['roi']:+.1%}"
    wr = "—" if k["wr"] is None else f"{k['wr']:.0%}"
    brier = "—" if k["brier"] is None else f"{k['brier']:.3f}"
    clv = "—" if k["clv"] is None else f"{k['clv']:+.2%}"
    items = [("Bankroll",bank,""),("Profit",profit,"neg" if (k["profit"] or 0)<0 else "pos"),
             ("ROI",roi,"neg" if (k["roi"] or 0)<0 else "pos"),("Win rate",wr,""),
             ("Resueltas",k["resolved"],""),("Pendientes",k["pending"],""),
             ("Brier",brier,""),("CLV medio",clv,"pos" if (k["clv"] or 0)>=0 else "neg")]
    h = '<div class="kpis">'
    for l,v,c in items:
        h += f'<div class="card"><div class="lbl">{l}</div><div class="val {c}">{v}</div></div>'
    return h + "</div>"


def _bets_table(bets):
    h = '<div class="tablewrap"><table><thead><tr><th>Fecha (MX)</th><th>Partido</th><th>Liga</th><th>Mercado</th><th>Prob</th><th>Odd</th><th>Stake</th><th>Estado</th><th>Profit</th><th>CLV</th></tr></thead><tbody>'
    for _, b in bets.iterrows():
        cls = get_pill_class(str(b["result"]))
        prof = "" if str(b["result"])=="pending" else f"{float(b['profit'] or 0):+.2f}u"
        clv = "—" if pd.isna(b["clv"]) else f"{float(b['clv'])*100:.1f}%"
        h += (f"<tr><td>{_mx(b['match_date'])}</td><td>{match_name(str(b['match']))}</td>"
              f"<td>{league_name(str(b['league'] or ''))}</td><td>{market_name(str(b['market']))}</td>"
              f"<td>{float(b['probability'])*100:.0f}%</td><td>{float(b['odds']):.2f}</td>"
              f"<td>{float(b['stake']):.2f}u</td><td>{_pill(str(b['result']))}</td><td>{prof}</td><td>{clv}</td></tr>")
    return h + "</tbody></table></div>"


def _scorers_table(scorers):
    if scorers.empty:
        return '<div class="section"><h2>⚽ Goleadores (papel)</h2><div class="muted">Sin goleadores aún</div></div>'
    h = '<div class="section"><h2>⚽ Goleadores (papel)</h2><div class="tablewrap"><table><thead><tr><th>Fecha (MX)</th><th>Partido</th><th>Jugador</th><th>Equipo</th><th>P</th><th>Fair</th><th>Estado</th></tr></thead><tbody>'
    for _, s in scorers.iterrows():
        h += (f"<tr><td>{_mx(s['match_date'])}</td><td>{match_name(str(s['match']))}</td>"
              f"<td>{s['player']}</td><td>{s['team']}</td><td>{float(s['probability'])*100:.0f}%</td>"
              f"<td>@{float(s['fair_odds']):.2f}</td><td>{_pill(str(s['result']))}</td></tr>")
    return h + "</tbody></table></div></div>"


def _by_table(rows, dim_lbl):
    if not rows: return ""
    h = f'<div class="section"><h2>📊 {dim_lbl} (180d, n≥3)</h2><div class="tablewrap"><table><thead><tr><th></th><th>Bets</th><th>Stake</th><th>Profit</th><th>ROI</th></tr></thead><tbody>'
    for name, n, st, pf, roi in rows:
        cls = "pos" if roi >= 0 else "neg"
        h += f"<tr><td>{name}</td><td>{n}</td><td>{st:.1f}u</td><td>{pf:+.2f}u</td><td class='{cls}'>{roi:+.1f}%</td></tr>"
    return h + "</tbody></table></div></div>"


def _gh_runs_table(runs):
    if not runs: return ""
    h = '<div class="tablewrap"><table><thead><tr><th>Corrida</th><th>Estado</th><th>Cuándo (UTC)</th><th>Link</th></tr></thead><tbody>'
    for r in runs:
        icon = "🔄" if r["status"] != "completed" else ("✅" if r["conclusion"] == "success" else "❌")
        h += (f"<tr><td>{r['name']}</td><td>{icon} {r['status']}</td><td>{r['conclusion'] or '—'}</td>"
              f"<td>{r['created']}</td><td><a href='{r['url']}' target='_blank' style='color:var(--blue)'>ver</a></td></tr>")
    return h + "</tbody></table></div>"


def render_page():
    """Renderiza la página completa 100% server-side."""
    from dashboard.display import league_name as _lg, market_name as _mk

    kpi = _kpis()
    by_mkt = _by("market")
    by_lg = _by("league")
    bets = _bets("all")
    scorers = get_scorers()
    narr = get_narrative()
    gh_runs = _gh_runs()
    failures_today = [r["name"] for r in gh_runs if r["status"]=="completed" and r["conclusion"]=="failure" and r["created"].startswith(datetime.now(timezone.utc).strftime("%d/%m"))]

    bank = "—" if kpi["bank"] is None else f"{kpi['bank']:.1f}u"
    profit = "—" if kpi["profit"] is None else f"{kpi['profit']:+.2f}u"
    roi = "—" if kpi["roi"] is None else f"{kpi['roi']:+.1%}"
    wr = "—" if kpi["wr"] is None else f"{kpi['wr']:.0%}"
    brier = "—" if kpi["brier"] is None else f"{kpi['brier']:.3f}"
    clv = "—" if kpi["clv"] is None else f"{kpi['clv']:+.2%}"
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    version = _app_version()

    # KPI cards
    kpi_html = '<div class="kpis">'
    for l, v, c in [("Bankroll",bank,""),("Profit",profit,"neg" if (kpi["profit"] or 0)<0 else "pos"),
                     ("ROI",roi,"neg" if (kpi["roi"] or 0)<0 else "pos"),("Win rate",wr,""),
                     ("Resueltas",kpi["resolved"],""),("Pendientes",kpi["pending"],""),
                     ("Brier",brier,""),("CLV medio",clv,"pos" if (kpi["clv"] or 0)>=0 else "neg")]:
        kpi_html += f'<div class="card"><div class="lbl">{l}</div><div class="val {c}">{v}</div></div>'
    kpi_html += "</div>"

    # Bets table
    bets_tbl = "<table><thead><tr><th>Fecha (MX)</th><th>Partido</th><th>Liga</th><th>Mercado</th><th>Prob</th><th>Odd</th><th>Stake</th><th>Estado</th><th>Profit</th><th>CLV</th></tr></thead><tbody>"
    for _, b in bets.iterrows():
        r = str(b["result"]); cls = {"win":"win","loss":"loss","pending":"pending"}.get(r,"push")
        prof = "" if r=="pending" else f"{float(b['profit'] or 0):+.2f}u"
        clv_s = "—" if pd.isna(b["clv"]) else f"{float(b['clv'])*100:.1f}%"
        bets_tbl += (f"<tr><td>{_mx(b['match_date'])}</td><td>{match_name(str(b['match']))}</td>"
                     f"<td>{league_name(str(b['league'] or ''))}</td><td>{market_name(str(b['market']))}</td>"
                     f"<td>{float(b['probability'])*100:.0f}%</td><td>{float(b['odds']):.2f}</td><td>{float(b['stake']):.2f}u</td>"
                     f"<td>{_pill(str(b['result']))}</td><td>{prof}</td><td>{clv}</td></tr>")
    bets_tbl += "</tbody></table></div>"

    # By market bars
    mkt_html = ""
    for name, n, st, pf, roi in by_mkt:
        cls = "pos" if roi >= 0 else "neg"
        mkt_html += f"<tr><td>{name}</td><td>{n}</td><td>{st:.2f}u</td><td>{pf:+.2f}u</td><td class='{cls}'>{roi:+.1f}% {bar}</td></tr>"
    mkt_html = f'<table><thead><tr><th>Mercado</th><th>Bets</th><th>Stake</th><th>Profit</th><th>ROI</th></tr></thead><tbody>{mkt_html}</tbody></table>'

    # By league bars
    lg_html = ""
    for name, n, st, pf, roi in by_lg:
        cls = "pos" if roi >= 0 else "neg"
        lg_html += f"<tr><td>{name}</td><td>{n}</td><td>{st:.2f}u</td><td>{pf:+.2f}u</td><td class='{cls}'>{roi:+.1f}% {bar}</td></tr>"
    lg_html = f'<table><thead><tr><th>Liga</th><th>Bets</th><th>Stake</th><th>Profit</th><th>ROI</th></tr></thead><tbody>{lg_html}</tbody></table>'

    html = f"""{PAGE_HEAD}
{banner}{alerts}
{kpis_html}
{mkt_html}
{lg_html}
{bets_tbl}
{sc_html}
{gh_html}
{PAGE_TAIL}"""
    return html


PAGE_HEAD = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<title>Betting Dashboard v%(version)s</title>
<meta http-equiv="refresh" content="120">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#0f1420; --card:#1a2233; --text:#e2e8f0; --muted:#8b98ad; --green:#22c55e; --red:#ef4444; --blue:#3b82f6; }
  * { box-sizing:border-box; margin:0; padding:0; font-family:'Segoe UI',system-ui,sans-serif; }
  body { background:var(--bg); color:var(--text); padding:20px; }
  h1 { font-size:1.3rem; margin-bottom:4px; }
  .sub { color:var(--muted); font-size:.85rem; margin-bottom:18px; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:20px; }
  .card { background:var(--card); border-radius:10px; padding:14px; }
  .card .lbl { color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.5px; }
  .card .val { font-size:1.5rem; font-weight:700; margin-top:4px; }
  .pos { color:var(--green); } .neg { color:var(--red); }
  table { width:100%; border-collapse:collapse; font-size:.82rem; background:var(--card); border-radius:10px; }
  .tablewrap { max-height:480px; overflow-y:auto; border-radius:10px; }
  th { text-align:left; padding:8px 10px; color:var(--muted); font-weight:600; border-bottom:1px solid #2a3550; position:sticky; top:0; background:#1a2233; z-index:1; }
  td { padding:7px 10px; border-bottom:1px solid #232d45; }
  tr:hover td { background:#202a40; }
  .pill { padding:2px 8px; border-radius:99px; font-size:.72rem; font-weight:600; }
  .pill.win{background:#14351f;color:var(--green)} .pill.loss{background:#3a1a1a;color:var(--red)}
  .pill.pending{background:#2a2a14;color:#eab308} .pill.push,.pill.stale{background:#1e2a3a;color:#93c5fd}
  .controls { display:flex; gap:10px; margin:14px 0; flex-wrap:wrap; align-items:center; }
  select, input[type=submit] { background:var(--card); color:var(--text); border:1px solid #2a3550; border-radius:8px; padding:6px 10px; font-size:.85rem; }
  input[type=submit] { cursor:pointer; }
  .section { margin-top:26px; } .section h2 { font-size:1.05rem; margin-bottom:10px; }
  .bar { color:var(--blue); font-family:monospace; }
  .muted { color:var(--muted); }
  a { color:var(--blue); }
</style></head><body>
<h1>⚽ Sport Betting Model — Dashboard <span style="font-size:.7rem;color:var(--muted);font-weight:400">v%(version)s</span></h1>
<div class="sub" id="sub">Conectando a la base de datos…</div>
%(banner)s
%(alerts)s
%(kpis_html)s
<div class="section"><h2>📋 Apuestas</h2>
%(bets_table)s
<div class="section"><h2>📊 Por mercado</h2>%(mkt_html)s</div>
<div class="section"><h2>🌍 Por liga</h2>%(lg_html)s</div>
%(sc_section)s
%(gh_html)s
</body></html>"""


def render_page():
    now = datetime.now(timezone.utc)
    version = _app_version()
    newer = _newer_release()
    kpi = compute_kpis()
    bets = _bets("all")
    by_mkt = _by("market")
    by_lg = _by("league")
    scorers = get_scorers()
    gh_runs = _gh_runs()
    failures_today = [r["name"] for r in gh_runs if r["status"]=="completed" and r["conclusion"]=="failure" and r["created"].startswith(datetime.now(timezone.utc).strftime("%d/%m"))]

    banner = ""
    if newer and newer != version:
        banner = f'<div class="upd">🔄 Nueva versión: <b>v{newer}</b> — <a href="https://github.com/{GH_REPO}/releases/latest" target="_blank" style="color:var(--green)">descargar</a></div>'
    if failures_today:
        banner += '<div class="alert">🚨 Fallaron hoy: ' + ", ".join(failures_today) + '</div>'

    bets_table = _bets_table_html(bets)
    mkt_table = _by_table_html(by_mkt, "Mercado")
    lg_table = _by_table_html(by_lg, "Liga")
    sc_section = _scorers_section_html(scorers)
    gh_html = _gh_panel_html(gh_runs)

    html = PAGE_HEAD % {"version": version, "hora": hora_now()} + banner + alerts
    html += kpi_cards_html(kpi)
    html += f'<div class="section"><h2>📋 Apuestas</h2>{bets_table}</div>'
    html += f'<div class="section"><h2>📊 Por mercado</h2>{mkt_table}</div>'
    html += f'<div class="section"><h2>🌍 Por liga</h2>{lg_table}</div>'
    html += sc_section
    html += gh_html
    return html
