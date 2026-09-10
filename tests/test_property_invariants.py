"""
Tests de propiedad (property-based) sobre las matemáticas del sistema.
==========================================================================
A diferencia de los tests de ejemplo (casos puntuales), estos verifican
INVARIANTES que deben cumplirse para CUALQUIER entrada válida, sobre las
funciones que la PRODUCCIÓN realmente usa (dixon_coles_model, poisson_markets,
betting_engine, corners_model, normalizador):

  - match_outcomes (Dixon-Coles) devuelve probabilidades que suman 1
  - totals_and_btts: over+under = 1 y btts_yes+btts_no = 1 (con y sin rho)
  - Kelly: sin edge → stake 0; nunca negativo; respeta el cap
  - Colas de Poisson de córners: over+under = 1 exacto
  - El normalizador de equipos es idempotente y produce ascii lowercase

Requieren `hypothesis` (pip install hypothesis). Si no está instalada,
todos los tests se saltan en vez de fallar.
"""

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st

from src.models.dixon_coles_model import match_outcomes
from src.models.poisson_markets import (
    poisson_matrix, dc_adjusted_matrix, totals_and_btts, totals_extended,
)
from src.models.betting_engine import kelly_stake
from src.models.corners_model import predict_corners
from src.utils.team_normalizer import normalize_team

# Lambdas razonables para fútbol. El pipeline capa a 2.5, pero dejamos
# margen (hasta 3.5) para no probar solo el caso feliz.
lambdas = st.floats(min_value=0.1, max_value=3.5, allow_nan=False)

# Odds reales de bookmaker
odds = st.floats(min_value=1.01, max_value=15.0, allow_nan=False)

probs = st.floats(min_value=0.01, max_value=0.99, allow_nan=False)

teams = st.sampled_from([
    "Manchester United", "manchester city", "Real Madrid CF", "BAYERN München",
    "psg", "Club Atlético Boca Juniors", "AS Roma", "  Liverpool FC  ",
    "São Paulo FC", "Dinamo Zagreb", "Legia Warszawa", "Jeonbuk Hyundai Motors",
])

rhos = st.floats(min_value=-0.2, max_value=0.2, allow_nan=False)


class TestMatchOutcomes:

    @settings(max_examples=100, deadline=None)
    @given(h=lambdas, a=lambdas)
    def test_probs_sum_to_one(self, h, a):
        home, draw, away = match_outcomes(h, a)
        assert 0 <= home <= 1 and 0 <= draw <= 1 and 0 <= away <= 1
        assert abs(home + draw + away - 1.0) < 1e-9

    @settings(max_examples=100, deadline=None)
    @given(h=lambdas, a=lambdas)
    def test_stronger_home_shifts_home_win(self, h, a):
        # Con más lambda local, P(home_win) nunca baja (monotonía razonable)
        p1 = match_outcomes(h, a)[0]
        p2 = match_outcomes(min(h + 0.5, 3.5), a)[0]
        assert p2 >= p1 - 1e-9


class TestMatrixAndTotals:

    @settings(max_examples=75, deadline=None)
    @given(h=lambdas, a=lambdas)
    def test_raw_matrix_is_proper_distribution(self, h, a):
        m = poisson_matrix(h, a, max_goals=10)
        assert (m >= 0).all()
        assert abs(m.sum() - 1.0) < 1e-9

    @settings(max_examples=75, deadline=None)
    @given(h=lambdas, a=lambdas, rho=rhos)
    def test_dc_matrix_normalized(self, h, a, rho):
        m = dc_adjusted_matrix(h, a, rho)
        assert (m >= 0).all()
        assert abs(m.sum() - 1.0) < 1e-9

    @settings(max_examples=100, deadline=None)
    @given(h=lambdas, a=lambdas, rho=rhos)
    def test_totals_and_btts_complements(self, h, a, rho):
        r = totals_and_btts(h, a, rho=rho)
        assert 0 <= r["over25"] <= 1
        assert abs(r["over25"] + r["under25"] - 1.0) < 1e-9
        assert abs(r["btts_yes"] + r["btts_no"] - 1.0) < 1e-9

    @settings(max_examples=75, deadline=None)
    @given(h=lambdas, a=lambdas)
    def test_totals_extended_monotone_in_line(self, h, a):
        r = totals_extended(h, a)
        # P(over 1.5) >= P(over 2.5) >= P(over 3.5)
        assert r["over_1.5"] >= r["over_2.5"] >= r["over_3.5"] - 1e-12
        assert r["over_3.5"] <= r["over_2.5"]
        for line in (1.5, 2.5, 3.5):
            assert abs(r[f"over_{line}"] + r[f"under_{line}"] - 1.0) < 1e-9


class TestKellyInvariants:

    @settings(max_examples=150, deadline=None)
    @given(p=probs, o=odds)
    def test_stake_never_negative_and_capped(self, p, o):
        stake = kelly_stake(p, o, bankroll=100.0)
        implied = 1.0 / o
        if p <= implied:
            # Sin edge (o edge negativo) → nunca apostar
            assert stake == 0
        else:
            assert 0 < stake <= 100.0 * 0.02 + 1e-9   # max_bet_pct = 2%

    @settings(max_examples=75, deadline=None)
    @given(o=odds)
    def test_no_edge_no_bet(self, o):
        # prob exactamente igual a la implícita → Kelly puro = 0
        assert kelly_stake(1.0 / o, o, bankroll=100.0) == 0

    @settings(max_examples=75, deadline=None)
    @given(o=st.floats(min_value=0.5, max_value=1.0, allow_nan=False))
    def test_invalid_odds_rejected(self, o):
        assert kelly_stake(0.6, o, bankroll=100.0) == 0


class TestCornersModel:

    @settings(max_examples=50, deadline=None)
    @given(
        ha=st.floats(min_value=0.3, max_value=2.5, allow_nan=False),
        hd=st.floats(min_value=0.3, max_value=2.5, allow_nan=False),
        aa=st.floats(min_value=0.3, max_value=2.5, allow_nan=False),
        ad=st.floats(min_value=0.3, max_value=2.5, allow_nan=False),
    )
    def test_all_lines_are_proper_probs(self, ha, hd, aa, ad):
        r = predict_corners(ha, hd, aa, ad)
        for line in ("85", "95", "105"):
            assert 0 <= r[f"over{line}"] <= 1
            assert 0 <= r[f"under{line}"] <= 1
            assert abs(r[f"over{line}"] + r[f"under{line}"] - 1.0) < 1e-9
        assert 0 <= r["home_dominance"] <= 1


class TestNormalizer:

    @settings(max_examples=50, deadline=None)
    @given(t=teams)
    def test_idempotent(self, t):
        once = normalize_team(t)
        assert once == normalize_team(once)

    @settings(max_examples=50, deadline=None)
    @given(t=teams)
    def test_ascii_lowercase_no_double_spaces(self, t):
        n = normalize_team(t)
        assert n == n.lower()
        assert n.isascii()
        assert "  " not in n
