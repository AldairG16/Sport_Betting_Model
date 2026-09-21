"""
tests/test_round11_audit.py
===========================
Tests de la Ronda 11: huella del fit en decision_log (F1/R13), histéresis
de tau con estado del histórico (F2), historización de refits y
histograma del gradiente (E2 heredado).
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.models.dc_mle_fitter as fitter
import src.pipeline.prediction_pipeline as pp


def _source(module):
    return inspect.getsource(module)


# ============================================================
# F1 — la huella del fit sí segmenta
# ============================================================

def test_decision_log_carries_fit_fingerprint():
    """mle_converged es constante False y no separa nada (F1). La huella
    real: CUÁNDO se ajustó y qué tan lejos de estacionario quedó."""
    src = _source(pp)
    assert '"fit_fingerprint": DC_FIT_FINGERPRINT' in src
    assert '"mle_final_grad": DC_FINAL_GRAD' in src


def test_fingerprint_uses_fitted_at_and_gradient():
    src = _source(pp)
    assert "fitted_at" in src and "final_grad_max" in src


def test_mle_converged_kept_as_witness():
    """Decisión F1: la bandera se queda (testigo del día que converja)."""
    src = _source(pp)
    assert '"mle_converged": DC_CONVERGED' in src


# ============================================================
# F2 — historización y estabilidad de rho
# ============================================================

def test_refits_are_historized():
    """model_state sobrescribe: sin history, la serie home_adv/rho se
    pierde y ninguna decisión puede apoyarse en ella (prohibición 21)."""
    src = _source(fitter)
    assert "model_state_history" in src
    assert "INSERT INTO model_state_history" in src


def test_tau_gate_has_hysteresis_with_state():
    src = _source(pp)
    assert "abs(_rho_fit) > 0.03" in src      # encendido
    assert "abs(_rho_fit) < 0.015" in src     # apagado
    assert "_previous_fit_rho" in src         # estado anterior
