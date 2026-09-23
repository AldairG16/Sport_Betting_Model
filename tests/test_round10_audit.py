"""
tests/test_round10_audit.py
===========================
Tests de la Ronda 10: banderas de cohorte (E3/R13), diagnóstico del
optimizador (E2) y trazabilidad de población del slate (E1).

Desde el 23-sep-26 se verifica EJECUTANDO el código (antes se buscaba el
texto en el fuente): el decision_log de una corrida del arnés, la puerta de
tau con histéresis y un ajuste DC-MLE real sobre partidos sintéticos.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.models.dc_mle_fitter as fitter
import src.pipeline.prediction_pipeline as pp


def _decision_logs(out):
    logs = [json.loads(b["decision_log"]) for b in out["bets"] if b.get("decision_log")]
    assert logs, "el escenario debe producir bets con decision_log"
    return logs


# ============================================================
# E3/R13 — cohortes separables en decision_log
# ============================================================

def test_decision_log_carries_cohort_flags(monkeypatch):
    """mle_converged y dc_rho_global viajan al decision_log: sin bandera, un
    cambio de CLV de BTTS no sería atribuible (blend MLE y tau cambiaron a
    la vez)."""
    from tests.pipeline_harness import run_pipeline
    for log in _decision_logs(run_pipeline(monkeypatch)):
        assert log["model"]["mle_converged"] is True
        assert log["model"]["dc_rho_global"] == pytest.approx(-0.09)


def test_converged_defaults_false_when_params_absent(monkeypatch):
    """Sin fit, la bandera es False (cohorte 'sin MLE'), no un NULL ambiguo,
    y la matriz usa Poisson sin tau propio."""
    from tests.pipeline_harness import run_pipeline
    for log in _decision_logs(run_pipeline(monkeypatch, dc_params={})):
        assert log["model"]["mle_converged"] is False
        assert log["model"]["dc_rho_global"] is None


# ============================================================
# Puerta de tau con histéresis (_resolve_dc_rho)
# ============================================================

@pytest.mark.parametrize("rho_fit,prev,expected", [
    (-0.05, None, -0.05),     # > 0.03: se enciende siempre
    (-0.01, -0.05, None),     # < 0.015: se apaga siempre
    (-0.02, -0.04, -0.02),    # zona intermedia: sigue encendida si lo estaba
    (-0.02, -0.01, None),     # ... y sigue apagada si lo estaba
    (-0.02, None, None),      # sin histórico: apagada
])
def test_tau_gate_has_hysteresis(monkeypatch, rho_fit, prev, expected):
    """Enciende con |rho| > 0.03, apaga solo bajo 0.015: el pricing de BTTS
    no cambia por el ruido de un refit en la zona intermedia."""
    monkeypatch.setattr(fitter, "_load_params",
                        lambda: {"rho": rho_fit, "converged": True,
                                 "fitted_at": "2030-01-01", "final_grad_max": 0.2})
    monkeypatch.setattr(pp, "_previous_fit_rho", lambda: prev)
    rho, converged, fingerprint, grad = pp._resolve_dc_rho()
    assert rho == expected
    assert converged is True and fingerprint == "2030-01-01|g0.2" and grad == 0.2


def test_resolve_rho_without_fit_is_poisson_and_unflagged(monkeypatch):
    monkeypatch.setattr(fitter, "_load_params", lambda: {})
    monkeypatch.setattr(pp, "_previous_fit_rho", lambda: None)
    assert pp._resolve_dc_rho() == (None, False, "?|g?", None)


# ============================================================
# E2 — diagnóstico del optimizador persistido (ajuste real)
# ============================================================

def _synthetic_league(n_teams=20, rounds=3, seed=3, lam_home=1.45, lam_away=1.10):
    """Liga sintética con ventaja local conocida: ln(1.45/1.10) ≈ 0.28."""
    rng = np.random.default_rng(seed)
    teams = [f"t{i:02d}" for i in range(n_teams)]
    start = pd.Timestamp.now().normalize() - pd.Timedelta(days=600)
    rows, day = [], 0
    for _ in range(rounds):
        for h in teams:
            for a in teams:
                if h == a:
                    continue
                if rng.random() < 0.5:     # ~la mitad de los cruces por vuelta
                    continue
                rows.append({"home_team": h, "away_team": a,
                             "home_goals": int(rng.poisson(lam_home)),
                             "away_goals": int(rng.poisson(lam_away)),
                             "date": start + pd.Timedelta(days=day), "neutral": False})
                day += 1
    return pd.DataFrame(rows)


def test_fit_records_optimizer_diagnostics(monkeypatch, tmp_path):
    """El ajuste guarda final_fun y final_grad_max (sin ellos no se
    distingue ruido de presupuesto entre corridas), persiste en Neon y
    recupera una ventaja local conocida."""
    league = _synthetic_league()
    assert len(league) >= 500
    saved = []
    monkeypatch.setattr(fitter.pd, "read_sql", lambda sql, con=None, **kw: league.copy())
    monkeypatch.setattr(fitter, "_load_params_from_db", lambda: {})
    monkeypatch.setattr(fitter, "DC_PARAMS_FILE", tmp_path / "dc_params.json")
    monkeypatch.setattr(fitter, "_save_params_to_db", lambda p: saved.append(p) or True)

    out = fitter.fit_dc_parameters(verbose=False)

    assert isinstance(out["final_fun"], float) and np.isfinite(out["final_fun"])
    assert isinstance(out["final_grad_max"], float) and out["final_grad_max"] >= 0
    assert isinstance(out["converged"], bool)
    assert out["n_matches"] == len(league) and out["n_teams"] == 20
    assert -0.5 < out["rho"] < 0
    assert out["home_adv"] == pytest.approx(np.log(1.45 / 1.10), abs=0.12)
    assert saved == [out]
    assert json.loads((tmp_path / "dc_params.json").read_text())["final_fun"] == out["final_fun"]


def test_fit_refuses_small_samples(monkeypatch, tmp_path):
    """Menos de 500 partidos → {} (MLE poco confiable), sin escribir nada."""
    monkeypatch.setattr(fitter.pd, "read_sql",
                        lambda sql, con=None, **kw: _synthetic_league(n_teams=6, rounds=1))
    monkeypatch.setattr(fitter, "_save_params_to_db", lambda p: pytest.fail("no debe guardar"))
    monkeypatch.setattr(fitter, "DC_PARAMS_FILE", tmp_path / "dc_params.json")
    assert fitter.fit_dc_parameters(verbose=False) == {}
    assert not (tmp_path / "dc_params.json").exists()


# ============================================================
# E1 — trazabilidad de población del slate
# ============================================================

def test_summary_counts_the_true_slate(monkeypatch):
    """El resumen reporta los partidos del slate real (ventana 3 días), no
    las filas totales de la base (había hasta 29 días)."""
    from tests.pipeline_harness import run_pipeline, default_matches
    run_pipeline(monkeypatch)
    assert pp.LAST_RUN_SUMMARY["matches"] == len(default_matches())
    assert pp.LAST_RUN_SUMMARY["failed_matches"] == 0
