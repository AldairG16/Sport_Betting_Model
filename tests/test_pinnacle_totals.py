"""
Más/menos 2.5 de Pinnacle derivado de su línea principal (25-sep-26).
Pinnacle cotiza 2.5 solo en ~14% de los partidos (su línea principal suele
ser 2.25, 2.75, 3...): sin derivarlo, goles — la familia más apostada —
casi no tenía referencia sharp.
"""

import math

import pytest

from src.features.pinnacle import (_poisson_pmf, _poisson_sf, over25_from_line, over_return,
                                   pinnacle_prob)


def _fair_prices(line: float, lam: float, margin: float = 1.03):
    """Cuotas (más, menos) de una línea asiática para goles ~ Poisson(λ),
    con margen repartido proporcionalmente."""
    lo, hi = 1.01, 50.0
    for _ in range(200):                              # cuota justa del más: retorno = 1
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if over_return(line, mid, lam) < 1 else (lo, mid)
    f_over = (lo + hi) / 2
    q_over = 1 / f_over
    return 1 / (q_over * margin), 1 / ((1 - q_over) * margin)


@pytest.mark.parametrize("line", [2.0, 2.25, 2.5, 2.75, 3.0, 3.25])
@pytest.mark.parametrize("lam", [2.1, 2.7, 3.3])
def test_derivation_recovers_the_poisson_tail(line, lam):
    over, under = _fair_prices(line, lam)
    assert over25_from_line(line, over, under) == pytest.approx(_poisson_sf(3, lam), abs=0.01)


def test_direct_when_the_line_is_2_5():
    assert over25_from_line(2.5, 1.95, 1.93) == pytest.approx((1 / 1.95) / (1 / 1.95 + 1 / 1.93))


@pytest.mark.parametrize("args", [(None, 1.9, 1.9), (2.75, None, 1.9), (2.75, 1.2, 1.2),
                                  ("x", 1.9, 1.9), (2.75, float("nan"), 1.9)])
def test_no_derivation_without_a_sane_pair(args):
    assert over25_from_line(*args) is None


def test_row_without_2_5_uses_the_main_line():
    over, under = _fair_prices(2.75, 2.8)
    row = {"pin_total_line": 2.75, "pin_total_over_odds": over, "pin_total_under_odds": under}
    p = pinnacle_prob("over25", row)
    assert p == pytest.approx(_poisson_sf(3, 2.8), abs=0.01)
    assert pinnacle_prob("under25", row) == pytest.approx(1 - p)


def test_poisson_helpers():
    assert sum(_poisson_pmf(k, 2.6) for k in range(40)) == pytest.approx(1.0)
    assert _poisson_sf(0, 2.6) == pytest.approx(1.0)
    assert _poisson_sf(3, 2.6) == pytest.approx(1 - math.exp(-2.6) * (1 + 2.6 + 2.6 ** 2 / 2))
