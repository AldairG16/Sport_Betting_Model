"""
dashboard/visibility.py
=======================
Lo que el programa analiza aunque no apueste (v1.7.0, 21-oct-26).

En modo recolección casi no hay apuestas reales (solo se apuesta si el
momio le gana a Pinnacle por 2 puntos) y el dashboard se veía vacío. Estas
funciones puras arman tres vistas a partir de tablas que el pipeline ya
llena:
  - predictions(): qué dice el programa de los próximos partidos
    (model_vs_sharp + momios de upcoming_matches);
  - history(): si acertó, contra el resultado real y contra Pinnacle;
  - proposals(): cada apuesta que el modelo propuso, la decisión y cómo
    terminó (model_proposals).
Sin base de datos ni Flask: dashboard/app.py hace las consultas.
"""

import math

import pandas as pd

from dashboard.display import league_name, match_name
from src.utils.min_odds import fmt_american, min_american, to_american

# Desde aquí el modelo corregido (PR #44: xG de Understat y peso de Dixon-Coles)
MODEL_FIXED = pd.Timestamp("2026-10-22", tz="UTC")
SIDES = (("home", "p_home", "pin_home", "home_odds"),
         ("draw", "p_draw", "pin_draw", "draw_odds"),
         ("away", "p_away", "pin_away", "away_odds"))
SIDE_LABEL = {"home": "local", "draw": "empate", "away": "visitante"}
# El laboratorio usa momios inventados en estos mercados: no cuentan.
FAKE_ODDS_DECISION = "mercado desactivado"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _iso(ts) -> str:
    """Fecha ISO con "T": la entiende cualquier navegador (con espacio, no)."""
    try:
        return pd.Timestamp(ts).isoformat()
    except (TypeError, ValueError):
        return str(ts)


def likely_score(lam_home, lam_away, max_goals: int = 7) -> dict | None:
    """Marcador más probable con Poisson; None sin goles esperados."""
    lh, la = _num(lam_home), _num(lam_away)
    if not lh or not la or lh <= 0 or la <= 0:
        return None
    pmf = lambda lam, k: math.exp(-lam) * lam ** k / math.factorial(k)
    best = max(((i, j) for i in range(max_goals) for j in range(max_goals)),
               key=lambda ij: pmf(lh, ij[0]) * pmf(la, ij[1]))
    return {"score": f"{best[0]}-{best[1]}", "prob": round(pmf(lh, best[0]) * pmf(la, best[1]), 3)}


def reading(model: list, pin: list) -> str:
    """Lectura en una línea: dónde ve el programa más que Pinnacle."""
    pairs = [(m - p, name) for (name, *_), m, p in zip(SIDES, model, pin) if m is not None and p is not None]
    if not pairs:
        return ""
    d, name = max(pairs)
    if d < 0.03:
        return "Coincide con el mercado"
    return f"Ve más al {SIDE_LABEL[name]} (+{d * 100:.0f} pts que Pinnacle)"


def _key(home_norm, away_norm) -> str:
    return f"{str(home_norm).lower()} vs {str(away_norm).lower()}"


def _canon(match: str, normalize=None) -> str:
    """Clave del partido con los nombres canónicos de hoy: un alias nuevo
    (21-oct: "pumas unam" → "unam pumas") dejaba el mismo partido dos veces."""
    if " vs " not in str(match):
        return str(match).lower()
    h, a = str(match).split(" vs ", 1)
    if normalize:
        h, a = normalize(h), normalize(a)
    return _key(h, a)


def _fresh_pinnacle(u) -> list | None:
    """1X2 de Pinnacle sin margen con la última recarga (cada 30 min en el
    closing); el de model_vs_sharp es el de la corrida de la mañana."""
    if u is None:
        return None
    odds = [_num(getattr(u, c, None)) for c in ("pin_home_odds", "pin_draw_odds", "pin_away_odds")]
    if not all(o and o > 1 for o in odds):
        return None
    inv = [1 / o for o in odds]
    return [x / sum(inv) for x in inv]


PICK_MIN_GAP = 0.03      # el programa tiene que ver 3+ pts más que el mercado para "preferir" algo


def _devig2(a, b) -> float | None:
    """Probabilidad sin margen del primer lado de un mercado de dos."""
    a, b = _num(a), _num(b)
    if not a or not b or a <= 1 or b <= 1:
        return None
    return (1 / a) / (1 / a + 1 / b)


def program_pick(model: list, pin: list, p_over25, p_btts, u) -> dict:
    """
    Lo que jugaría el programa en el partido (v1.7.2, 21-oct-26: el dueño
    quería ver una apuesta aunque no tenga valor). Entre 1X2, más/menos 2.5 y
    ambos anotan, el mercado donde el programa ve más que el mercado sin
    margen (Pinnacle en el 1X2; el promedio de las casas en goles y ambos
    anotan). Con menos de 3 pts de diferencia no prefiere nada y se muestra
    lo más probable del 1X2. "value" = el mejor momio de Europa ya paga el
    mínimo; en PlayDoit hay que revisarlo.
    """
    cands = []
    for (name, _, _, odds_col), m, p in zip(SIDES, model, pin):
        if m is not None and p is not None:
            cands.append((f"Gana {'el ' + SIDE_LABEL[name] if name != 'draw' else ''}".replace("Gana empate", "Empate").strip()
                          if name != "draw" else "Empate", m, p, getattr(u, odds_col, None) if u is not None else None))
    p_over = _num(p_over25)
    if p_over is not None and u is not None:
        mk = _devig2(getattr(u, "consensus_over25_odds", None), getattr(u, "consensus_under25_odds", None)) \
            or _devig2(getattr(u, "over25_odds", None), getattr(u, "under25_odds", None))
        if mk is not None:
            cands.append(("Más de 2.5 goles", p_over, mk, getattr(u, "over25_odds", None)))
            cands.append(("Menos de 2.5 goles", 1 - p_over, 1 - mk, getattr(u, "under25_odds", None)))
    pb = _num(p_btts)
    if pb is not None and u is not None:
        mk = _devig2(getattr(u, "btts_yes_odds", None), getattr(u, "btts_no_odds", None))
        if mk is not None:
            cands.append(("Ambos anotan", pb, mk, getattr(u, "btts_yes_odds", None)))
            cands.append(("No anotan ambos", 1 - pb, 1 - mk, getattr(u, "btts_no_odds", None)))
    if not cands:
        return {"label": "", "text": ""}
    label, m, mk, best = max(cands, key=lambda c: c[1] - c[2])
    if m - mk < PICK_MIN_GAP:
        # nada mejor que el mercado: lo más probable del 1X2
        label, m, mk, best = max(cands[:3], key=lambda c: c[1])
        lean = False
    else:
        lean = True
    min_us = min_american(m, pin_prob=mk)
    best_us = to_american(_num(best)) if _num(best) else None
    value = bool(min_us is not None and best_us is not None and best_us >= min_us)
    return {"label": label, "model": round(m, 3), "market": round(mk, 3), "lean": lean,
            "min_us": fmt_american(min_us) if min_us is not None else "",
            "best_us": fmt_american(best_us) if best_us is not None else "", "value": value}


def predictions(mvs: pd.DataFrame, upcoming: pd.DataFrame, pending_matches: set,
                normalize=None) -> list:
    """
    mvs: filas de model_vs_sharp de los próximos días. upcoming: la última
    fila de upcoming_matches por partido (nombres, mejores momios y
    Pinnacle). pending_matches: claves "local vs visitante" (normalizadas)
    con apuesta real pendiente. normalize: normalize_team (para unir alias).
    """
    up = {}
    for r in upcoming.itertuples(index=False):
        up[_canon(_key(r.home_team_norm, r.away_team_norm), normalize)] = r
    latest = {}
    order = mvs.sort_values("created_at") if "created_at" in mvs.columns else mvs
    for r in order.itertuples(index=False):           # el más reciente manda
        latest[(_canon(r.match, normalize), str(r.match_date))] = r
    out = []
    for (key, _), r in sorted(latest.items(), key=lambda kv: pd.Timestamp(kv[1].match_date)):
        u = up.get(key)
        model = [_num(getattr(r, c)) for _, c, _, _ in SIDES]
        pin = _fresh_pinnacle(u) or [_num(getattr(r, c)) for _, _, c, _ in SIDES]
        sides = []
        for (name, _, _, odds_col), m, p in zip(SIDES, model, pin):
            best = _num(getattr(u, odds_col, None)) if u is not None else None
            min_us = min_american(m, pin_prob=p) if m is not None else None
            sides.append({
                "side": name, "model": m, "pin": p,
                "best_us": fmt_american(to_american(best)) if best else "",
                "min_us": fmt_american(min_us) if min_us is not None else "",
                # el mejor momio de Europa ya paga el mínimo (PlayDoit casi nunca)
                "value_at_best": bool(best and min_us is not None and to_american(best) >= min_us),
            })
        lam_h, lam_a = _num(getattr(r, "lam_home", None)), _num(getattr(r, "lam_away", None))
        out.append({
            "match_date": _iso(r.match_date),
            "match": (f"{u.home_team} vs {u.away_team}" if u is not None else match_name(r.match)),
            "league": league_name(r.league) if r.league else "",
            "sides": sides,
            "lam": [round(lam_h, 2), round(lam_a, 2)] if lam_h and lam_a else None,
            "likely": likely_score(lam_h, lam_a),
            "p_over25": _num(getattr(r, "p_over25", None)),
            "p_btts": _num(getattr(r, "p_btts", None)),
            "reading": reading(model, pin),
            "pick": program_pick(model, pin, getattr(r, "p_over25", None), getattr(r, "p_btts", None), u),
            # sin goles esperados guardados = corrida anterior a la corrección
            # del modelo (21-oct-26): su lectura no vale
            "old_model": not (lam_h and lam_a),
            "real_bet": key in pending_matches,
        })
    return out


def _outcome(hg, ag) -> int:
    return 0 if hg > ag else (1 if hg == ag else 2)


def history(mvs: pd.DataFrame, results: pd.DataFrame) -> dict:
    """
    ¿Acertó el programa? mvs: model_vs_sharp de partidos ya jugados.
    results: matches con (h, a, d, home_goals, away_goals), nombres en
    minúsculas. Un partido cuenta si su resultado está a ±1 día.
    """
    res = {}
    for r in results.itertuples(index=False):
        d = pd.Timestamp(r.d).date()
        for k in (-1, 0, 1):
            res.setdefault((f"{r.h} vs {r.a}", d + pd.Timedelta(days=k)), (int(r.home_goals), int(r.away_goals)))
    rows = []
    for r in mvs.itertuples(index=False):
        md = pd.Timestamp(r.match_date)
        md_utc = md.tz_convert("UTC") if md.tzinfo else md.tz_localize("UTC")
        score = res.get((str(r.match).lower(), md_utc.date()))
        model = [_num(getattr(r, c)) for _, c, _, _ in SIDES]
        pin = [_num(getattr(r, c)) for _, _, c, _ in SIDES]
        if score is None or None in model or None in pin:
            continue
        o = _outcome(*score)
        rows.append({
            "match_date": _iso(r.match_date), "match": match_name(r.match),
            "league": league_name(r.league) if r.league else "",
            "score": f"{score[0]}-{score[1]}", "outcome": SIDES[o][0],
            "model": model, "pin": pin,
            "model_hit": max(range(3), key=lambda i: model[i]) == o,
            "pin_hit": max(range(3), key=lambda i: pin[i]) == o,
            "model_p_actual": model[o], "pin_p_actual": pin[o],
            "fixed": md_utc >= MODEL_FIXED,
        })

    def summary(rs):
        if not rs:
            return None
        n = len(rs)
        return {"n": n,
                "model_hit": round(sum(x["model_hit"] for x in rs) / n, 3),
                "pin_hit": round(sum(x["pin_hit"] for x in rs) / n, 3),
                "model_p_actual": round(sum(x["model_p_actual"] for x in rs) / n, 3),
                "pin_p_actual": round(sum(x["pin_p_actual"] for x in rs) / n, 3)}
    return {"rows": rows, "all": summary(rows),
            "fixed": summary([x for x in rows if x["fixed"]])}


def profit_1u(odds, result) -> float | None:
    """Ganancia a 1 unidad con la liquidación de siempre."""
    o = _num(odds)
    if o is None:
        return None
    return {"win": o - 1, "half_win": (o - 1) / 2, "push": 0.0,
            "half_loss": -0.5, "loss": -1.0}.get(str(result))


def proposals(df: pd.DataFrame) -> dict:
    """Cada propuesta del modelo (una por partido y mercado), su decisión y
    resultado; resumen por decisión sin los mercados con momio inventado."""
    from dashboard.display import market_name, result_label
    rows, groups = [], {}
    for r in df.itertuples(index=False):
        p1 = profit_1u(r.odds, r.result)
        min_us = min_american(_num(r.probability), pin_prob=_num(r.pin_prob)) if _num(r.probability) else None
        decision = str(r.decision or "")
        rows.append({
            "match_date": _iso(r.match_date), "match": match_name(r.match),
            "league": league_name(r.league) if r.league else "",
            "market": market_name(r.market), "probability": _num(r.probability),
            "pin_prob": _num(r.pin_prob), "odds_us": fmt_american(to_american(r.odds)),
            "min_us": fmt_american(min_us) if min_us is not None else "",
            "decision": decision, "fake_odds": decision == FAKE_ODDS_DECISION,
            "result_key": str(r.result or "pending"), "result": result_label(r.result or "pending"),
            "profit_1u": None if p1 is None else round(p1, 2),
        })
        if p1 is not None and decision != FAKE_ODDS_DECISION:
            g = groups.setdefault(decision, {"decision": decision, "n": 0, "won": 0, "profit_1u": 0.0})
            g["n"] += 1
            g["won"] += int(str(r.result) in ("win", "half_win"))
            g["profit_1u"] = round(g["profit_1u"] + p1, 2)
    summary = sorted(groups.values(), key=lambda g: -g["n"])
    for g in summary:
        g["roi"] = round(g["profit_1u"] / g["n"], 3) if g["n"] else None
    return {"rows": rows, "summary": summary}


__all__ = ["predictions", "history", "proposals", "likely_score", "reading", "profit_1u"]
