"""
tests/test_round10_audit.py
===========================
Tests de la Ronda 10: banderas de cohorte (E3/R13), diagnóstico del
optimizador (E2) y trazabilidad de población del slate (E1).
"""

import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.models.dc_mle_fitter as fitter
import src.pipeline.prediction_pipeline as pp


def _source(module):
    return inspect.getsource(module)


# ============================================================
# E3/R13 — cohortes separables en decision_log
# ============================================================

def test_decision_log_carries_cohort_flags():
    """mle_converged y dc_rho_global viajan al decision_log: mañana
    cambiaron blend MLE y tau de BTTS a la vez — sin bandera, el CLV de
    BTTS no sería atribuible."""
    src = _source(pp)
    assert '"mle_converged": DC_CONVERGED' in src
    assert '"dc_rho_global": DC_RHO_GLOBAL' in src


def test_converged_defaults_false_when_params_absent():
    """Sin params, la bandera es False (cohorte 'sin MLE'), no un NULL
    ambiguo."""
    src = _source(pp)
    assert "DC_CONVERGED = False" in src
    assert 'bool(_dc_params.get("converged", False))' in src


# ============================================================
# E2 — diagnóstico del optimizador persistido
# ============================================================

def test_fit_records_optimizer_diagnostics():
    """final_fun y final_grad_max en el payload: sin ellos no se distingue
    ruido de presupuesto entre corridas."""
    src = _source(fitter)
    assert '"final_fun"' in src
    assert '"final_grad_max"' in src


def test_tau_gate_threshold_unchanged():
    """La puerta del rho (|rho|<0.02 → Poisson cruda) no se tocó — el
    escalonado de E3 se resolvió con banderas, no cambiando pricing."""
    src = _source(pp)
    assert "abs(_rho_fit) < 0.02" in src


# ============================================================
# E1 — trazabilidad de población del slate
# ============================================================

def test_slate_window_print_uses_true_slate_count():
    """El contorno de E1: 'pares con cuota' debe reportarse junto al número
    de partidos del slate real (ventana 3d), no de las filas totales de la
    base (había hasta 29 días)."""
    src = _source(pp)
    assert "total_matches = len(df)" in src
    assert "sweep_total" in src and "sweep_ref" in src
