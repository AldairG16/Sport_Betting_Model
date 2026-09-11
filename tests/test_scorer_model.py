"""
Tests del modelo de anytime goalscorer.
Verifica la matemática sin tocar la DB (rates inyectados).
"""

import math

import pytest

from src.models.scorer_model import anytime_scorer_prob, _parse_scorers


RATES = {
    "kylian mbappe": {"team": "francia", "goals": 17, "team_matches": 18,
                      "rate_weighted": 0.85, "rate_raw": 0.94},
    "defensa raro":  {"team": "equipo x", "goals": 2,  "team_matches": 20,
                      "rate_weighted": 0.10, "rate_raw": 0.10},
    "suplente":      {"team": "equipo y", "goals": 1,  "team_matches": 20,
                      "rate_weighted": 0.05, "rate_raw": 0.05},
}


class TestAnytimeScorer:

    def test_elite_returns_valid_prob(self):
        r = anytime_scorer_prob("Kylian Mbappé", 2.0, rates=RATES)
        assert r is not None
        assert 0 < r["prob"] < 1
        # P = 1 - exp(-λ) con λ escalado por contexto y cap a 1.2
        lam = min(0.85 * (2.0 / 0.94), 1.2)
        assert abs(r["prob"] - (1 - math.exp(-lam))) < 0.01
        assert abs(r["fair_odds"] - 1 / r["prob"]) < 0.05

    def test_accent_insensitive_lookup(self):
        # "Mbappé" (acento, como en match_events) debe matchear con/sin acento
        r1 = anytime_scorer_prob("kylian mbappé", 1.5, rates=RATES)
        assert r1 is not None and r1["prob"] > 0
        # El key sin acento resuelve al mismo jugador vía tolerancia
        r2 = anytime_scorer_prob("kylian mbappe", 1.5, rates=RATES)
        assert r2 is not None and r2["prob"] == r1["prob"]

    def test_more_expected_goals_higher_prob(self):
        p1 = anytime_scorer_prob("kylian mbappe", 1.0, rates=RATES)["prob"]
        p2 = anytime_scorer_prob("kylian mbappe", 2.5, rates=RATES)["prob"]
        assert p2 > p1  # monotonía en goles esperados del equipo

    def test_low_sample_rejected(self):
        # <4 goles o rate <0.22 → no opina
        assert anytime_scorer_prob("defensa raro", 2.0, rates=RATES) is None
        assert anytime_scorer_prob("suplente", 2.0, rates=RATES) is None

    def test_unknown_player_none(self):
        assert anytime_scorer_prob("nadie conocido", 2.0, rates=RATES) is None

    def test_lambda_capped(self):
        # Contexto extremo (5 goles esperados) no produce λ infinito
        r = anytime_scorer_prob("kylian mbappe", 5.0, rates=RATES)
        assert r["lambda"] <= 1.2

    def test_parse_scorers_formats(self):
        assert _parse_scorers(None) == []
        assert _parse_scorers("") == []
        assert _parse_scorers('[]') == []
        parsed = _parse_scorers('[{"player":"Test","minute":23,"penalty":false}]')
        assert parsed[0]["player"] == "Test"
        assert _parse_scorers("json roto{{{") == []
        assert _parse_scorers([{"player": "X"}]) == [{"player": "X"}]
