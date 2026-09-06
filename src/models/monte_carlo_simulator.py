"""
Monte Carlo Match Simulator
===========================
Simulación de partidos con incertidumbre en los lambdas.

¿Por qué Monte Carlo si ya tenemos Poisson analítico?
  - El modelo Poisson analítico asume que lambda es EXACTAMENTE conocido.
  - En la práctica, lambda tiene error de estimación (~15% de CV).
  - Monte Carlo con lambda-noise captura esa incertidumbre →
    probabilidades más conservadoras y realistas para partidos difíciles.
  - También soporta líneas adicionales (1.5, 3.5) y corners de forma natural.

Cuando se usa como señal del ensemble:
  - Alta coincidencia MC ↔ Poisson analítico → confianza alta
  - Gran divergencia → el partido es incierto → reducir stake

Parámetros:
  - SIMULATIONS = 50,000  (balance precisión / velocidad)
  - LAMBDA_CV   = 0.15    (coeficiente de variación del lambda: 15%)
"""

import numpy as np

SIMULATIONS = 50_000
LAMBDA_CV   = 0.15       # 15% de incertidumbre en la estimación de lambda
RNG         = np.random.default_rng(seed=42)   # reproducible entre runs


def simulate_match(
    home_xg:     float,
    away_xg:     float,
    simulations: int = SIMULATIONS,
    lambda_cv:   float = LAMBDA_CV,
) -> dict:
    """
    Simula un partido usando distribución Poisson con ruido gaussiano en lambda.

    El ruido en lambda modela la incertidumbre del modelo:
      λ_sim ~ Normal(λ, λ * cv)  truncado a > 0

    Args:
        home_xg:     lambda esperado goles local
        away_xg:     lambda esperado goles visitante
        simulations: número de simulaciones
        lambda_cv:   coeficiente de variación del lambda (incertidumbre)

    Returns:
        {
            "home_win": float,  "draw": float,  "away_win": float,
            "over15":   float,  "over25": float, "over35": float,
            "btts":     float,
            "uncertainty": float  — desviación estándar de los resultados
                                    (alta = partido muy incierto)
        }
    """
    # ── Generar lambdas con incertidumbre ────────────────────────────────
    home_lambdas = np.abs(
        RNG.normal(home_xg, home_xg * lambda_cv, simulations)
    ).clip(0.01)

    away_lambdas = np.abs(
        RNG.normal(away_xg, away_xg * lambda_cv, simulations)
    ).clip(0.01)

    # ── Simular goles ─────────────────────────────────────────────────────
    home_goals = RNG.poisson(home_lambdas)
    away_goals = RNG.poisson(away_lambdas)

    total_goals = home_goals + away_goals

    # ── Resultados ────────────────────────────────────────────────────────
    home_win = float(np.mean(home_goals > away_goals))
    draw     = float(np.mean(home_goals == away_goals))
    away_win = float(np.mean(home_goals < away_goals))

    over15 = float(np.mean(total_goals > 1))
    over25 = float(np.mean(total_goals > 2))
    over35 = float(np.mean(total_goals > 3))

    btts = float(np.mean((home_goals > 0) & (away_goals > 0)))

    # ── Uncertainty score ─────────────────────────────────────────────────
    # Mide qué tan incierto es el resultado: alta varianza → partido abierto.
    # Calculado como la std de resultados codificados: 0=away, 1=draw, 2=home
    outcomes = np.where(
        home_goals > away_goals, 2,
        np.where(home_goals == away_goals, 1, 0)
    )
    uncertainty = float(np.std(outcomes))

    return {
        "home_win":    round(home_win, 4),
        "draw":        round(draw,     4),
        "away_win":    round(away_win, 4),
        "over15":      round(over15,   4),
        "over25":      round(over25,   4),
        "over35":      round(over35,   4),
        "btts":        round(btts,     4),
        "uncertainty": round(uncertainty, 4),
    }


def mc_confidence_vs_analytical(mc_probs: dict, analytical_probs: dict) -> float:
    """
    Calcula el nivel de acuerdo entre Monte Carlo y Poisson analítico.

    Si ambos coinciden → el modelo tiene alta confianza en sus predicciones.
    Si divergen → alta incertidumbre → reducir el peso del modelo.

    Args:
        mc_probs:         resultado de simulate_match()
        analytical_probs: {"home_win": float, "draw": float, "away_win": float}

    Returns:
        float en [0, 1] donde 1.0 = acuerdo perfecto
    """
    markets = ["home_win", "draw", "away_win"]

    total_diff = sum(
        abs(mc_probs.get(m, 0) - analytical_probs.get(m, 0))
        for m in markets
    )

    # max diff posible = 2.0 (todo en un lado)
    agreement = max(0.0, 1.0 - (total_diff / 2.0))
    return round(agreement, 3)
