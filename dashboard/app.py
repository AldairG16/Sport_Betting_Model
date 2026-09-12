"""
Dashboard Web Local — Sport Betting Model
==========================================
App Flask read-only que se conecta a la misma DB (Neon) que el pipeline
y muestra: bankroll, apuestas (con filtros), curva de resultados, ROI por
mercado/liga y CLV.

Uso:
    python scripts/run_dashboard.py        → http://127.0.0.1:5050

 Seguridad:
    - Solo escucha en 127.0.0.1 (nadie fuera de esta PC puede verlo).
    - Solo SELECT: no inserta, no actualiza, no borra nada.
    - DB_URL se lee de .env / entorno — nunca va en el código.
"""

import sys
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, request
from sqlalchemy import text

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.database import engine  # noqa: E402
from dashboard.display import league_name, team_name, market_name, match_name, result_label  # noqa: E402

app = Flask(__name__)


def _app_version() -> str:
    """Versión desde el archivo VERSION (junto al exe o en la raíz del repo)."""
    import sys as _sys
    from pathlib import Path as _P
    candidates = [_P(__file__).parent.parent / "VERSION"]
    if getattr(_sys, "frozen", False):
        candidates.insert(0, _P(_sys.executable).parent / "VERSION")
    for c in candidates:
        try:
            if c.exists():
                raw = c.read_bytes()
                # PowerShell `echo x > file` escribe UTF-16 con BOM —
                # limpiar bytes nulos y BOM (UTF-16 y UTF-8) para no
                # leer una versión corrupta
                v = raw.decode("utf-8", errors="ignore")
                v = v.replace(chr(0), "")
                v = v.lstrip("﻿").strip()
                return v or "dev"
        except OSError:
            pass
    return "dev"


REPO_RELEASES_API = "https://api.github.com/repos/AldairG16/Sport_Betting_Model/releases/latest"


@app.route("/api/version")
def api_version():
    import urllib.request
    latest, url = None, None
    try:
        req = urllib.request.Request(REPO_RELEASES_API, headers={"User-Agent": "betting-dashboard"})
        with urllib.request.urlopen(req, timeout=5) as r:
            tag = __import__("json").loads(r.read()).get("tag_name", "")
            if tag.startswith("v"):
                latest = tag[1:]
                url = f"https://github.com/AldairG16/Sport_Betting_Model/releases/latest"
    except Exception:
        pass
    current = _app_version()
    return jsonify({
        "current": current,
        "latest": latest,
        "update_available": bool(latest and latest != current),
        "release_url": url,
    })

RESOLVED = ("win", "loss", "push", "half_win", "half_loss")
WIN_LIKE = ("win", "half_win")

# Multiplicador de profit por resultado (half_win/half_loss son fraccionales)
_PROFIT_FACTOR = {
    "win": 1.0, "half_win": 0.5,
    "loss": -1.0, "half_loss": -0.5,
    "push": 0.0,
}


def _q(sql: str, params: dict | None = None) -> pd.DataFrame:
    try:
        return pd.read_sql(text(sql), engine, params=params or {})
    except Exception as e:
        app.logger.warning(f"query falló: {e}")
        return pd.DataFrame()


def _profit(df: pd.DataFrame) -> pd.Series:
    """Profit real de cada bet (half_win/half_loss fraccionados)."""
    if df.empty:
        return pd.Series(dtype=float)
    factor = df["result"].map(_PROFIT_FACTOR).fillna(0.0)
    return (df["odds"] - 1.0) * df["stake"] * factor


@app.route("/api/kpis")
def kpis():
    # Ventana preferida: 90 días. Si no hay NADA resuelto en ella (sistema
    # recién reactivado, bets nuevas aún pendientes), caer al histórico
    # completo para no mostrar un dashboard vacío.
    df = _q("""
        SELECT result, stake, odds, probability, clv, match_date
        FROM bets_history
        WHERE match_date >= NOW() - INTERVAL '90 days'
    """)
    window = "90d"
    has_resolved = (not df.empty) and df["result"].isin(RESOLVED).any()
    if not has_resolved:
        df = _q("""
            SELECT result, stake, odds, probability, clv, match_date
            FROM bets_history
        """)
        window = "histórico"
    if df.empty:
        return jsonify({"ok": False, "msg": "Sin datos en bets_history"})

    resolved = df[df["result"].isin(RESOLVED)]
    pending = df[df["result"] == "pending"]
    wins = resolved[resolved["result"].isin(WIN_LIKE)]
    profit = _profit(resolved).sum()
    staked = float(resolved["stake"].sum())
    clv = df["clv"].dropna()

    bank = _q("SELECT bankroll FROM bankroll ORDER BY updated_at DESC LIMIT 1")
    bankroll = float(bank.iloc[0]["bankroll"]) if not bank.empty else None

    # Brier solo con bets resueltas binarias (excluye push)
    binary = resolved[resolved["result"] != "push"]
    brier = float(((binary["probability"] - binary["result"].isin(WIN_LIKE).astype(float)) ** 2).mean()) if len(binary) else None

    return jsonify({
        "ok": True,
        "window": window,
        "bankroll": bankroll,
        "bets_90d": int(len(df)),
        "resolved": int(len(resolved)),
        "pending": int(len(pending)),
        "win_rate": round(len(wins) / len(resolved), 3) if len(resolved) else None,
        "profit": round(float(profit), 2),
        "roi": round(float(profit / staked), 3) if staked > 0 else None,
        "brier": round(brier, 4) if brier else None,
        "clv_avg": round(float(clv.mean()), 4) if len(clv) else None,
        "clv_n": int(len(clv)),
    })


@app.route("/api/equity")
def equity():
    df = _q("""
        SELECT match_date, result, stake, odds, market
        FROM bets_history
        WHERE result IN ('win', 'loss', 'push', 'half_win', 'half_loss')
        ORDER BY match_date
    """)
    if df.empty:
        return jsonify({"ok": False})
    df["profit"] = _profit(df)
    df["cum"] = df["profit"].cumsum()
    return jsonify({
        "ok": True,
        "dates": df["match_date"].astype(str).str[:10].tolist(),
        "cumulative": [round(x, 3) for x in df["cum"]],
        "daily": [round(x, 3) for x in df.groupby(df["match_date"].astype(str).str[:10])["profit"].sum()],
        "daily_dates": df.groupby(df["match_date"].astype(str).str[:10])["profit"].sum().index.tolist(),
    })


@app.route("/api/bets")
def bets():
    status = request.args.get("status", "all")
    market = request.args.get("market", "")
    league = request.args.get("league", "")
    limit = min(int(request.args.get("limit", 100)), 500)

    where, params = ["1=1"], {}
    if status == "pending":
        where.append("result = 'pending'")
    elif status == "resolved":
        where.append("result IN ('win','loss','push','half_win','half_loss')")
    elif status == "stale":
        where.append("result = 'stale'")
    else:
        # Vista default: TODO excepto stale (bets antiguas sin fuente de
        # resultado — historial muerto que no debe estorbar el día a día)
        where.append("result IS DISTINCT FROM 'stale'")
    if market:
        where.append("market = :market"); params["market"] = market
    if league:
        where.append("league = :league"); params["league"] = league

    df = _q(f"""
        SELECT match_date, match, league, market, probability, odds,
               stake, result, profit, closing_odds, clv
        FROM bets_history
        WHERE {' AND '.join(where)}
        ORDER BY match_date DESC
        LIMIT {limit}
    """, params)
    if df.empty:
        return jsonify({"ok": False})

    for col, fn in (("match", match_name), ("league", league_name), ("market", market_name)):
        if col in df.columns:
            df[col] = df[col].apply(lambda v: fn(v) if v else v)
    # result_key crudo para las clases CSS (pill verde/roja); result ya
    # traducido ("Ganada"/"Perdida") para el texto visible
    df["result_key"] = df["result"]
    df["result"] = df["result"].apply(lambda r: result_label(r) if r else r)
    return jsonify({
        "ok": True,
        "bets": df.fillna("").to_dict(orient="records"),
    })


@app.route("/api/by/<dim>")
def by_dim(dim):
    if dim not in ("market", "league"):
        return jsonify({"ok": False}), 404
    df = _q(f"""
        SELECT {dim}, result, stake, odds
        FROM bets_history
        WHERE result IN ('win','loss','push','half_win','half_loss')
          AND match_date >= NOW() - INTERVAL '180 days'
    """)
    if df.empty:
        return jsonify({"ok": False})
    df["profit"] = _profit(df)
    g = df.groupby(dim).agg(
        n=("profit", "size"),
        profit=("profit", "sum"),
        staked=("stake", "sum"),
        wr=("result", lambda r: r.isin(WIN_LIKE).mean()),
    )
    g["roi"] = (g["profit"] / g["staked"] * 100).round(1)
    g = g[g["n"] >= 3].sort_values("roi", ascending=False)
    label_fn = league_name if dim == "league" else market_name
    return jsonify({
        "ok": True,
        "labels": [label_fn(x) for x in g.index],
        "n": g["n"].astype(int).tolist(),
        "roi": g["roi"].tolist(),
        "profit": g["profit"].round(2).tolist(),
        "wr": (g["wr"] * 100).round(1).tolist(),
    })


@app.route("/api/clv")
def clv_scatter():
    df = _q("""
        SELECT clv, result, odds, stake
        FROM bets_history
        WHERE clv IS NOT NULL
          AND result IN ('win','loss','half_win','half_loss')
        ORDER BY match_date
    """)
    if df.empty:
        return jsonify({"ok": False})
    return jsonify({
        "ok": True,
        "x": list(range(1, len(df) + 1)),
        "clv": [round(float(v), 4) for v in df["clv"]],
        "wins": df["result"].isin(WIN_LIKE).tolist(),
    })


@app.route("/api/scorers")
def scorers():
    df = _q("""
        SELECT match_date, match, league, player, team,
               probability, fair_odds, odds_placed, result
        FROM goalscorer_picks
        WHERE match_date >= NOW() - INTERVAL '7 days'
        ORDER BY match_date DESC
        LIMIT 100
    """)
    if df.empty:
        return jsonify({"ok": False})
    if "league" in df.columns:
        df["league"] = df["league"].apply(lambda v: league_name(v) if v else v)
    if "match" in df.columns:
        df["match"] = df["match"].apply(lambda v: match_name(v) if v else v)
    df["result_key"] = df["result"]
    if "result" in df.columns:
        df["result"] = df["result"].apply(lambda r: result_label(r) if r else r)
    return jsonify({"ok": True, "picks": df.fillna("").to_dict(orient="records")})


# ─────────────────────────────────────────────────────────────
# PANEL DE CONTROL (GitHub Actions)
# Requiere GH_TOKEN en .env (PAT con permiso Actions:write del repo).
# Sin token, el panel explica cómo crearlo — nada se rompe.
# ─────────────────────────────────────────────────────────────
import os as _os
import json as _json
import urllib.request as _urlreq

GH_REPO = "AldairG16/Sport_Betting_Model"
GH_API = f"https://api.github.com/repos/{GH_REPO}"

# key → (archivo YAML del workflow, nombre visible)
# La API de dispatch requiere el ARCHIVO (closing.yml), no el nombre.
DISPATCHABLE = {
    "morning":  ("morning.yml",  "Morning Pipeline"),
    "evening":  ("evening.yml",  "Evening Pipeline"),
    "closing":  ("closing.yml",  "Closing Odds Pipeline"),
    "weekly":   ("weekly.yml",   "Weekly Pipeline"),
}


def _gh_headers():
    token = _os.environ.get("GH_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "betting-dashboard",
    }


def _gh_call(url: str, method: str = "GET") -> dict:
    req = _urlreq.Request(url, headers=_gh_headers(), method=method)
    try:
        with _urlreq.urlopen(req, timeout=10) as r:
            body = r.read().decode()
            return {"ok": True, "data": _json.loads(body) if body else {}}
    except _urlreq.HTTPError as e:
        return {"ok": False, "status": e.code, "msg": e.read().decode()[:200]}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@app.route("/api/gh/status")
def gh_status():
    token = _os.environ.get("GH_TOKEN", "")
    if not token:
        return jsonify({"ok": False, "configured": False,
                        "msg": "Falta GH_TOKEN en .env (PAT con Actions:write)"})
    runs = _gh_call(f"{GH_API}/actions/runs?per_page=8")
    if not runs["ok"]:
        return jsonify({"ok": False, "configured": True,
                        "msg": f"GitHub API: {runs.get('status')} {runs.get('msg','')}"})
    items = [{
        "name": r["name"], "status": r["status"], "conclusion": r["conclusion"],
        "created": r["created_at"][:16].replace("T", " "), "url": r["html_url"],
    } for r in runs["data"].get("workflow_runs", [])]
    return jsonify({"ok": True, "configured": True, "runs": items})


@app.route("/api/gh/dispatch/<wf>", methods=["POST"])
def gh_dispatch(wf):
    if wf not in DISPATCHABLE:
        return jsonify({"ok": False, "msg": "workflow desconocido"}), 404
    if not _os.environ.get("GH_TOKEN"):
        return jsonify({"ok": False, "msg": "Falta GH_TOKEN en .env"}), 400
    url = f"{GH_API}/actions/workflows/{DISPATCHABLE[wf][0]}/dispatches"
    req = _urlreq.Request(
        url, headers=_gh_headers(), method="POST",
        data=_json.dumps({"ref": "master"}).encode(),
    )
    try:
        with _urlreq.urlopen(req, timeout=10):
            return jsonify({"ok": True, "msg": f"{DISPATCHABLE[wf][1]} disparado"})
    except _urlreq.HTTPError as e:
        return jsonify({"ok": False, "msg": f"HTTP {e.code}: {e.read().decode()[:150]}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


PAGE = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<title>Betting Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
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
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:20px; }
  @media (max-width:900px){ .grid2{grid-template-columns:1fr;} }
  .chartbox { background:var(--card); border-radius:10px; padding:14px; height:320px; position:relative; }
  .chartbox h3 { font-size:.9rem; margin-bottom:8px; color:var(--muted); font-weight:600; }
  table { width:100%; border-collapse:collapse; font-size:.82rem; background:var(--card); border-radius:10px; overflow:hidden; }
  th { text-align:left; padding:8px 10px; color:var(--muted); font-weight:600; border-bottom:1px solid #2a3550; }
  td { padding:7px 10px; border-bottom:1px solid #232d45; }
  tr:hover td { background:#202a40; }
  .pill { padding:2px 8px; border-radius:99px; font-size:.72rem; font-weight:600; }
  .pill.win{background:#14351f;color:var(--green)} .pill.loss{background:#3a1a1a;color:var(--red)}
  .pill.pending{background:#2a2a14;color:#eab308} .pill.push,.pill.half_win,.pill.half_loss{background:#1e2a3a;color:#93c5fd}
  .controls { display:flex; gap:10px; margin:14px 0; flex-wrap:wrap; }
  select { background:var(--card); color:var(--text); border:1px solid #2a3550; border-radius:8px; padding:6px 10px; }
  .section { margin-top:26px; } .section h2 { font-size:1.05rem; margin-bottom:10px; }
  #err { color:var(--red); font-size:.85rem; margin:20px 0; display:none; }
</style></head><body>
<h1>⚽ Sport Betting Model — Dashboard <span id="ver" style="font-size:.7rem;color:var(--muted);font-weight:400"></span></h1>
<div class="sub" id="sub">Conectando a la base de datos…</div>
<div id="upd" style="display:none;background:#14351f;border:1px solid #22c55e44;color:var(--green);border-radius:8px;padding:8px 12px;margin-bottom:14px;font-size:.85rem"></div>
<div id="err"></div>
<div class="kpis" id="kpis"></div>
<div class="grid2">
  <div class="chartbox"><h3>Curva de resultados (profit acumulado)</h3><canvas id="equity"></canvas></div>
  <div class="chartbox"><h3>Profit por día</h3><canvas id="daily"></canvas></div>
</div>
<div class="grid2">
  <div class="chartbox"><h3>ROI % por mercado (180d, n≥3)</h3><canvas id="bymarket"></canvas></div>
  <div class="chartbox"><h3>ROI % por liga (180d, n≥3)</h3><canvas id="byleague"></canvas></div>
</div>
<div class="grid2">
  <div class="chartbox"><h3>CLV por apuesta (verde=ganada)</h3><canvas id="clvchart"></canvas></div>
  <div class="chartbox"><h3>Bankroll</h3><canvas id="bankchart"></canvas></div>
</div>
<div class="section"><h2>📋 Apuestas</h2>
  <div class="controls">
    <select id="fstatus"><option value="all">Activas (pendientes + resueltas)</option><option value="pending">Solo pendientes</option><option value="resolved">Solo resueltas</option><option value="stale">Histórico muerto (stale)</option></select>
    <select id="fmarket"><option value="">Todos los mercados</option></select>
    <select id="fleague"><option value="">Todas las ligas</option></select>
  </div>
  <table><thead><tr><th>Fecha</th><th>Partido</th><th>Liga</th><th>Mercado</th><th>Prob</th><th>Odd</th><th>Stake</th><th>Resultado</th><th>Profit</th><th>CLV</th></tr></thead>
  <tbody id="betsbody"><tr><td colspan="10" style="color:var(--muted)">Cargando…</td></tr></tbody></table>
</div>
<div class="section"><h2>⚽ Goleadores (anytime scorer · papel)</h2>
  <table><thead><tr><th>Fecha</th><th>Partido</th><th>Jugador</th><th>Equipo</th><th>P(anota)</th><th>Fair odd</th><th>Odd real</th><th>Resultado</th></tr></thead>
  <tbody id="scorersbody"><tr><td colspan="8" style="color:var(--muted)">Cargando…</td></tr></tbody></table>
  <div style="color:var(--muted);font-size:.75rem;margin-top:6px">Fair odd = 1/P(modelo). Si encuentras cuota real MEJOR que la fair, hay valor — regístrala en la DB (odds_placed).</div>
</div>
<div class="section"><h2>🎮 Panel de control (GitHub Actions)</h2>
  <div class="controls">
    <button onclick="dispatch('morning')">☀️ Ejecutar Morning</button>
    <button onclick="dispatch('evening')">🌙 Ejecutar Evening</button>
    <button onclick="dispatch('closing')">🎯 Ejecutar Closing</button>
    <button onclick="dispatch('weekly')">📅 Ejecutar Weekly</button>
    <button onclick="loadGh()" style="background:#2a3550">🔄 Refrescar estado</button>
  </div>
  <div id="ghmsg" style="font-size:.82rem;margin-bottom:10px;color:var(--muted)">Cargando…</div>
  <table><thead><tr><th>Corrida</th><th>Estado</th><th>Resultado</th><th>Cuándo (UTC)</th><th>Link</th></tr></thead>
  <tbody id="ghbody"><tr><td colspan="5" style="color:var(--muted)">Cargando…</td></tr></tbody></table>
</div>
<style>
button { background:#14351f; color:var(--green); border:1px solid #22c55e44; border-radius:8px;
         padding:8px 14px; cursor:pointer; font-size:.85rem; }
button:hover { background:#1a4527; }
</style>
<script>
const money = v => (v>=0?'+':'') + Number(v).toFixed(2) + 'u';
// Fechas: la DB guarda UTC; mostrar en hora del usuario (Mexico City)
const mxdate = s => { try { return new Date(s).toLocaleString('es-MX',{timeZone:'America/Mexico_City',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}); } catch(e){ return String(s).slice(0,16); } };
const pct = v => v==null?'—':(v>=0?'+':'') + (v*100).toFixed(1) + '%';
function err(msg){ const e=document.getElementById('err'); e.style.display='block'; e.textContent='⚠️ '+msg; }
async function jget(u){ const r=await fetch(u); const d=await r.json(); if(d.ok===false) throw new Error(d.msg||'sin datos'); return d; }

async function loadKpis(){
  try{
    const d=await jget('/api/kpis');
    const cards=[
      ['Bankroll', d.bankroll==null?'—':d.bankroll.toFixed(1)+'u',''],
      ['Profit 90d', money(d.profit), d.profit>=0?'pos':'neg'],
      ['ROI 90d', pct(d.roi), d.roi>=0?'pos':'neg'],
      ['Win rate', d.win_rate==null?'—':(d.win_rate*100).toFixed(1)+'%',''],
      ['Bets 90d', d.bets_90d,''],
      ['Resueltas', d.resolved,''],
      ['Pendientes', d.pending,''],
      ['Brier', d.brier==null?'—':d.brier.toFixed(3),''],
      ['CLV medio (n='+d.clv_n+')', d.clv_avg==null?'—':(d.clv_avg>=0?'+':'')+(d.clv_avg*100).toFixed(2)+'%', d.clv_avg>=0?'pos':'neg'],
    ];
    document.getElementById('kpis').innerHTML=cards.map(c=>
      `<div class="card"><div class="lbl">${c[0]}</div><div class="val ${c[2]}">${c[1]}</div></div>`).join('');
    document.getElementById('sub').textContent='Datos: '+(d.window||'90d')+' · solo lectura · '+new Date().toLocaleString('es-MX');
  }catch(e){ err('KPIs: '+e.message); }
}
function cctx(id){ return document.getElementById(id).getContext('2d'); }
Chart.defaults.color='#8b98ad'; Chart.defaults.borderColor='#2a3550';
async function loadEquity(){
  try{ const d=await jget('/api/equity');
    new Chart(cctx('equity'),{type:'line',data:{labels:d.dates,datasets:[{data:d.cumulative,borderWidth:2,pointRadius:0,borderColor:'#3b82f6',fill:true,backgroundColor:'rgba(59,130,246,.08)'}]},options:{plugins:{legend:{display:false}},scales:{x:{ticks:{maxTicksLimit:8}}}}});
    const colors=d.daily.map(v=>v>=0?'#22c55e':'#ef4444');
    new Chart(cctx('daily'),{type:'bar',data:{labels:d.daily_dates,datasets:[{data:d.daily,backgroundColor:colors}]},options:{plugins:{legend:{display:false}},scales:{x:{ticks:{maxTicksLimit:10}}}}});
  }catch(e){ err('Curva: '+e.message); }
}
async function loadBy(){
  for(const [dim,id] of [['market','bymarket'],['league','byleague']]){
    try{ const d=await jget('/api/by/'+dim);
      const colors=d.roi.map(v=>v>=0?'#22c55e':'#ef4444');
      new Chart(cctx(id),{type:'bar',data:{labels:d.labels,datasets:[{label:'ROI %',data:d.roi,backgroundColor:colors}]},
        options:{indexAxis:'y',plugins:{legend:{display:false},tooltip:{callbacks:{afterLabel:c=>'n='+d.n[c.dataIndex]+' · wr '+d.wr[c.dataIndex]+'%'}}},scales:{x:{ticks:{callback:v=>v+'%'}}}}});
      if(dim==='market'){ const sel=document.getElementById('fmarket'); d.labels.forEach(l=>sel.add(new Option(l,l))); }
      else{ const sel=document.getElementById('fleague'); d.labels.forEach(l=>sel.add(new Option(l,l))); }
    }catch(e){ err(dim+': '+e.message); }
  }
}
async function loadClv(){
  try{ const d=await jget('/api/clv');
    new Chart(cctx('clvchart'),{type:'scatter',data:{datasets:[
      {label:'ganada',data:d.x.map((x,i)=>d.wins[i]?{x,y:d.clv[i]}:null).filter(Boolean),backgroundColor:'#22c55e'},
      {label:'perdida',data:d.x.map((x,i)=>!d.wins[i]?{x,y:d.clv[i]}:null).filter(Boolean),backgroundColor:'#ef4444'}]},
      options:{plugins:{legend:{display:false}},scales:{y:{ticks:{callback:v=>(v*100).toFixed(1)+'%'}}}}});
  }catch(e){ document.getElementById('clvchart').canvas.parentNode.innerHTML+='<div style="color:var(--muted)">Sin datos de CLV aún</div>'; }
}
async function loadBank(){
  const d=await fetch('/api/equity').then(r=>r.json()).catch(()=>({ok:false}));
  if(!d.ok){ document.getElementById('bankchart').canvas.parentNode.innerHTML+='<div style="color:var(--muted)">Sin datos</div>'; return; }
  new Chart(cctx('bankchart'),{type:'line',data:{labels:d.dates,datasets:[{data:d.cumulative.map(v=>100+v),borderWidth:2,pointRadius:0,borderColor:'#eab308'}]},options:{plugins:{legend:{display:false}},scales:{x:{ticks:{maxTicksLimit:6}}}}});
}
async function loadBets(){
  const q=new URLSearchParams({status:document.getElementById('fstatus').value,market:document.getElementById('fmarket').value,league:document.getElementById('fleague').value,limit:150});
  const body=document.getElementById('betsbody');
  try{ const d=await jget('/api/bets?'+q);
    body.innerHTML=d.bets.map(b=>{
      const rk=(b.result_key||b.result||'pending').toLowerCase();
      const prof=rk==='pending'?'':money(b.profit||0);
      const cls=(rk==='win'?'win':rk==='loss'?'loss':rk==='pending'?'pending':'push');
      const clv=b.clv===''?'—':(Number(b.clv)*100).toFixed(1)+'%';
      return `<tr><td>${mxdate(b.match_date)}</td><td>${b.match}</td><td>${b.league||''}</td><td>${b.market}</td><td>${(b.probability*100).toFixed(0)}%</td><td>${b.odds}</td><td>${b.stake}u</td><td><span class="pill ${cls}">${b.result}</span></td><td>${prof}</td><td>${clv}</td></tr>`;
    }).join('');
  }catch(e){ body.innerHTML='<tr><td colspan="10" style="color:var(--muted)">'+e.message+'</td></tr>'; }
}
['fstatus','fmarket','fleague'].forEach(id=>document.getElementById(id).onchange=loadBets);
async function loadScorers(){
  try{ const d=await jget('/api/scorers');
    document.getElementById('scorersbody').innerHTML=d.picks.map(p=>{
      const rk=(p.result_key||p.result||'pending').toLowerCase();
      const cls=(rk==='win'?'win':rk==='loss'?'loss':'pending');
      const real=p.odds_placed===''?'—':p.odds_placed;
      return `<tr><td>${mxdate(p.match_date)}</td><td>${p.match}</td><td>${p.player}</td><td>${p.team}</td><td>${(p.probability*100).toFixed(0)}%</td><td>@${p.fair_odds}</td><td>${real}</td><td><span class="pill ${cls}">${p.result}</span></td></tr>`;
    }).join('');
  }catch(e){ document.getElementById('scorersbody').innerHTML='<tr><td colspan="8" style="color:var(--muted)">'+e.message+'</td></tr>'; }
}
async function loadVersion(){
  try{ const d=await (await fetch('/api/version')).json();
    document.getElementById('ver').textContent='v'+d.current;
    if(d.update_available){
      const u=document.getElementById('upd'); u.style.display='block';
      u.innerHTML='🔄 Nueva versión disponible: <b>v'+d.latest+'</b> (tienes v'+d.current+') — <a href="'+d.release_url+'" target="_blank" style="color:var(--green)">descargar</a>';
    }
  }catch(e){}
}
async function loadGh(){
  const msg=document.getElementById('ghmsg'), body=document.getElementById('ghbody');
  try{
    const d=await (await fetch('/api/gh/status')).json();
    if(!d.configured){ msg.innerHTML='⚠️ '+d.msg+' — créalo en github.com/settings/tokens (fine-grained, permiso <b>Actions: Read and write</b> del repo) y agrégalo a tu .env como GH_TOKEN=xxx'; body.innerHTML=''; return; }
    if(!d.ok){ msg.textContent='⚠️ '+d.msg; return; }
    msg.textContent='Últimas corridas del sistema:';
    body.innerHTML=d.runs.map(r=>{
      const icon=r.status!=='completed'?'🔄':(r.conclusion==='success'?'✅':'❌');
      return `<tr><td>${r.name}</td><td>${icon} ${r.status}</td><td>${r.conclusion||'—'}</td><td>${r.created}</td><td><a href="${r.url}" target="_blank" style="color:var(--blue)">ver</a></td></tr>`;
    }).join('');
  }catch(e){ msg.textContent='⚠️ '+e.message; }
}
async function dispatch(wf){
  const msg=document.getElementById('ghmsg');
  msg.textContent='Disparando '+wf+'…';
  try{
    const r=await fetch('/api/gh/dispatch/'+wf,{method:'POST'});
    const d=await r.json();
    msg.textContent = d.ok ? ('✅ '+d.msg+' — aparece abajo en segundos') : ('⚠️ '+d.msg);
  }catch(e){ msg.textContent='⚠️ '+e.message; }
  setTimeout(loadGh, 4000);
}
loadVersion(); loadKpis(); loadEquity(); loadBy(); loadClv(); loadBank(); loadBets(); loadScorers(); loadGh();
</script></body></html>"""


@app.route("/")
def index():
    return PAGE


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
