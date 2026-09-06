"""
Corners Stats
=============
Ratings de córners por equipo calculados desde datos históricos en DB.

Metodología:
  - Decay exponencial (CORNERS_DECAY=0.87) — partidos recientes pesan más
  - Ventana de CORNERS_WINDOW=20 últimos partidos con datos válidos
  - attack_rating  = córners generados / CORNERS_BASELINE
  - defense_rating = córners concedidos / CORNERS_BASELINE (1.0 = promedio)
  - Solo usa partidos donde AMBOS equipos tienen córners registrados (> 0)
"""

import numpy as np
import pandas as pd
from sqlalchemy import text
from config.database import engine

CORNERS_DECAY   = 0.87
CORNERS_WINDOW  = 20
CORNERS_MIN     = 5      # mínimo de partidos para retornar datos útiles
CORNERS_BASELINE = 5.0   # promedio europeo de córners por equipo (~5 por partido)


def get_team_corners(team: str) -> dict | None:
    """
    Retorna ratings de córners para un equipo desde la DB.

    Args:
        team: nombre del equipo (se normaliza a lowercase)

    Returns:
        {
            "corners_for":     float  — promedio de córners generados (decay-weighted)
            "corners_against": float  — promedio de córners concedidos
            "attack_rating":   float  — corners_for / baseline  (1.0 = promedio)
            "defense_rating":  float  — corners_against / baseline
            "matches":         int    — partidos con datos válidos
        }
        None si hay menos de CORNERS_MIN partidos con datos.
    """
    team = team.lower().strip()

    query = text("""
        SELECT
            date,
            CASE WHEN LOWER(home_team) = :team
                 THEN home_corners
                 ELSE away_corners
            END AS corners_for,
            CASE WHEN LOWER(home_team) = :team
                 THEN away_corners
                 ELSE home_corners
            END AS corners_against
        FROM matches
        WHERE (LOWER(home_team) = :team OR LOWER(away_team) = :team)
          AND home_corners IS NOT NULL
          AND away_corners IS NOT NULL
          AND home_corners > 0
          AND away_corners > 0
        ORDER BY date DESC
        LIMIT :window
    """)

    try:
        df = pd.read_sql(query, engine, params={"team": team, "window": CORNERS_WINDOW})
    except Exception:
        return None

    if len(df) < CORNERS_MIN:
        return None

    # Decay weights: más peso a partidos recientes
    weights = np.array([CORNERS_DECAY ** i for i in range(len(df))])
    weights = weights / weights.sum()

    corners_for     = float(np.average(df["corners_for"],     weights=weights))
    corners_against = float(np.average(df["corners_against"], weights=weights))

    return {
        "corners_for":     round(corners_for,     3),
        "corners_against": round(corners_against, 3),
        "attack_rating":   round(corners_for     / CORNERS_BASELINE, 3),
        "defense_rating":  round(corners_against / CORNERS_BASELINE, 3),
        "matches":         len(df),
    }
