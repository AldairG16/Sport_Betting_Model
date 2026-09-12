"""
xG Proxy Calculator
===================
Calcula Expected Goals (xG) aproximado usando datos de shots_on_target
ya disponibles en la tabla `matches`.

Por qué es mejor que goles reales:
  - Los goles tienen alta varianza (un penalti, un error de portero)
  - Los shots on target reflejan mejor la calidad real del juego
  - Reducción de ruido estadístico en lambdas del modelo Poisson

Conversion rates basados en datos reales de las 5 grandes ligas:
  - Shot on target: ~30% de probabilidad de gol
  - Shot off target: ~8% de probabilidad de gol
"""

import pandas as pd
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team

# =========================
# CONSTANTES
# =========================

# 13-sep-26: recalibrado con datos reales. Las tasas viejas (0.30/0.08)
# daban xG ~1.88 a un equipo PROMEDIO cuando el promedio real de la liga
# es ~1.35-1.40 goles/equipo → +35% sistemático en ataque y defensa de
# TODOS los equipos → lambdas inflados → overs sobrevalorados (era el
# sesgo del 57.3% predicho vs 44.4% real). Con 0.28/0.03 el equipo
# promedio (4.2 SoT, 12 tiros) da xG ~1.41 ✓
SHOT_ON_TARGET_RATE = 0.28   # Conversión real SoT → gol
SHOT_RATE           = 0.03   # Conversión real tiro no a puerta → gol (deflectes/errores)
# 12-sep-26: 15 partidos alcanzaban 6 meses hacia atrás (mar→sep) y con
# decay 0.87 la primavera seguía dominando — el xG de Tottenham decía 1.82
# goles/partido con el equipo en 0 goles en 4 jornadas, y ese número
# diluía 40% de la señal de forma actual en cada lambda. Ventana más
# corta + decay más fuerte para que la forma reciente mande.
XG_DECAY            = 0.80   # Decay exponencial (recientes pesan más)
XG_WINDOW           = 10     # Últimos 10 partidos


# =========================
# MAIN FUNCTION
# =========================

def get_team_xg(team: str) -> dict | None:
    """
    Calcula el xG promedio ponderado (ataque y defensa) de un equipo
    usando los últimos XG_WINDOW partidos con decay exponencial.

    Returns:
        {
            "xg_for":     float  — xG generado por partido (ataque)
            "xg_against": float  — xG concedido por partido (defensa)
            "matches":    int    — partidos usados
        }
        None si hay menos de 3 partidos con datos de shots.
    """
    team = normalize_team(team)

    df = pd.read_sql(
        text("""
            SELECT home_team, away_team,
                   home_shots, away_shots,
                   home_shots_target, away_shots_target,
                   date
            FROM matches
            WHERE (LOWER(home_team) = LOWER(:team)
                OR LOWER(away_team) = LOWER(:team))
              AND home_shots_target IS NOT NULL
              AND away_shots_target IS NOT NULL
              AND home_shots_target > 0
            ORDER BY date DESC
            LIMIT :window
        """),
        engine,
        params={"team": team, "window": XG_WINDOW}
    )

    if df.empty or len(df) < 3:
        return None

    xg_for_w     = 0.0
    xg_against_w = 0.0
    total_weight = 0.0

    for i, (_, row) in enumerate(df.iterrows()):

        weight = XG_DECAY ** i

        if row.home_team.lower() == team.lower():
            sot_for     = row.home_shots_target or 0
            shots_for   = max(0, (row.home_shots or 0) - sot_for)
            sot_ag      = row.away_shots_target or 0
            shots_ag    = max(0, (row.away_shots or 0) - sot_ag)
        else:
            sot_for     = row.away_shots_target or 0
            shots_for   = max(0, (row.away_shots or 0) - sot_for)
            sot_ag      = row.home_shots_target or 0
            shots_ag    = max(0, (row.home_shots or 0) - sot_ag)

        xg_for     = sot_for   * SHOT_ON_TARGET_RATE + shots_for * SHOT_RATE
        xg_against = sot_ag    * SHOT_ON_TARGET_RATE + shots_ag  * SHOT_RATE

        xg_for_w     += xg_for     * weight
        xg_against_w += xg_against * weight
        total_weight += weight

    if total_weight == 0:
        return None

    return {
        "xg_for":     round(xg_for_w / total_weight, 3),
        "xg_against": round(xg_against_w / total_weight, 3),
        "matches":    len(df)
    }
