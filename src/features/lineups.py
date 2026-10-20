"""
src/features/lineups.py
=======================
GUARDIA DE ALINEACIONES: comparar el once confirmado con los goleadores.

Idea profesional: ~80% del movimiento real de cuotas ocurre cuando salen
las alineaciones (~40 min antes del partido). Si el goleador estelar de un
equipo NO está en el once confirmado, las apuestas de ese lado pierden su
base — hay que cancelarlas, no apostarlas a ciegas.

Flujo (lo llama revalidate_pending_bets en el closing):
  1. Los onces salen de ESPN (tabla lineups, src/features/espn_lineups.py).
  2. Se comparan contra los goleadores top del equipo (match_events, 0 coste).
  3. Si falta un goleador de élite → cancela la bet de ese lado.

Hasta el 20-oct-26 los onces venían de API-Football (plan gratuito); la
cuenta quedó suspendida el 16-oct y el filtro estaba apagado. ESPN es
gratis, no pide clave y ya se consulta para las alertas de alineación.
"""

import unicodedata


def _norm(name: str) -> str:
    n = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return " ".join(n.lower().replace("'", "").split())


def star_missing(lineup_surnames: set[str], top_scorers: list[str]) -> str | None:
    """Devuelve el nombre del goleador de élite ausente, o None.

    top_scorers: nombres completos (ya normalizados) ordenados por tasa.
    Un goleador 'está' si su apellido aparece entre los titulares.
    """
    for scorer in top_scorers:
        surname = _norm(scorer).split()[-1]
        if surname not in lineup_surnames:
            return scorer
    return None
