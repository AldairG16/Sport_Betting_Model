"""
Cards Model
===========
Modelo Poisson para predecir tarjetas amarillas totales por partido.

Misma lógica que corners_model.py:
  lambda_home = home_attack × away_defense × CARDS_BASELINE
  lambda_away = away_attack × home_defense × CARDS_BASELINE
  lambda_total = lambda_home + lambda_away

  P(over X) = 1 - Poisson.cdf(floor(X), lambda_total)

Mercados cubiertos:
  - Total tarjetas: Over/Under 3.5 / 4.5 / 5.5

Uso en el pipeline:
  - Se apuesta cuando el modelo tiene edge vs CARDS_DEFAULT_ODDS (1.80)
  - Sin fetch de odds extra — ahorro de créditos API
"""

import numpy as np
from scipy.stats import poisson

CARDS_BASELINE = 2.0    # promedio de amarillas por equipo por partido
CARDS_HOME_ADV = 0.95   # equipo visitante recibe ligeramente más tarjetas


def predict_cards(
    home_attack:    float,
    home_defense:   float,
    away_attack:    float,
    away_defense:   float,
) -> dict:
    """
    Predice distribución de tarjetas totales para un partido.

    Args:
        home_attack:    attack_rating del local  (cards_received / baseline)
        home_defense:   defense_rating del local (cards provoked / baseline)
        away_attack:    attack_rating del visitante
        away_defense:   defense_rating del visitante

    Returns:
        {
            "lambda_home":  float  — esperanza tarjetas local
            "lambda_away":  float  — esperanza tarjetas visitante
            "lambda_total": float  — esperanza total
            "over35":  float, "under35":  float,
            "over45":  float, "under45":  float,
            "over55":  float, "under55":  float,
        }
    """
    lambda_home  = home_attack  * away_defense * CARDS_HOME_ADV * CARDS_BASELINE
    lambda_away  = away_attack  * home_defense * (2.0 - CARDS_HOME_ADV) * CARDS_BASELINE

    # Caps razonables (rarísimo ver >8 amarillas por equipo)
    lambda_home  = max(0.5, min(lambda_home, 8.0))
    lambda_away  = max(0.5, min(lambda_away, 8.0))
    lambda_total = lambda_home + lambda_away

    result = {
        "lambda_home":  round(lambda_home,  2),
        "lambda_away":  round(lambda_away,  2),
        "lambda_total": round(lambda_total, 2),
    }

    for line in [3.5, 4.5, 5.5]:
        k     = int(line)
        over  = float(1 - poisson.cdf(k, lambda_total))
        under = float(poisson.cdf(k, lambda_total))
        key   = str(line).replace(".", "")
        result[f"over{key}"]  = round(over,  4)
        result[f"under{key}"] = round(under, 4)

    return result
