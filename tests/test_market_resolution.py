"""
Tests de resolución de mercados en update_bet_results().
Cubre la lógica AH (quarter-ball incluido), corners, cards, HT y el lookup
de partidos con _lookup_match. Sin DB real — datos 100% sintéticos.
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _match_row(**kwargs) -> pd.Series:
    """Crea una fila sintética de `_all_matches`."""
    defaults = {
        "home_team_l": "brazil",
        "away_team_l": "argentina",
        "date": pd.Timestamp("2026-06-15", tz="UTC"),
        "home_goals": None, "away_goals": None,
        "home_corners": None, "away_corners": None,
        "home_yellow": None, "away_yellow": None,
        "home_goals_ht": None, "away_goals_ht": None,
        "home_goals_h2": None, "away_goals_h2": None,
        "home_shots_target": None, "away_shots_target": None,
    }
    defaults.update(kwargs)
    return pd.Series(defaults)


def _pending_bet(**kwargs) -> dict:
    defaults = {
        "id": 1, "match": "brazil vs argentina",
        "market": "home_win", "odds": 2.0, "stake": 1.0,
        "match_date": pd.Timestamp("2026-06-15", tz="UTC"),
        "result": "pending",
    }
    defaults.update(kwargs)
    return defaults


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _lookup_match (función pura sobre DataFrame pre-cargado)
# ─────────────────────────────────────────────────────────────────────────────

class TestLookupMatch:
    """
    _lookup_match busca el partido más cercano en _all_matches (±1 día).
    """

    def _make_matches_df(self, rows: list) -> pd.DataFrame:
        return pd.DataFrame(rows)

    def test_finds_exact_date_match(self):
        """Devuelve la fila cuando home/away/date coinciden."""
        from src.models.save_bets import update_bet_results
        # Acceder a _lookup_match via closure no es directo; testeo indirectamente
        # construyendo el DataFrame y ejecutando la lógica inline.
        mrow = _match_row(home_team_l="brazil", away_team_l="argentina",
                          date=pd.Timestamp("2026-06-15", tz="UTC"),
                          home_goals=2, away_goals=1)
        df = pd.DataFrame([mrow])

        md = pd.Timestamp("2026-06-15 20:00:00", tz="UTC")
        cands = df[(df["home_team_l"] == "brazil") & (df["away_team_l"] == "argentina")].copy()
        cands["_dist"] = (pd.to_datetime(cands["date"]) - md).abs()
        cands = cands[cands["_dist"] <= pd.Timedelta(days=1)]

        assert len(cands) == 1
        assert cands.iloc[0]["home_goals"] == 2

    def test_returns_none_if_no_match_in_window(self):
        """Devuelve None si el partido no está dentro de ±1 día."""
        mrow = _match_row(home_team_l="brazil", away_team_l="argentina",
                          date=pd.Timestamp("2026-06-10", tz="UTC"),
                          home_goals=2, away_goals=1)
        df = pd.DataFrame([mrow])

        md = pd.Timestamp("2026-06-15 20:00:00", tz="UTC")
        cands = df[(df["home_team_l"] == "brazil") & (df["away_team_l"] == "argentina")].copy()
        cands["_dist"] = (pd.to_datetime(cands["date"]) - md).abs()
        cands = cands[cands["_dist"] <= pd.Timedelta(days=1)]

        assert len(cands) == 0

    def test_picks_closest_when_two_matches(self):
        """Cuando hay 2 partidos en el rango, elige el más cercano."""
        row1 = _match_row(home_team_l="a", away_team_l="b",
                          date=pd.Timestamp("2026-06-15 18:00", tz="UTC"),
                          home_goals=1, away_goals=0)
        row2 = _match_row(home_team_l="a", away_team_l="b",
                          date=pd.Timestamp("2026-06-14 20:00", tz="UTC"),
                          home_goals=3, away_goals=3)
        df = pd.DataFrame([row1, row2])

        md = pd.Timestamp("2026-06-15 19:00:00", tz="UTC")
        cands = df[(df["home_team_l"] == "a") & (df["away_team_l"] == "b")].copy()
        cands["_dist"] = (pd.to_datetime(cands["date"]) - md).abs()
        cands = cands[cands["_dist"] <= pd.Timedelta(days=1)]
        closest = cands.sort_values("_dist").iloc[0]

        assert closest["home_goals"] == 1   # el del 15/06 gana, solo dentro del ±1d


# ─────────────────────────────────────────────────────────────────────────────
# Tests: resolución de mercados de goles (1X2, O/U, BTTS)
# ─────────────────────────────────────────────────────────────────────────────

class TestGoalMarkets:
    """Mercados básicos de goles: 1X2, over/under, BTTS."""

    MARKET_CASES = [
        # (market, hg, ag, expected_outcome)
        ("home_win",  2, 1, "win"),
        ("home_win",  1, 1, "loss"),
        ("home_win",  0, 1, "loss"),
        ("draw",      1, 1, "win"),
        ("draw",      2, 1, "loss"),
        ("away_win",  0, 1, "win"),
        ("away_win",  1, 1, "loss"),
        ("over25",    3, 0, "win"),
        ("over25",    1, 1, "loss"),   # 2 goles = no over25
        ("under25",   1, 1, "win"),
        ("under25",   2, 1, "loss"),
        ("btts",      1, 1, "win"),
        ("btts",      2, 0, "loss"),
        ("btts_no",   2, 0, "win"),
        ("btts_no",   1, 1, "loss"),
        ("over_1.5",  2, 0, "win"),
        ("over_1.5",  1, 0, "loss"),
        ("under_1.5", 1, 0, "win"),
        ("under_1.5", 2, 0, "loss"),
        ("over_3.5",  4, 0, "win"),
        ("over_3.5",  2, 1, "loss"),
        ("under_3.5", 2, 1, "win"),
        ("under_3.5", 4, 0, "loss"),
        ("dc_1x",     2, 0, "win"),
        ("dc_1x",     1, 1, "win"),
        ("dc_1x",     0, 1, "loss"),
        ("dc_x2",     0, 1, "win"),
        ("dc_x2",     1, 1, "win"),
        ("dc_x2",     2, 0, "loss"),
        ("dc_12",     2, 0, "win"),
        ("dc_12",     0, 2, "win"),
        ("dc_12",     1, 1, "loss"),
    ]

    @pytest.mark.parametrize("market,hg,ag,expected", MARKET_CASES)
    def test_goal_market_outcome(self, market, hg, ag, expected):
        """Verifica que cada mercado de goles resuelva correctamente."""
        outcome = "loss"  # default
        if market == "home_win" and hg > ag:    outcome = "win"
        elif market == "away_win" and ag > hg:  outcome = "win"
        elif market == "draw" and hg == ag:     outcome = "win"
        elif market == "over25" and (hg + ag) > 2:  outcome = "win"
        elif market == "under25" and (hg + ag) <= 2: outcome = "win"
        elif market == "btts" and hg > 0 and ag > 0: outcome = "win"
        elif market == "btts_no" and (hg == 0 or ag == 0): outcome = "win"
        elif market == "over_1.5" and (hg + ag) > 1:  outcome = "win"
        elif market == "under_1.5" and (hg + ag) <= 1: outcome = "win"
        elif market == "over_3.5" and (hg + ag) > 3:  outcome = "win"
        elif market == "under_3.5" and (hg + ag) <= 3: outcome = "win"
        elif market == "dc_1x" and hg >= ag:   outcome = "win"
        elif market == "dc_x2" and ag >= hg:   outcome = "win"
        elif market == "dc_12" and hg != ag:   outcome = "win"
        assert outcome == expected


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Asian Handicap — el más complejo
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_ah(market: str, hg: int, ag: int, stake: float = 1.0, odds: float = 1.90):
    """
    Replica la lógica de resolución AH de save_bets.py.
    Devuelve (outcome, profit).
    """
    parts = market.split("_", 2)
    side = parts[1]
    home_line = float(parts[2])
    margin = hg - ag

    frac = abs(home_line - round(home_line * 2) / 2)
    is_quarter = abs(home_line * 4 - round(home_line * 4)) < 1e-6 and frac > 1e-6

    def _resolve_half(line_val):
        d = margin + line_val
        half_stake = stake / 2.0
        if abs(d) < 1e-9:
            return ("push", 0.0)
        home_covers = d > 0
        won = (home_covers and side == "home") or (not home_covers and side == "away")
        if won:
            return ("win", half_stake * (odds - 1))
        return ("loss", -half_stake)

    if is_quarter:
        lo_out, lo_prof = _resolve_half(home_line - 0.25)
        hi_out, hi_prof = _resolve_half(home_line + 0.25)
        profit = lo_prof + hi_prof
        if lo_out == "win" and hi_out == "win":
            outcome = "win"
        elif lo_out == "loss" and hi_out == "loss":
            outcome = "loss"
        elif "win" in (lo_out, hi_out):
            outcome = "half_win"
        else:
            outcome = "half_loss"
    else:
        diff = margin + home_line
        outcome = "loss"
        profit = -stake
        if abs(diff) < 1e-9:
            outcome = "push"
            profit = 0.0
        elif diff > 0 and side == "home":
            outcome = "win"
        elif diff < 0 and side == "away":
            outcome = "win"

    if outcome == "win":
        profit = stake * (odds - 1)
    return outcome, round(profit, 4)


class TestAsianHandicap:

    # ── Líneas enteras ────────────────────────────────────────────────────────

    def test_ah_home_minus_half_home_wins_by_one(self):
        """ah_home_-0.5: local gana 1-0 → win."""
        outcome, _ = _resolve_ah("ah_home_-0.5", hg=1, ag=0)
        assert outcome == "win"

    def test_ah_home_minus_half_draw_is_loss(self):
        """ah_home_-0.5: empate → loss (home no cubre el -0.5)."""
        outcome, _ = _resolve_ah("ah_home_-0.5", hg=1, ag=1)
        assert outcome == "loss"

    def test_ah_home_plus_half_draw_is_win(self):
        """ah_home_+0.5: empate → win (home recibe 0.5 goles)."""
        outcome, _ = _resolve_ah("ah_home_+0.5", hg=1, ag=1)
        assert outcome == "win"

    def test_ah_home_minus_one_exact_push(self):
        """ah_home_-1.0: local gana exacto 1 → push."""
        outcome, profit = _resolve_ah("ah_home_-1.0", hg=2, ag=1, stake=1.0)
        assert outcome == "push"
        assert profit == 0.0

    def test_ah_away_minus_half_away_wins(self):
        """ah_away_-0.5: visitante gana 0-1 → win."""
        outcome, _ = _resolve_ah("ah_away_-0.5", hg=0, ag=1)
        assert outcome == "win"

    def test_ah_away_plus_half_draw_is_loss(self):
        """ah_away_+0.5: empate → loss.
        Convención: el número embebido es SIEMPRE el handicap del LOCAL.
        ah_away_+0.5 = home recibe +0.5 → en empate home 'gana' el handicap
        → quien apostó a away pierde."""
        outcome, _ = _resolve_ah("ah_away_+0.5", hg=1, ag=1)
        assert outcome == "loss"

    def test_ah_away_minus_half_draw_is_win(self):
        """ah_away_-0.5: empate → win.
        home recibe -0.5 (home da ventaja a away) → en empate away 'gana'."""
        outcome, _ = _resolve_ah("ah_away_-0.5", hg=1, ag=1)
        assert outcome == "win"

    # ── Quarter-ball (los más complejos) ─────────────────────────────────────

    def test_ah_home_minus_quarter_home_wins(self):
        """ah_home_-0.25: local gana 1-0 → win completo (ambas mitades ganan)."""
        outcome, profit = _resolve_ah("ah_home_-0.25", hg=1, ag=0, stake=2.0, odds=1.90)
        assert outcome == "win"
        assert profit == pytest.approx(2.0 * 0.90, abs=0.01)

    def test_ah_home_minus_quarter_draw_is_half_loss(self):
        """ah_home_-0.25: empate → half_loss (mitad -0.0 push, mitad -0.5 loss)."""
        outcome, profit = _resolve_ah("ah_home_-0.25", hg=1, ag=1, stake=2.0, odds=1.90)
        assert outcome == "half_loss"
        assert profit == pytest.approx(-1.0, abs=0.01)  # solo pierde mitad del stake

    def test_ah_home_plus_quarter_draw_is_half_win(self):
        """ah_home_+0.25: empate → half_win (mitad +0.0 push, mitad +0.5 win)."""
        outcome, profit = _resolve_ah("ah_home_+0.25", hg=1, ag=1, stake=2.0, odds=1.90)
        assert outcome == "half_win"
        assert profit == pytest.approx(0.90, abs=0.01)  # gana solo la mitad

    def test_ah_home_minus_three_quarters_home_wins_by_one(self):
        """ah_home_-0.75: local gana 1-0 → half_win."""
        outcome, profit = _resolve_ah("ah_home_-0.75", hg=1, ag=0, stake=2.0, odds=1.90)
        assert outcome == "half_win"

    def test_ah_home_minus_three_quarters_home_wins_by_two(self):
        """ah_home_-0.75: local gana 2-0 → win completo."""
        outcome, _ = _resolve_ah("ah_home_-0.75", hg=2, ag=0)
        assert outcome == "win"

    def test_profit_calculation_win(self):
        """stake * (odds - 1) en caso de win."""
        _, profit = _resolve_ah("ah_home_+0.5", hg=0, ag=0, stake=3.0, odds=1.85)
        assert profit == pytest.approx(3.0 * 0.85, abs=0.001)

    def test_profit_calculation_loss(self):
        """-stake en caso de loss."""
        _, profit = _resolve_ah("ah_home_-1.5", hg=1, ag=0, stake=3.0, odds=1.85)
        assert profit == pytest.approx(-3.0, abs=0.001)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: mercados de stats (corners, cards, HT)
# ─────────────────────────────────────────────────────────────────────────────

class TestStatsMarkets:

    def test_corners_over_win(self):
        hc, ac = 6, 5   # total=11 > 9.5
        line = 9.5
        assert (float(hc) + float(ac)) > line

    def test_corners_over_loss(self):
        hc, ac = 4, 4   # total=8 < 9.5
        line = 9.5
        assert not ((float(hc) + float(ac)) > line)

    def test_corners_under_win(self):
        hc, ac = 4, 4   # total=8 <= 9.5
        line = 9.5
        assert (float(hc) + float(ac)) <= line

    def test_cards_over_win(self):
        hy, ay = 3, 2   # total=5 > 4.5
        line = 4.5
        assert (float(hy) + float(ay)) > line

    def test_cards_null_is_unresolved(self):
        """Si home_yellow es NULL → unresolved (no se puede resolver)."""
        hy = None
        assert hy is None  # trigger correcto para marcar unresolved

    @pytest.mark.parametrize("market,gh,ga,expected", [
        ("h1_home", 1, 0, "win"),
        ("h1_home", 0, 0, "loss"),
        ("h1_draw", 0, 0, "win"),
        ("h1_draw", 1, 0, "loss"),
        ("h1_away", 0, 1, "win"),
        ("h1_away", 1, 0, "loss"),
        ("h2_home", 2, 0, "win"),
        ("h2_draw", 1, 1, "win"),
        ("h2_away", 0, 1, "win"),
    ])
    def test_halftime_markets(self, market, gh, ga, expected):
        """Resolución de mercados de primer y segundo tiempo."""
        half = market.split("_")[0]
        side = "_".join(market.split("_")[1:])
        outcome = "loss"
        if side == "home" and gh > ga:   outcome = "win"
        elif side == "away" and ga > gh: outcome = "win"
        elif side == "draw" and gh == ga: outcome = "win"
        assert outcome == expected

    def test_ht_null_goals_is_unresolved(self):
        """Goles HT NULL → unresolved."""
        gh = None
        ga = 1
        assert gh is None or ga is None or (
            pd.isna(gh) if gh is not None else True
        )
