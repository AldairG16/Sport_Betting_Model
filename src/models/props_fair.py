"""
src/models/props_fair.py
========================
Precio justo de córners y tarjetas totales, calibrado contra la historia
(20-oct-26, a pedido del dueño: "córners y tarjetas, donde las casas cuidan
menos").

El modelo de córners del pipeline (corners_model + corners_stats) exagera:
medido el 20-oct sobre 25,046 partidos de 2022 en adelante (18 ligas), cuando
decía 15% de "más de 9.5" salían 42%, y cuando decía 85% salían 60%. Acierta
MENOS que usar solo el promedio de la liga (pérdida logarítmica 0.7119 contra
0.6898). Usa un promedio europeo fijo (5 córners por equipo), le cree del todo
a los últimos 20 partidos de cada equipo y supone Poisson.

Este módulo corrige las tres cosas:
  - base: el promedio de local y de visitante de LA LIGA (último año);
  - equipos: su desvío contra la liga, encogido (prior de 6 partidos) y
    creído solo en parte (ALPHA: lo que minimizó la pérdida en 2012-2021);
  - distribución: binomial negativa para córners (sobre-dispersos, k≈48) y
    Poisson para tarjetas.
Resultado del 20-oct en la prueba (2022+): córners "más de 9.5" 0.6861 contra
0.6890 del promedio de liga y 0.7119 del modelo viejo; tarjetas (prueba 2022+, con
α=0.4 de 2012-21) mejor que el promedio de liga.

El precio mínimo es el que deja ≥ 2 pt de ventaja (min_odds.min_american), el
mismo margen del filtro Pinnacle. Que el modelo esté calibrado no prueba que
le gane a PlayDoit: solo que su probabilidad es correcta en promedio. La
medición de verdad es con los precios del mercado (shadow) y con el dueño.
"""

import numpy as np
import pandas as pd
from scipy.stats import nbinom, poisson

WINDOW = 20
DECAY = 0.87
PRIOR_N = 6
MIN_MATCHES = 5
LEAGUE_DAYS = 365
MIN_LEAGUE_MATCHES = 30
MIN_GAP = 0.04          # el partido se aparta 4 pt o más del promedio de su liga

PROPS = {
    "corners": {"cols": ("home_corners", "away_corners"), "alpha": 0.3, "dispersion": 48.0,
                "lines": (8.5, 9.5, 10.5, 11.5), "name": "córners"},
    "cards": {"cols": ("home_yellow", "away_yellow"), "alpha": 0.4, "dispersion": None,
              "lines": (3.5, 4.5, 5.5), "name": "tarjetas amarillas"},
}


# ─────────────────────────────────────────────────────────────────────────────
# Funciones puras
# ─────────────────────────────────────────────────────────────────────────────

def team_rating(history: list, league_avg: float) -> tuple | None:
    """(a favor, en contra) por partido, ponderado (lo reciente pesa más) y
    encogido hacia el promedio de la liga. history: [(a favor, en contra)],
    del más reciente al más viejo. None con menos de MIN_MATCHES."""
    h = [x for x in history if x[0] is not None and x[1] is not None][:WINDOW]
    if len(h) < MIN_MATCHES or not league_avg:
        return None
    w = np.array([DECAY ** i for i in range(len(h))])
    w /= w.sum()
    f = float(np.dot(w, [x[0] for x in h]))
    a = float(np.dot(w, [x[1] for x in h]))
    n = len(h)
    shrink = lambda v: (n * v + PRIOR_N * league_avg) / (n + PRIOR_N)
    return shrink(f), shrink(a)


def expected_total(home_avg: float, away_avg: float, rating_home, rating_away, alpha: float) -> float:
    """Total esperado: el de la liga más ALPHA × el desvío que ponen los equipos."""
    base = home_avg + away_avg
    if rating_home is None or rating_away is None:
        return base
    m = (home_avg + away_avg) / 2.0
    lh = home_avg * (rating_home[0] / m) * (rating_away[1] / m)
    la = away_avg * (rating_away[0] / m) * (rating_home[1] / m)
    return base + alpha * ((lh + la) - base)


def prob_over(mu: float, line: float, dispersion: float | None) -> float:
    """P(total > línea). Binomial negativa (var = μ + μ²/k) o Poisson."""
    k = int(np.floor(line))
    if dispersion:
        return float(1.0 - nbinom.cdf(k, dispersion, dispersion / (dispersion + mu)))
    return float(1.0 - poisson.cdf(k, mu))


def fair_lines(mu: float, prop: str) -> dict:
    """{línea: P(más de)} para las líneas habituales del mercado."""
    cfg = PROPS[prop]
    return {line: prob_over(mu, line, cfg["dispersion"]) for line in cfg["lines"]}


def best_pick(mu: float, league_mu: float, prop: str, min_gap: float = MIN_GAP) -> dict | None:
    """La línea y el lado donde el partido más se aparta del promedio de su
    liga, si la diferencia llega a min_gap. Una casa que pone la misma línea a
    todos los partidos de la liga se equivoca justo ahí; donde el partido es
    "promedio", nuestro precio es el suyo y no hay nada que ganar."""
    from src.utils.min_odds import min_american
    cfg = PROPS[prop]
    best = None
    for line in cfg["lines"]:
        p = prob_over(mu, line, cfg["dispersion"])
        gap = p - prob_over(league_mu, line, cfg["dispersion"])
        side, prob = ("más", p) if gap > 0 else ("menos", 1.0 - p)
        if abs(gap) >= min_gap and prob >= 0.5 and (best is None or abs(gap) > best["gap"]):
            best = {"line": line, "side": side, "prob": prob, "gap": abs(gap),
                    "min_american": min_american(prob)}
    return best


# ─────────────────────────────────────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────────────────────────────────────

HISTORY_DAYS = 900          # alcanza para el año de liga y los 20 partidos de cada equipo


def load_history(engine=None, days: int = HISTORY_DAYS) -> pd.DataFrame:
    """Una sola consulta: los partidos recientes con córners o tarjetas. Todo
    lo demás se calcula en memoria (el 20-oct, una consulta por equipo y liga
    tardaba 7 min para 122 partidos). El nombre es `home_team` (canónico, en
    minúsculas): `team_home_norm` quedó vacío en los partidos recientes."""
    from sqlalchemy import text
    if engine is None:
        from config.database import engine
    cols = sorted({c for cfg in PROPS.values() for c in cfg["cols"]})
    df = pd.read_sql(text(f"""
        SELECT date, league, LOWER(home_team) AS home, LOWER(away_team) AS away, {", ".join(cols)}
        FROM matches
        WHERE date >= CURRENT_DATE - {int(days)}
          AND ({" OR ".join(f"{c} IS NOT NULL" for c in cols)})
    """), engine)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date", ascending=False).reset_index(drop=True)


def league_averages(hist: pd.DataFrame, league: str, prop: str, before) -> tuple | None:
    """Promedio por partido del local y del visitante en la liga, el año antes de `before`."""
    hc, ac = PROPS[prop]["cols"]
    b = pd.Timestamp(before)
    m = hist[(hist["league"] == league) & (hist["date"] < b)
             & (hist["date"] >= b - pd.Timedelta(days=LEAGUE_DAYS))].dropna(subset=[hc, ac])
    if len(m) < MIN_LEAGUE_MATCHES:
        return None
    return float(m[hc].mean()), float(m[ac].mean())


def team_history(hist: pd.DataFrame, team: str, prop: str, before) -> list:
    """[(a favor, en contra)] de los últimos partidos del equipo antes de
    `before`, del más reciente (hist viene ordenado del más reciente)."""
    hc, ac = PROPS[prop]["cols"]
    m = hist[((hist["home"] == team) | (hist["away"] == team)) & (hist["date"] < pd.Timestamp(before))]
    m = m.dropna(subset=[hc, ac]).head(WINDOW)
    home = m["home"] == team
    return list(zip(np.where(home, m[hc], m[ac]).astype(float), np.where(home, m[ac], m[hc]).astype(float)))


def match_props(hist: pd.DataFrame, home: str, away: str, league: str, before) -> dict:
    """{prop: {"mu", "league_mu", "lines", "pick", "teams_known"}} para un
    partido, con lo que se sabía antes de `before`. Sin promedio de liga no
    hay precio (no se inventa)."""
    out = {}
    for prop, cfg in PROPS.items():
        avgs = league_averages(hist, league, prop, before)
        if avgs is None:
            continue
        m = (avgs[0] + avgs[1]) / 2.0
        rh = team_rating(team_history(hist, home, prop, before), m)
        ra = team_rating(team_history(hist, away, prop, before), m)
        mu = expected_total(avgs[0], avgs[1], rh, ra, cfg["alpha"])
        out[prop] = {"mu": mu, "league_mu": avgs[0] + avgs[1], "lines": fair_lines(mu, prop),
                     "pick": best_pick(mu, avgs[0] + avgs[1], prop),
                     "teams_known": rh is not None and ra is not None}
    return out
