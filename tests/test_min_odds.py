"""
Cuota mínima para apostar en PlayDoit (src/utils/min_odds.py), en formato
americano (+128 / -140) como la muestra PlayDoit, y su lugar en los mensajes
REALES de Telegram.

v1.4.0 probaba una función de formato que ningún mensaje usaba
(_format_bet_line): la prueba pasaba y el mensaje de la mañana nunca mostró
la cuota mínima. Estas pruebas van contra los constructores que sí se envían.
"""

import math

import pandas as pd
import pytest

from src.utils.min_odds import (MIN_EDGE_TO_PLACE, american_to_decimal, fmt_american,
                                min_american, min_odds, playdoit_line, rule_examples,
                                to_american)


def _edge(prob, american):
    return prob - 1.0 / american_to_decimal(american)


def _one_point_worse(a):
    """El precio inmediatamente peor en la escala americana (-100 = +100)."""
    return -101 if a == 100 else a - 1


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


@pytest.mark.parametrize("decimal,american", [
    (2.28, 128), (1.714, -140),       # los ejemplos del dueño: +128 / -140
    (2.00, 100), (1.998, 100),        # -100 y +100 son el mismo precio: se muestra +100
    (1.58, -172), (2.08, 108), (1.80, -125), (3.30, 230),
])
def test_decimal_to_american(decimal, american):
    assert to_american(decimal) == american


@pytest.mark.parametrize("bad", [None, "", "x", 1.0, 0.5, float("nan"), float("inf")])
def test_to_american_without_a_usable_price(bad):
    assert to_american(bad) is None


def test_american_format_matches_playdoit():
    assert (fmt_american(128), fmt_american(-140), fmt_american(None)) == ("+128", "-140", "—")
    assert american_to_decimal(128) == pytest.approx(2.28)
    assert american_to_decimal(-140) == pytest.approx(1 + 100 / 140)


@pytest.mark.parametrize("prob,expected", [
    (0.60, -138),     # 1/0.58 = 1.7241 → 100/0.7241 = 138.1 → -138
    (0.75, -270),     # 1/0.73 = 1.3699 → 270.4 → -270
    (0.50, 109),      # 2.0833 → +108.3 → hacia arriba +109
    (0.25, 335),      # 4.3478 → +334.8 → +335
    (0.52, 100),      # exacto: 2.00 → +100
])
def test_min_american(prob, expected):
    assert min_american(prob) == expected


@pytest.mark.parametrize("prob", [i / 200 for i in range(6, 196)])      # 3% … 97.5%
def test_min_american_is_the_worst_price_that_still_pays(prob):
    """A la cuota mostrada el edge alcanza; un punto peor, ya no. Y los
    ejemplos del mensaje caen cada uno de su lado."""
    a = min_american(prob)
    assert a is not None and abs(a) >= 100
    assert _edge(prob, a) >= MIN_EDGE_TO_PLACE - 1e-12
    assert _edge(prob, _one_point_worse(a)) < MIN_EDGE_TO_PLACE
    better, worse = rule_examples(a)
    assert _edge(prob, better) >= MIN_EDGE_TO_PLACE and better % 5 == 0
    assert worse is None or (_edge(prob, worse) < MIN_EDGE_TO_PLACE and worse % 5 == 0)


@pytest.mark.parametrize("prob", [None, "", "x", 0, 1, 1.2, 0.02, 0.015, float("nan")])
def test_no_minimum_without_a_usable_probability(prob):
    assert min_odds(prob) is None and min_american(prob) is None
    assert playdoit_line(prob) == ""


def test_revalidation_uses_the_same_threshold():
    """La revalidación pre-kickoff mantiene una bet si el edge a la cuota
    nueva es ≥ KEEP_EDGE: el mismo umbral que la cuota mínima mostrada."""
    import scripts.revalidate_pending_bets as rv
    assert rv.KEEP_EDGE == MIN_EDGE_TO_PLACE


PICK = {"match": "alpha vs beta", "league": "soccer_spain_la_liga", "market": "home_win",
        "probability": 0.60, "odds": 1.92, "edge": 0.08, "stake": 1.0,
        "match_date": "2026-09-26T19:00:00+00:00"}
RULE = "PlayDoit: apuesta si paga -138 o mejor (-125 ✅ · -150 ❌)"


def _no_decimal_odds(msg):
    """Ni la cuota europea (1.92) ni la mínima decimal (1.73)."""
    return not any(x in msg for x in ("1.92", "1.73", "@1", "@2"))


def test_morning_and_night_previews_show_the_playdoit_minimum():
    """El candidato de la mañana (format_bets_message) y el de la noche
    (send_tomorrow_preview) salen de _build_bets_by_league."""
    from scripts.notify_telegram import _build_bets_by_league, format_bets_message
    for msg, n in (format_bets_message(pd.DataFrame([PICK])),
                   _build_bets_by_league(pd.DataFrame([PICK]), "PICKS DE MAÑANA")):
        assert n == 1 and RULE in msg and _no_decimal_odds(msg)
        assert "Número más alto = paga más" in msg


def test_kickoff_confirmation_shows_the_playdoit_minimum():
    from scripts.revalidate_pending_bets import _format_confirmations
    msg = _format_confirmations([PICK])
    assert RULE in msg and _no_decimal_odds(msg)
    assert "Número más alto = paga más" in msg


def test_without_probability_the_reference_price_is_american():
    from scripts.revalidate_pending_bets import _format_confirmations
    msg = _format_confirmations([dict(PICK, probability=None)])
    assert "Mejor cuota: -109" in msg and "PlayDoit: apuesta" not in msg


def test_evening_summary_preview_of_tomorrow():
    from scripts.notify_telegram import _tomorrow_line
    line = _tomorrow_line(pd.Series(PICK))
    assert "PlayDoit -138 o mejor" in line and "home_win" not in line
    assert _no_decimal_odds(line)


def test_paper_section_price_is_american(tmp_path, monkeypatch):
    """La sección de paper-trading (Mundial) también muestra +/-."""
    import json
    from datetime import date
    import scripts.notify_telegram as nt
    log = tmp_path / "paper.jsonl"
    log.write_text(json.dumps(dict(PICK, match_date="2026-09-26T19:00:00+00:00")) + "\n",
                   encoding="utf-8")
    monkeypatch.setattr(nt, "PAPER_LOG_PATH", log)
    section = nt._format_paper_bets_section(date(2026, 9, 26))
    assert "@-109" in section and _no_decimal_odds(section)


def test_analyst_verdict_price_is_american():
    from scripts.notify_telegram import _format_single_match_message
    msg = _format_single_match_message("alpha vs beta", [dict(
        PICK, probability=60, verdict="STRONG", decision="APUESTA", confidence=4)])
    assert "@-109" in msg and _no_decimal_odds(msg)


def test_min_american_matches_decimal_minimum():
    """El mínimo americano es el mismo precio que el decimal (±redondeo)."""
    for prob in (0.3, 0.45, 0.6, 0.72):
        assert math.isclose(american_to_decimal(min_american(prob)), min_odds(prob), abs_tol=0.01)
