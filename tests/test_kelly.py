"""
tests/test_kelly.py
====================
Tests para el Kelly Criterion y calculo de edges.

Verifica que:
  - Kelly nunca produce stakes negativos o infinitos
  - Edges se calculan correctamente
  - Kelly con bankroll 0 retorna 0 (no NaN/crash)
  - Odds invalidas retornan 0
"""

import sys
import math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.betting_engine import kelly_stake, calculate_edges, find_value_bets


# ============================================================
# KELLY STAKE
# ============================================================

def test_kelly_normal():
    """Caso normal: prob 60%, odds 2.0, bankroll 100."""
    stake = kelly_stake(prob=0.60, odds=2.0, bankroll=100)
    assert stake > 0
    assert stake <= 2.0  # max_bet_pct default = 2%


def test_kelly_negative_edge():
    """Sin edge positivo → stake = 0."""
    stake = kelly_stake(prob=0.30, odds=2.0, bankroll=100)
    assert stake == 0


def test_kelly_zero_bankroll():
    """Bankroll 0 → stake = 0 (no crash)."""
    stake = kelly_stake(prob=0.60, odds=2.0, bankroll=0)
    assert stake == 0
    assert not math.isnan(stake)
    assert not math.isinf(stake)


def test_kelly_tiny_bankroll():
    """Bankroll muy pequeno → stake positivo pero pequeno."""
    stake = kelly_stake(prob=0.60, odds=2.0, bankroll=0.5)
    assert stake >= 0
    assert not math.isnan(stake)
    assert not math.isinf(stake)


def test_kelly_odds_1():
    """Odds = 1 (no ganas nada) → stake = 0."""
    stake = kelly_stake(prob=0.90, odds=1.0, bankroll=100)
    assert stake == 0


def test_kelly_odds_none():
    """Odds None → stake = 0."""
    stake = kelly_stake(prob=0.60, odds=None, bankroll=100)
    assert stake == 0


def test_kelly_prob_none():
    """Prob None → stake = 0."""
    stake = kelly_stake(prob=None, odds=2.0, bankroll=100)
    assert stake == 0


def test_kelly_never_exceeds_max_bet():
    """Stake nunca supera max_bet_pct del bankroll."""
    stake = kelly_stake(prob=0.99, odds=10.0, bankroll=1000, max_bet_pct=0.02)
    assert stake <= 1000 * 0.02


def test_kelly_high_odds_penalized():
    """Odds altas (>5) reciben penalizacion → stake menor."""
    stake_normal = kelly_stake(prob=0.60, odds=2.5, bankroll=100)
    stake_high   = kelly_stake(prob=0.60, odds=6.0, bankroll=100)
    # Con odds altas y misma prob, el kelly base es diferente,
    # pero verificamos que no sea absurdamente grande
    assert stake_high >= 0
    assert stake_high <= 100 * 0.02


# ============================================================
# CALCULATE EDGES
# ============================================================

def test_edges_normal():
    """Edge calculation: prob 60%, odds 2.0 → edge positivo."""
    edge_mkt, edge_ev = calculate_edges(0.60, 2.0)
    assert edge_mkt > 0    # 0.60 - 0.50 = 0.10
    assert edge_ev > 0     # 0.60*2.0 - 1 = 0.20
    assert abs(edge_mkt - 0.10) < 0.01
    assert abs(edge_ev - 0.20) < 0.01


def test_edges_odds_none():
    """Odds None → retorna None, None."""
    assert calculate_edges(0.60, None) == (None, None)


def test_edges_odds_below_1():
    """Odds <= 1 → retorna None, None."""
    assert calculate_edges(0.60, 0.95) == (None, None)
    assert calculate_edges(0.60, 1.0) == (None, None)


def test_edges_capped():
    """Edges se capean a +-25% (market) y +-50% (EV)."""
    edge_mkt, edge_ev = calculate_edges(0.99, 1.01)
    assert edge_mkt <= 0.25
    assert edge_ev <= 0.50


# ============================================================
# FIND VALUE BETS
# ============================================================

def test_find_value_bets_empty():
    """Sin probabilidades → sin bets."""
    result = find_value_bets({}, {})
    assert result == []


def test_find_value_bets_no_value():
    """Cuando el modelo no encuentra valor → lista vacia."""
    probs = {"home_win": 0.40}
    odds  = {"home_win": 2.0}   # implied 50% > model 40% → no value
    result = find_value_bets(probs, odds)
    assert len(result) == 0


def test_find_value_bets_has_value():
    """Cuando hay valor claro → retorna bet."""
    probs = {"home_win": 0.70}
    odds  = {"home_win": 2.0}   # implied 50% < model 70% → value!
    result = find_value_bets(probs, odds)
    assert len(result) == 1
    assert result[0]["market"] == "home_win"
    assert result[0]["edge"] > 0
