"""
src/models/program_picks.py
===========================
El pick del programa en cada partido y cómo terminó (21-oct-26).

El dashboard mostraba el pick de cada partido (lo que jugaría el programa,
v1.7.2) pero no se guardaba en ningún lado: upcoming_matches se poda a los
~7 días y model_vs_sharp se sobrescribe en cada corrida. El dueño preguntó
"cómo nos fue con las apuestas de nuestros modelos" y no había respuesta.

  - pick(): la regla (antes vivía en dashboard/visibility.program_pick).
  - build_picks() / snapshot_picks(): el pick de los próximos partidos con
    los momios del momento. Corre en morning, evening y cada closing; el
    último antes del partido es el que cuenta.
  - resolve_picks(): los liquida con la misma función que las apuestas, a
    1u y al momio estimado de PlayDoit (donde apuesta el dueño). Sin ese
    estimado (ambos anotan no tiene promedio de casas) cuenta en ganados y
    perdidos pero no en unidades.
"""

import math

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger
from src.utils.min_odds import min_american, to_american

log = get_logger(__name__)

PICK_MIN_GAP = 0.03      # el programa tiene que ver 3+ pts más que el mercado para "preferir" algo
SNAPSHOT_HOURS = 72      # los mismos 3 días que muestra el dashboard

# (lado, columna de model_vs_sharp, columna de Pinnacle, mejor momio, promedio, mercado, nombre)
SIDES = (("home", "p_home", "pin_home", "home_odds", "consensus_home_odds", "home_win", "Gana el local"),
         ("draw", "p_draw", "pin_draw", "draw_odds", "consensus_draw_odds", "draw", "Empate"),
         ("away", "p_away", "pin_away", "away_odds", "consensus_away_odds", "away_win", "Gana el visitante"))

PICKS_DDL = """
    CREATE TABLE IF NOT EXISTS program_picks (
        id SERIAL PRIMARY KEY,
        match TEXT NOT NULL, match_date TIMESTAMPTZ NOT NULL, league TEXT, display TEXT,
        market TEXT NOT NULL, label TEXT, model_prob NUMERIC, market_prob NUMERIC,
        lean BOOLEAN, min_us INT, best_odds NUMERIC, playdoit_odds NUMERIC, value BOOLEAN,
        picked_at TIMESTAMPTZ DEFAULT NOW(),
        result TEXT, profit NUMERIC, resolved_at TIMESTAMPTZ,
        UNIQUE (match, match_date)
    )
"""
# 'reconstruido': hecho después con la predicción guardada antes del partido
# (backfill_picks); NULL = guardado en su momento por snapshot_picks
PICKS_ALTER = "ALTER TABLE program_picks ADD COLUMN IF NOT EXISTS source TEXT"
_COLS = ("match", "match_date", "league", "display", "market", "label", "model_prob", "market_prob",
         "lean", "min_us", "best_odds", "playdoit_odds", "value")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def devig2(a, b) -> float | None:
    """Probabilidad sin margen del primer lado de un mercado de dos."""
    a, b = _num(a), _num(b)
    if not a or not b or a <= 1 or b <= 1:
        return None
    return (1 / a) / (1 / a + 1 / b)


def fair_1x2(u) -> list | None:
    """1X2 de Pinnacle sin margen con la última recarga de momios (cada 30
    min en el closing); el de model_vs_sharp es el de la última corrida."""
    if u is None:
        return None
    odds = [_num(getattr(u, c, None)) for c in ("pin_home_odds", "pin_draw_odds", "pin_away_odds")]
    if not all(o and o > 1 for o in odds):
        return None
    inv = [1 / o for o in odds]
    return [x / sum(inv) for x in inv]


def playdoit_estimate(consensus, ratio=None):
    """Momio estimado de PlayDoit: el promedio de las casas por lo que PlayDoit
    paga frente al promedio, aprendido de las capturas (playdoit_prices);
    sin capturas suficientes, el promedio tal cual (medido el 19-oct-26:
    PlayDoit ≈ promedio). Decimal o None."""
    c = _num(consensus)
    if not c or c <= 1:
        return None
    r = _num(ratio) or 1.0
    return max(1.01, c * r)


def pick(model: list, pin: list, p_over25, p_btts, u, ratio=None) -> dict | None:
    """
    Lo que jugaría el programa en el partido: entre 1X2, más/menos 2.5 y
    ambos anotan, el mercado donde ve más que el mercado sin margen
    (Pinnacle en el 1X2; el promedio de las casas en goles y ambos anotan).
    Con menos de 3 pts de diferencia no prefiere nada: lo más probable del
    1X2 (lean=False). Momios en decimal; None sin datos.
    """
    cands = []
    g = (lambda col: getattr(u, col, None) if u is not None and col else None)
    for (_, _, _, odds_col, cons_col, market, label), m, p in zip(SIDES, model, pin):
        if m is not None and p is not None:
            cands.append((market, label, m, p, g(odds_col), g(cons_col)))
    p_over = _num(p_over25)
    if p_over is not None and u is not None:
        mk = devig2(g("consensus_over25_odds"), g("consensus_under25_odds")) \
            or devig2(g("over25_odds"), g("under25_odds"))
        if mk is not None:
            cands.append(("over25", "Más de 2.5 goles", p_over, mk, g("over25_odds"), g("consensus_over25_odds")))
            cands.append(("under25", "Menos de 2.5 goles", 1 - p_over, 1 - mk, g("under25_odds"),
                          g("consensus_under25_odds")))
    pb = _num(p_btts)
    if pb is not None and u is not None:
        mk = devig2(g("btts_yes_odds"), g("btts_no_odds"))
        if mk is not None:
            cands.append(("btts", "Ambos anotan", pb, mk, g("btts_yes_odds"), None))
            cands.append(("btts_no", "No anotan ambos", 1 - pb, 1 - mk, g("btts_no_odds"), None))
    if not cands:
        return None
    market, label, m, mk, best, cons = max(cands, key=lambda c: c[2] - c[3])
    lean = m - mk >= PICK_MIN_GAP
    if not lean:
        market, label, m, mk, best, cons = max(cands[:3], key=lambda c: c[2])
    min_us = min_american(m, pin_prob=mk)
    best_us = to_american(_num(best)) if _num(best) else None
    est = playdoit_estimate(cons, ratio)
    est_us = to_american(est) if est else None
    return {"market": market, "label": label, "model": round(m, 3), "market_prob": round(mk, 3),
            "lean": lean, "min_us": min_us, "best_odds": _num(best), "playdoit_odds": est,
            "value": bool(min_us is not None and best_us is not None and best_us >= min_us),
            "playdoit_value": bool(min_us is not None and est_us is not None and est_us >= min_us)}


def match_key(home, away, normalize=None) -> str:
    if normalize:
        home, away = normalize(home), normalize(away)
    return f"{str(home).lower()} vs {str(away).lower()}"


def build_picks(mvs: pd.DataFrame, upcoming: pd.DataFrame, ratio=None, normalize=None) -> list:
    """Función pura: una fila de program_picks por partido de model_vs_sharp
    (la más reciente) con la última fila de upcoming_matches."""
    up = {match_key(r.home_team_norm, r.away_team_norm, normalize): r for r in upcoming.itertuples(index=False)}
    latest = {}
    order = mvs.sort_values("created_at") if "created_at" in mvs.columns else mvs
    for r in order.itertuples(index=False):
        h, _, a = str(r.match).partition(" vs ")
        latest[(match_key(h, a, normalize), str(r.match_date))] = r
    rows = []
    for (key, _), r in latest.items():
        u = up.get(key)
        model = [_num(getattr(r, c)) for _, c, *_ in SIDES]
        pin = fair_1x2(u) or [_num(getattr(r, c)) for _, _, c, *_ in SIDES]
        pk = pick(model, pin, getattr(r, "p_over25", None), getattr(r, "p_btts", None), u, ratio)
        if not pk:
            continue
        rows.append({"match": str(r.match).lower(), "match_date": pd.Timestamp(r.match_date).to_pydatetime(),
                     "league": r.league, "display": f"{u.home_team} vs {u.away_team}" if u is not None else None,
                     "market": pk["market"], "label": pk["label"], "model_prob": pk["model"],
                     "market_prob": pk["market_prob"], "lean": pk["lean"], "min_us": pk["min_us"],
                     "best_odds": pk["best_odds"], "playdoit_odds": pk["playdoit_odds"],
                     "value": pk["playdoit_value"]})
    return rows


_UPCOMING_SQL = """
    SELECT DISTINCT ON (home_team_norm, away_team_norm)
           home_team_norm, away_team_norm, home_team, away_team, home_odds, draw_odds, away_odds,
           pin_home_odds, pin_draw_odds, pin_away_odds, over25_odds, under25_odds,
           consensus_over25_odds, consensus_under25_odds, btts_yes_odds, btts_no_odds,
           consensus_home_odds, consensus_draw_odds, consensus_away_odds
    FROM upcoming_matches
    WHERE match_date BETWEEN NOW() AND NOW() + make_interval(hours => :h)
    ORDER BY home_team_norm, away_team_norm, updated_at DESC NULLS LAST
"""


def snapshot_picks(hours: int = SNAPSHOT_HOURS, engine=None) -> int:
    """Guarda el pick de los partidos de las próximas `hours` horas con los
    momios de ahora. Un partido ya empezado no se toca. Un fallo es ERROR."""
    if engine is None:
        from config.database import engine
    from src.models.playdoit_prices import playdoit_ratio
    from src.utils.team_normalizer import normalize_team
    try:
        mvs = pd.read_sql(text("""
            SELECT * FROM model_vs_sharp
            WHERE match_date BETWEEN NOW() + INTERVAL '5 minutes' AND NOW() + make_interval(hours => :h)
        """), engine, params={"h": hours})
        if mvs.empty:
            return 0
        up = pd.read_sql(text(_UPCOMING_SQL), engine, params={"h": hours})
        try:
            pd_rows = pd.read_sql(text("""SELECT decimal, consensus_decimal, boosted FROM playdoit_prices
                                          WHERE captured_at >= NOW() - INTERVAL '60 days'"""), engine)
            ratio = playdoit_ratio(pd_rows.to_dict("records")) if not pd_rows.empty else None
        except Exception:
            ratio = None                       # sin capturas todavía: el promedio tal cual
        rows = build_picks(mvs, up, ratio, normalize_team)
        if not rows:
            return 0
        with engine.begin() as conn:
            conn.execute(text(PICKS_DDL))
            conn.execute(text(PICKS_ALTER))
            values, params = [], {}
            for k, r in enumerate(rows):
                values.append("(" + ", ".join(f":{c}{k}" for c in _COLS) + ")")
                params.update({f"{c}{k}": r[c] for c in _COLS})
            conn.execute(text(f"""
                INSERT INTO program_picks ({", ".join(_COLS)})
                VALUES {", ".join(values)}
                ON CONFLICT (match, match_date) DO UPDATE SET
                    league = EXCLUDED.league, display = EXCLUDED.display, market = EXCLUDED.market,
                    label = EXCLUDED.label, model_prob = EXCLUDED.model_prob,
                    market_prob = EXCLUDED.market_prob, lean = EXCLUDED.lean, min_us = EXCLUDED.min_us,
                    best_odds = EXCLUDED.best_odds, playdoit_odds = EXCLUDED.playdoit_odds,
                    value = EXCLUDED.value, picked_at = NOW()
                WHERE program_picks.result IS NULL AND program_picks.match_date > NOW()
            """), params)
    except Exception as e:
        log.error(f"❌ Picks del programa no guardados: {type(e).__name__}: {str(e)[:200]}")
        return 0
    print(f"🎯 Picks del programa: {len(rows)} guardados "
          f"({sum(1 for r in rows if r['lean'])} con ventaja, {sum(1 for r in rows if r['value'])} con valor en PlayDoit)")
    return len(rows)


def resolve_picks(engine=None) -> dict:
    """Liquida los picks ya jugados con la misma función que las apuestas
    (stake 1, al momio estimado de PlayDoit). Sin datos a los
    SHADOW_STALE_DAYS días quedan 'stale'."""
    if engine is None:
        from config.database import engine
    from src.models.save_bets import SHADOW_STALE_DAYS, _preload_matches, plan_shadow_resolutions
    with engine.begin() as conn:
        conn.execute(text(PICKS_DDL))
        conn.execute(text(PICKS_ALTER))
    pending = pd.read_sql(text("""
        SELECT id, match, match_date, market, playdoit_odds FROM program_picks
        WHERE result IS NULL AND match_date < NOW() - INTERVAL '3 hours'
    """), engine)
    if pending.empty:
        return {"pending": 0, "resolved": 0}
    no_price = set(pending.loc[pending["playdoit_odds"].isna(), "id"])
    # sin momio de PlayDoit el resultado cuenta igual; las unidades no
    plan = plan_shadow_resolutions(pending.assign(odds=pending["playdoit_odds"].fillna(2.0).astype(float)),
                                   _preload_matches(pending["match_date"]))
    for p in plan:
        if p["id"] in no_price:
            p["profit"] = None
    with engine.begin() as conn:
        if plan:
            conn.execute(text("""
                UPDATE program_picks SET result = :result, profit = :profit, resolved_at = NOW()
                WHERE id = :id AND result IS NULL
            """), plan)
        stale = conn.execute(text(f"""
            UPDATE program_picks SET result = 'stale', resolved_at = NOW()
            WHERE result IS NULL AND match_date < NOW() - INTERVAL '{SHADOW_STALE_DAYS} days'
        """)).rowcount
    print(f"🎯 Picks del programa liquidados: {len(plan)}/{len(pending)}"
          + (f", {stale} sin datos → stale" if stale else ""))
    return {"pending": int(len(pending)), "resolved": len(plan), "stale": int(stale or 0)}


# ─────────────────────────────────────────────────────────────────────────────
# Picks reconstruidos (22-oct-26)
# ─────────────────────────────────────────────────────────────────────────────
# El dueño: "por qué ninguno de los partidos anteriores tiene un pick, se
# supone que los estábamos guardando". Se guardan desde el 21-oct; antes se
# calculaban al vuelo. Con lo que había ANTES de cada partido (la predicción
# de model_vs_sharp y la última foto de momios de odds_history previa al
# inicio) se reconstruye el mismo pick, sin mirar el resultado.
# model_vs_sharp guarda goles y ambos anotan solo desde el 21-oct: antes, el
# pick sale del 1X2. El promedio de las casas está en odds_history desde el
# 12-oct: antes no hay momio de PlayDoit y el pick cuenta en ganados y
# perdidos, no en unidades.

def build_past_picks(mvs: pd.DataFrame, snaps: pd.DataFrame, normalize=None) -> list:
    """Función pura: un pick por partido de model_vs_sharp, con la foto de
    momios más reciente ANTERIOR al inicio (snaps: odds_history, con
    `match` visible, `kickoff` y `captured_at`)."""
    from types import SimpleNamespace
    idx = {}
    for r in (snaps.to_dict("records") if not snaps.empty else []):
        if pd.Timestamp(r["captured_at"]) >= pd.Timestamp(r["kickoff"]):
            continue                                   # momio en vivo: no cuenta
        h, _, a = str(r["match"]).partition(" vs ")
        idx.setdefault(match_key(h, a, normalize), []).append(r)
    rows = []
    for r in (mvs.to_dict("records") if not mvs.empty else []):
        kick = pd.Timestamp(r["match_date"])
        h, _, a = str(r["match"]).partition(" vs ")
        cands = [x for x in idx.get(match_key(h, a, normalize), [])
                 if abs((pd.Timestamp(x["kickoff"]) - kick).total_seconds()) <= 3 * 3600
                 and pd.Timestamp(x["captured_at"]) < kick]
        snap = max(cands, key=lambda x: pd.Timestamp(x["captured_at"])) if cands else None
        u = SimpleNamespace(**snap) if snap else None
        model = [_num(r.get(c)) for _, c, *_ in SIDES]
        pin = fair_1x2(u) or [_num(r.get(c)) for _, _, c, *_ in SIDES]
        pk = pick(model, pin, r.get("p_over25"), r.get("p_btts"), u)
        if not pk:
            continue
        rows.append({"match": str(r["match"]).lower(), "match_date": kick.to_pydatetime(), "league": r.get("league"),
                     "display": snap["match"] if snap else None, "market": pk["market"], "label": pk["label"],
                     "model_prob": pk["model"], "market_prob": pk["market_prob"], "lean": pk["lean"],
                     "min_us": pk["min_us"], "best_odds": pk["best_odds"], "playdoit_odds": pk["playdoit_odds"],
                     "value": pk["playdoit_value"], "source": "reconstruido"})
    return rows


def backfill_picks(since: str = "2026-09-01", engine=None) -> int:
    """Guarda los picks reconstruidos de los partidos ya jugados que no
    tienen uno guardado (nunca pisa uno guardado en su momento) y los
    liquida. Se puede correr de nuevo: lo ya guardado no se toca."""
    if engine is None:
        from config.database import engine
    from src.utils.team_normalizer import normalize_team
    mvs = pd.read_sql(text("""
        SELECT * FROM model_vs_sharp
        WHERE match_date >= CAST(:s AS date) AND match_date < NOW() - INTERVAL '3 hours'
    """), engine, params={"s": since})
    snaps = pd.read_sql(text("""
        SELECT DISTINCT ON (match_key) match, kickoff, captured_at, home_odds, draw_odds, away_odds,
               over25_odds, under25_odds, btts_yes_odds, btts_no_odds, pin_home_odds, pin_draw_odds,
               pin_away_odds, consensus_home_odds, consensus_draw_odds, consensus_away_odds,
               consensus_over25_odds, consensus_under25_odds
        FROM odds_history
        WHERE kickoff >= CAST(:s AS date) - 1 AND captured_at < kickoff
        ORDER BY match_key, captured_at DESC
    """), engine, params={"s": since})
    rows = build_past_picks(mvs, snaps, normalize_team)
    if not rows:
        return 0
    cols = _COLS + ("source",)
    with engine.begin() as conn:
        conn.execute(text(PICKS_DDL))
        conn.execute(text(PICKS_ALTER))
        for i in range(0, len(rows), 200):
            part = rows[i:i + 200]
            values, params = [], {}
            for k, r in enumerate(part):
                values.append("(" + ", ".join(f":{c}{k}" for c in cols) + ")")
                params.update({f"{c}{k}": r[c] for c in cols})
            conn.execute(text(f"""
                INSERT INTO program_picks ({", ".join(cols)}) VALUES {", ".join(values)}
                ON CONFLICT (match, match_date) DO NOTHING
            """), params)
    print(f"🎯 Picks reconstruidos: {len(rows)} partidos desde {since}")
    resolve_picks(engine=engine)
    return len(rows)

