"""
tests/test_sanity_audit.py
==========================
Auditor de sanidad (scripts/weekly_sanity_audit.py), 22-sep-26:

- La fusión de duplicados reventaba con NaN en columnas enteras ("integer
  out of range") y se revertía entera: los duplicados nunca se fusionaban.
- El chequeo de xG usaba solo tiros del local contra un rango fijo.
- Se prueban las funciones puras, sin DB.
"""

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.weekly_sanity_audit as sa


# ============================================================
# Fusión de duplicados
# ============================================================

@pytest.mark.parametrize("value,expected", [
    (float("nan"), None),
    (None, None),
    (np.nan, None),
    (np.int64(7), 7),
    (5.0, 5),
    (np.float64(3.0), 3),
])
def test_sql_value_never_sends_nan(value, expected):
    assert sa._sql_value(value) == expected


def _match(id_, home, away, league="soccer_epl", date="2026-09-20", hg=1, ag=1, **stats):
    row = {"id": id_, "date": date, "league": league, "home_team": home,
           "away_team": away, "home_goals": hg, "away_goals": ag}
    for c in sa._STAT_COLS:
        row[c] = stats.get(c, float("nan"))
    return row


def test_merge_params_have_no_nan():
    """Caso real del 21/22-sep: donante con stats vacías (NaN de pandas)."""
    keep = _match(1425318, "nottm forest", "arsenal")
    donor = _match(99, "nott'm forest", "arsenal", home_corners=np.float64(6.0))
    params = sa.merge_params(keep, donor)
    assert params["kid"] == 1425318
    assert params["hc"] == 6
    assert all(v is None or isinstance(v, int) for v in params.values())
    assert not any(isinstance(v, float) and math.isnan(v) for v in params.values())


def test_name_variant_is_a_duplicate_and_clean_name_is_kept():
    df = pd.DataFrame([_match(1, "nott'm forest", "arsenal"),
                       _match(2, "nottm forest", "arsenal")])
    pairs = sa.find_duplicate_pairs(df)
    assert len(pairs) == 1
    keep, donor = sa.choose_keep(*pairs[0])
    assert keep["id"] == 2 and donor["id"] == 1


def test_same_day_and_score_in_different_leagues_is_not_a_duplicate():
    df = pd.DataFrame([_match(1, "atletico mg", "flamengo", league="soccer_brazil_campeonato"),
                       _match(2, "atletico go", "fluminense", league="soccer_brazil_serie_b")])
    assert sa.find_duplicate_pairs(df) == []


def test_missing_league_still_allows_merge():
    """Si una fila no trae liga (fuente distinta), manda el parecido de nombres."""
    df = pd.DataFrame([_match(1, "nott'm forest", "arsenal", league=None),
                       _match(2, "nottm forest", "arsenal")])
    assert len(sa.find_duplicate_pairs(df)) == 1


def test_keep_prefers_row_with_more_stats_when_both_names_are_clean():
    a = _match(1, "man united", "chelsea")
    b = _match(2, "man utd", "chelsea", home_corners=5, away_corners=3)
    keep, donor = sa.choose_keep(a, b)
    assert keep["id"] == 2


# ============================================================
# Sanidad del xG proxy
# ============================================================

def test_xg_check_uses_both_sides_against_real_goals():
    """Con tiros de local y visitante típicos y goles reales de ~1.4 por
    equipo, el proxy cuadra (ratio ~1). Mirando solo al local, el número
    salía inflado por la ventaja de local — la falsa alarma de "1.63"."""
    xg, goals, ratio = sa.xg_proxy_vs_goals(hsot=4.9, asot=3.9, hsh=13.8, ash=11.2,
                                            hg=1.55, ag=1.25)
    assert 0.90 <= ratio <= 1.10
    home_only, _, _ = sa.xg_proxy_vs_goals(4.9, 4.9, 13.8, 13.8, 1.55, 1.55)
    assert home_only > xg


def test_xg_check_flags_a_real_inflation():
    _, _, ratio = sa.xg_proxy_vs_goals(hsot=6.5, asot=5.5, hsh=18, ash=16, hg=1.3, ag=1.1)
    assert ratio > sa.XG_RATIO_BAND[1]
