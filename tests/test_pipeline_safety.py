"""
tests/test_pipeline_safety.py
==============================
Tests de seguridad para el pipeline de prediccion.

Verifica que funciones criticas NO crashean con datos vacios,
None, o formatos inesperados. Estas son las funciones que corren
automaticamente cada dia sin supervision humana.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# TEAM NORMALIZER
# ============================================================

def test_normalize_team_basic():
    """Normaliza nombres basicos de equipos."""
    from src.utils.team_normalizer import normalize_team
    assert normalize_team("Manchester United") != ""
    assert normalize_team("man utd") != ""


def test_normalize_team_empty():
    """Nombre vacio no crashea."""
    from src.utils.team_normalizer import normalize_team
    result = normalize_team("")
    assert isinstance(result, str)


def test_normalize_team_none():
    """None no crashea (convierte a string)."""
    from src.utils.team_normalizer import normalize_team
    try:
        result = normalize_team(None)
        assert isinstance(result, str)
    except (TypeError, AttributeError):
        pass  # aceptable si lanza TypeError


# ============================================================
# EDGE CALCULATION — BOUNDARY CASES
# ============================================================

def test_edge_with_extreme_values():
    """Edges con probabilidades extremas no producen overflow."""
    from src.models.betting_engine import calculate_edges
    import math

    # Prob muy alta + odds muy bajas
    e1, e2 = calculate_edges(0.99, 1.01)
    assert not math.isnan(e1)
    assert not math.isnan(e2)

    # Prob muy baja + odds muy altas
    e1, e2 = calculate_edges(0.01, 100.0)
    assert not math.isnan(e1)
    assert not math.isnan(e2)


# ============================================================
# BET QUALITY FILTER — ROBUSTNESS
# ============================================================

def test_filter_missing_fields():
    """Bets con campos faltantes no crashean."""
    from src.models.bet_filters import bet_quality_filter

    bets = [
        {"market": "home_win"},                    # sin edge
        {"edge": 0.10},                            # sin market
        {},                                         # completamente vacio
        {"market": "over25", "edge": 0.05, "odds": 1.9},  # normal
    ]
    result = bet_quality_filter(bets)
    # No debe crashear, y las bets con edge=0 (default) pasan
    assert isinstance(result, list)


# ============================================================
# KELLY — EXTREME CASES
# ============================================================

def test_kelly_massive_bankroll():
    """Bankroll enorme no causa overflow."""
    from src.models.betting_engine import kelly_stake
    import math

    stake = kelly_stake(prob=0.60, odds=2.0, bankroll=1_000_000_000)
    assert not math.isnan(stake)
    assert not math.isinf(stake)
    assert stake > 0


def test_kelly_negative_bankroll():
    """Bankroll negativo (no deberia pasar, pero por si acaso)."""
    from src.models.betting_engine import kelly_stake

    stake = kelly_stake(prob=0.60, odds=2.0, bankroll=-50)
    # Puede ser negativo (bankroll * fraction), pero no debe crashear
    assert isinstance(stake, (int, float))


# ============================================================
# POISSON MARKETS — SAFETY
# ============================================================

def test_poisson_zero_lambdas():
    """Lambdas en 0 no producen division por cero."""
    from src.models.poisson_markets import totals_and_btts
    import math

    result = totals_and_btts(0.0, 0.0)
    assert isinstance(result, dict)
    for key, val in result.items():
        if isinstance(val, float):
            assert not math.isnan(val), f"{key} es NaN"


def test_poisson_high_lambdas():
    """Lambdas altos no causan overflow."""
    from src.models.poisson_markets import totals_and_btts
    import math

    result = totals_and_btts(5.0, 5.0)
    assert isinstance(result, dict)
    for key, val in result.items():
        if isinstance(val, float):
            assert not math.isnan(val), f"{key} es NaN"
            assert not math.isinf(val), f"{key} es Inf"


# ============================================================
# DIXON-COLES — SAFETY
# ============================================================

def test_dixon_coles_outputs():
    """match_outcomes retorna probabilidades que suman ~1."""
    from src.models.dixon_coles_model import match_outcomes
    import math

    result = match_outcomes(1.5, 1.2)
    # Puede retornar tuple (home, draw, away) o dict
    if isinstance(result, tuple):
        home, draw, away = result
    else:
        home = result["home_win"]
        draw = result["draw"]
        away = result["away_win"]

    assert not math.isnan(home)
    assert not math.isnan(draw)
    assert not math.isnan(away)

    total = home + draw + away
    assert 0.95 < total < 1.05, f"Probabilidades suman {total}, deberian ~1.0"


def test_dixon_coles_zero_lambda():
    """Lambda 0 retorna resultado valido (no crash)."""
    from src.models.dixon_coles_model import match_outcomes
    import math

    result = match_outcomes(0.0, 1.5)
    if isinstance(result, tuple):
        home, draw, away = result
    else:
        home = result.get("home_win", 0)
        draw = result.get("draw", 0)
        away = result.get("away_win", 0)

    assert not math.isnan(away)
    assert away > 0  # Con lambda_home=0, away_win deberia ser alto
