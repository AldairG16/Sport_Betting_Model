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
from src.models.program_picks import fair_1x2, pick
from src.utils.min_odds import fmt_american, min_american, to_american

# Desde aquí el modelo corregido (PR #44: xG de Understat y peso de Dixon-Coles)
MODEL_FIXED = pd.Timestamp("2026-10-22", tz="UTC")
SIDES = (("home", "p_home", "pin_home", "home_odds"),
         ("draw", "p_draw", "pin_draw", "draw_odds"),
         ("away", "p_away", "pin_away", "away_odds"))
SIDE_LABEL = {"home": "local", "draw": "empate", "away": "visitante"}
# El laboratorio usa momios inventados en estos mercados: no cuentan.
FAKE_ODDS_DECISION = "mercado desactivado"
# ...y estos tuvieron momio inventado hasta el 21-oct-26 aunque la decisión
# guardada fuera otra (liga bloqueada): por nombre de mercado.
FAKE_ODDS_MARKETS = frozenset({"over_1.5", "under_1.5", "over_3.5", "under_3.5",
                               "shots_over_5.5", "shots_under_5.5"})


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
    return fair_1x2(u)


def program_pick(model: list, pin: list, p_over25, p_btts, u, ratio=None) -> dict:
    """
    Lo que jugaría el programa en el partido (v1.7.2, 21-oct-26: el dueño
    quería ver una apuesta aunque no tenga valor), con el texto para la
    página. La regla vive en src/models/program_picks.pick: la misma con la
    que se guarda cada pick para medir cómo terminó. "value" = el mejor
    momio de Europa ya paga el mínimo; "pd_value" = el estimado de PlayDoit.
    """
    pk = pick(model, pin, p_over25, p_btts, u, ratio)
    if not pk:
        return {"label": "", "text": ""}
    min_us, best = pk["min_us"], pk["best_odds"]
    best_us = to_american(best) if best else None
    est_us = to_american(pk["playdoit_odds"]) if pk["playdoit_odds"] else None
    return {"label": pk["label"], "market_key": pk["market"], "model": pk["model"], "market": pk["market_prob"],
            "lean": pk["lean"], "min_us": fmt_american(min_us) if min_us is not None else "",
            "best_us": fmt_american(best_us) if best_us is not None else "", "value": pk["value"],
            "pd_est_us": fmt_american(est_us) if est_us is not None else "",
            "pd_value": pk["playdoit_value"], "pd_learned": bool(_num(ratio))}


def predictions(mvs: pd.DataFrame, upcoming: pd.DataFrame, pending_matches: set,
                normalize=None, ratio=None) -> list:
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
            "pick": program_pick(model, pin, getattr(r, "p_over25", None), getattr(r, "p_btts", None), u, ratio),
            # sin goles esperados guardados = corrida anterior a la corrección
            # del modelo (21-oct-26): su lectura no vale
            "old_model": not (lam_h and lam_a),
            "real_bet": key in pending_matches,
        })
    return out


def _outcome(hg, ag) -> int:
    return 0 if hg > ag else (1 if hg == ag else 2)


def history(mvs: pd.DataFrame, results: pd.DataFrame, normalize=None) -> dict:
    """
    ¿Acertó el programa? mvs: model_vs_sharp de partidos ya jugados.
    results: matches con (h, a, d, home_goals, away_goals), nombres en
    minúsculas. Un partido cuenta si su resultado está a ±1 día.
    normalize: nombre canónico de un equipo (21-oct-26). model_vs_sharp
    guarda el nombre del día en que se analizó; si después se aprendió un
    alias, matches ya usa el nuevo y el partido no aparecía.
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
        score = res.get((_canon_match(r.match, normalize), md_utc.date()))
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


def _canon_match(match, normalize=None) -> str:
    m = str(match)
    if normalize and " vs " in m:
        h, a = m.split(" vs ", 1)
        m = f"{normalize(h)} vs {normalize(a)}"
    return m.lower()


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
        fake = decision == FAKE_ODDS_DECISION or str(r.market) in FAKE_ODDS_MARKETS
        rows.append({
            "match_date": _iso(r.match_date), "match": match_name(r.match),
            "league": league_name(r.league) if r.league else "",
            "market": market_name(r.market), "probability": _num(r.probability),
            "pin_prob": _num(r.pin_prob), "odds_us": fmt_american(to_american(r.odds)),
            "min_us": fmt_american(min_us) if min_us is not None else "",
            "decision": decision, "fake_odds": fake,
            "result_key": str(r.result or "pending"), "result": result_label(r.result or "pending"),
            "profit_1u": None if p1 is None else round(p1, 2),
        })
        if p1 is not None and not fake:
            g = groups.setdefault(decision, {"decision": decision, "n": 0, "won": 0, "profit_1u": 0.0})
            g["n"] += 1
            g["won"] += int(str(r.result) in ("win", "half_win"))
            g["profit_1u"] = round(g["profit_1u"] + p1, 2)
    summary = sorted(groups.values(), key=lambda g: -g["n"])
    for g in summary:
        g["roi"] = round(g["profit_1u"] / g["n"], 3) if g["n"] else None
    return {"rows": rows, "summary": summary}


# ─────────────────────────────────────────────────────────────────────────────
# Resultados (v1.9.0, 21-oct-26): "cómo nos fue con las apuestas de nuestros
# modelos" en una sola pestaña.
# ─────────────────────────────────────────────────────────────────────────────

PLAYED = ("win", "loss", "push", "half_win", "half_loss")
COLLECTION_MODE = pd.Timestamp("2026-09-25", tz="UTC")     # solo dinero real si le gana a Pinnacle
REAL_PERIODS = ((pd.Timestamp("2026-09-14", tz="UTC"), "Hasta el 13-sep"),
                (COLLECTION_MODE, "14 al 24-sep"),
                (None, "Desde el 25-sep (modo recolección)"))


def _bool(v) -> bool:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return False
    return bool(v)


def _record(rows: list) -> dict | None:
    """Ganados, perdidos y unidades de filas con result_key y profit."""
    played = [x for x in rows if x["result_key"] in PLAYED]
    if not played:
        return None
    priced = [x["profit"] for x in played if x["profit"] is not None]
    profit = round(sum(priced), 2)
    return {"n": len(played),
            "won": sum(1 for x in played if x["result_key"] in ("win", "half_win")),
            "lost": sum(1 for x in played if x["result_key"] in ("loss", "half_loss")),
            "profit": profit, "priced": len(priced),
            "roi": round(profit / len(priced), 3) if priced else None}


def picks_board(df: pd.DataFrame) -> dict:
    """Los picks del programa guardados (program_picks) y cómo terminaron:
    todos, los que tenían ventaja según el programa y los que pagaban el
    mínimo en PlayDoit."""
    from dashboard.display import result_label
    rows = []
    for r in df.itertuples(index=False):
        res = str(r.result) if isinstance(r.result, str) and r.result else "pending"
        po = _num(r.playdoit_odds)
        rows.append({
            "match_date": _iso(r.match_date),
            "match": r.display if isinstance(r.display, str) and r.display else match_name(r.match),
            "league": league_name(r.league) if isinstance(r.league, str) and r.league else "",
            "label": r.label, "model": _num(r.model_prob), "market": _num(r.market_prob),
            "lean": _bool(r.lean), "value": _bool(r.value),
            "playdoit_us": fmt_american(to_american(po)) if po else "",
            "min_us": fmt_american(int(r.min_us)) if _num(r.min_us) is not None else "",
            "result_key": res, "result": result_label(res), "profit": _num(r.profit),
        })
    return {"rows": rows, "all": _record(rows),
            "lean": _record([x for x in rows if x["lean"]]),
            "value": _record([x for x in rows if x["value"]])}


def real_periods(df: pd.DataFrame) -> list:
    """Apuestas con dinero real por etapa del sistema. df: bets_history con
    match_date, result, stake y profit (las 'stale' son canceladas)."""
    out = []
    dates = pd.to_datetime(df["match_date"], utc=True) if not df.empty else pd.Series(dtype="datetime64[ns, UTC]")
    start = None
    for end, label in REAL_PERIODS:
        mask = pd.Series(True, index=df.index)
        if start is not None:
            mask &= dates >= start
        if end is not None:
            mask &= dates < end
        part = df[mask]
        played = part[part["result"].isin(PLAYED)] if not part.empty else part
        out.append({"label": label,
                    "n": int(len(played)),
                    "won": int(played["result"].isin(["win", "half_win"]).sum()) if len(played) else 0,
                    "lost": int(played["result"].isin(["loss", "half_loss"]).sum()) if len(played) else 0,
                    "staked": round(float(pd.to_numeric(played["stake"], errors="coerce").sum()), 2) if len(played) else 0.0,
                    "profit": round(float(pd.to_numeric(played["profit"], errors="coerce").sum()), 2) if len(played) else 0.0,
                    "cancelled": int((part["result"] == "stale").sum()) if not part.empty else 0})
        start = end
    return out


def proposal_weeks(df: pd.DataFrame) -> list:
    """Lo que el modelo propuso, por semana, a 1u y al mejor momio (sin los
    mercados con momio inventado). df: model_proposals sin repetir partido y
    mercado, con match_date, market, odds, result."""
    if df.empty:
        return []
    d = df[df["result"].isin(PLAYED) & ~df["market"].isin(FAKE_ODDS_MARKETS)].copy()
    if d.empty:
        return []
    when = pd.to_datetime(d["match_date"], utc=True)
    d["week"] = when.dt.tz_convert(None).dt.to_period("W-SUN").dt.start_time
    d["old"] = when < MODEL_FIXED                          # la semana del arreglo sale en dos filas
    d["p1"] = [profit_1u(o, r) for o, r in zip(d["odds"], d["result"])]
    out = []
    for (week, old), g in d.groupby(["week", "old"]):
        out.append({"week": week.strftime("%Y-%m-%d"), "old_model": bool(old), "n": int(len(g)),
                    "won": int(g["result"].isin(["win", "half_win"]).sum()),
                    "lost": int(g["result"].isin(["loss", "half_loss"]).sum()),
                    "profit_1u": round(float(pd.Series(g["p1"]).dropna().sum()), 2)})
    return out


__all__ = ["predictions", "history", "proposals", "likely_score", "reading", "profit_1u",
           "picks_board", "real_periods", "proposal_weeks"]
