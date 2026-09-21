"""
tests/test_round15_audit.py
===========================
Tests de la Ronda 15: ancla empírica de BTTS (J1), rho en la señal 3 del
ensemble (J2/R6) y fallback unificado de tau (J3).
"""

import inspect
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.pipeline.prediction_pipeline as pp
import src.models.ensemble_model as em
from src.features.league_calibration import (
    BTTS_SHRINK,
    LEAGUE_FACTORS,
    get_btts_rate,
    get_over25_rate,
)


def _source(module):
    return inspect.getsource(module)


# ============================================================
# J1 — BTTS anclado a la tasa real de liga
# ============================================================

def test_btts_rate_present_for_every_league():
    """18/18 ligas con btts_rate (14 medidas en Q-BA, 4 default)."""
    for lg, f in LEAGUE_FACTORS.items():
        assert "btts_rate" in f, f"{lg} sin btts_rate"
        assert 0.30 <= f["btts_rate"] <= 0.70, f"{lg}: tasa fuera de rango"


def test_get_btts_rate_mirrors_get_over25_rate():
    """Getter simétrico, con fallback parcial de liga y default."""
    assert get_btts_rate("soccer_epl") == LEAGUE_FACTORS["soccer_epl"]["btts_rate"]
    assert get_btts_rate("soccer_epl_extra") == get_btts_rate("soccer_epl")


def test_pipeline_applies_btts_shrink():
    """El shrink va donde el de over25, ANTES de los caps, y con bandera
    R13 en el decision_log."""
    src = _source(pp)
    assert "get_btts_rate(league_key)" in src
    assert "poisson_probs[\"btts_yes\"] * (1 - BTTS_SHRINK)" in src
    assert '"btts_shrink": BTTS_SHRINK' in src


def test_btts_shrink_matches_over25_family():
    """Mismo estimador, misma data family (Q-BA), mismo peso — elegido r15."""
    assert BTTS_SHRINK == 0.20


# ============================================================
# J2 — la señal 3 del ensemble en la misma matriz (R6)
# ============================================================

def test_ensemble_propagates_rho():
    """Los cinco sitios que valoran la matriz de marcadores reciben el
    mismo rho: pipeline DC + h1 + h2, ensemble señal 3, AH/DNB."""
    src_pp = _source(pp)
    assert src_pp.count("rho=DC_RHO_SCORE") >= 5   # dc, h1, h2, prob_ah, dnb
    src_em = _source(em)
    assert "rho=rho" in _source(em) or "match_outcomes(lh, la, rho=rho)" in src_em
    assert inspect.signature(em.ensemble_predict).parameters["rho"] is not None


def test_ensemble_signal3_coheres_with_signal1():
    """Señal 1 (DC con rho) y señal 3 (_form_probs con el mismo rho) deben
    coincidir en equipos promedio — era la familia del bug I1."""
    from src.models.ensemble_model import _form_probs
    from src.pipeline.prediction_pipeline import compute_lambdas
    from src.models.dixon_coles_model import match_outcomes
    f = LEAGUE_FACTORS["soccer_epl"]
    ha, tempo = f["home_advantage"], f["tempo"]
    b = 1.35
    form = _form_probs(b, b, b, b, home_advantage=ha, tempo=tempo,
                       baseline=b, rho=-0.0947)
    lh, la = compute_lambdas(b, b, b, b, ha, tempo)
    dc = match_outcomes(lh, la, rho=-0.0947)
    assert all(abs(x - y) < 1e-9 for x, y in zip(form, dc))


# ============================================================
# J3 — fallback de tau unificado
# ============================================================

def test_totals_and_btts_uses_score_rho():
    """BTTS y la matriz comparten DC_RHO_SCORE: sin fallbacks distintos
    para el mismo tau (era 1.11pp de divergencia si el refit entregaba
    rho bajo)."""
    src = _source(pp)
    assert "totals_and_btts(lambda_home, lambda_away,\n                                        rho=DC_RHO_SCORE)" in src


def test_dc_rho_score_falls_back_to_literature():
    """Sin fit: la matriz Y BTTS caen juntos al rho de la literatura."""
    src = _source(pp)
    assert "DC_RHO_SCORE = DC_RHO_GLOBAL if DC_RHO_GLOBAL is not None else RHO_DC_LITERATURE" in src
