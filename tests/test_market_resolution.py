"""
Tests de resolución de mercados (liquidación de bets).

Hasta el 22-sep-26 este archivo REIMPLEMENTABA la lógica dentro de cada
test ("testeo indirectamente ... ejecutando la lógica inline"): probaba
una copia, no el código que liquida el dinero, e incluía tautologías como
`assert hy is None`. Ahora que la liquidación es una función pura
(save_bets.resolve_market) y el buscador de partidos también
(save_bets.make_match_lookup), se ejecuta el código de producción con las
mismas tablas de casos. Sin DB real — datos 100% sintéticos.
"""

import pandas as pd
import pytest

from src.models.save_bets import resolve_market, make_match_lookup


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _match_row(**kwargs) -> pd.Series:
    """Fila sintética de matches (como la precarga de update_bet_results)."""
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


def _resolve(market, hg, ag, stake=1.0, odds=1.90, **stats):
    return resolve_market(market, _match_row(home_goals=hg, away_goals=ag, **stats), odds, stake)


# ─────────────────────────────────────────────────────────────────────────────
# Buscador de partidos (make_match_lookup)
# ─────────────────────────────────────────────────────────────────────────────

class TestLookupMatch:

    def test_finds_exact_date_match(self):
        lookup = make_match_lookup(pd.DataFrame([
            _match_row(home_goals=2, away_goals=1)]))
        row = lookup("brazil", "argentina", pd.Timestamp("2026-06-15 20:00", tz="UTC"))
        assert row is not None and row["home_goals"] == 2

    def test_returns_none_outside_two_day_window(self):
        lookup = make_match_lookup(pd.DataFrame([
            _match_row(date=pd.Timestamp("2026-06-10", tz="UTC"), home_goals=2, away_goals=1)]))
        assert lookup("brazil", "argentina", pd.Timestamp("2026-06-15 20:00", tz="UTC")) is None

    def test_picks_closest_when_two_matches(self):
        lookup = make_match_lookup(pd.DataFrame([
            _match_row(home_team_l="a", away_team_l="b",
                       date=pd.Timestamp("2026-06-15 18:00", tz="UTC"), home_goals=1, away_goals=0),
            _match_row(home_team_l="a", away_team_l="b",
                       date=pd.Timestamp("2026-06-14 20:00", tz="UTC"), home_goals=3, away_goals=3)]))
        row = lookup("a", "b", pd.Timestamp("2026-06-15 19:00", tz="UTC"))
        assert row["home_goals"] == 1

    def test_unknown_teams_return_none(self):
        lookup = make_match_lookup(pd.DataFrame([_match_row(home_goals=1, away_goals=0)]))
        assert lookup("x", "y", pd.Timestamp("2026-06-15", tz="UTC")) is None

    def test_falls_back_to_normalized_names(self):
        """matches con el nombre crudo del CSV ("nott'm forest") y búsqueda
        con el nombre normalizado de la API: el fallback los empareja."""
        from src.utils.team_normalizer import normalize_team
        lookup = make_match_lookup(pd.DataFrame([
            _match_row(home_team_l="nott'm forest", away_team_l="man united",
                       home_goals=2, away_goals=2)]))
        home, away = normalize_team("Nott'm Forest"), normalize_team("Man United")
        assert home != "nott'm forest"          # de verdad ejercita el fallback
        row = lookup(home, away, pd.Timestamp("2026-06-15 16:00", tz="UTC"))
        assert row is not None and row["home_goals"] == 2

    def test_naive_dates_in_matches_against_aware_kickoff(self):
        """matches.date es DATE (sin zona) y shadow_bets.match_date TIMESTAMPTZ."""
        lookup = make_match_lookup(pd.DataFrame([
            _match_row(date="2026-06-15", home_goals=1, away_goals=0)]))
        assert lookup("brazil", "argentina", pd.Timestamp("2026-06-15 20:00", tz="UTC")) is not None
        assert lookup("brazil", "argentina", pd.Timestamp("2026-06-15 20:00")) is not None


# ─────────────────────────────────────────────────────────────────────────────
# Mercados de goles (1X2, O/U, BTTS, DC)
# ─────────────────────────────────────────────────────────────────────────────

class TestGoalMarkets:

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
        outcome, profit = _resolve(market, hg, ag, stake=2.0, odds=1.80)
        assert outcome == expected
        assert profit == pytest.approx(2.0 * 0.80 if expected == "win" else -2.0)

    @pytest.mark.parametrize("market,hg,ag,expected,profit", [
        ("dnb_home", 2, 1, "win", 0.9),
        ("dnb_home", 1, 1, "push", 0.0),
        ("dnb_home", 0, 1, "loss", -1.0),
        ("dnb_away", 1, 1, "push", 0.0),
        ("dnb_away", 0, 2, "win", 0.9),
    ])
    def test_draw_no_bet(self, market, hg, ag, expected, profit):
        assert _resolve(market, hg, ag) == (expected, pytest.approx(profit))

    def test_market_without_resolver_branch_is_unresolved_not_a_loss(self):
        """Un nombre sin rama no se liquida como pérdida inventada (D10)."""
        assert _resolve("mercado_inventado", 1, 0) == ("unresolved", 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Asian Handicap — la línea embebida es SIEMPRE la del local
# ─────────────────────────────────────────────────────────────────────────────

class TestAsianHandicap:

    @pytest.mark.parametrize("market,hg,ag,expected", [
        ("ah_home_-0.5", 1, 0, "win"),
        ("ah_home_-0.5", 1, 1, "loss"),
        ("ah_home_+0.5", 1, 1, "win"),
        ("ah_away_-0.5", 0, 1, "win"),
        ("ah_away_+0.5", 1, 1, "loss"),   # el local recibe +0.5: en empate cubre él
        ("ah_away_-0.5", 1, 1, "win"),    # el local da 0.5: en empate cubre el visitante
        ("ah_home_-0.75", 2, 0, "win"),
    ])
    def test_half_and_whole_lines(self, market, hg, ag, expected):
        assert _resolve(market, hg, ag)[0] == expected

    def test_whole_line_exact_is_push(self):
        assert _resolve("ah_home_-1.0", 2, 1, stake=1.0) == ("push", 0.0)

    def test_quarter_line_win(self):
        outcome, profit = _resolve("ah_home_-0.25", 1, 0, stake=2.0, odds=1.90)
        assert outcome == "win" and profit == pytest.approx(2.0 * 0.90, abs=0.01)

    def test_quarter_line_draw_is_half_loss(self):
        outcome, profit = _resolve("ah_home_-0.25", 1, 1, stake=2.0, odds=1.90)
        assert outcome == "half_loss" and profit == pytest.approx(-1.0, abs=0.01)

    def test_quarter_line_draw_is_half_win(self):
        outcome, profit = _resolve("ah_home_+0.25", 1, 1, stake=2.0, odds=1.90)
        assert outcome == "half_win" and profit == pytest.approx(0.90, abs=0.01)

    def test_three_quarter_line_win_by_one_is_half_win(self):
        assert _resolve("ah_home_-0.75", 1, 0, stake=2.0, odds=1.90)[0] == "half_win"

    def test_profit_win_and_loss(self):
        assert _resolve("ah_home_+0.5", 0, 0, stake=3.0, odds=1.85)[1] == pytest.approx(2.55)
        assert _resolve("ah_home_-1.5", 1, 0, stake=3.0, odds=1.85)[1] == pytest.approx(-3.0)


# ─────────────────────────────────────────────────────────────────────────────
# Mercados de estadísticas (córners, tarjetas, tiros, 1T/2T)
# ─────────────────────────────────────────────────────────────────────────────

class TestStatsMarkets:

    @pytest.mark.parametrize("market,h,a,expected", [
        ("corners_over_9.5", 6, 5, "win"),
        ("corners_over_9.5", 4, 4, "loss"),
        ("corners_under_9.5", 4, 4, "win"),
    ])
    def test_corners(self, market, h, a, expected):
        assert _resolve(market, 1, 0, home_corners=h, away_corners=a)[0] == expected

    def test_cards(self):
        assert _resolve("cards_over_4.5", 1, 0, home_yellow=3, away_yellow=2)[0] == "win"
        assert _resolve("cards_under_4.5", 1, 0, home_yellow=3, away_yellow=2)[0] == "loss"

    def test_missing_stats_are_unresolved_without_profit(self):
        assert _resolve("cards_over_4.5", 1, 0) == ("unresolved", 0.0)
        assert _resolve("corners_over_9.5", 1, 0) == ("unresolved", 0.0)

    def test_shots(self):
        assert _resolve("shots_over_5.5", 1, 0, home_shots_target=4, away_shots_target=3)[0] == "win"

    @pytest.mark.parametrize("market,gh,ga,expected", [
        ("h1_home", 1, 0, "win"),
        ("h1_home", 0, 0, "loss"),
        ("h1_draw", 0, 0, "win"),
        ("h1_draw", 1, 0, "loss"),
        ("h1_away", 0, 1, "win"),
        ("h1_away", 1, 0, "loss"),
    ])
    def test_first_half(self, market, gh, ga, expected):
        assert _resolve(market, 2, 2, home_goals_ht=gh, away_goals_ht=ga)[0] == expected

    @pytest.mark.parametrize("market,gh,ga,expected", [
        ("h2_home", 2, 0, "win"),
        ("h2_draw", 1, 1, "win"),
        ("h2_away", 0, 1, "win"),
    ])
    def test_second_half(self, market, gh, ga, expected):
        assert _resolve(market, 3, 3, home_goals_h2=gh, away_goals_h2=ga)[0] == expected

    def test_half_time_goals_missing_is_unresolved(self):
        assert _resolve("h1_home", 1, 0, home_goals_ht=None, away_goals_ht=1) == ("unresolved", 0.0)
