"""
tests/test_round11_audit.py
===========================
Tests de la Ronda 11: huella del fit en decision_log (F1/R13), histéresis
de tau con estado del histórico (F2), historización de refits.

Desde el 23-sep-26 se verifica ejecutando el código (antes se buscaba el
texto en el fuente). La histéresis de la puerta de tau se prueba caso por
caso en test_round10_audit.py.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.models.dc_mle_fitter as fitter
import src.pipeline.prediction_pipeline as pp
from tests.fake_db import FakeEngine, FakeResult


# ============================================================
# F1 — la huella del fit sí segmenta
# ============================================================

def test_decision_log_carries_fit_fingerprint(monkeypatch):
    """mle_converged no separa semanas (F1). La huella real: CUÁNDO se
    ajustó y qué tan lejos de estacionario quedó. mle_converged se queda
    como testigo del día que converja."""
    from tests.pipeline_harness import run_pipeline
    out = run_pipeline(monkeypatch, dc_params={"rho": -0.09, "converged": False,
                                               "fitted_at": "2030-02-03T07:00",
                                               "final_grad_max": 1.25})
    logs = [json.loads(b["decision_log"]) for b in out["bets"] if b.get("decision_log")]
    assert logs
    for log in logs:
        assert log["model"]["fit_fingerprint"] == "2030-02-03T07:00|g1.25"
        assert log["model"]["mle_final_grad"] == 1.25
        assert log["model"]["mle_converged"] is False


# ============================================================
# F2 — historización y estado de la histéresis
# ============================================================

def test_refits_are_historized(monkeypatch):
    """model_state se sobrescribe: sin histórico, la serie home_adv/rho se
    pierde (prohibición 21). Cada fit va también a model_state_history, una
    vez por fitted_at."""
    eng = FakeEngine()
    monkeypatch.setattr(fitter, "engine", eng)
    params = {"rho": -0.08, "home_adv": 0.25, "fitted_at": "2030-02-03T07:00"}
    assert fitter._save_params_to_db(params) is True
    (_, current), = eng.statements("INSERT INTO model_state (key, value, updated_at)")
    (sql, hist), = eng.statements("INSERT INTO model_state_history")
    assert json.loads(current["p"]) == params == json.loads(hist["p"])
    assert hist["fa"] == "2030-02-03T07:00"
    assert "WHERE NOT EXISTS" in sql            # un refit no se historiza dos veces


def test_save_failure_is_reported_not_raised(monkeypatch):
    def boom(sql, params):
        raise RuntimeError("neon caído")
    monkeypatch.setattr(fitter, "engine", FakeEngine(boom))
    assert fitter._save_params_to_db({"rho": -0.1}) is False


@pytest.mark.parametrize("rows,expected", [
    ([("-0.03",), ("-0.05",)], -0.05),     # el fit ANTERIOR al vigente
    ([("-0.03",)], None),                  # un solo fit: sin estado previo
    ([("-0.03",), (None,)], None),
])
def test_previous_fit_rho_reads_the_prior_fit(monkeypatch, rows, expected):
    """El estado de la histéresis sale del histórico de fits."""
    monkeypatch.setattr(pp, "engine", FakeEngine(lambda sql, p: FakeResult(rows=rows)))
    assert pp._previous_fit_rho() == expected


def test_previous_fit_rho_tolerates_db_errors(monkeypatch):
    def boom(sql, params):
        raise RuntimeError("sin tabla")
    monkeypatch.setattr(pp, "engine", FakeEngine(boom))
    assert pp._previous_fit_rho() is None
