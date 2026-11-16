"""
src/models/combo_odds.py
========================
¿Cuánto vale un PLAYBOOST? (16-nov-26)

El dueño: "muchas veces en PlayDoit sale este tipo de apuestas, donde son
positivas pero son varios resultados en 1, entonces necesitamos buscar
diferentes maneras de ver cómo ganar el resultado". Eligió (B1) que el revisor
de capturas (scripts/playdoit_checker.py) diga si cada boost vale.

Un boost junta varias apuestas de un partido ("ambos anotan + más de 2.5 goles
+ más de 8.5 córners") y paga un momio subido. Las piezas NO salen por
separado: ambos anotan y más de 2.5 van juntas casi siempre. Aquí se cuenta,
en el historial (`historical_odds`, 2012 a enero de 2026, con el precio de
Pinnacle), cada cuánto salieron TODAS juntas en los partidos que el mercado
veía parecidos (la probabilidad del local, la del visitante y la de más de 2.5
goles, a ±3 puntos; si hay menos de MIN_N partidos así, ±5 y ±8), con la liga
encogida hacia el total (LEAGUE_K). La consulta cuenta en la base y devuelve
solo los conteos.

Lo medido (audit/m45_combo_calib.py, 3,000 partidos de 2024-26 estimados solo
con partidos de antes de 2024): sale como dice, por tercios a 1-3 puntos
("dice 33 → salió 34, dice 47 → salió 49" en ambos + más de 2.5). Multiplicar
cada pieza como si no tuvieran que ver se equivoca hasta 15 puntos (26.9 contra
41.7 que salió) y no mirar el partido también pierde.

Lo que no se puede medir: lo de un jugador (anota, remates, asistencias) y el
empate anula dentro de una combinada. Para lo de un jugador se dice cuánto
tendría que salir.

Funciones puras salvo history_probability (una consulta).
"""

import math

from sqlalchemy import text

from src.utils.min_odds import MIN_EDGE_TO_PLACE, min_american, to_american

TOLS = (0.03, 0.05, 0.08)      # qué tan parecido según el mercado (la mayor diferencia de las 3 probabilidades)
MIN_N = 800                    # partidos parecidos para quedarse con la tolerancia más chica
LEAGUE_K = 300                 # cuánto pesa el total frente a la liga (partidos)
MIN_EDGE = MIN_EDGE_TO_PLACE   # la misma regla de todo el sistema: 2 puntos sobre lo que pide el momio

# (mercado de la captura) → tipo de pierna
_KIND = {"1x2": "result", "resultado": "result", "doble_oportunidad": "double", "total_goles": "goals",
         "ambos_anotan": "btts", "corners_total": "corners", "corners": "corners", "tiros_de_esquina": "corners",
         "goles_local": "home_goals", "goles_visitante": "away_goals", "goles_primer_tiempo": "ht_goals",
         "resultado_primer_tiempo": "ht_result"}
# líneas que se aceptan (siempre x.5: sin empate de línea)
_LINES = {"goals": (0.5, 7.5), "corners": (2.5, 18.5), "home_goals": (0.5, 5.5), "away_goals": (0.5, 5.5),
          "ht_goals": (0.5, 4.5)}
_SIDE = {"local": "home", "1": "home", "empate": "draw", "x": "draw", "visitante": "away", "2": "away"}
_OVER = {"mas": "over", "más": "over", "over": "over", "menos": "under", "under": "under"}
_YES = {"si": "yes", "sí": "yes", "yes": "yes", "no": "no"}


def _line(v):
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and abs(f * 2 - round(f * 2)) < 1e-9 and round(f * 2) % 2 == 1 else None


def parse_leg(leg: dict) -> dict:
    """Una pata de la captura → {"kind", "sel", "line"} que se puede medir, o
    {"kind": "player"} (de un jugador) o {"kind": "unknown"}."""
    mk = str(leg.get("mercado") or "").lower().strip()
    sel = str(leg.get("seleccion") or "").lower().strip().replace(" ", "")
    if mk == "jugador" or leg.get("jugador"):
        return {"kind": "player", "who": str(leg.get("jugador") or "").strip(),
                "what": str(leg.get("seleccion") or "").strip()}
    kind = _KIND.get(mk)
    if kind in ("result", "ht_result"):
        side = _SIDE.get(sel)
        return {"kind": kind, "sel": side} if side else {"kind": "unknown"}
    if kind == "double":
        return {"kind": kind, "sel": sel} if sel in ("1x", "x2", "12") else {"kind": "unknown"}
    if kind == "btts":
        yes = _YES.get(sel)
        return {"kind": kind, "sel": yes} if yes else {"kind": "unknown"}
    if kind in _LINES:
        side, line = _OVER.get(sel), _line(leg.get("linea"))
        lo, hi = _LINES[kind]
        if side and line is not None and lo <= line <= hi:
            return {"kind": kind, "sel": side, "line": line}
    return {"kind": "unknown"}


def measurable(legs: list) -> bool:
    return all(x["kind"] not in ("player", "unknown") for x in legs)


def needs(legs: list) -> set:
    """Las columnas del historial que tienen que estar."""
    out = set()
    for x in legs:
        if x["kind"] == "corners":
            out.add("corners")
        if x["kind"] in ("ht_goals", "ht_result"):
            out.add("ht")
    return out


def predicate(legs: list) -> tuple[str, dict]:
    """La condición SQL de que salgan todas (columnas hg, ag, hgh, agh, hc, ac)
    y sus parámetros. Solo piezas fijas: las líneas van como parámetro."""
    parts, params = [], {}
    for i, x in enumerate(legs):
        k, s = x["kind"], x.get("sel")
        if k == "result":
            parts.append({"home": "hg > ag", "draw": "hg = ag", "away": "ag > hg"}[s])
        elif k == "ht_result":
            parts.append({"home": "hgh > agh", "draw": "hgh = agh", "away": "agh > hgh"}[s])
        elif k == "double":
            parts.append({"1x": "hg >= ag", "x2": "ag >= hg", "12": "hg <> ag"}[s])
        elif k == "btts":
            parts.append("(hg > 0 AND ag > 0)" if s == "yes" else "NOT (hg > 0 AND ag > 0)")
        else:
            col = {"goals": "(hg + ag)", "corners": "(hc + ac)", "home_goals": "hg", "away_goals": "ag",
                   "ht_goals": "(hgh + agh)"}[k]
            params[f"l{i}"] = float(x["line"])
            parts.append(f"{col} {'>' if s == 'over' else '<'} :l{i}")
    return " AND ".join(parts) or "TRUE", params


_HISTORY_SQL = """
WITH h AS (
    SELECT sport_key AS lg, home_goals AS hg, away_goals AS ag, home_goals_ht AS hgh, away_goals_ht AS agh,
           home_corners AS hc, away_corners AS ac,
           (1 / pin_home) / (1 / pin_home + 1 / pin_draw + 1 / pin_away) AS fh,
           (1 / pin_away) / (1 / pin_home + 1 / pin_draw + 1 / pin_away) AS fa,
           CASE WHEN avg_over25 > 1 AND avg_under25 > 1
                THEN (1 / avg_over25) / (1 / avg_over25 + 1 / avg_under25) END AS fo
    FROM historical_odds
    WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL AND pin_home > 1 AND pin_draw > 1 AND pin_away > 1
), x AS (
    SELECT lg, ({pred}) AS hit, {dist} AS d FROM h WHERE {where}
)
SELECT {counts} FROM x WHERE d <= :tmax
"""


def history_sql(legs: list, fh=None, fa=None, fo=None) -> tuple[str, dict]:
    """La consulta (y sus parámetros) que cuenta, por tolerancia, los partidos
    parecidos y en cuántos salió todo; también en la liga (:lg)."""
    pred, params = predicate(legs)
    where = ["TRUE"]
    if "corners" in needs(legs):
        where.append("hc IS NOT NULL AND ac IS NOT NULL")
    if "ht" in needs(legs):
        where.append("hgh IS NOT NULL AND agh IS NOT NULL")
    dist = []
    if fh is not None and fa is not None:
        dist += ["ABS(fh - :fh)", "ABS(fa - :fa)"]
        params.update({"fh": float(fh), "fa": float(fa)})
    if fo is not None:
        dist.append("ABS(fo - :fo)")
        where.append("fo IS NOT NULL")
        params["fo"] = float(fo)
    d = "0" if not dist else (dist[0] if len(dist) == 1 else f"GREATEST({', '.join(dist)})")
    counts = []
    for i, t in enumerate(TOLS):
        params[f"t{i}"] = t
        counts += [f"COUNT(*) FILTER (WHERE d <= :t{i}) AS n{i}",
                   f"COUNT(*) FILTER (WHERE d <= :t{i} AND hit) AS h{i}",
                   f"COUNT(*) FILTER (WHERE d <= :t{i} AND lg = :lg) AS nl{i}",
                   f"COUNT(*) FILTER (WHERE d <= :t{i} AND lg = :lg AND hit) AS hl{i}"]
    params["tmax"] = TOLS[-1]
    return _HISTORY_SQL.format(pred=pred, dist=d, where=" AND ".join(where), counts=", ".join(counts)), params


def pick_estimate(counts: dict) -> dict | None:
    """De los conteos por tolerancia: la más chica con MIN_N partidos (o la
    más grande) y la frecuencia con la liga encogida hacia el total."""
    for i, t in enumerate(TOLS):
        n = int(counts.get(f"n{i}") or 0)
        if n >= MIN_N or i == len(TOLS) - 1:
            if n == 0:
                return None
            hits, nl, hl = int(counts.get(f"h{i}") or 0), int(counts.get(f"nl{i}") or 0), int(counts.get(f"hl{i}") or 0)
            p_all = hits / n
            p = (hl + LEAGUE_K * p_all) / (nl + LEAGUE_K)
            return {"p": p, "n": n, "n_league": nl, "tol": t, "p_all": p_all}
    return None


def history_probability(engine, legs: list, fh=None, fa=None, fo=None, league=None) -> dict | None:
    """Cada cuánto salieron todas juntas en los partidos parecidos (pick_estimate)."""
    sql, params = history_sql(legs, fh, fa, fo)
    params["lg"] = str(league or "")
    with engine.connect() as conn:
        row = conn.execute(text(sql), params).mappings().first()
    return pick_estimate(dict(row or {}))


# ─────────────────────────────────────────────────────────────────────────────
# El veredicto
# ─────────────────────────────────────────────────────────────────────────────

def judge(price_decimal: float, p: float) -> dict:
    """Misma regla que todo el sistema: vale si la probabilidad le gana por 2
    puntos o más a la que pide el momio."""
    ok = p - 1 / price_decimal >= MIN_EDGE - 1e-9
    return {"verdict": "apuesta" if ok else "no", "prob": p, "fair_us": to_american(1 / p) if p > 0 else None,
            "min_american": min_american(p), "ev": p * price_decimal - 1}


def player_bound(price_decimal: float, p_team: float | None) -> dict:
    """Con una pata de jugador: lo demás sale p_team (1 si no hay más). Vale
    solo si lo del jugador sale, en esos partidos, más de `need` de cada 100;
    si need pasa de 1, no vale aunque el jugador cumpliera siempre."""
    target = 1 / price_decimal + MIN_EDGE
    base = 1.0 if p_team is None else p_team
    need = target / base if base > 0 else float("inf")
    return {"verdict": "no" if need > 1 else "jugador", "need": need, "p_team": p_team, "target": target}


def leg_label(x: dict, home: str, away: str) -> str:
    """La pata en palabras, como en PlayDoit."""
    k, s = x["kind"], x.get("sel")
    team = {"home": home, "away": away}
    if k == "result":
        return "Empate" if s == "draw" else f"Gana {team[s]}"
    if k == "ht_result":
        return "Empate al medio tiempo" if s == "draw" else f"{team[s]} gana el 1er tiempo"
    if k == "double":
        return {"1x": f"{home} o empate", "x2": f"Empate o {away}", "12": f"{home} o {away}"}[s]
    if k == "btts":
        return "Ambos anotan" if s == "yes" else "No anotan los dos"
    if k == "player":
        return f"{x.get('who') or 'Jugador'}: {x.get('what') or 'de un jugador'}"
    if k == "unknown":
        return "otra apuesta"
    word = "Más" if s == "over" else "Menos"
    line = f"{x['line']:g}"
    return {"goals": f"{word} de {line} goles", "corners": f"{word} de {line} córners",
            "home_goals": f"{home}: {word.lower()} de {line} goles", "away_goals": f"{away}: {word.lower()} de {line} goles",
            "ht_goals": f"1er tiempo: {word.lower()} de {line} goles"}[k]
