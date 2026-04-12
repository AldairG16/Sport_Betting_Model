"""
Corners Model
=============
Modelo Poisson para predecir tiros de esquina por partido.

¿Por qué Poisson?
  Los córners son eventos discretos independientes dentro de un partido —
  exactamente el supuesto que necesita Poisson. La media lambda se estima
  multiplicando el attack_rating del equipo ofensivo por el defense_rating
  del rival (igual que goles, pero con baseline de córners).

Diferencias vs modelo de goles:
  - SIN corrección Dixon-Coles (no hay clustering en bajos córners)
  - La suma de dos Poisson independientes es Poisson con λ = λ1 + λ2
  - Home advantage menor (~1.05 vs 1.20 para goles)

Mercados cubiertos:
  - Total corners: Over/Under 8.5 / 9.5 / 10.5
  - Home corners:  Over/Under 4.5 / 5.5
  - Away corners:  Over/Under 3.5 / 4.5

Uso en el pipeline:
  - Las lambdas se comparan con la dominancia de córners para calibrar
    la confianza en apuestas de 1x2 y totals de goles.
  - Si hay cuotas de córners disponibles, se puede apostar directamente.
"""

import numpy as np
from scipy.stats import poisson

HOME_CORNER_ADVANTAGE = 1.05    # local gana ~5% más córners
CORNERS_BASELINE      = 5.0     # media europea de córners por equipo


def predict_corners(
    home_attack:    float,
    home_defense:   float,
    away_attack:    float,
    away_defense:   float,
    home_advantage: float = HOME_CORNER_ADVANTAGE,
) -> dict:
    """
    Predice distribución de córners para un partido.

    Args:
        home_attack:    attack_rating del local  (corners_for / baseline)
        home_defense:   defense_rating del local (corners_against / baseline)
        away_attack:    attack_rating del visitante
        away_defense:   defense_rating del visitante
        home_advantage: multiplicador de ventaja local para córners

    Returns:
        {
            "lambda_home":  float  — esperanza córners local
            "lambda_away":  float  — esperanza córners visitante
            "lambda_total": float  — esperanza total
            "over85":  float, "under85":  float,
            "over95":  float, "under95":  float,
            "over105": float, "under105": float,
            "home_over45": float, "home_under45": float,
            "home_over55": float, "home_under55": float,
            "away_over35": float, "away_under35": float,
            "away_over45": float, "away_under45": float,
            "home_dominance": float  — ratio lambda_home / lambda_total
        }
    """
    lambda_home  = home_attack * away_defense * home_advantage * CORNERS_BASELINE
    lambda_away  = away_attack * home_defense * CORNERS_BASELINE

    # Caps razonables (rarísimo ver >12 corners por equipo)
    lambda_home  = max(1.0, min(lambda_home, 12.0))
    lambda_away  = max(1.0, min(lambda_away, 10.0))
    lambda_total = lambda_home + lambda_away

    result = {
        "lambda_home":  round(lambda_home,  2),
        "lambda_away":  round(lambda_away,  2),
        "lambda_total": round(lambda_total, 2),
    }

    # ============================
    # TOTAL CORNERS  (suma de dos Poisson = Poisson con λ_total)
    # ============================
    for line in [8.5, 9.5, 10.5]:
        k   = int(line)          # P(X > 8.5) = P(X >= 9) = 1 - CDF(8)
        over  = float(1 - poisson.cdf(k, lambda_total))
        under = float(poisson.cdf(k, lambda_total))
        key   = str(line).replace(".", "")
        result[f"over{key}"]  = round(over,  4)
        result[f"under{key}"] = round(under, 4)

    # ============================
    # HOME CORNERS
    # ============================
    for line in [4.5, 5.5]:
        k     = int(line)
        over  = float(1 - poisson.cdf(k, lambda_home))
        under = float(poisson.cdf(k, lambda_home))
        key   = str(line).replace(".", "")
        result[f"home_over{key}"]  = round(over,  4)
        result[f"home_under{key}"] = round(under, 4)

    # ============================
    # AWAY CORNERS
    # ============================
    for line in [3.5, 4.5]:
        k     = int(line)
        over  = float(1 - poisson.cdf(k, lambda_away))
        under = float(poisson.cdf(k, lambda_away))
        key   = str(line).replace(".", "")
        result[f"away_over{key}"]  = round(over,  4)
        result[f"away_under{key}"] = round(under, 4)

    # ============================
    # DOMINANCIA LOCAL
    # ============================
    result["home_dominance"] = round(lambda_home / lambda_total, 3)

    return result


def corners_to_confidence_signal(corners: dict) -> float:
    """
    Convierte las predicciones de córners en un ajuste de confianza
    para usar en el pipeline de predicciones.

    Lógica:
      - Dominancia de córners >= 0.60 → refuerza apuesta home_win (+0.05)
      - Dominancia de córners <= 0.40 → refuerza apuesta away_win (+0.05)
      - Caso contrario → sin ajuste (0.0)

    Returns:
        float en [-0.05, +0.05] — boost de confianza para 1x2
    """
    dominance = corners.get("home_dominance", 0.5)

    if dominance >= 0.60:
        return +0.04
    elif dominance <= 0.40:
        return -0.04
    else:
        return 0.0
