"""
tests/test_bet_filters.py
==========================
Tests para el filtro de calidad de apuestas.

Verifica que:
  - Edges irrealistas (>15%) se bloquean
  - Mercados liquidos con edge alto se bloquean
  - Bets normales pasan sin problemas
  - Lista vacia retorna lista vacia
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.bet_filters import bet_quality_filter, MAX_REALISTIC_EDGE, CAPPED_EDGE


def test_empty_input():
    """Lista vacia retorna lista vacia."""
    assert bet_quality_filter([]) == []
    assert bet_quality_filter(None) is None


def test_normal_bets_pass():
    """Bets con edge razonable pasan el filtro."""
    bets = [
        {"market": "home_win", "edge": 0.10, "odds": 2.0},
        {"market": "draw",     "edge": 0.12, "odds": 3.5},
        {"market": "over25",   "edge": 0.08, "odds": 1.95},
    ]
    result = bet_quality_filter(bets)
    assert len(result) == 3


def test_blocks_unrealistic_edge():
    """Edge > 15% se bloquea (modelo roto o odds fijas)."""
    bets = [
        {"market": "home_win", "edge": 0.20, "odds": 2.0},   # 20% edge = sospechoso
        {"market": "home_win", "edge": 0.16, "odds": 1.8},   # 16% = sospechoso
        {"market": "home_win", "edge": 0.14, "odds": 2.5},   # 14% = OK
    ]
    result = bet_quality_filter(bets)
    assert len(result) == 1
    assert result[0]["edge"] == 0.14


def test_blocks_capped_markets():
    """Mercados liquidos (over25, btts) con edge > 12% se bloquean."""
    bets = [
        {"market": "over25",   "edge": 0.13, "odds": 1.95},  # > 12% = bloqueado
        {"market": "btts",     "edge": 0.13, "odds": 1.80},  # > 12% = bloqueado
        {"market": "under25",  "edge": 0.11, "odds": 2.10},  # 11% = OK
        {"market": "btts_no",  "edge": 0.05, "odds": 2.00},  # 5% = OK
    ]
    result = bet_quality_filter(bets)
    assert len(result) == 2
    assert all(b["edge"] <= CAPPED_EDGE for b in result)


def test_non_capped_market_high_edge():
    """home_win con edge 14% pasa (no esta en CAPPED_MARKETS)."""
    bets = [{"market": "home_win", "edge": 0.14, "odds": 2.0}]
    result = bet_quality_filter(bets)
    assert len(result) == 1


def test_edge_exactly_at_threshold():
    """Edge exactamente en el umbral pasa (usamos > no >=)."""
    bets = [
        {"market": "home_win", "edge": MAX_REALISTIC_EDGE, "odds": 2.0},
        {"market": "over25",   "edge": CAPPED_EDGE,        "odds": 1.95},
    ]
    result = bet_quality_filter(bets)
    assert len(result) == 2
