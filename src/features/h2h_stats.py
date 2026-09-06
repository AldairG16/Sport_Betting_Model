"""
Head-to-Head (H2H) Stats
========================
Analiza los últimos enfrentamientos directos entre dos equipos
para generar factores de ajuste en los lambdas del modelo Poisson.

Por qué importa el H2H:
  - Ciertos equipos tienen dinámicas específicas entre sí (derbies,
    rivalidades históricas, estilos de juego que se cancelan)
  - El H2H captura patrones que el ELO y la forma individual no ven
  - Se aplica con peso conservador (15%) para no sobreajustar
"""

import numpy as np
import pandas as pd
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team

# =========================
# CONSTANTES
# =========================

H2H_WINDOW = 8     # Últimos 8 encuentros directos (suficientes sin ruido)
H2H_DECAY  = 0.85  # Decay: encuentros recientes pesan más
H2H_MIN    = 3     # Mínimo de encuentros para confiar en la señal


# =========================
# MAIN FUNCTION
# =========================

def get_h2h_stats(home_team: str, away_team: str, cutoff_date=None) -> dict | None:
    """
    Calcula estadísticas H2H entre home_team y away_team.

    Perspectiva: siempre desde el equipo que juega de LOCAL hoy
    (home_team), independientemente de cómo jugaron en el pasado.

    Args:
        home_team:    nombre del equipo local
        away_team:    nombre del equipo visitante
        cutoff_date:  opcional. Si se pasa, excluye partidos en date >= cutoff.
                      CRÍTICO para backtest: evita temporal leakage.

    Returns:
        {
            "h2h_home_goals":    float  — goles promedio del local en H2H
            "h2h_away_goals":    float  — goles promedio del visitante en H2H
            "h2h_home_win_rate": float  — tasa de victoria del local en H2H [0,1]
            "h2h_draw_rate":     float  — tasa de empates en H2H [0,1]
            "h2h_avg_goals":     float  — total goles promedio por partido H2H
            "h2h_home_factor":   float  — multiplicador de lambda_home [0.85, 1.15]
            "h2h_goal_factor":   float  — multiplicador de goles totales [0.80, 1.25]
            "h2h_matches":       int    — encuentros usados
        }
        None si hay menos de H2H_MIN encuentros.
    """
    home = normalize_team(home_team)
    away = normalize_team(away_team)

    where_clause = """
        ((LOWER(home_team) = LOWER(:home) AND LOWER(away_team) = LOWER(:away))
         OR (LOWER(home_team) = LOWER(:away) AND LOWER(away_team) = LOWER(:home)))
    """
    params = {"home": home, "away": away, "window": H2H_WINDOW}
    if cutoff_date is not None:
        where_clause += " AND date < :cutoff"
        params["cutoff"] = pd.to_datetime(cutoff_date).strftime("%Y-%m-%d")

    df = pd.read_sql(
        text(f"""
            SELECT home_team, away_team, home_goals, away_goals, date
            FROM matches
            WHERE {where_clause}
            ORDER BY date DESC
            LIMIT :window
        """),
        engine,
        params=params
    )

    if df.empty or len(df) < H2H_MIN:
        return None

    home_goals_w = 0.0
    away_goals_w = 0.0
    total_weight = 0.0
    home_wins    = 0
    draws        = 0
    n            = len(df)

    for i, (_, row) in enumerate(df.iterrows()):

        weight = H2H_DECAY ** i

        # Siempre desde perspectiva del "home" actual
        if row.home_team.lower() == home.lower():
            hg = row.home_goals
            ag = row.away_goals
        else:
            hg = row.away_goals
            ag = row.home_goals

        home_goals_w += hg * weight
        away_goals_w += ag * weight
        total_weight += weight

        if hg > ag:
            home_wins += 1
        elif hg == ag:
            draws += 1

    if total_weight == 0:
        return None

    h2h_home_goals    = home_goals_w / total_weight
    h2h_away_goals    = away_goals_w / total_weight
    h2h_home_win_rate = home_wins / n
    h2h_draw_rate     = draws / n
    h2h_avg_goals     = h2h_home_goals + h2h_away_goals

    # =============================================
    # FACTORES DE AJUSTE
    # =============================================
    # h2h_home_factor: si en H2H el local suele ganar → boost sutil
    # Rango [0.85, 1.15] para no sobreajustar
    h2h_home_factor = float(np.clip(0.85 + h2h_home_win_rate * 0.30, 0.85, 1.15))

    # h2h_goal_factor: si los H2H son high-scoring o low-scoring
    # Normalizado vs 2.5 goles/partido (media europea)
    h2h_goal_factor = float(np.clip(h2h_avg_goals / 2.5, 0.80, 1.25))

    return {
        "h2h_home_goals":    round(h2h_home_goals, 3),
        "h2h_away_goals":    round(h2h_away_goals, 3),
        "h2h_home_win_rate": round(h2h_home_win_rate, 3),
        "h2h_draw_rate":     round(h2h_draw_rate, 3),
        "h2h_avg_goals":     round(h2h_avg_goals, 3),
        "h2h_home_factor":   round(h2h_home_factor, 3),
        "h2h_goal_factor":   round(h2h_goal_factor, 3),
        "h2h_matches":       n
    }
