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
# Lo que el dashboard gasta de la base también cuenta para el plan gratis de
# Neon (11-nov-26): se mide y se guarda cada 30 min en pipeline_runs ("dashboard")
from src.utils import db_meter as _meter  # noqa: E402
_meter.install(engine)
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


def _newer(latest: str | None, current: str) -> bool:
    """¿La versión publicada es más nueva que la instalada? Antes bastaba con
    que fuera distinta: instalada una versión antes de publicarla, el banner
    ofrecía "actualizar" a una más vieja."""
    def parts(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except ValueError:
            return None
    a, b = parts(latest), parts(current)
    return bool(a and b and a > b)

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
        "update_available": _newer(latest, current),
        "release_url": url,
    })

RESOLVED = ("win", "loss", "push", "half_win", "half_loss")
WIN_LIKE = ("win", "half_win")


DB_DOWN = "Sin conexión con la base de datos — se reintenta sola en el próximo refresco"
PARTIAL = "No se pudieron leer todos los datos (la base tardó en responder). Recarga en un minuto."
QUERY_RETRY_S = 1.5


def _q(sql: str, params: dict | None = None, optional: bool = False) -> pd.DataFrame:
    """La consulta como DataFrame. Un error de conexión se reintenta una vez.
    Si falla igual, la respuesta entera dice que faltan datos (_partial_is_an_error)
    en vez de mostrar la página a medias: el 25-oct la consulta de los picks
    falló y "Anteriores" salió con todos los partidos en "Sin análisis".
    optional: tablas que pueden no existir todavía (no marcan la respuesta)."""
    import time
    for attempt in (1, 2):
        try:
            return pd.read_sql(text(sql), engine, params=params or {})
        except Exception as e:
            err = e
            transient = "OperationalError" in f"{type(e).__name__} {e}"
            if transient and attempt == 1:
                time.sleep(QUERY_RETRY_S)
                continue
            break
    app.logger.warning(f"query falló: {err}")
    # error de CONEXIÓN (no de datos): la respuesta lo dice en vez de
    # un "sin datos" que parece que no hay apuestas
    if "OperationalError" in f"{type(err).__name__} {err}":
        g.db_down = True
    if not optional:
        g.partial = True
    return pd.DataFrame()


@app.after_request
def _partial_is_an_error(resp):
    """Una respuesta armada con una consulta que falló no se muestra como si
    estuviera completa (24-oct-26)."""
    if g.get("partial") and resp.is_json:
        data = resp.get_json(silent=True)
        if isinstance(data, dict) and data.get("ok"):
            resp.set_data(jsonify({"ok": False, "msg": DB_DOWN if g.get("db_down") else PARTIAL}).get_data())
    return resp


_METER_FLUSH = {"last": None}
METER_FLUSH_S = 1800


@app.after_request
def _flush_db_meter(resp):
    """Cada 30 min, lo que leyó el dashboard va a pipeline_runs (mode
    "dashboard") para el presupuesto del plan gratis (db_budget). Solo después
    de una consulta real: la base ya está despierta, no se despierta para esto."""
    import time
    now = time.time()
    if _METER_FLUSH["last"] is None:
        _METER_FLUSH["last"] = now
    t = _meter.totals()
    if now - _METER_FLUSH["last"] < METER_FLUSH_S or not t["queries"] or g.get("db_down"):
        return resp
    try:
        from src.utils.pipeline_runs import record_run
        record_run(engine, "dashboard", 0, now - _METER_FLUSH["last"], t["rows"], t["cells"], t["bytes"], t["awake_s"])
        _meter.reset()
        _METER_FLUSH["last"] = now
    except Exception as e:
        app.logger.warning(f"consumo de la base: no se pudo guardar ({type(e).__name__})")
    return resp


_BUDGET_CACHE = {"at": 0.0, "st": None}
BUDGET_TTL_S = 1800


def _budget_status() -> dict:
    """El presupuesto del plan gratis (db_budget), una vez cada 30 min."""
    import time
    from src.utils import db_budget
    if _BUDGET_CACHE["st"] is None or time.time() - _BUDGET_CACHE["at"] > BUDGET_TTL_S:
        _BUDGET_CACHE.update(at=time.time(), st=db_budget.status(engine))
    return _BUDGET_CACHE["st"]


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
    losses = resolved[resolved["result"].isin(("loss", "half_loss"))]
    profit = _profit(resolved).sum()
    staked = float(resolved["stake"].sum())
    # Solo apuestas jugadas (y las pendientes en el conteo): las 'stale'
    # (canceladas o sin fuente de resultado) no se apostaron o no tienen
    # resultado. Hasta v1.6.0 "Bets 90d" y el CLV medio las incluían.
    clv = resolved["clv"].dropna()

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
        "bets_90d": int(len(resolved) + len(pending)),
        "resolved": int(len(resolved)),
        "pending": int(len(pending)),
        # ganadas / (ganadas + perdidas), igual que el reporte semanal
        "win_rate": (round(len(wins) / (len(wins) + len(losses)), 3)
                     if len(wins) + len(losses) else None),
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
    elif status == "cancelled":
        where.append(CANCELLED_SQL)
    else:
        # Vista default: TODO excepto stale (bets antiguas sin fuente de
        # resultado — historial muerto que no debe estorbar el día a día).
        # Las canceladas antes del kickoff también se guardan 'stale': se ven
        # hasta que empieza el partido (el dueño las vio en Telegram y debe
        # saber que ya no van); después quedan en el filtro "Canceladas".
        where.append(f"(result IS DISTINCT FROM 'stale' OR ({CANCELLED_SQL} AND match_date > NOW()))")
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
# LO QUE EL PROGRAMA ANALIZA AUNQUE NO APUESTE (v1.7.0, 21-oct-26)
# En modo recolección casi no hay apuestas reales y el dashboard se veía
# vacío. La lógica está en dashboard/visibility.py (funciones puras).
# ─────────────────────────────────────────────────────────────
from dashboard import visibility as _vis  # noqa: E402


def _days(default: int = 14) -> int:
    try:
        return max(1, min(int(request.args.get("days", default)), 60))
    except ValueError:
        return default


@app.route("/api/predictions")
def api_predictions():
    mvs = _q("""
        SELECT * FROM model_vs_sharp
        WHERE match_date BETWEEN NOW() AND NOW() + INTERVAL '3 days'
        ORDER BY match_date
    """)
    if mvs.empty:
        return _no_data("El programa todavía no analizó los próximos partidos")
    up = _q("""
        SELECT DISTINCT ON (home_team_norm, away_team_norm)
               home_team_norm, away_team_norm, home_team, away_team, home_odds, draw_odds, away_odds,
               pin_home_odds, pin_draw_odds, pin_away_odds, over25_odds, under25_odds,
               consensus_over25_odds, consensus_under25_odds, btts_yes_odds, btts_no_odds,
               consensus_home_odds, consensus_draw_odds, consensus_away_odds
        FROM upcoming_matches
        WHERE match_date BETWEEN NOW() AND NOW() + INTERVAL '3 days'
        ORDER BY home_team_norm, away_team_norm, updated_at DESC NULLS LAST
    """)
    bets = _q("SELECT match FROM bets_history WHERE result = 'pending'")
    from src.utils.team_normalizer import normalize_team
    pending = set()
    for m in (bets["match"] if not bets.empty else []):
        if " vs " in str(m):
            h, a = str(m).split(" vs ", 1)
            pending.add(f"{normalize_team(h)} vs {normalize_team(a)}".lower())
    if up.empty:
        up = pd.DataFrame(columns=["home_team_norm", "away_team_norm", "home_team", "away_team",
                                   "home_odds", "draw_odds", "away_odds",
                                   "pin_home_odds", "pin_draw_odds", "pin_away_odds", "over25_odds",
                                   "under25_odds", "consensus_over25_odds", "consensus_under25_odds",
                                   "btts_yes_odds", "btts_no_odds", "consensus_home_odds",
                                   "consensus_draw_odds", "consensus_away_odds"])
    # el pick guardado de cada partido (v2.1): el que se ve es el que se mide
    # sin el horario viejo de un reprogramado ('stale', 6-nov-26) y, si hubiera
    # dos, el más reciente: la clave es el partido sin la fecha
    picks = _q("""
        SELECT match, label, family, market, model_prob, market_prob, playdoit_odds, reason, lineup, star_note,
               form_note
        FROM program_picks WHERE match_date BETWEEN NOW() - INTERVAL '3 hours' AND NOW() + INTERVAL '3 days'
          AND result IS DISTINCT FROM 'stale'
        ORDER BY picked_at NULLS FIRST
    """)
    saved = {}
    for p in (picks.to_dict("records") if not picks.empty else []):
        if " vs " in str(p["match"]):
            h, a = str(p["match"]).split(" vs ", 1)
            saved[f"{normalize_team(h)} vs {normalize_team(a)}".lower()] = p
    from src.models.program_picks import load_bias
    from src.features.espn_lineups import espn_leagues
    return jsonify({"ok": True, "matches": _vis.predictions(mvs, up, pending, normalize_team, saved, load_bias(),
                                                            set(espn_leagues()))})


# Titulares habituales de cada equipo (10-nov-26): salen de sus partidos ya
# terminados y no cambian entre una recarga y otra (5 min), pero se volvían a
# bajar de la base cada vez. Una copia por 30 min para los equipos de hoy.
_PROFILES_CACHE: dict = {}
PROFILES_TTL_S = 1800


def _cached_profiles(load, teams: list) -> dict:
    import time
    key = tuple(teams)
    hit = _PROFILES_CACHE.get(key)
    if hit is not None and time.time() - hit[0] < PROFILES_TTL_S:
        return hit[1]
    profiles = load(engine, teams)
    _PROFILES_CACHE.clear()                  # una sola copia: la de los equipos de hoy
    _PROFILES_CACHE[key] = (time.time(), profiles)
    return profiles


@app.route("/api/lineups")
def api_lineups():
    """Veredicto por partido cuando salen las alineaciones (v1.7.1)."""
    import json as _js
    from dashboard.lineup_verdict import lineup_verdicts
    from src.models.lineup_alerts import load_profiles
    fx = _q("""
        SELECT DISTINCT ON (home_team_norm, away_team_norm)
               home_team_norm, away_team_norm, home_team, away_team, sport_key, match_date,
               pin_home_odds, pin_draw_odds, pin_away_odds
        FROM upcoming_matches
        WHERE match_date BETWEEN NOW() - INTERVAL '2 hours' AND NOW() + INTERVAL '14 hours'
        ORDER BY home_team_norm, away_team_norm, updated_at DESC NULLS LAST
    """)
    if fx.empty:
        return _no_data("Sin partidos hoy")
    fx = fx.sort_values("match_date")
    teams = sorted(set(fx.home_team_norm) | set(fx.away_team_norm))
    load = lambda v: v if isinstance(v, (list, dict)) else _js.loads(v or "null")
    lu = _q("""
        SELECT team, kickoff, formation, starters, names FROM lineups
        WHERE team = ANY(:t) AND kickoff BETWEEN NOW() - INTERVAL '4 hours' AND NOW() + INTERVAL '16 hours'
    """, {"t": teams})
    lineups = {}
    for r in (lu.itertuples(index=False) if not lu.empty else []):
        lineups.setdefault(r.team, []).append({"kickoff": r.kickoff, "formation": r.formation,
                                               "starters": load(r.starters), "names": load(r.names)})
    try:
        profiles = _cached_profiles(load_profiles, teams)
    except Exception as e:
        app.logger.warning(f"perfiles de alineación: {e}")
        profiles = {}
    al = _q("""
        SELECT home, away, weakened, missing, market, min_american, analyst_verdict, analyst_reason
        FROM lineup_alerts WHERE kickoff BETWEEN NOW() - INTERVAL '4 hours' AND NOW() + INTERVAL '16 hours'
        ORDER BY alerted_at
    """) if teams else pd.DataFrame()
    alerts = [{**r, "missing": load(r.get("missing"))} for r in (al.to_dict("records") if not al.empty else [])]
    mv = _q("""
        SELECT DISTINCT ON (match) match, pin_home, pin_draw, pin_away FROM model_vs_sharp
        WHERE match_date BETWEEN NOW() - INTERVAL '4 hours' AND NOW() + INTERVAL '16 hours'
        ORDER BY match, created_at ASC
    """)
    morning = {str(r.match).lower(): [float(r.pin_home), float(r.pin_draw), float(r.pin_away)]
               for r in (mv.itertuples(index=False) if not mv.empty else [])}
    now = pd.Timestamp.now(tz="UTC")
    return jsonify({"ok": True, "matches": lineup_verdicts(fx, lineups, profiles, alerts, morning, now)})


@app.route("/api/predictions/history")
def api_predictions_history():
    d = _days()
    mvs = _q("""
        SELECT * FROM model_vs_sharp
        WHERE match_date BETWEEN NOW() - make_interval(days => :d) AND NOW() - INTERVAL '2 hours'
          AND skipped_at IS NULL AND low_history IS NULL
        ORDER BY match_date DESC
    """, {"d": d})
    if mvs.empty:
        return _no_data("Sin partidos analizados en esos días")
    res = _q("""
        SELECT LOWER(home_team) AS h, LOWER(away_team) AS a, date::date AS d, home_goals, away_goals
        FROM matches
        WHERE date >= CURRENT_DATE - make_interval(days => :d2) AND home_goals IS NOT NULL
    """, {"d2": d + 2})
    from src.utils.team_normalizer import normalize_team
    out = _vis.history(mvs, res if not res.empty else
                       pd.DataFrame(columns=["h", "a", "d", "home_goals", "away_goals"]),
                       normalize_team)
    return jsonify({"ok": True, "days": d, **out})


@app.route("/api/proposals")
def api_proposals():
    d = _days()
    df = _q("""
        SELECT DISTINCT ON (match, market) match, match_date, league, market, odds,
               probability, pin_prob, decision, result
        FROM model_proposals
        WHERE match_date >= NOW() - make_interval(days => :d)
        ORDER BY match, market, evaluated_at DESC
    """, {"d": d})
    if df.empty:
        return _no_data("Sin propuestas del modelo en esos días")
    df = df.sort_values("match_date", ascending=False)
    return jsonify({"ok": True, "days": d, **_vis.proposals(df)})


@app.route("/api/picks")
def api_picks():
    """Los picks del programa guardados (src/models/program_picks.py) y cómo
    terminaron: lo que el dueño ve en cada partido, medido."""
    d = _days(30)
    df = _q("""
        SELECT match, match_date, league, display, label, model_prob, market_prob, lean, min_us,
               playdoit_odds, value, result, profit
        FROM program_picks
        WHERE match_date >= NOW() - make_interval(days => :d) AND match_date < NOW()
        ORDER BY match_date DESC
    """, {"d": d})
    if df.empty:
        return _no_data("Los picks se guardan desde el 21-oct. Cada pick se califica unas horas después de que termina su partido (en la corrida de la noche o en la de las 6:30 am)")
    return jsonify({"ok": True, "days": d, **_vis.picks_board(df)})


@app.route("/api/match")
def api_match():
    """Partido abierto (v1.10.0): los últimos 5 de cada equipo con marcador,
    goles, córners, tarjetas y su once; la alineación del partido y los
    últimos 5 entre ellos. ?home=&away= con los nombres canónicos y
    ?date= la hora del partido (cuenta lo anterior a ella)."""
    from dashboard import match_detail as _md
    home = (request.args.get("home") or "").strip().lower()
    away = (request.args.get("away") or "").strip().lower()
    try:
        when = pd.Timestamp(request.args.get("date") or pd.Timestamp.now(tz="UTC"))
        when = when.tz_convert("UTC") if when.tzinfo else when.tz_localize("UTC")
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "fecha inválida"})
    if not home or not away:
        return jsonify({"ok": False, "msg": "falta el partido"})
    p = {"h": home, "a": away, "d": when.to_pydatetime()}
    matches = _q("""
        SELECT date, league, LOWER(home_team) AS h, LOWER(away_team) AS a, home_goals, away_goals,
               home_corners, away_corners, home_yellow, away_yellow, home_red, away_red
        FROM matches
        WHERE (LOWER(home_team) IN (:h, :a) OR LOWER(away_team) IN (:h, :a))
          AND home_goals IS NOT NULL AND date < :d AND date >= CAST(:d AS date) - 1100
        ORDER BY date DESC LIMIT 150
    """, p)
    lineups = _q("""
        SELECT team, kickoff, formation, starters, names FROM lineups
        WHERE team IN (:h, :a) AND kickoff BETWEEN CAST(:d AS timestamptz) - INTERVAL '400 days'
                                               AND CAST(:d AS timestamptz) + INTERVAL '1 day'
        ORDER BY kickoff DESC LIMIT 60
    """, p)
    extra = _espn_history(p)
    if matches.empty and lineups.empty and extra.empty:
        return _no_data("Sin partidos anteriores de estos equipos en la base")
    fill = _q("""
        SELECT kickoff AS date, home AS h, away AS a, home_goals, away_goals, home_corners, away_corners,
               home_cards, away_cards
        FROM live_scores WHERE status = 'final' AND (home IN (:h, :a) OR away IN (:h, :a)) AND kickoff < :d
          AND kickoff >= CAST(:d AS timestamptz) - INTERVAL '400 days'
    """, p, optional=True)
    detail = _md.match_detail(matches, lineups, home, away, when, extra, fill)
    view = _market_view(home, away, when)
    angle = _md.goals_angle(detail, view["markets"])
    if angle:
        angle["evidence"] = _goals_evidence()
    return jsonify({"ok": True, **detail, **view, "goals_angle": angle})


def _goals_evidence() -> dict | None:
    """Lo que midió el buscador de ventajas del lunes para "alguno recibe 2+
    por partido → más de 2.5" (v2.4.7): cuántos partidos, cuánto salió,
    cuánto esperaba el mercado y cuánto dejó apostarlo siempre."""
    st = _q("SELECT value FROM model_state WHERE key = 'edge_finder'", optional=True)
    if st.empty:
        return None
    v = st.iloc[0]["value"]
    h = ((_json.loads(v) if isinstance(v, str) else v) or {}).get("hypotheses", {}).get("alguno_goleable")
    p = (h or {}).get("pooled") or {}
    if not p.get("n"):
        return None
    return {"n": p["n"], "hit": p["hit"], "expected": p.get("expected"), "roi": p["roi"], "verdict": h["verdict"]}


def _market_view(home: str, away: str, when) -> dict:
    """Lo que vio el programa en cada mercado (v2.4.6): su último análisis del
    partido y la última foto de momios antes del inicio (o la de ahora)."""
    from src.models import program_picks as _pp
    from src.utils.team_normalizer import normalize_team
    mv = _q("""SELECT * FROM model_vs_sharp WHERE match = :m AND skipped_at IS NULL
               AND match_date BETWEEN CAST(:d AS timestamptz) - INTERVAL '12 hours'
                                  AND CAST(:d AS timestamptz) + INTERVAL '36 hours'
               ORDER BY created_at DESC LIMIT 1""", {"m": f"{home} vs {away}", "d": when.to_pydatetime()})
    if mv.empty:
        return {"markets": [], "picked": None}
    r = mv.iloc[0].to_dict()
    kick = pd.Timestamp(r["match_date"])
    kick = kick.tz_convert("UTC") if kick.tzinfo else kick.tz_localize("UTC")
    before = min(kick, pd.Timestamp.now(tz="UTC"))
    snaps = _q(_pp._SNAPS_SQL, {"a": (kick - pd.Timedelta(hours=3)).to_pydatetime(),
                                "b": (kick + pd.Timedelta(hours=3)).to_pydatetime()})
    snap = _pp._snap_before(_pp._snap_index(snaps, normalize_team), _pp.match_key(home, away, normalize_team),
                            kick, before) if not snaps.empty else None
    from types import SimpleNamespace
    u = SimpleNamespace(**{"pin_total_line": None, "pin_total_over_odds": None, "pin_total_under_odds": None,
                           "corners_line": None, "cards_line": None, **snap}) if snap else None
    pk = _q("""SELECT market FROM program_picks WHERE match = :m
               AND match_date BETWEEN CAST(:k AS timestamptz) - INTERVAL '3 hours' AND CAST(:k AS timestamptz) + INTERVAL '3 hours'
               ORDER BY picked_at DESC NULLS LAST LIMIT 1""", {"m": f"{home} vs {away}", "k": kick.to_pydatetime()})
    return {"markets": _pp.market_view(r, u, bias=_pp.load_bias()),
            "picked": (pk.iloc[0]["market"] if not pk.empty else None)}


def _espn_history(p: dict) -> pd.DataFrame:
    """Copas, Champions, Europa y Conference League y amistosos de los dos
    equipos (espn_matches, src/features/espn_history.py), con las columnas
    de matches. Vacío si la tabla todavía no existe."""
    return _q("""
        SELECT kickoff AS date, competition AS league, kind, home AS h, away AS a, home_goals, away_goals,
               home_corners, away_corners, home_cards, away_cards
        FROM espn_matches
        WHERE (home IN (:h, :a) OR away IN (:h, :a)) AND kickoff < :d
          AND kickoff >= CAST(:d AS timestamptz) - INTERVAL '400 days'
        ORDER BY kickoff DESC LIMIT 60
    """, p)


# ─────────────────────────────────────────────────────────────
# DASHBOARD v2.0 (22-oct-26): partidos, equipos y gráficas
# ─────────────────────────────────────────────────────────────
_COVERED = "(SELECT league FROM model_vs_sharp UNION SELECT league FROM upcoming_matches)"


def _since() -> str:
    from dashboard.board import SINCE
    return SINCE


_PAST_CACHE = {"at": 0.0, "rows": None}
PAST_TTL_S = 300          # ~5 s de consultas; "Anteriores" y "Gráficas" las comparten


def _past_rows() -> list:
    """Partidos jugados desde SINCE en las ligas que analiza el programa,
    con lo que dijo el programa, su pick y las apuestas reales (caché de 5 min).
    `?fresh=1` (el botón "Actualizar", v2.5.3) se la salta: si no, el botón
    podía devolver lo mismo hasta 5 min."""
    import time
    fresh = request.args.get("fresh") == "1"
    if not fresh and _PAST_CACHE["rows"] is not None and time.time() - _PAST_CACHE["at"] < PAST_TTL_S:
        return _PAST_CACHE["rows"]
    rows = _load_past_rows()
    if rows and not g.get("partial"):        # a medias no se guarda
        _PAST_CACHE.update(at=time.time(), rows=rows)
    return rows


def _load_past_rows() -> list:
    from dashboard import board as _b
    from src.utils.team_normalizer import normalize_team
    p = {"s": _since()}
    matches = _q(f"""
        SELECT date, league, LOWER(home_team) AS h, LOWER(away_team) AS a, home_goals, away_goals,
               home_corners, away_corners, home_yellow, away_yellow, home_red, away_red
        FROM matches
        WHERE date >= CAST(:s AS date) AND home_goals IS NOT NULL AND league IN {_COVERED}
        ORDER BY date DESC
    """, p)
    mvs = _q("""SELECT match, match_date, p_home, p_draw, p_away, pin_home, pin_draw, pin_away, low_history
                FROM model_vs_sharp WHERE match_date >= CAST(:s AS date) - 2 AND skipped_at IS NULL""", p)
    picks = _q("""SELECT match, match_date, league, label, family, market, reason, model_prob, market_prob, result,
                         profit, source, playdoit_odds, star_note, form_note
                  FROM program_picks WHERE match_date >= CAST(:s AS date) - 2""", p)
    bets = _q("""SELECT match, match_date, market, result, profit, stake FROM bets_history
                 WHERE match_date >= CAST(:s AS date) - 2""", p)
    # marcador de ESPN (v2.4.4): el de los que ya empezaron y, desde v2.5.1, los
    # córners y tarjetas que `matches` no tiene (closing y evening la llenan)
    live = _q("""SELECT home, away, kickoff, home_goals, away_goals, home_corners, away_corners,
                        home_cards, away_cards, status, clock
                 FROM live_scores WHERE kickoff >= CAST(:s AS date) - 2""", p, optional=True)
    return _b.past_matches(matches, mvs, picks, bets, normalize_team, live=live)


@app.route("/api/board/past")
def api_board_past():
    from dashboard import board as _b
    rows = _past_rows()
    if not rows:
        return _no_data("Sin partidos jugados desde septiembre")
    return jsonify({"ok": True, "since": _since(), "rows": rows, "summary": _b.summary(rows),
                    "weekly": _b.weekly_accuracy(rows)})


@app.route("/api/board/results")
def api_board_results():
    """Ganadas y perdidas por día, por tipo de pick y por liga (v2.5.3)."""
    from dashboard import board as _b
    rows = _past_rows()
    if not rows:
        return _no_data("Sin partidos jugados desde septiembre")
    return jsonify({"ok": True, "since": _since(), **_b.results_board(rows)})


@app.route("/api/board/leagues")
def api_board_leagues():
    """Por liga: ganados y perdidos de cada liga con sus partidos, de hoy y
    desde el 22-oct (v2.10.0)."""
    from dashboard import board as _b
    rows = _past_rows()
    if not rows:
        return _no_data("Sin partidos jugados desde septiembre")
    now = pd.Timestamp.now(tz="UTC")
    start = pd.Timestamp(_b._mx_day(now), tz="UTC") + pd.Timedelta(hours=6)     # 00:00 de México
    up = _q("""SELECT match, match_date, league, label, reason FROM program_picks
               WHERE match_date > :n AND match_date < :e AND market IS NOT NULL AND result IS NULL""",
            {"n": now.to_pydatetime(), "e": (start + pd.Timedelta(days=1)).to_pydatetime()}, optional=True)
    upcoming = []
    for p in (up.to_dict("records") if up is not None and not up.empty else []):
        when = pd.Timestamp(p["match_date"])
        when = when.tz_convert("UTC") if when.tzinfo else when.tz_localize("UTC")
        if not now < when < start + pd.Timedelta(days=1):
            continue
        upcoming.append({"match": match_name(p["match"]), "league": league_name(p.get("league")),
                         "type": p.get("label"), "rule": not (p.get("reason") or ""),
                         "time": (when - pd.Timedelta(hours=6)).strftime("%H:%M")})
    return jsonify({"ok": True, **_b.league_board(rows, now=now, upcoming=upcoming)})


@app.route("/api/board/parlay")
def api_board_parlay():
    """Los parlays con bono (v2.9.0; la pestaña nació en v2.6.0): las piernas
    las guarda el pipeline (src/models/program_parlays.py) y dashboard/parlay.py
    arma cada parlay con su bono y su pago anticipado."""
    from dashboard import parlay as _px
    from src.features.espn_lineups import espn_leagues
    from src.models import program_parlays as _pl
    from src.utils.team_normalizer import normalize_team
    # la tabla nace con el primer parlay que guarda el pipeline; * porque las columnas
    # (slot de v2.7.0, rule/kind/seq/pa de v2.9.0) las agrega el pipeline
    legs = _q("SELECT * FROM program_parlays ORDER BY day DESC, rank", optional=True)
    if g.get("db_down"):
        return _no_data()
    if not legs.empty and "rule" in legs.columns:
        legs = legs[legs["rule"] == _pl.RULE]
    elif not legs.empty:
        legs = legs.iloc[0:0]
    live = official = None
    open_legs = legs[legs["result"].isna()] if not legs.empty else legs
    if not open_legs.empty:                     # sin liquidar: el marcador de ESPN o el oficial
        p = {"d": (pd.to_datetime(open_legs["match_date"], utc=True).min() - pd.Timedelta(days=2)).to_pydatetime()}
        live = _q("""SELECT * FROM live_scores WHERE kickoff >= :d""", p, optional=True)
        official = _q("""SELECT date, LOWER(home_team) AS h, LOWER(away_team) AS a, home_goals, away_goals,
                                home_corners, away_corners, home_yellow, away_yellow, home_red, away_red
                         FROM matches WHERE date >= CAST(:d AS date) AND home_goals IS NOT NULL""", p)
    board = _px.parlay_board(legs, live, normalize=normalize_team, official=official,
                             covered=set(espn_leagues()))
    return jsonify({"ok": True, **board,
                    "rules": {"strong": _pl.STRONG_FAV, "price_band": _pl.PRICE_BAND,
                              "min_odds_us": _odds_us(_pl.MIN_LEG_ODDS),
                              "day_min": _pl.DAY_MIN_LEGS, "slot_min": _pl.SLOT_MIN_LEGS, "max_legs": _pl.MAX_LEGS,
                              "bonus": {str(k): v for k, v in _pl.BONUS.items()},
                              "block_minutes": _pl.BLOCK_MINUTES,
                              "leagues": [league_name(x) for x in _pl.LEAGUES]}})


@app.route("/api/board/teams")
def api_board_teams():
    from dashboard import board as _b
    m = _q(f"""
        SELECT league, LOWER(home_team) AS h, LOWER(away_team) AS a FROM matches
        WHERE date >= CAST(:s AS date) AND home_goals IS NOT NULL AND league IN {_COVERED}
    """, {"s": _since()})
    if m.empty:
        return _no_data("Sin equipos con partidos desde septiembre")
    return jsonify({"ok": True, "teams": _b.team_list(m)})


@app.route("/api/board/team")
def api_board_team():
    from dashboard import board as _b
    t = (request.args.get("name") or "").strip().lower()
    if not t:
        return jsonify({"ok": False, "msg": "falta el equipo"})
    p = {"s": _since(), "t": t, "t1": f"{t} vs %", "t2": f"% vs {t}"}
    m = _q("""
        SELECT date, league, LOWER(home_team) AS h, LOWER(away_team) AS a, home_goals, away_goals,
               home_corners, away_corners, home_yellow, away_yellow, home_red, away_red
        FROM matches
        WHERE date >= CAST(:s AS date) AND home_goals IS NOT NULL
          AND (LOWER(home_team) = :t OR LOWER(away_team) = :t)
    """, p)
    up = _q("""
        SELECT match, match_date, league, p_home, p_draw, p_away FROM model_vs_sharp
        WHERE match_date >= NOW() AND (match LIKE :t1 OR match LIKE :t2) AND skipped_at IS NULL
    """, p)
    extra = _q("""
        SELECT kickoff AS date, competition AS league, kind, home AS h, away AS a, home_goals, away_goals,
               home_corners, away_corners, home_cards, away_cards
        FROM espn_matches WHERE kickoff >= CAST(:s AS date) AND (home = :t OR away = :t)
    """, p)
    if m.empty and up.empty and extra.empty:
        return _no_data("Sin partidos de este equipo desde septiembre")
    finals = _q("""SELECT home, away, kickoff, home_goals, away_goals, home_corners, away_corners,
                          home_cards, away_cards, status
                   FROM live_scores WHERE status = 'final' AND kickoff >= CAST(:s AS date) - 2
                     AND (home = :t OR away = :t)""", p, optional=True)
    if not m.empty and not finals.empty:                      # córners y tarjetas de ESPN (v2.5.1)
        m = pd.DataFrame(_b.fill_from_espn(m.to_dict("records"), finals.to_dict("records")))
    return jsonify({"ok": True, **_b.team_view(t, m, up, extra)})


@app.route("/api/board/header")
def api_board_header():
    """El encabezado (v2.11.0, 8-nov-26): los 🎯 ya cobrados desde el 22-oct y
    lo que dejan apostando 50, lo mismo que la noche de Telegram y "Por liga".
    Hasta v2.10.0 decía "Bankroll 63.4u" de la tabla bankroll, que dejó de
    moverse con el modo colección (25-sep-26): el dueño, "arregla también lo
    del bankroll del encabezado"."""
    from src.models import telegram_reports as TR
    rows = _q(TR.SINCE_SQL, {"s": TR.FIXED_SINCE.to_pydatetime(), "n": pd.Timestamp.now(tz="UTC").to_pydatetime()})
    if g.get("db_down"):
        return _no_data()
    budget = _budget_status()          # 11-nov-26: en modo ahorro la página se actualiza cada 15 min
    return jsonify({"ok": True, "since": TR.FIXED_SINCE.strftime("%Y-%m-%d"), **TR.record(rows.to_dict("records")),
                    "budget_level": budget.get("level", "normal")})


@app.route("/api/board/charts")
def api_board_charts():
    """Las gráficas. v2.11.0: sin la del bankroll (bankroll_history dejó de
    moverse el 25-sep-26 y salía una línea plana)."""
    from dashboard import board as _b
    props = _q("""
        SELECT DISTINCT ON (match, market) match_date, market, odds, result
        FROM model_proposals ORDER BY match, market, evaluated_at DESC
    """)
    rows = _past_rows()
    return jsonify({"ok": True, "picks": _b.picks_series(rows),
                    "accuracy": _b.weekly_accuracy(rows), "proposals": _vis.proposal_weeks(props),
                    "families": _b.summary(rows)["families"]})


@app.route("/api/scoreboard")
def api_scoreboard():
    """Dinero real por etapa y lo que el modelo propuso por semana."""
    bets = _q("SELECT match_date, result, stake, profit FROM bets_history")
    props = _q("""
        SELECT DISTINCT ON (match, market) match_date, market, odds, result
        FROM model_proposals
        ORDER BY match, market, evaluated_at DESC
    """)
    if bets.empty and props.empty:
        return _no_data("Sin apuestas ni propuestas todavía")
    last = None
    if not bets.empty:
        played = bets[bets["result"].isin(_vis.PLAYED)]
        if not played.empty:
            last = _vis._iso(pd.to_datetime(played["match_date"], utc=True).max())
    return jsonify({"ok": True,
                    "real": _vis.real_periods(bets) if not bets.empty else [],
                    "last_real_bet": last,
                    "weeks": _vis.proposal_weeks(props)})


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
    """, optional=True)                          # la tabla todavía no existe en producción
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
<title>Apuestas</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>window.APP_V = "__V__";</script>
<script src="/chartjs.js"></script>
<style>
  :root { --bg:#0f1420; --card:#1a2233; --card2:#222c42; --line:#2a3550; --text:#e2e8f0; --muted:#94a3b8;
          --green:#22c55e; --red:#ef4444; --yellow:#fbbf24; --blue:#60a5fa; --home:#3b82f6; --draw:#64748b; --away:#f97316; }
  * { box-sizing:border-box; margin:0; padding:0; font-family:'Segoe UI',system-ui,sans-serif; }
  body { background:var(--bg); color:var(--text); padding:16px; max-width:1250px; margin:0 auto; font-size:14px; }
  .muted { color:var(--muted); } .small { font-size:.78rem; }
  .pos { color:var(--green); } .neg { color:var(--red); }
  header { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:10px; }
  header h1 { font-size:1.25rem; margin-right:auto; }
  .chip { background:var(--card); border-radius:99px; padding:5px 12px; font-size:.82rem; white-space:nowrap; }
  .chip b { font-weight:700; }
  .chip.rf { border:1px solid var(--line); color:var(--text); cursor:pointer; font:inherit; font-size:.82rem; }
  .chip.rf:hover { background:var(--card2); } .chip.rf:disabled { opacity:.6; cursor:default; }
  #updated.chg { color:var(--green); font-weight:600; }
  .tabs { display:flex; gap:4px; border-bottom:1px solid var(--line); margin-bottom:14px; overflow-x:auto; }
  .tab { background:none; border:none; color:var(--muted); padding:10px 16px; font-size:.95rem; cursor:pointer;
         border-bottom:3px solid transparent; }
  .tab:hover { color:var(--text); }
  .tab.on { color:var(--text); border-bottom-color:var(--blue); font-weight:600; }
  .pane { display:none; } .pane.on { display:block; }
  .bar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }
  .pill { background:var(--card); border:1px solid var(--line); color:var(--muted); border-radius:99px; padding:5px 12px;
          font-size:.82rem; cursor:pointer; }
  .pill.on { background:var(--blue); border-color:var(--blue); color:#fff; }
  select, input[type=text] { background:var(--card); color:var(--text); border:1px solid var(--line); border-radius:8px; padding:6px 10px; font-size:.85rem; }
  .day { color:var(--muted); font-weight:600; font-size:.78rem; text-transform:uppercase; letter-spacing:.6px; margin:16px 0 6px; }
  /* partido próximo */
  .m { background:var(--card); border-radius:10px; margin-bottom:6px; }
  .mr { display:grid; grid-template-columns:58px minmax(0,1fr) 230px minmax(0,1fr) 190px 34px; gap:10px; align-items:center;
        padding:10px 14px; cursor:pointer; }
  .mr:hover { background:var(--card2); border-radius:10px; }
  .t { font-weight:600; } .t.h { text-align:right; }
  .time { font-weight:600; } .time .small { font-weight:400; display:block; }
  .pb { display:flex; height:22px; border-radius:6px; overflow:hidden; font-size:.72rem; font-weight:600; color:#fff; }
  .pb div { display:flex; align-items:center; justify-content:center; min-width:0; overflow:hidden; }
  .pb .h { background:var(--home); } .pb .d { background:var(--draw); } .pb .a { background:var(--away); }
  .pb.thin { height:8px; margin-top:3px; opacity:.75; font-size:0; }
  .pk { font-size:.8rem; line-height:1.3; }
  .pk b { font-size:.85rem; }
  .ic { font-size:1.15rem; text-align:center; }
  .det { display:none; padding:4px 14px 14px; }
  .m.open .det { display:block; }
  /* detalle */
  .two { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  .tp { background:var(--card2); border-radius:10px; padding:10px 12px; }
  .tp h3 { font-size:.95rem; margin-bottom:6px; }
  .form { display:flex; gap:4px; margin-bottom:6px; }
  .f { width:22px; height:22px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:.7rem; font-weight:700; color:#fff; }
  .f.G { background:var(--green); } .f.E { background:var(--draw); } .f.P { background:var(--red); }
  table { width:100%; border-collapse:collapse; font-size:.8rem; }
  th { text-align:left; color:var(--muted); font-weight:600; padding:5px 6px; border-bottom:1px solid var(--line); white-space:nowrap; }
  td { padding:5px 6px; border-bottom:1px solid #26304a; }
  .xi { color:var(--muted); font-size:.75rem; margin-top:6px; line-height:1.4; }
  .h2h { margin-top:10px; font-size:.8rem; }
  /* anteriores */
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin-bottom:12px; }
  .k { background:var(--card); border-radius:10px; padding:10px 14px; }
  .k .v { font-size:1.5rem; font-weight:700; } .k .l { color:var(--muted); font-size:.75rem; }
  .tbl { background:var(--card); border-radius:10px; overflow:hidden; }
  .tbl table { font-size:.84rem; }
  .tbl tr.dh td { background:#151c2b; color:var(--muted); font-weight:600; font-size:.72rem; text-transform:uppercase; letter-spacing:.5px; }
  .tbl tr.r { cursor:pointer; } .tbl tr.r:hover td { background:var(--card2); }
  .sc { font-weight:700; text-align:center; white-space:nowrap; }
  .more { display:block; margin:10px auto; background:var(--card); color:var(--text); border:1px solid var(--line); border-radius:8px; padding:8px 18px; cursor:pointer; }
  /* equipos */
  .teams { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; max-height:180px; overflow-y:auto; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:12px 0; }
  .cb { background:var(--card); border-radius:10px; padding:10px 12px; height:260px; position:relative; }
  .cb h3 { font-size:.85rem; color:var(--muted); margin-bottom:6px; font-weight:600; }
  .cb canvas { width:100%!important; height:215px!important; }
  .cb.wide { height:300px; margin:12px 0; } .cb.wide canvas { height:255px!important; }
  .cb.free { height:auto; margin:12px 0; } .chartbox { position:relative; height:300px; }
  .chartbox canvas { height:100%!important; }
  tr.few td { color:var(--muted); } td.nw { white-space:nowrap; color:var(--muted); }
  /* parlay */
  .xsum { padding:10px 14px; font-size:.9rem; display:flex; gap:14px; flex-wrap:wrap; align-items:center; }
  .xsum b.big { font-size:1.25rem; }
  .tbl tr.out td { opacity:.45; }
  /* por liga */
  #lhead th { cursor:pointer; user-select:none; white-space:normal; vertical-align:bottom; } #lhead th.on { color:var(--text); }
  #tab-ligas .tbl { overflow-x:auto; }
  .wl { display:inline-flex; width:64px; height:8px; border-radius:4px; overflow:hidden; vertical-align:middle;
        background:var(--line); margin-right:6px; }
  .wl .w { background:var(--green); } .wl .l { background:var(--red); }
  .tbl tr.sub td { background:#151c2b; font-size:.8rem; padding:4px 6px 4px 26px; }
  .tbl tr.tot td { font-weight:700; border-top:2px solid var(--line); }
  #err { color:var(--red); font-size:.85rem; margin:8px 0; display:none; }
  @media (max-width:900px) {
    body { padding:10px; }
    .mr { grid-template-columns:48px minmax(0,1fr) minmax(0,1fr); }
    .mr .pbw { grid-column:1 / -1; order:5; }
    .mr .pk { grid-column:1 / 3; order:6; } .mr .ic { order:7; }
    .two, .grid2 { grid-template-columns:1fr; }
    #tab-ligas th, #tab-ligas td { padding:5px 3px; } #tab-ligas tr.sub td { padding-left:14px; }
    .hide-sm { display:none; }
  }
</style></head><body>
<header>
  <h1>⚽ Apuestas <span id="ver" class="muted small"></span></h1>
  <span class="chip" id="chiptarget">🎯 …</span>
  <span class="chip" id="chiptoday">Hoy …</span>
  <button class="chip rf" id="refresh" title="Vuelve a leer todo ahora (solo se actualiza sola cada 5 min)">🔄 Actualizar</button>
  <span class="small muted" id="updated"></span>
</header>
<div id="err"></div>
<div class="tabs" id="tabs">
  <button class="tab" data-tab="proximos">Próximos</button>
  <button class="tab" data-tab="parlay">Parlay</button>
  <button class="tab" data-tab="anteriores">Anteriores</button>
  <button class="tab" data-tab="resultados">Ganadas y perdidas</button>
  <button class="tab" data-tab="ligas">Por liga</button>
  <button class="tab" data-tab="equipos">Equipos</button>
  <button class="tab" data-tab="graficas">Gráficas</button>
</div>

<div class="pane" id="tab-proximos">
  <div class="bar" id="daybar"></div>
  <div class="bar">
    <select id="uleague"><option value="">Todas las ligas</option></select>
    <select id="ufam"><option value="">Todas las apuestas</option><option>Ganador</option><option>Goles</option><option>Ambos anotan</option><option>Córners</option><option>Tarjetas</option></select>
    <span class="small muted" style="margin-left:auto">De cada 100: <span style="color:var(--home)">■</span> gana el local <span style="color:var(--draw)">■</span> empate <span style="color:var(--away)">■</span> gana el visitante · barra gruesa: el programa · delgada: Pinnacle</span>
  </div>
  <div class="small muted" style="margin:0 0 10px">🎯 = <b>apuesta recomendada</b>: un equipo muy favorito (según Pinnacle, la casa más precisa, gana 65 de cada 100 partidos así o más) que paga -500 o mejor en PlayDoit, en una liga con pago anticipado. Si gana menos de 70 de cada 100, solo cuando su momio llega al mínimo que se muestra: más barato, a la larga, se pierde. Los partidos sin 🎯 muestran lo que más le gusta al programa, solo para seguirlo, y dicen por qué no son 🎯.</div>
  <div id="uplist" class="muted">Cargando…</div>
</div>

<div class="pane" id="tab-parlay">
  <div class="small muted" id="xrule" style="margin-bottom:8px"></div>
  <div id="xnext" class="muted">Cargando…</div>
  <div class="day">Cómo le ha ido</div>
  <div class="kpis" id="xkpis"></div>
  <div class="tbl"><table><thead><tr><th>Día</th><th>Parlay</th><th>Partidos</th><th>Momio</th><th>Resultado</th></tr></thead>
    <tbody id="xhist"></tbody></table></div>
</div>

<div class="pane" id="tab-resultados">
  <div class="bar" id="rgroups">
    <span class="pill on" data-g="picks" title="Todos los picks del programa desde el 22 oct">🤖 Todos los picks</span>
    <span class="pill" data-g="rule" title="Solo las apuestas recomendadas (🎯)">🎯 Recomendados</span>
    <span class="pill" data-g="bets" title="Las apuestas con dinero real registradas">💰 Apuestas reales</span>
    <span class="small muted" style="margin-left:auto">Solo partidos ya terminados y cobrados · la ganancia es apostando 50 a cada uno al momio de PlayDoit</span>
  </div>
  <div class="kpis" id="rkpis"></div>
  <div class="cb wide"><h3>Ganadas y perdidas por día · ganancia acumulada apostando 50 a cada uno</h3><canvas id="cdaily"></canvas></div>
  <div class="grid2">
    <div class="tbl"><table><thead><tr><th>Tipo de apuesta</th><th title="Ganadas - perdidas">G-P</th><th>Acierto</th><th title="Apostando 50 a cada uno">Ganancia</th></tr></thead>
      <tbody id="rtypes"></tbody></table></div>
    <div class="tbl"><table><thead><tr><th>Liga</th><th title="Ganadas - perdidas">G-P</th><th>Acierto</th><th title="Apostando 50 a cada uno">Ganancia</th></tr></thead>
      <tbody id="rleagues"></tbody></table></div>
  </div>
  <div class="cb free"><h3>Ganancia por liga, apostando 50 a cada uno</h3><div class="chartbox" id="cleaguesbox"><canvas id="cleagues"></canvas></div></div>
  <div class="grid2">
    <div class="tbl"><table><thead><tr><th colspan="4">✅ Mejores aciertos</th></tr></thead><tbody id="rbest"></tbody></table></div>
    <div class="tbl"><table><thead><tr><th colspan="4">❌ Fallos en los que el programa estaba más seguro</th></tr></thead><tbody id="rworst"></tbody></table></div>
  </div>
</div>

<div class="pane" id="tab-ligas">
  <div class="bar" id="lperiods">
    <span class="pill on" data-p="today" title="Los partidos de hoy: los que ya terminaron y los que faltan">📅 Hoy</span>
    <span class="pill" data-p="since" title="Todos los partidos ya cobrados desde el 22 oct">📊 Desde el 22 oct</span>
  </div>
  <div class="bar" id="lgroups">
    <span class="pill on" data-g="picks" title="El pick de cada partido que analizó el programa">🤖 Todos los partidos</span>
    <span class="pill" data-g="rule" title="Solo las apuestas recomendadas">🎯 Solo recomendados</span>
  </div>
  <div class="small muted" id="lnote" style="margin:0 0 10px"></div>
  <div class="kpis" id="lkpis"></div>
  <div class="tbl"><table><thead><tr id="lhead"></tr></thead>
    <tbody id="lbody"><tr><td colspan="6" class="muted">Cargando…</td></tr></tbody></table></div>
</div>

<div class="pane" id="tab-anteriores">
  <div class="kpis" id="pkpis"></div>
  <div class="bar">
    <select id="pleague"><option value="">Todas las ligas</option></select>
    <input type="text" id="pteam" placeholder="Buscar equipo">
    <span class="pill" id="ponly" title="Solo los partidos que analizó el programa">🤖 Solo analizados</span>
  </div>
  <div class="tbl"><table><thead><tr><th class="hide-sm">Liga</th><th style="text-align:right">Local</th><th></th><th>Visitante</th>
    <th title="Córners local - visitante">Córners</th><th title="Tarjetas del partido (roja = 1)" class="hide-sm">Tarjetas</th>
    <th title="¿Pasó lo que el programa veía más probable?">🤖</th><th title="¿Pasó lo que Pinnacle veía más probable?">🏦</th>
    <th title="Pick del programa">Pick</th><th title="Apuestas con dinero real" class="hide-sm">Apuesta</th></tr></thead>
    <tbody id="pastbody"><tr><td colspan="10" class="muted">Cargando…</td></tr></tbody></table></div>
  <button class="more" id="pmore" style="display:none">Ver más</button>
</div>

<div class="pane" id="tab-equipos">
  <div class="bar">
    <select id="tleague"><option value="">Todas las ligas</option></select>
    <input type="text" id="tsearch" placeholder="Buscar equipo">
  </div>
  <div class="teams" id="teamlist"></div>
  <div id="teamview"></div>
</div>

<div class="pane" id="tab-graficas">
  <div class="grid2">
    <div class="cb"><h3>Ganancia acumulada de los picks, apostando 50 a cada uno</h3><canvas id="cpicks"></canvas></div>
    <div class="cb"><h3>% de veces que ganó el favorito, por semana</h3><canvas id="cacc"></canvas></div>
  </div>
  <div class="grid2">
    <div class="cb"><h3>Ganancia por tipo de apuesta desde el 22 oct, apostando 50 a cada uno</h3><canvas id="cfam"></canvas></div>
    <div class="cb"><h3>Ganancia por semana de las apuestas que propone el modelo, apostando 50 a cada una</h3><canvas id="cprops"></canvas></div>
  </div>
</div>
<script src="/ui_es5.js?v=__V__"></script></body></html>""".replace("__HOW_TO_READ__", HOW_TO_READ)


@app.route("/")
def index():
    # ?v=versión: tras actualizar el .exe el navegador no reusa el JS viejo
    # guardado en caché (página nueva con código viejo = secciones vacías).
    # no-store + la recarga sola al cambiar la versión (ui_es5.js): el 21-oct
    # el dueño siguió viendo la página vieja en una pestaña abierta antes.
    resp = app.make_response(PAGE.replace("__V__", _app_version()))
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
