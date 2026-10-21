"""
src/utils/output_checks.py
==========================
Chequeo diario de las SALIDAS del sistema (21-oct-26).

Las auditorías revisaban que cada paso corriera (flujos en verde, sin
excepciones) y los errores graves corrían "bien" con números equivocados:
el xG de Understat como total de la temporada tuvo 3 semanas el modelo en
el tope de goles en las 5 grandes ligas sin que nada fallara. Esto revisa
que lo que sale tenga sentido y avisa como ERROR (llega a Telegram como
"errores tolerados") el mismo día:

  - modelo: goles esperados en el tope, distancia a Pinnacle (pipeline_invariants,
    al final de cada corrida del pipeline);
  - liquidación: cada resultado de los últimos días se vuelve a calcular con
    una implementación propia, sin resolve_market (settlement_mismatches);
  - resultados: partidos analizados que ya se jugaron y no tienen marcador;
  - pendientes atorados más allá de la ventana de 'stale';
  - xG real fuera de escala;
  - lo aprendido: claves del weekly sin actualizar en 9+ días.

Las funciones de revisión son puras; run_output_checks hace las consultas.
"""

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

LAMBDA_CAP_HIT = 3.49
MAX_CAP_SHARE = 0.05          # partidos con goles esperados en el tope
FAR_FROM_PIN = 0.20           # un resultado del 1X2 a 20+ pts de Pinnacle
MAX_FAR_SHARE = 0.10
MAX_MEDIAN_DIST = 0.09
MIN_ROWS = 20                 # con menos partidos no se juzga
MAX_MISSING_RESULTS = 0.15
MAX_XG_PER_MATCH = 4.0
WEEKLY_KEYS = ("anchor_weights", "side_bias", "calibration_factors", "dc_params",
               "sharp_reference", "strategy_lab", "rule_evidence")
WEEKLY_MAX_AGE_DAYS = 9
RESOLVED = ("win", "loss", "push", "half_win", "half_loss")


# ─────────────────────────────────────────────────────────────────────────────
# Modelo
# ─────────────────────────────────────────────────────────────────────────────

def pipeline_invariants(records: list) -> list:
    """records: filas de model_vs_sharp de una corrida (p_*, pin_*, lam_*).
    Devuelve los avisos (vacío = todo en orden)."""
    rows = [r for r in records or [] if r]
    if len(rows) < MIN_ROWS:
        return []
    out = []
    lams = [r for r in rows if r.get("lam_home") is not None and r.get("lam_away") is not None]
    if len(lams) >= MIN_ROWS:
        capped = sum(1 for r in lams if max(r["lam_home"], r["lam_away"]) >= LAMBDA_CAP_HIT)
        if capped / len(lams) > MAX_CAP_SHARE:
            out.append(f"Modelo: {capped} de {len(lams)} partidos con goles esperados en el tope "
                       f"(3.5) — algún dato de entrada está fuera de escala")
    dists = []
    for r in rows:
        try:
            dists.append(max(abs(float(r[f"p_{s}"]) - float(r[f"pin_{s}"])) for s in ("home", "draw", "away")))
        except (KeyError, TypeError, ValueError):
            pass
    if len(dists) >= MIN_ROWS:
        far = sum(1 for d in dists if d > FAR_FROM_PIN)
        med = sorted(dists)[len(dists) // 2]
        if far / len(dists) > MAX_FAR_SHARE:
            out.append(f"Modelo: {far} de {len(dists)} partidos a más de {FAR_FROM_PIN * 100:.0f} pts de Pinnacle")
        if med > MAX_MEDIAN_DIST:
            out.append(f"Modelo: distancia típica a Pinnacle de {med * 100:.1f} pts (normal: ~6)")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Liquidación independiente
# ─────────────────────────────────────────────────────────────────────────────

def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _ou(total, line, side):
    def one(L):
        if abs(total - L) < 1e-9:
            return 0
        return 1 if ((total > L) if side == "over" else (total < L)) else -1
    if abs(line * 4 - round(line * 4)) < 1e-9 and abs(line * 2 - round(line * 2)) > 1e-9:
        return {2: "win", 1: "half_win", 0: "push", -1: "half_loss", -2: "loss"}[one(line - 0.25) + one(line + 0.25)]
    return {1: "win", 0: "push", -1: "loss"}[one(line)]


def _ah(margin, line, side):
    def one(L):
        d = margin + L
        if abs(d) < 1e-9:
            return 0
        return 1 if ((d > 0) == (side == "home")) else -1
    if abs(line * 4 - round(line * 4)) < 1e-9 and abs(line * 2 - round(line * 2)) > 1e-9:
        return {2: "win", 1: "half_win", 0: "push", -1: "half_loss", -2: "loss"}[one(line - 0.25) + one(line + 0.25)]
    return {1: "win", 0: "push", -1: "loss"}[one(line)]


def settle_independent(market: str, r: dict) -> str | None:
    """Resultado de un mercado con una implementación propia (sin
    resolve_market). None si falta el dato o el mercado no está cubierto.
    Las tarjetas cuentan solo amarillas, como la liquidación del sistema."""
    hg, ag = int(r["home_goals"]), int(r["away_goals"])
    simple = {"home_win": hg > ag, "draw": hg == ag, "away_win": hg < ag,
              "btts": hg > 0 and ag > 0, "btts_no": not (hg > 0 and ag > 0),
              "dc_1x": hg >= ag, "dc_x2": hg <= ag, "dc_12": hg != ag}
    if market in simple:
        return "win" if simple[market] else "loss"
    if market in ("dnb_home", "dnb_away"):
        return "push" if hg == ag else ("win" if (hg > ag) == (market == "dnb_home") else "loss")
    g = re.fullmatch(r"(over|under)(\d)(\d)", market)
    if g:
        return _ou(hg + ag, float(f"{g.group(2)}.{g.group(3)}"), g.group(1))
    g = re.fullmatch(r"(over|under)_(\d+(?:\.\d+)?)", market)
    if g:
        return _ou(hg + ag, float(g.group(2)), g.group(1))
    g = re.fullmatch(r"ah_(home|away)_([-+]?\d+(?:\.\d+)?)", market)
    if g:
        return _ah(hg - ag, float(g.group(2)), g.group(1))
    g = re.fullmatch(r"h1_(home|draw|away)", market)
    if g:
        h1, a1 = _num(r.get("home_goals_ht")), _num(r.get("away_goals_ht"))
        if h1 is None or a1 is None:
            return None
        res = "home" if h1 > a1 else ("draw" if h1 == a1 else "away")
        return "win" if res == g.group(1) else "loss"
    for prop, cols in (("corners", ("home_corners", "away_corners")), ("cards", ("home_yellow", "away_yellow"))):
        g = re.fullmatch(prop + r"_(over|under)_(\d+(?:\.\d+)?)", market)
        if g:
            a, b = _num(r.get(cols[0])), _num(r.get(cols[1]))
            return None if a is None or b is None else _ou(a + b, float(g.group(2)), g.group(1))
    return None


def _match_index(matches: list) -> dict:
    idx = defaultdict(list)
    for m in matches:
        idx[(str(m["h"]).lower(), str(m["a"]).lower())].append(m)
    return idx


def _find(idx, match: str, when, normalize=None):
    if " vs " not in str(match):
        return None
    h, a = str(match).split(" vs ", 1)
    if normalize:
        h, a = normalize(h), normalize(a)
    import pandas as pd
    d = pd.Timestamp(when)
    if pd.isna(d):
        return None
    d = (d.tz_convert("UTC") if d.tzinfo else d).date()
    cand = [m for m in idx.get((h.lower(), a.lower()), []) if abs((pd.Timestamp(m["d"]).date() - d).days) <= 1]
    if len({(m["home_goals"], m["away_goals"]) for m in cand}) != 1:
        return None
    return cand[0]


def settlement_mismatches(rows: list, matches: list, normalize=None) -> list:
    """rows: [{"table", "id", "match", "match_date", "market", "result"}] ya
    liquidadas; matches: [{"h", "a", "d", goles, 1T, córners, tarjetas}].
    Devuelve las que no coinciden con la liquidación independiente."""
    idx = _match_index(matches)
    bad = []
    for r in rows:
        m = _find(idx, r["match"], r["match_date"], normalize)
        if m is None:
            continue
        mine = settle_independent(r["market"], m)
        if mine is not None and mine != r["result"]:
            bad.append({**r, "expected": mine, "score": f"{m['home_goals']}-{m['away_goals']}"})
    return bad


def missing_results(analyzed: list, matches: list, normalize=None) -> list:
    """Partidos analizados (model_vs_sharp: match, match_date) sin marcador."""
    idx = _match_index(matches)
    return [a for a in analyzed if _find(idx, a["match"], a["match_date"], normalize) is None]


def stale_learning(updated: dict, now: datetime | None = None) -> list:
    """{clave: updated_at} → claves del weekly sin actualizar en 9+ días."""
    now = now or datetime.now(timezone.utc)
    out = []
    for k in WEEKLY_KEYS:
        ts = updated.get(k)
        if ts is None:
            out.append(f"Aprendizaje: '{k}' no existe")
            continue
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        if now - ts > timedelta(days=WEEKLY_MAX_AGE_DAYS):
            out.append(f"Aprendizaje: '{k}' sin actualizar desde {ts:%d-%b} (el weekly no lo corrió)")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Corrida diaria (evening)
# ─────────────────────────────────────────────────────────────────────────────

def run_output_checks(engine=None, normalize=None, days: int = 3) -> list:
    """Corre todo y avisa cada problema como ERROR. Devuelve los avisos."""
    import pandas as pd
    if engine is None:
        from config.database import engine
    if normalize is None:
        from src.utils.team_normalizer import normalize_team as normalize
    problems = []

    def q(sql, **p):
        return pd.read_sql(text(sql), engine, params=p)

    matches = q("""
        SELECT LOWER(home_team) AS h, LOWER(away_team) AS a, date::date AS d, home_goals, away_goals,
               home_goals_ht, away_goals_ht, home_corners, away_corners, home_yellow, away_yellow
        FROM matches WHERE home_goals IS NOT NULL AND date >= CURRENT_DATE - make_interval(days => :d)
    """, d=days + 3).to_dict("records")

    # 1) liquidación de lo resuelto en los últimos días
    rows = []
    for table in ("bets_history", "shadow_bets", "model_proposals", "program_picks"):
        try:
            df = q(f"""SELECT id, match, match_date, market, result FROM {table}
                       WHERE result IN {RESOLVED} AND match_date >= NOW() - make_interval(days => :d)""", d=days)
            rows += [{**r, "table": table} for r in df.to_dict("records")]
        except Exception as e:
            if table == "program_picks" and "does not exist" in str(e):
                continue                  # nace con el primer pick guardado (21-oct-26)
            problems.append(f"Liquidación: no se pudo leer {table} ({type(e).__name__})")
    bad = settlement_mismatches(rows, matches, normalize)
    if bad:
        ex = "; ".join(f"{b['table']} #{b['id']} {b['market']} {b['score']}: {b['result']}≠{b['expected']}"
                       for b in bad[:3])
        problems.append(f"Liquidación: {len(bad)} resultados no coinciden con el cálculo independiente ({ex})")

    # 2) resultados que faltan
    try:
        an = q("""SELECT match, match_date FROM model_vs_sharp
                  WHERE match_date BETWEEN NOW() - make_interval(days => :d) AND NOW() - INTERVAL '8 hours'""",
               d=days).to_dict("records")
        miss = missing_results(an, matches, normalize)
        if len(an) >= MIN_ROWS and len(miss) / len(an) > MAX_MISSING_RESULTS:
            problems.append(f"Resultados: {len(miss)} de {len(an)} partidos analizados sin marcador "
                            f"({', '.join(m['match'] for m in miss[:3])}…)")
    except Exception as e:
        problems.append(f"Resultados: no se pudo revisar ({type(e).__name__})")

    # 3) pendientes atorados más allá de la ventana de 'stale'
    try:
        st = q("""SELECT
            (SELECT COUNT(*) FROM bets_history WHERE result IN ('pending', 'unresolved')
                AND match_date < NOW() - INTERVAL '8 days') AS bets,
            (SELECT COUNT(*) FROM shadow_bets WHERE result IS NULL AND match_date < NOW() - INTERVAL '11 days') AS shadow,
            (SELECT COUNT(*) FROM model_proposals WHERE result IS NULL AND match_date < NOW() - INTERVAL '11 days') AS props
        """).iloc[0]
        for name, n in (("apuestas reales", st.bets), ("sombra", st.shadow), ("propuestas", st.props)):
            if int(n or 0) > 0:
                problems.append(f"Pendientes: {int(n)} {name} atoradas más allá de su ventana")
    except Exception as e:
        problems.append(f"Pendientes: no se pudo revisar ({type(e).__name__})")

    # 4) xG real fuera de escala
    try:
        mx = q("""SELECT MAX(xg_for / NULLIF(matches, 0)) AS m FROM team_xg_real
                  WHERE season = (SELECT MAX(season) FROM team_xg_real)""").iloc[0]["m"]
        if mx is not None and float(mx) > MAX_XG_PER_MATCH:
            problems.append(f"xG real: {float(mx):.1f} por partido (fuera de escala)")
    except Exception:
        pass

    # 5) lo aprendido por el weekly
    try:
        st = q("SELECT key, updated_at FROM model_state WHERE key = ANY(:k)", k=list(WEEKLY_KEYS))
        problems += stale_learning({r.key: pd.Timestamp(r.updated_at).to_pydatetime() for r in st.itertuples()})
    except Exception as e:
        problems.append(f"Aprendizaje: no se pudo revisar ({type(e).__name__})")

    for p in problems:
        log.error(f"❌ Chequeo de salidas — {p}")
    if not problems:
        print(f"✅ Chequeo de salidas: liquidación ({len(rows)} revisadas), resultados, pendientes, "
              f"xG y aprendizaje en orden")
    return problems
