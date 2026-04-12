"""
Cards Stats
===========
Ratings de tarjetas amarillas por equipo calculados desde datos históricos en DB.

Metodología:
  - Decay exponencial (CARDS_DECAY=0.87) — partidos recientes pesan más
  - Ventana de CARDS_WINDOW=20 últimos partidos con datos válidos
  - attack_rating  = tarjetas recibidas / CARDS_BASELINE  (equipo propenso a tarjetas)
  - defense_rating = tarjetas forzadas / CARDS_BASELINE   (equipo que provoca faltas)
  - Solo usa partidos donde AMBOS equipos tienen amarillas registradas (>= 0)

Nota: "defense_rating" aquí representa cuántas tarjetas sufre el rival cuando
juega contra este equipo — proxy de presión/agresividad que induce tarjetas.
"""

import numpy as np
import pandas as pd
from sqlalchemy import text
from config.database import engine

CARDS_DECAY    = 0.87
CARDS_WINDOW   = 20
CARDS_MIN      = 5       # mínimo de partidos para retornar datos útiles
CARDS_BASELINE = 2.0     # promedio de amarillas por equipo por partido (~4 totales)


def get_team_cards(team: str) -> dict | None:
    """
    Retorna ratings de tarjetas para un equipo desde la DB.

    Args:
        team: nombre del equipo (normalizado a lowercase)

    Returns:
        {
            "cards_for":     float  — promedio de amarillas recibidas (decay-weighted)
            "cards_against": float  — promedio de amarillas forzadas al rival
            "attack_rating": float  — cards_for / baseline  (1.0 = promedio)
            "defense_rating": float — cards_against / baseline
            "matches":       int    — partidos con datos válidos
        }
        None si hay menos de CARDS_MIN partidos con datos.
    """
    team = team.lower().strip()

    query = text("""
        SELECT
            date,
            CASE WHEN LOWER(home_team) = :team
                 THEN home_yellow
                 ELSE away_yellow
            END AS cards_for,
            CASE WHEN LOWER(home_team) = :team
                 THEN away_yellow
                 ELSE home_yellow
            END AS cards_against
        FROM matches
        WHERE (LOWER(home_team) = :team OR LOWER(away_team) = :team)
          AND home_yellow IS NOT NULL
          AND away_yellow IS NOT NULL
        ORDER BY date DESC
        LIMIT :window
    """)

    try:
        df = pd.read_sql(query, engine, params={"team": team, "window": CARDS_WINDOW})
    except Exception:
        return None

    if len(df) < CARDS_MIN:
        return None

    # Decay weights: más peso a partidos recientes
    weights = np.array([CARDS_DECAY ** i for i in range(len(df))])
    weights = weights / weights.sum()

    cards_for     = float(np.average(df["cards_for"],     weights=weights))
    cards_against = float(np.average(df["cards_against"], weights=weights))

    return {
        "cards_for":      round(cards_for,     3),
        "cards_against":  round(cards_against, 3),
        "attack_rating":  round(cards_for     / CARDS_BASELINE, 3),
        "defense_rating": round(cards_against / CARDS_BASELINE, 3),
        "matches":        len(df),
    }
