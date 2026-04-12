"""
Shots Stats
===========
Ratings de tiros al arco (shots on target) por equipo desde DB histórica.

Metodología:
  - Usa home_shots_target / away_shots_target de la tabla matches
  - Solo partidos donde shots_target > 0 (filtra datos imputados con 0)
  - Decay exponencial (SHOTS_DECAY=0.87)
  - attack_rating  = shots on target generados / SHOTS_BASELINE
  - defense_rating = shots on target concedidos / SHOTS_BASELINE

Nota:
  El xG proxy en xg_proxy.py ya usa shots para estimar goles esperados.
  Este módulo se enfoca en la distribución de tiros como mercado separado.
"""

import numpy as np
import pandas as pd
from sqlalchemy import text
from config.database import engine

SHOTS_DECAY    = 0.87
SHOTS_WINDOW   = 20
SHOTS_MIN      = 5      # mínimo partidos para ser útil
SHOTS_BASELINE = 3.5    # promedio europeo de tiros al arco por equipo por partido


def get_team_shots(team: str) -> dict | None:
    """
    Retorna ratings de tiros al arco para un equipo.

    Args:
        team: nombre del equipo (lowercase)

    Returns:
        {
            "shots_for":      float  — tiros al arco promedio generados (decay-weighted)
            "shots_against":  float  — tiros al arco promedio concedidos
            "attack_rating":  float  — shots_for / baseline  (1.0 = promedio)
            "defense_rating": float  — shots_against / baseline
            "matches":        int
        }
        None si hay menos de SHOTS_MIN partidos con datos.
    """
    team = team.lower().strip()

    query = text("""
        SELECT
            date,
            CASE WHEN LOWER(home_team) = :team
                 THEN home_shots_target
                 ELSE away_shots_target
            END AS shots_for,
            CASE WHEN LOWER(home_team) = :team
                 THEN away_shots_target
                 ELSE home_shots_target
            END AS shots_against
        FROM matches
        WHERE (LOWER(home_team) = :team OR LOWER(away_team) = :team)
          AND home_shots_target IS NOT NULL
          AND away_shots_target IS NOT NULL
          AND home_shots_target > 0
          AND away_shots_target > 0
        ORDER BY date DESC
        LIMIT :window
    """)

    try:
        df = pd.read_sql(query, engine, params={"team": team, "window": SHOTS_WINDOW})
    except Exception:
        return None

    if len(df) < SHOTS_MIN:
        return None

    weights = np.array([SHOTS_DECAY ** i for i in range(len(df))])
    weights = weights / weights.sum()

    shots_for     = float(np.average(df["shots_for"],     weights=weights))
    shots_against = float(np.average(df["shots_against"], weights=weights))

    return {
        "shots_for":      round(shots_for,     3),
        "shots_against":  round(shots_against, 3),
        "attack_rating":  round(shots_for     / SHOTS_BASELINE, 3),
        "defense_rating": round(shots_against / SHOTS_BASELINE, 3),
        "matches":        len(df),
    }
