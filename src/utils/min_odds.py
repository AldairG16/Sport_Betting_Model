"""
src/utils/min_odds.py
=====================
Cuota mínima a la que un pick todavía vale la pena apostarlo.

Por qué (24-sep-26): la cuota que muestra el sistema es la MEJOR entre
~20 casas europeas (The Odds API). El dueño apuesta en PlayDoit, que la API
no cubre y casi nunca iguala esa cuota. Sin una referencia, parte del edge
se perdía al apostar sin saberlo. Con la probabilidad del pick, la cuota
mínima es la que deja un edge ≥ MIN_EDGE_TO_PLACE:

    p − 1/cuota ≥ min_edge   ⇔   cuota ≥ 1 / (p − min_edge)

Es el mismo umbral con el que la revalidación pre-kickoff decide mantener
una bet cuya cuota empeoró (scripts/revalidate_pending_bets.KEEP_EDGE).
"""

import math

MIN_EDGE_TO_PLACE = 0.02


def min_odds(prob, min_edge: float = MIN_EDGE_TO_PLACE) -> float | None:
    """Cuota mínima redondeada HACIA ARRIBA a 2 decimales (apostar justo en
    el número mostrado nunca deja menos edge que el umbral). None si la
    probabilidad no alcanza para ningún precio."""
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    if not (0 < p < 1) or p - min_edge <= 0:
        return None
    return math.ceil(100.0 / (p - min_edge) - 1e-9) / 100.0
