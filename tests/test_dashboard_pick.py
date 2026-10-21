"""
Pick del programa en el dashboard (v1.7.2, 21-oct-26): el dueño quería ver
qué jugaría el programa aunque no tenga valor. Entre 1X2, más/menos 2.5 y
ambos anotan, el mercado donde el programa ve más que el mercado; si no ve
nada (menos de 3 pts), lo más probable del 1X2. Sin DB.
"""

from types import SimpleNamespace

from dashboard import visibility as V

UP = SimpleNamespace(home_odds=1.27, draw_odds=8.0, away_odds=11.0, over25_odds=1.18, under25_odds=5.07,
                     consensus_over25_odds=1.177, consensus_under25_odds=4.957,
                     btts_yes_odds=1.55, btts_no_odds=2.70)
PIN = [0.783, 0.129, 0.088]


def test_pick_is_the_market_where_the_program_sees_most():
    pk = V.program_pick([0.83, 0.10, 0.07], PIN, 0.83, 0.61, UP)
    assert pk["label"] == "Gana el local" and pk["lean"] is True
    assert pk["model"] == 0.83 and pk["market"] == 0.783
    assert pk["min_us"] == "-321" and pk["best_us"] == "-370" and pk["value"] is False


def test_without_a_gap_it_shows_the_most_likely_outcome_not_a_lean():
    pk = V.program_pick([0.79, 0.13, 0.08], PIN, 0.80, 0.63, UP)
    assert pk["label"] == "Gana el local" and pk["lean"] is False


def test_goals_markets_enter_with_the_average_price():
    pk = V.program_pick([0.78, 0.13, 0.09], PIN, 0.65, 0.62, UP)          # el mercado da ~81% a más de 2.5
    assert pk["label"] == "Menos de 2.5 goles" and pk["lean"] is True
    assert pk["best_us"] == "+407"


def test_value_when_the_best_price_pays_the_minimum():
    up = SimpleNamespace(**{**UP.__dict__, "home_odds": 1.40})              # -250 ≥ -322
    pk = V.program_pick([0.83, 0.10, 0.07], PIN, None, None, up)
    assert pk["value"] is True


def test_no_data_no_pick():
    assert V.program_pick([None, None, None], [None, None, None], None, None, None) == {"label": "", "text": ""}
