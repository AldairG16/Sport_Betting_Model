"""
src/models/bet_filters.py
=========================
Filtro de calidad de apuestas — versión simplificada.

Los filtros principales (MIN_EDGE, MAX_ODDS, exclusive groups, disabled
markets) ahora viven en prediction_pipeline.py para evitar duplicación.

Este módulo solo aplica reglas de SANIDAD que deben correr ANTES de que
el pipeline ordene y seleccione las mejores bets por partido.
"""


# Umbral máximo de edge realista — edges >15% son casi siempre
# señal de que el modelo está roto o las odds son fijas/infladas.
MAX_REALISTIC_EDGE = 0.15

# Mercados donde el edge real rara vez supera 12%
# (mercados muy líquidos con líneas eficientes)
CAPPED_MARKETS = {"over25", "under25", "btts", "btts_no"}
CAPPED_EDGE = 0.12


def bet_quality_filter(bets):
    """
    Filtro de sanidad pre-selección.

    Reglas:
      1. Edge máximo realista (>15% = modelo roto)
      2. Cap de edge para mercados muy líquidos (over25, btts)

    NO filtra por MIN_EDGE ni MAX_ODDS — eso lo hace el pipeline.
    """
    if not bets:
        return bets

    filtered = []

    for bet in bets:
        edge = bet.get("edge", 0)
        market = bet.get("market", "")

        # ❌ Edge irreal — señal de modelo roto o odds fijas
        if edge > MAX_REALISTIC_EDGE:
            continue

        # ❌ Mercados líquidos con edge sospechosamente alto
        if market in CAPPED_MARKETS and edge > CAPPED_EDGE:
            continue

        filtered.append(bet)

    return filtered
