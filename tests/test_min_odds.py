"""
Cuota mínima para apostar en PlayDoit (src/utils/min_odds.py) y su lugar
en los mensajes de Telegram (picks y confirmaciones pre-kickoff).
"""

import pytest

from src.utils.min_odds import MIN_EDGE_TO_PLACE, min_odds


@pytest.mark.parametrize("prob,expected", [
    (0.60, 1.73),     # 1 / 0.58 = 1.7241 → hacia arriba
    (0.50, 2.09),     # 1 / 0.48 = 2.0833
    (0.25, 4.35),     # 1 / 0.23 = 4.3478
    (0.52, 2.00),     # exacto: 1 / 0.50
])
def test_min_odds_leaves_at_least_the_required_edge(prob, expected):
    got = min_odds(prob)
    assert got == expected
    assert prob - 1.0 / got >= MIN_EDGE_TO_PLACE - 1e-12     # a esa cuota el edge alcanza
    assert prob - 1.0 / (got - 0.01) < MIN_EDGE_TO_PLACE     # un centavo menos, ya no


@pytest.mark.parametrize("prob", [None, "", "x", 0, 1, 1.2, 0.02, 0.015, float("nan")])
def test_min_odds_without_a_usable_probability(prob):
    assert min_odds(prob) is None


def test_revalidation_uses_the_same_threshold():
    """La revalidación pre-kickoff mantiene una bet si el edge a la cuota
    nueva es ≥ KEEP_EDGE: el mismo umbral que la cuota mínima mostrada."""
    import scripts.revalidate_pending_bets as rv
    assert rv.KEEP_EDGE == MIN_EDGE_TO_PLACE


def test_pick_message_shows_the_playdoit_floor():
    from scripts.notify_telegram import _format_bet_line
    line = _format_bet_line(1, {"match": "Alpha vs Beta", "league": "soccer_spain_la_liga",
                                "market": "home_win", "probability": 0.60, "odds": 1.92,
                                "edge": 0.08, "stake": 1.0,
                                "match_date": "2026-09-26T19:00:00+00:00"}, suspicious=False)
    assert "@1.92" in line and "PlayDoit: apuesta solo si paga ≥ 1.73" in line


def test_confirmation_message_shows_the_playdoit_floor():
    from scripts.revalidate_pending_bets import _format_confirmations
    msg = _format_confirmations([{"match": "alpha vs beta", "market": "home_win", "odds": 1.92,
                                  "stake": 1.0, "edge": 0.08, "probability": 0.60,
                                  "match_date": "2026-09-26T19:00:00+00:00"}])
    assert "PlayDoit: apuesta solo si paga ≥ 1.73" in msg
