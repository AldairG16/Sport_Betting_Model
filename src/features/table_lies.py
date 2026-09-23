"""
src/features/table_lies.py
==========================
SHADE "LA TABLA MIENTE" (Flepp, 2024, Economic Inquiry).

Hallazgo académico: el mercado sobrevalora a los equipos cuyos PUNTOS
sobre-rinden su desempeño subyacente, y subvalora a los que sub-rinden
(la tabla miente). La corrección: medir la suerte de cada equipo y ajustar
su probabilidad en el próximo partido en sentido contrario.

Fórmula:
  xPts(partido) = 3·P(ganar) + 1·P(empate), con P de Poisson usando los
  goles del partido como intensidades (λcasa = gf, λvisit = ga).
  suerte = puntos_reales − Σ xPts  (ventana de 15 partidos, decay suave)
  ajuste de probabilidad = clamp(suerte × 0.02, −0.08, +0.08)

Suerte positiva = equipo sobre-rinde → el mercado lo infla → nuestra
probabilidad se reduce (apostar contra si las cuotas no lo reflejan).

Costo: 0 créditos — solo lee matches.
"""


import numpy as np
import pandas as pd
from scipy.stats import poisson
from sqlalchemy import text

from config.database import engine
from src.utils.team_normalizer import normalize_team

WINDOW = 15
PTS_SHADE = 0.02      # ajuste de prob por punto de suerte
SHADE_CLAMP = 0.08    # tope del ajuste (±8%)

_luck_cache: dict = {}
_CACHE_TTL_S = 6 * 3600
_cache_ts: dict = {}


def match_xpts(gf: float, ga: float) -> float:
    """
    Puntos esperados de una actuación con gf goles a favor y ga en contra,
    usando los goles como intensidades de Poisson. Puro — testeable.
    """
    gf = max(float(gf), 0.1)
    ga = max(float(ga), 0.1)
    max_g = 10
    idx = np.arange(0, max_g + 1)
    ph = poisson.pmf(idx, gf)
    pa = poisson.pmf(idx, ga)
    i, j = np.meshgrid(idx, idx, indexing="ij")
    m = ph[:, None] * pa[None, :]
    p_win = float(m[i > j].sum())
    p_draw = float(m[i == j].sum())
    return 3.0 * p_win + p_draw


def get_luck(team: str, cutoff=None, window: int = WINDOW) -> dict | None:
    """
    Suerte del equipo: puntos reales − puntos esperados (xPts) en su
    ventana reciente. Returns {points, xpts, luck, matches} o None.
    """
    team = normalize_team(team)
    cache_key = (team, str(cutoff)[:10])
    import time
    now = time.time()
    if cache_key in _luck_cache and now - _cache_ts.get(cache_key, 0) < _CACHE_TTL_S:
        return _luck_cache[cache_key]

    params = {"team": team, "w": window}
    cutoff_clause = ""
    if cutoff is not None:
        cutoff_clause = "AND date < :cutoff"
        params["cutoff"] = pd.to_datetime(cutoff).strftime("%Y-%m-%d")

    df = pd.read_sql(text(f"""
        SELECT date, home_team, away_team, home_goals, away_goals
        FROM matches
        WHERE (LOWER(home_team) = LOWER(:team) OR LOWER(away_team) = LOWER(:team))
          AND home_goals IS NOT NULL
          {cutoff_clause}
        ORDER BY date DESC
        LIMIT :w
    """), engine, params=params)

    if len(df) < 5:
        return None

    df_chrono = df.iloc[::-1]
    points, xpts = 0.0, 0.0
    n = 0
    for _, r in df_chrono.iterrows():
        if pd.isna(r["home_goals"]) or pd.isna(r["away_goals"]):
            continue
        home = r["home_team"].lower() == team.lower()
        gf, ga = float(r["home_goals"]), float(r["away_goals"])
        if not home:
            gf, ga = ga, gf
        points += 3 if gf > ga else (1 if gf == ga else 0)
        xpts += match_xpts(gf, ga)
        n += 1
    if n == 0:
        return None

    luck = points - xpts
    out = {"points": points, "xpts": round(xpts, 2), "luck": round(luck, 2), "matches": n}
    _luck_cache[cache_key] = out
    _cache_ts[cache_key] = now
    return out
