"""
tests/test_pipeline_extended.py
===============================
Escenario EXTENDIDO del pipeline (tests/pipeline_harness.extended_kwargs):
recorre los caminos que el escenario base no toca — DC-MLE con sus tres
pesos, entre semana, liga dura, pocas casas / ilíquido, papel (Mundial),
córners y tarjetas, doble oportunidad con calibración activa, spread alto
y línea blanda, partido raro, sin historial, sin cuotas, empate
contextual, movimiento de línea en contra, "la tabla miente", clima,
fatiga, motivación, H2H, correlación y tope por slate.

El golden congela apuestas, shadow, papel y decision_log. Es la red de
seguridad del refactor en etapas de run_prediction_pipeline: un refactor
correcto no cambia NI UN número.
"""

import json
import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.pipeline_harness import run_pipeline, normalize_full, extended_kwargs

GOLDEN = Path(__file__).parent / "golden" / "pipeline_extended.json"


def _approx_equal(got, exp, path="") -> None:
    if isinstance(exp, float):
        assert isinstance(got, (int, float)) and math.isclose(got, exp, abs_tol=1e-5), f"{path}: {got} != {exp}"
    elif isinstance(exp, dict):
        assert set(got) == set(exp), f"{path}: claves {sorted(set(got) ^ set(exp))}"
        for k in exp:
            _approx_equal(got[k], exp[k], f"{path}.{k}")
    elif isinstance(exp, list):
        assert len(got) == len(exp), f"{path}: {len(got)} != {len(exp)} elementos"
        for i, (g, e) in enumerate(zip(got, exp)):
            _approx_equal(g, e, f"{path}[{i}]")
    else:
        assert got == exp, f"{path}: {got!r} != {exp!r}"


def test_extended_scenario_matches_golden(monkeypatch):
    got = json.loads(json.dumps(normalize_full(run_pipeline(monkeypatch, **extended_kwargs())),
                                default=str))
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(got, indent=1, ensure_ascii=False), encoding="utf-8")
        pytest.skip("golden extendido regenerado")
    _approx_equal(got, json.loads(GOLDEN.read_text(encoding="utf-8")))


def test_extended_scenario_exercises_the_paths_it_claims(monkeypatch):
    """Si un cambio del arnés deja de recorrer un camino, el golden pierde
    valor sin avisar: esto lo hace visible."""
    out = normalize_full(run_pipeline(monkeypatch, **extended_kwargs()))
    matches = {b["match"] for b in out["bets"]}
    assert sum(1 for b in out["bets"] if b["match"] == "lam vs mu") == 2   # correlación
    assert out["paper"], "papel (Mundial)"
    assert "psi vs ome" not in matches                                     # ilíquido
    assert "gamma vs kappa" not in matches                                 # partido raro
    assert "nof vs mu" not in matches                                      # sin historial
    assert not any(b["match"] == "alpha vs kappa" and b["market"] == "home_win"
                   for b in out["bets"])                                   # línea en contra
    mle = {k.split("|")[0]: v["model"]["mle_weight"] for k, v in out["decision_logs"].items()}
    assert mle.get("lam vs mu") == 0.55                                    # ≥30 partidos
    assert any(s["market"].startswith("cards_") for s in out["shadow"])
    assert any(s["market"].startswith("dc_") for s in out["shadow"])


def test_dry_run_computes_everything_but_writes_nothing(monkeypatch):
    """Modo ensayo: mismo cálculo, cero escrituras y cero Telegram."""
    import src.pipeline.prediction_pipeline as pp
    import scripts.notify_telegram as nt
    sent = []
    monkeypatch.setattr(nt, "send_message", lambda msg, *a, **k: sent.append(msg))
    kwargs = extended_kwargs()
    real = normalize_full(run_pipeline(monkeypatch, **kwargs))
    monkeypatch.setattr(pp, "run_prediction_pipeline",
                        lambda _f=pp.run_prediction_pipeline: _f(dry_run=True))
    dry = run_pipeline(monkeypatch, **extended_kwargs())
    assert dry["bets"] == [] and dry["shadow"] == [] and dry["paper"] == []
    assert sorted((b["match"], b["market"]) for b in dry["returned"]) == \
        sorted((b["match"], b["market"]) for b in real["bets"] + real["paper"])
    assert pp.LAST_RUN_SUMMARY["dry_run"] is True
    assert pp.LAST_RUN_SUMMARY["bets"] == len(real["bets"])
    assert not sent


def test_circuit_breaker_stops_betting(monkeypatch):
    import scripts.notify_telegram as nt
    sent = []
    monkeypatch.setattr(nt, "send_message", lambda msg, *a, **k: sent.append(msg))
    import src.models.bankroll_manager as bm
    monkeypatch.setattr(bm, "get_bankroll_stats", lambda: {"drawdown_pct": 95.0})
    out = run_pipeline(monkeypatch, bankroll=5.0)
    assert out["bets"] == [] and out["shadow"] == []
    assert any("CIRCUIT BREAKER" in m for m in sent)
