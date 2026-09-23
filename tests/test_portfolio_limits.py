"""
tests/test_portfolio_limits.py
==============================
Límites de cartera del slate (_apply_portfolio_limits, extraída del
pipeline el 22-sep-26): correlación, sospechosas, exposición ≤ 15% del
bankroll, penalización por concentración y tope acumulado por fecha.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.pipeline.prediction_pipeline as pp


def _bet(match, market="home_win", odds=2.0, prob=0.55, stake=2.0, date="2030-01-05"):
    return {"match": match, "market": market, "odds": odds, "probability": prob,
            "edge": prob - 1 / odds, "stake": stake, "match_date": date}


@pytest.fixture
def no_pending(monkeypatch):
    monkeypatch.setattr(pp.pd, "read_sql", lambda *a, **k: pd.DataFrame({"d": [], "s": []}))


def test_two_bets_on_same_match_are_scaled_by_sqrt_n(no_pending):
    out = pp._apply_portfolio_limits([_bet("a vs b"), _bet("a vs b", "over25")], 100.0)
    assert [b["stake"] for b in out] == [1.41, 1.41]      # 2.0 / √2


def test_suspicious_bets_are_removed(no_pending):
    bets = [_bet("a vs b", prob=0.55),
            _bet("c vs d", odds=6.0, prob=0.40),           # > 2× la implícita (0.167)
            {**_bet("e vs f"), "edge": 0.50}]              # edge en el tope artificial
    out = pp._apply_portfolio_limits(bets, 100.0)
    assert [b["match"] for b in out] == ["a vs b"]


def test_total_exposure_is_capped_at_15_percent(no_pending):
    bets = [_bet(f"m{i} vs x", market=m, stake=2.0)
            for i, m in enumerate(["home_win", "over25", "btts", "draw",
                                   "under25", "btts_no", "dnb_away", "dc_1x", "h1_home", "ah_home_+0.50"])]
    out = pp._apply_portfolio_limits(bets, 100.0)          # 20u pedidos
    assert sum(b["stake"] for b in out) <= 15.0 + 0.05


def test_dominant_market_is_penalized(no_pending):
    bets = [_bet(f"m{i} vs x", stake=2.0) for i in range(4)] + [_bet("z vs y", "over25", stake=1.0)]
    out = pp._apply_portfolio_limits(bets, 100.0)          # home_win = 89% del stake
    assert {b["stake"] for b in out if b["market"] == "home_win"} == {1.5}
    assert [b["stake"] for b in out if b["market"] == "over25"] == [1.0]


def test_slate_cap_counts_already_pending_stake(monkeypatch):
    monkeypatch.setattr(pp.pd, "read_sql",
                        lambda *a, **k: pd.DataFrame({"d": ["2030-01-05"], "s": [14.0]}))
    out = pp._apply_portfolio_limits([_bet("a vs b", stake=2.0),
                                      _bet("c vs d", stake=2.0, date="2030-01-06")], 100.0)
    by_date = {b["match_date"]: b["stake"] for b in out}
    assert by_date["2030-01-05"] == 1.0                    # solo quedaba 1u de las 15u
    assert by_date["2030-01-06"] == 2.0                    # otra fecha: intacta


def test_slate_cap_is_skipped_without_db(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("sin DB")
    monkeypatch.setattr(pp.pd, "read_sql", boom)
    out = pp._apply_portfolio_limits([_bet("a vs b")], 100.0)
    assert out[0]["stake"] == 2.0
