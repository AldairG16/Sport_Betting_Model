"""
tests/test_round5_audit.py
==========================
Tests de la Ronda 5: regla de combinación modelo/mercado (N1/N2),
regresión N5 (baseline), anclaje extendido y devig con guardia.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.features.market_calibration as mc
from src.features.market_calibration import calibrate_probability
from src.features.team_form import KALMAN_BASELINE
from src.models.ensemble_model import _form_probs, ensemble_predict
from src.pipeline.prediction_pipeline import (
    ANCHOR_PARAMETRIC_PREFIXES,
    _anchorable,
    _devig_three_way,
    _devig_two_way,
)


# ============================================================
# N1 — el precio del mercado ya no se descarta
# ============================================================

def test_is_strong_edge_is_gone():
    """
    is_strong_edge descartaba el precio del mercado con desacuerdos ≥7.1pp
    (confianza de producción 0.8-1.0). Q-G midió brecha 19.8-58.4pt justo
    en esos tramos: la función sobra y su reaparición es una regresión.
    """
    assert not hasattr(mc, "is_strong_edge"), (
        "is_strong_edge volvió a existir — el mercado vuelve a descartarse")


def test_model_weight_decreases_with_disagreement():
    """
    A MAYOR desacuerdo con el mercado, MENOS peso del modelo (la inversión
    exacta de la lógica vieja). Se mide como distancia al precio de mercado.
    """
    near = abs(calibrate_probability(0.50, 0.40, edge=0.05) - 0.40)
    far = abs(calibrate_probability(0.50, 0.40, edge=0.25) - 0.40)
    assert far < near, (
        f"el peso del modelo crece con el desacuerdo ({far=}, {near=})")


# ============================================================
# N2 — simetría: el signo del desacuerdo no cambia la fiabilidad
# ============================================================

def test_blend_is_symmetric_in_edge_sign():
    """
    Con el mismo |edge|, el resultado debe quedar equidistante del precio
    de mercado, venga el desacuerdo de arriba o de abajo.
    """
    up = calibrate_probability(0.50, 0.40, edge=+0.10)
    down = calibrate_probability(0.50, 0.60, edge=-0.10)
    assert abs((up - 0.40) - (0.60 - down)) < 1e-9, (
        f"blend asimétrico: {up=} vs {down=}")


def test_direction_no_longer_amplifies():
    """
    La tabla vieja daba peso 0.90 al modelo con edge +0.25 y 0.50 con
    −0.25. Ahora ambos |edge| deben producir el mismo peso (0.30).
    """
    w_up = abs(calibrate_probability(0.70, 0.45, edge=+0.25) - 0.45) / 0.25
    w_down = abs(calibrate_probability(0.20, 0.45, edge=-0.25) - 0.45) / 0.25
    assert abs(w_up - w_down) < 1e-9


# ============================================================
# N5 (R8) — el baseline del ensemble vuelve a ser KALMAN_BASELINE
# ============================================================

def test_ensemble_baseline_defaults_to_kalman():
    """D11 no puede volver por la puerta de atrás: default None → Kalman."""
    assert inspect.signature(_form_probs).parameters["baseline"].default is None
    assert inspect.signature(ensemble_predict).parameters["baseline"].default is None

    a = _form_probs(1.5, 1.2, 1.1, 1.4, 1.214, 1.129)
    b = _form_probs(1.5, 1.2, 1.1, 1.4, 1.214, 1.129, baseline=KALMAN_BASELINE)
    assert a == b, "el default de baseline no coincide con KALMAN_BASELINE"


# ============================================================
# (b) — anclaje extendido + devig con guardia
# ============================================================

def test_anchorable_covers_parametric_markets():
    for market in ("ah_home_-0.5", "ah_away_+0.5", "dnb_home", "dnb_away",
                   "corners_over_9.5", "corners_under_10.5",
                   "cards_over_4.5", "cards_under_3.5", "h1_home", "h2_away"):
        assert _anchorable(market), f"debería anclar: {market}"


def test_anchorable_excludes_markets_without_clean_devig():
    """DC (odds parciales) y shots no tienen devig confiable: no anclan."""
    for market in ("dc_1x", "dc_x2", "dc_12", "shots_over_5.5"):
        assert not _anchorable(market), f"NO debería anclar: {market}"


def test_devig_two_way_basic():
    # par justo 2.00/2.00 → 0.5/0.5
    assert abs(_devig_two_way(2.0, 2.0) - 0.5) < 1e-9


def test_devig_two_way_rejects_inconsistent_book():
    """
    Las cuotas son máximo entre casas: un par con booksum < 0.95 no es un
    mercado real. La advertencia de A sobre anclar AH con cuotas máximas.
    """
    assert _devig_two_way(2.2, 2.2) is None    # booksum 0.909
    assert _devig_two_way(1.05, 1.05) is None  # booksum 1.905
    assert _devig_two_way(2.0, None) is None   # falta una pata


def test_devig_three_way_needs_full_trio():
    assert _devig_three_way(2.0, 3.2, 4.0) is not None
    assert _devig_three_way(2.0, None, 4.0) is None


def test_all_fixed_markets_in_anchor_prefixes_documented():
    """Los prefijos paramétricos no deben pisar mercados de otro dominio."""
    # h1_/h2_ son anclables (trío HT); shots no está en la lista
    assert "shots_over_" not in ANCHOR_PARAMETRIC_PREFIXES
    assert "ah_home_" in ANCHOR_PARAMETRIC_PREFIXES
