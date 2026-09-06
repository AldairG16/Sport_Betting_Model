"""
Asian Handicap Model
====================
Calcula probabilidades para mercados de handicap usando la distribución
de Poisson bivariada del modelo Dixon-Coles.

Mercados soportados:
  - Draw No Bet (DNB / AH 0):
      Home DNB: gana si home > away. Reembolso si empate.
      Away DNB: gana si away > home. Reembolso si empate.

  - Asian Handicap 0.5 (AH ±0.5):
      Home -0.5: home debe GANAR (= home_win 1X2). Sin empate posible.
      Away +0.5: away no debe PERDER (= away no pierde).
      Home +0.5: home no debe PERDER.
      Away -0.5: away debe GANAR (= away_win 1X2).

  - Asian Handicap por línea (AH -N / +N donde N = 0.5, 1, 1.5, 2 ...):
      Calcula P(home - away > -line) para cualquier línea dada.
      Maneja líneas enteras (posible push/reembolso) y medias (sin empate).

La ventaja principal:
  - El AH elimina el empate → mercado de 2 resultados → más fácil encontrar edge
  - Mejor para equipos con mucha diferencia de nivel
  - Win rate naturalmente más alto (50% sin edge vs 33% en 1X2)

Uso en el pipeline:
  probabilities["ah_home"] = p_home_ah(lambda_home, lambda_away, line)
  probabilities["ah_away"] = 1 - p_home_ah(lambda_home, lambda_away, line)
"""

import numpy as np
from scipy.stats import poisson


# ─── Constantes ───────────────────────────────────────────────────────────────
MAX_GOALS = 15   # máximo de goles a considerar en la distribución
MIN_PROB  = 1e-6


def _score_matrix(lambda_home: float, lambda_away: float) -> np.ndarray:
    """
    Matriz de probabilidades de marcadores [home_goals x away_goals].
    Forma: matrix[i][j] = P(home=i, away=j)
    """
    h = np.arange(MAX_GOALS + 1)
    a = np.arange(MAX_GOALS + 1)
    ph = poisson.pmf(h, lambda_home)
    pa = poisson.pmf(a, lambda_away)
    return np.outer(ph, pa)   # shape: (MAX+1, MAX+1)


def prob_dnb_home(lambda_home: float, lambda_away: float) -> float:
    """
    Draw No Bet — lado local.

    Retorna P(home gana | no hay empate).
    En la práctica la casa de apuestas reembolsa si empate.
    La probabilidad que usamos para calcular edge es solo P(home gana),
    pero la odd del mercado ya incorpora el reembolso implícito.

    DNB home price ≈ P(home wins) / (P(home wins) + P(away wins))
    """
    matrix = _score_matrix(lambda_home, lambda_away)
    p_home_win = float(np.sum(np.tril(matrix, k=-1)))   # home > away
    p_away_win = float(np.sum(np.triu(matrix, k=1)))    # away > home
    denom = p_home_win + p_away_win
    if denom < MIN_PROB:
        return 0.5
    return p_home_win / denom


def prob_dnb_away(lambda_home: float, lambda_away: float) -> float:
    """Draw No Bet — lado visitante."""
    return 1.0 - prob_dnb_home(lambda_home, lambda_away)


def prob_ah(lambda_home: float, lambda_away: float, line: float) -> tuple[float, float]:
    """
    Asian Handicap para cualquier línea.

    Args:
        lambda_home: goles esperados del local
        lambda_away: goles esperados del visitante
        line:        handicap aplicado al LOCAL (negativo = favorito)
                     Ejemplos: -0.5, -1, -1.5, +0.5, +1, +1.5

    Returns:
        (p_home_covers, p_away_covers)

    Lógica:
        line = -0.5  → home gana si home_goals > away_goals + 0.5
                        es decir, home_goals >= away_goals + 1  (= home win)
        line = -1.0  → home gana si home_goals > away_goals + 1
                        home_goals = away_goals + 1 → PUSH (reembolso mitad)
        line = -1.5  → home gana si home_goals > away_goals + 1.5
                        es decir, home_goals >= away_goals + 2
        line = +0.5  → home gana si home_goals + 0.5 > away_goals
                        es decir, home_goals >= away_goals  (home no pierde)
    """
    matrix = _score_matrix(lambda_home, lambda_away)

    # La condición para que el LOCAL cubra: home_goals - away_goals > -line
    # es decir: home_goals - away_goals > -line
    # margen = home_goals - away_goals (puede ser negativo)

    p_home  = 0.0
    p_away  = 0.0
    p_push  = 0.0

    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            prob  = matrix[i, j]
            margin = i - j   # positivo = home gana
            diff  = margin + line   # si diff > 0, home cubre

            if abs(diff) < 1e-9:
                # Línea exacta → push (si línea entera)
                p_push += prob
            elif diff > 0:
                p_home += prob
            else:
                p_away += prob

    # En push: mitad del stake se reembolsa (contamos 0.5 para cada lado)
    p_home += p_push * 0.5
    p_away += p_push * 0.5

    total = p_home + p_away
    if total < MIN_PROB:
        return 0.5, 0.5

    return float(p_home / total), float(p_away / total)


def find_best_ah_line(
    lambda_home: float,
    lambda_away: float,
    available_lines: list[float],
    market_odds_home: dict[float, float],
    market_odds_away: dict[float, float],
    min_edge: float = 0.04
) -> list[dict]:
    """
    Busca la línea de AH con mayor edge dado un conjunto de líneas disponibles.

    Args:
        lambda_home:       goles esperados local
        lambda_away:       goles esperados visitante
        available_lines:   líneas disponibles (ej: [-1.5, -0.5, 0.5, 1.5])
        market_odds_home:  {line: odd_home} para cada línea
        market_odds_away:  {line: odd_away} para cada línea
        min_edge:          edge mínimo para considerar value

    Returns:
        Lista de bets con value [{market, line, probability, odds, edge}]
    """
    value_bets = []

    for line in available_lines:
        p_home, p_away = prob_ah(lambda_home, lambda_away, line)

        # Revisar lado local
        if line in market_odds_home:
            odds_h = market_odds_home[line]
            if odds_h and odds_h > 1:
                implied = 1.0 / odds_h
                edge = p_home - implied
                if edge >= min_edge:
                    value_bets.append({
                        "market":      f"ah_home_{line:+.1f}",
                        "line":        line,
                        "side":        "home",
                        "probability": round(p_home, 4),
                        "odds":        odds_h,
                        "edge":        round(edge, 4),
                    })

        # Revisar lado visitante
        if line in market_odds_away:
            odds_a = market_odds_away[line]
            if odds_a and odds_a > 1:
                implied = 1.0 / odds_a
                edge = p_away - implied
                if edge >= min_edge:
                    value_bets.append({
                        "market":      f"ah_away_{line:+.1f}",
                        "line":        line,
                        "side":        "away",
                        "probability": round(p_away, 4),
                        "odds":        odds_a,
                        "edge":        round(edge, 4),
                    })

    return sorted(value_bets, key=lambda x: x["edge"], reverse=True)


def get_dnb_probs(lambda_home: float, lambda_away: float) -> dict:
    """
    Retorna probabilidades DNB para ambos lados.

    Returns:
        {"dnb_home": float, "dnb_away": float}
    """
    ph = prob_dnb_home(lambda_home, lambda_away)
    return {
        "dnb_home": round(ph, 4),
        "dnb_away": round(1.0 - ph, 4),
    }
