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
    - Solo lectura: no escribe en la base. (v1.4.0 pedía teclear la cuota
      tomada en PlayDoit; se quitó en v1.5.0 — el dueño no quiere pasos
      manuales.)
    - DB_URL se lee de .env / entorno — nunca va en el código.
"""

import sys
from pathlib import Path

import pandas as pd
from flask import Flask, g, jsonify, request
from sqlalchemy import create_engine, text

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.database import DATABASE_URL  # noqa: E402

# El dashboard es un proceso de larga vida: entre refrescos (5 min) Neon
# cierra las conexiones inactivas y la laptop puede dormir. Con el motor
# compartido del pipeline, la primera consulta sobre una conexión muerta
# fallaba ("SSL connection has been closed unexpectedly", decenas de veces
# en dash_start.log) y esa sección mostraba "sin datos" (23-sep-26).
# pool_pre_ping prueba la conexión antes de usarla y reconecta sola;
# pool_recycle la renueva antes de que el servidor la corte.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=240)  # noqa: E402
from dashboard.display import league_name, market_name, match_name, result_label  # noqa: E402
from src.utils.bet_status import CANCELLED_SQL  # noqa: E402
from src.utils.min_odds import (HOW_TO_READ, fmt_american, min_american,  # noqa: E402
                                rule_examples, to_american)

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

# ── JS del dashboard y Chart.js servidos localmente (sin depender del CDN) ──
from flask import send_file, redirect as _redirect


def _asset(name: str) -> Path | None:
    """
    Archivo estático del dashboard. Orden de búsqueda:
      1. junto al .exe — permite corregir el JS en vivo sin recompilar;
      2. dentro del .exe (release.yml lo empaqueta con --add-data);
      3. dashboard/ del repo (python scripts/run_dashboard.py).
    """
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / name)
        if getattr(sys, "_MEIPASS", None):
            candidates.append(Path(sys._MEIPASS) / "dashboard" / name)
    candidates.append(Path(__file__).parent / name)
    return next((c for c in candidates if c.exists()), None)


@app.route("/ui_es5.js")
def ui_es5_js():
    path = _asset("ui_es5.js")
    if path is None:
        return "/* ui_es5.js no encontrado */", 404
    return send_file(path, mimetype="application/javascript")


@app.route("/chartjs.js")
def chartjs_local():
    path = _asset("chart2.min.js")
    if path is None:
        return _redirect("https://cdn.jsdelivr.net/npm/chart.js@2.9.4/dist/Chart.min.js")
    return send_file(path, mimetype="application/javascript")



@app.route("/api/health")
def api_health():
    """Latido del proceso (sin base de datos): lo consultan el supervisor
    (dashboard/supervisor.py) y la página cuando pierde la conexión."""
    return jsonify({"ok": True, "version": _app_version()})


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
                url = "https://github.com/AldairG16/Sport_Betting_Model/releases/latest"
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


DB_DOWN = "Sin conexión con la base de datos — se reintenta sola en el próximo refresco"


def _q(sql: str, params: dict | None = None) -> pd.DataFrame:
    try:
        return pd.read_sql(text(sql), engine, params=params or {})
    except Exception as e:
        app.logger.warning(f"query falló: {e}")
        # error de CONEXIÓN (no de datos): la respuesta lo dice en vez de
        # un "sin datos" que parece que no hay apuestas
        if "OperationalError" in f"{type(e).__name__} {e}":
            g.db_down = True
        return pd.DataFrame()


def _no_data(msg: str = "sin datos"):
    """Respuesta vacía que distingue "no hay filas" de "no hubo conexión"."""
    return jsonify({"ok": False, "msg": DB_DOWN if g.get("db_down") else msg})


def _profit(df: pd.DataFrame) -> pd.Series:
    """
    Profit real de cada bet: la columna `profit` que escribe la liquidación
    (save_bets.resolve_market), la misma que mueve el bankroll.

    Hasta el 22-sep-26 se recalculaba como (odds − 1)·stake·factor, que en
    una pérdida da −(odds − 1)·stake en vez de −stake: a cuota 1.80 una
    pérdida contaba −0.80u y a cuota 3.00, −2.00u. Con las cuotas de este
    sistema (muchas > 2) el ROI a 90 días salía −34% cuando el real era −16%,
    y no cuadraba con el bankroll.
    """
    if df.empty:
        return pd.Series(dtype=float)
    return pd.to_numeric(df["profit"], errors="coerce").fillna(0.0)


def _odds_us(odds) -> str:
    """Cuota decimal guardada → americana (+128 / -140), como la ve PlayDoit."""
    return fmt_american(to_american(odds))


def _playdoit_min(prob, pin_prob=None) -> str:
    """Peor cuota americana que todavía vale la pena en PlayDoit (con la
    probabilidad más conservadora entre el modelo y Pinnacle); "" si no hay."""
    a = min_american(prob, pin_prob=pin_prob)
    return "" if a is None else fmt_american(a)


def _playdoit_hint(prob, pin_prob=None) -> str:
    """Ejemplo para el tooltip: un precio que sirve y uno que ya no."""
    a = min_american(prob, pin_prob=pin_prob)
    if a is None:
        return ""
    better, worse = rule_examples(a)
    return f"Sirve {fmt_american(better)}" + (f"; {fmt_american(worse)} ya no" if worse is not None else "")


@app.route("/api/kpis")
def kpis():
    # Ventana preferida: 90 días. Si no hay NADA resuelto en ella (sistema
    # recién reactivado, bets nuevas aún pendientes), caer al histórico
    # completo para no mostrar un dashboard vacío.
    df = _q("""
        SELECT result, stake, odds, probability, clv, match_date, profit
        FROM bets_history
        WHERE match_date >= NOW() - INTERVAL '90 days'
    """)
    window = "90d"
    has_resolved = (not df.empty) and df["result"].isin(RESOLVED).any()
    if not has_resolved:
        df = _q("""
            SELECT result, stake, odds, probability, clv, match_date, profit
            FROM bets_history
        """)
        window = "histórico"
    if df.empty:
        return _no_data("Sin datos en bets_history")

    resolved = df[df["result"].isin(RESOLVED)]
    pending = df[df["result"] == "pending"]
    wins = resolved[resolved["result"].isin(WIN_LIKE)]
    profit = _profit(resolved).sum()
    staked = float(resolved["stake"].sum())
    clv = df["clv"].dropna()

    # Misma lectura que bankroll_manager.get_current_bankroll. Antes pedía
    # columnas que la tabla no tiene (bankroll, updated_at): el KPI salía "—".
    bank = _q("SELECT current_bankroll AS bankroll FROM bankroll ORDER BY id LIMIT 1")
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
        SELECT match_date, result, stake, odds, market, profit
        FROM bets_history
        WHERE result IN ('win', 'loss', 'push', 'half_win', 'half_loss')
        ORDER BY match_date
    """)
    if df.empty:
        return _no_data()
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
        where.append(f"result = 'stale' AND NOT {CANCELLED_SQL}")
    else:
        # Vista default: TODO excepto stale (bets antiguas sin fuente de
        # resultado — historial muerto que no debe estorbar el día a día).
        # Las canceladas antes del kickoff también se guardan 'stale', pero
        # sí se muestran: el dueño las vio en Telegram y debe saber que ya no van.
        where.append(f"(result IS DISTINCT FROM 'stale' OR {CANCELLED_SQL})")
    if market:
        where.append("market = :market"); params["market"] = market
    if league:
        where.append("league = :league"); params["league"] = league

    df = _q(f"""
        SELECT id, match_date, match, league, market, probability, odds,
               stake, result, profit, closing_odds, clv,
               COALESCE(pin_close_prob,
                        CAST(decision_log->'market_ctx'->>'pin_prob' AS float)) AS pin_prob,
               {CANCELLED_SQL} AS cancelled
        FROM bets_history
        WHERE {' AND '.join(where)}
        ORDER BY match_date DESC
        LIMIT {limit}
    """, params)
    if df.empty:
        return _no_data()

    # En formato americano, como los muestra PlayDoit (src/utils/min_odds):
    # la mejor cuota europea (referencia) y la peor que todavía vale la pena.
    df["odds_us"] = df["odds"].map(_odds_us)
    pin = df["pin_prob"] if "pin_prob" in df.columns else [None] * len(df)
    df["min_us"] = [_playdoit_min(p, q) for p, q in zip(df["probability"], pin)]
    df["min_hint"] = [_playdoit_hint(p, q) for p, q in zip(df["probability"], pin)]
    # canceladas antes del kickoff (revalidación o lineup guard): 'stale'
    # también las marcaba, y el dashboard las mostraba como "Sin fuente"
    if "cancelled" in df.columns:
        df.loc[df["cancelled"].fillna(False).astype(bool) & (df["result"] == "stale"),
               "result"] = "cancelled"
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
        SELECT {dim}, result, stake, odds, profit
        FROM bets_history
        WHERE result IN ('win','loss','push','half_win','half_loss')
          AND match_date >= NOW() - INTERVAL '180 days'
    """)
    if df.empty:
        return _no_data()
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
        # clave cruda para el valor de los filtros: /api/bets filtra por la
        # clave ("under_3.5"), no por el nombre visible ("Menos 3.5 goles")
        "keys": [str(x) for x in g.index],
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
        return _no_data()
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
               probability, fair_odds, result
        FROM goalscorer_picks
        WHERE match_date >= NOW() - INTERVAL '7 days'
        ORDER BY match_date DESC
        LIMIT 100
    """)
    if df.empty:
        return _no_data()
    df["fair_us"] = df["fair_odds"].map(_odds_us)
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


@app.route("/api/narrative")
def api_narrative():
    df = _q("""
        SELECT week_start, narrative, engine, created_at
        FROM weekly_narrative ORDER BY created_at DESC LIMIT 1
    """)
    if df.empty:
        return _no_data()
    r = df.iloc[0]
    return jsonify({"ok": True, "narrative": r["narrative"],
                    "engine": r["engine"],
                    "created": str(r["created_at"])[:16]})


@app.route("/api/narrative/generate", methods=["POST"])
def api_narrative_generate():
    try:
        from scripts.weekly_narrator import generate_weekly_narrative
        txt = generate_weekly_narrative(verbose=False)
        if not txt:
            return jsonify({"ok": False, "msg": "Ollama no disponible o sin modelo de chat"})
        return jsonify({"ok": True, "narrative": txt})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:200]})


PAGE = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<title>Betting Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="/chartjs.js"></script>
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
  .pill.cancelled{background:#2a2f3a;color:#9aa4b8;text-decoration:line-through}
  .controls { display:flex; gap:10px; margin:14px 0; flex-wrap:wrap; }
  .tablewrap { max-height:480px; overflow-y:auto; border-radius:10px; }
  .tablewrap thead th { position:sticky; top:0; background:#1a2233; z-index:1; }
  select { background:var(--card); color:var(--text); border:1px solid #2a3550; border-radius:8px; padding:6px 10px; }
  .section { margin-top:26px; } .section h2 { font-size:1.05rem; margin-bottom:10px; }
  #err { color:var(--red); font-size:.85rem; margin:20px 0; display:none; }
</style></head><body>
<h1>⚽ Sport Betting Model — Dashboard <span id="ver" style="font-size:.7rem;color:var(--muted);font-weight:400"></span></h1>
<div class="sub" id="sub">Conectando a la base de datos…</div>
<div id="upd" style="display:none;background:#14351f;border:1px solid #22c55e44;color:var(--green);border-radius:8px;padding:8px 12px;margin-bottom:14px;font-size:.85rem"></div>
<div id="err"></div>
<div class="kpis" id="kpis"></div>
<div class="section" id="narrsec" style="display:none;margin-top:0">
  <h2>🧠 Diagnóstico IA de la semana</h2>
  <div class="card" id="narrtext" style="font-size:.9rem;white-space:pre-wrap;line-height:1.5"></div>
  <div style="display:flex;justify-content:space-between;margin-top:6px">
    <span id="narreng" style="color:var(--muted);font-size:.72rem"></span>
    <button id="genbtn" onclick="genNarrative()" style="font-size:.72rem;padding:4px 10px">🧠 Generar diagnóstico ahora</button>
  </div>
</div>
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
    <select id="frefresh" title="Auto-refresco de datos"><option value="0">Auto-refresco: off</option><option value="5" selected>Auto-refresco: 5 min</option><option value="15">15 min</option><option value="30">30 min</option></select>
    <button onclick="exportCSV()" style="font-size:.75rem;padding:6px 10px">⬇️ CSV</button>
  </div>
  <div class="tablewrap"><table><thead><tr><th>Fecha</th><th>Partido</th><th>Liga</th><th>Mercado</th><th>Prob</th><th title="La más alta entre casas europeas: solo referencia">Mejor cuota</th><th title="En PlayDoit apuesta solo si paga esto o mejor">PlayDoit</th><th>Stake</th><th>Resultado</th><th>Profit</th><th>CLV</th></tr></thead>
  <tbody id="betsbody"><tr><td colspan="11" style="color:var(--muted)">Cargando…</td></tr></tbody></table></div>
  <div style="color:var(--muted);font-size:.75rem;margin-top:6px">Cuotas en formato americano, como las muestra PlayDoit. En PlayDoit apuesta solo si paga el mínimo de la columna <b>PlayDoit</b> o mejor. __HOW_TO_READ__</div>
  <button onclick="exportCSV()" style="margin-top:8px;font-size:.75rem">⬇️ Exportar CSV</button>
</div>
<div class="section"><h2>⚽ Goleadores (anytime scorer · papel)</h2>
  <div class="tablewrap" style="max-height:300px"><table><thead><tr><th>Fecha</th><th>Partido</th><th>Jugador</th><th>Equipo</th><th>P(anota)</th><th>Cuota justa</th><th>Resultado</th></tr></thead>
  <tbody id="scorersbody"><tr><td colspan="7" style="color:var(--muted)">Cargando…</td></tr></tbody></table></div>
  <div style="color:var(--muted);font-size:.75rem;margin-top:6px">Cuota justa = la que corresponde a la probabilidad del modelo. Si PlayDoit paga más que la justa, hay valor.</div>
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
  <div class="tablewrap" style="max-height:260px"><table><thead><tr><th>Corrida</th><th>Estado</th><th>Resultado</th><th>Cuándo (UTC)</th><th>Link</th></tr></thead>
  <tbody id="ghbody"><tr><td colspan="5" style="color:var(--muted)">Cargando…</td></tr></tbody></table></div>
</div>
<style>
button { background:#14351f; color:var(--green); border:1px solid #22c55e44; border-radius:8px;
         padding:8px 14px; cursor:pointer; font-size:.85rem; }
button:hover { background:#1a4527; }
</style>
<script src="/ui_es5.js"></script></body></html>""".replace("__HOW_TO_READ__", HOW_TO_READ)


@app.route("/")
def index():
    return PAGE


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
