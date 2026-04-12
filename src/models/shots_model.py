"""
Shots on Target Model
=====================
Modelo Poisson para predecir tiros al arco (shots on target) por partido.

¿Por qué es útil?
  - Los tiros al arco son un predictor adelantado de goles (xG implícito)
  - Un equipo con muchos SOT pero pocos goles está "debido" a convertir
  - Mercado shots on target es eficiente pero menos cubierto que 1x2

Diferencias vs modelo de goles:
  - SIN corrección Dixon-Coles
  - La suma de SOT home + away sigue distribución Poisson con λ = λ1 + λ2
  - Home advantage menor (~1.08)
  - Baseline ~3.5 SOT por equipo (EPL promedio)

Mercados cubiertos:
  - Total SOT: Over/Under 4.5 / 5.5 / 6.5
  - Correlación con goles: si lambda_shots_ratio >> lambda_goals_ratio,
    el equipo está "creando pero no convirtiendo"

Uso en el pipeline:
  - Ajuste de confianza basado en dominancia de tiros
  - Correlación con over/under de goles
"""

import numpy as np
from scipy.stats import poisson

HOME_SHOT_ADVANTAGE = 1.08
SHOTS_BASELINE      = 3.5     # media europea SOT por equipo por partido


def predict_shots(
    home_attack:    float,
    home_defense:   float,
    away_attack:    float,
    away_defense:   float,
    home_advantage: float = HOME_SHOT_ADVANTAGE,
) -> dict:
    """
    Predice distribución de tiros al arco para un partido.

    Args:
        home_attack:    attack_rating del local  (shots_for / baseline)
        home_defense:   defense_rating del local (shots_against / baseline)
        away_attack:    attack_rating del visitante
        away_defense:   defense_rating del visitante
        home_advantage: multiplicador de ventaja local para SOT

    Returns:
        {
            "lambda_home":  float,
            "lambda_away":  float,
            "lambda_total": float,
            "over45":  float, "under45":  float,
            "over55":  float, "under55":  float,
            "over65":  float, "under65":  float,
            "home_dominance": float,
            "pressure_index": float  — SOT ratio vs expected goals ratio
        }
    """
    lambda_home  = home_attack * away_defense * home_advantage * SHOTS_BASELINE
    lambda_away  = away_attack * home_defense * SHOTS_BASELINE

    # Caps razonables
    lambda_home  = max(0.5, min(lambda_home, 10.0))
    lambda_away  = max(0.5, min(lambda_away,  9.0))
    lambda_total = lambda_home + lambda_away

    result = {
        "lambda_home":  round(lambda_home,  2),
        "lambda_away":  round(lambda_away,  2),
        "lambda_total": round(lambda_total, 2),
    }

    # ============================
    # TOTAL SOT  (Poisson con λ_total)
    # ============================
    for line in [4.5, 5.5, 6.5]:
        k     = int(line)
        over  = float(1 - poisson.cdf(k, lambda_total))
        under = float(poisson.cdf(k, lambda_total))
        key   = str(line).replace(".", "")
        result[f"over{key}"]  = round(over,  4)
        result[f"under{key}"] = round(under, 4)

    # ============================
    # DOMINANCIA LOCAL
    # ============================
    result["home_dominance"] = round(lambda_home / lambda_total, 3)

    # ============================
    # PRESSURE INDEX
    # ============================
    # > 1.0 → el equipo crea más tiros de lo que convierte (presión alta)
    # Se usa como señal auxiliar para over 2.5 goles
    expected_goals_proxy = (lambda_home + lambda_away) * 0.30
    pressure_index = round(lambda_total / max(expected_goals_proxy, 0.1), 3)
    result["pressure_index"] = pressure_index

    return result


def shots_to_confidence_signal(shots: dict) -> float:
    """
    Convierte las predicciones de SOT en ajuste de confianza para el pipeline.

    Lógica:
      - pressure_index alto (> 2.5): muchos tiros → partido abierto → boost over 2.5
      - home_dominance >= 0.60 → refuerza home_win (+0.03)
      - home_dominance <= 0.40 → refuerza away_win (+0.03)

    Returns:
        float en [-0.05, +0.05]
    """
    dominance = shots.get("home_dominance", 0.5)

    if dominance >= 0.62:
        return +0.03
    elif dominance <= 0.38:
        return -0.03
    else:
        return 0.0
