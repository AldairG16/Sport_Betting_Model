"""
tests/test_round15_audit.py
===========================
Tests de la Ronda 15: ancla empírica de BTTS (J1), rho en la señal 3 del
ensemble (J2/R6) y fallback unificado de tau (J3).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.pipeline.prediction_pipeline as pp
from src.features.league_calibration import (
    BTTS_SHRINK,
    LEAGUE_FACTORS,
    get_btts_rate,
)


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


def _model_probs_for(monkeypatch, btts_rate: float) -> dict:
    """model_probs de alpha vs beta con una tasa BTTS de liga dada."""
    from tests.pipeline_harness import run_pipeline, default_matches
    seen = []
    real = pp._stage_blend

    def spy(model_probs, market_probs, market_probs_raw):
        seen.append(dict(model_probs))
        return real(model_probs, market_probs, market_probs_raw)

    run_pipeline(monkeypatch, matches=default_matches().iloc[[0]],
                 overrides={"_stage_blend": spy, "get_btts_rate": lambda league: btts_rate})
    return seen[0]


def test_pipeline_applies_btts_shrink(monkeypatch):
    """El BTTS del modelo se encoge hacia la tasa real de la liga con peso
    BTTS_SHRINK (mismo estimador que over25), y btts_no es su complemento."""
    low = _model_probs_for(monkeypatch, 0.30)
    high = _model_probs_for(monkeypatch, 0.70)
    assert high["btts"] - low["btts"] == pytest.approx(BTTS_SHRINK * 0.40, abs=1e-9)
    assert high["btts_no"] - low["btts_no"] == pytest.approx(-BTTS_SHRINK * 0.40, abs=1e-9)


def test_decision_log_flags_btts_shrink(monkeypatch):
    """Bandera R13: el shrink viaja al decision_log para separar cohortes."""
    import json
    from tests.pipeline_harness import run_pipeline
    logs = [json.loads(b["decision_log"]) for b in run_pipeline(monkeypatch)["bets"]
            if b.get("decision_log")]
    assert logs and all(log["model"]["btts_shrink"] == BTTS_SHRINK for log in logs)


def test_btts_shrink_matches_over25_family():
    """Mismo estimador, misma data family (Q-BA), mismo peso — elegido r15."""
    assert BTTS_SHRINK == 0.20


# ============================================================
# J2 — la señal 3 del ensemble en la misma matriz (R6)
# ============================================================

SCORE_MATRIX_USERS = ("match_outcomes", "prob_ah", "get_dnb_probs",
                      "totals_and_btts", "ensemble_predict")


def _rhos_seen(monkeypatch, **kwargs) -> dict:
    """rho con que el pipeline llama a cada función que valora la matriz de
    marcadores (1X2 + 1T/2T, AH, DNB, totales/BTTS, señal 3 del ensemble)."""
    from tests.pipeline_harness import run_pipeline
    seen = {name: [] for name in SCORE_MATRIX_USERS}

    def spy_for(name):
        real = getattr(pp, name)

        def spy(*a, **k):
            seen[name].append(k.get("rho"))
            return real(*a, **k)
        return spy

    run_pipeline(monkeypatch, overrides={n: spy_for(n) for n in SCORE_MATRIX_USERS}, **kwargs)
    return seen


def test_ensemble_propagates_rho(monkeypatch):
    """Todos los sitios que valoran la matriz de marcadores reciben EL
    MISMO rho (el del fit, −0.09 en el arnés): era la familia del bug I1."""
    seen = _rhos_seen(monkeypatch)
    for name, rhos in seen.items():
        assert rhos, f"{name} no se ejercitó"
        assert set(rhos) == {-0.09}, f"{name}: {set(rhos)}"
    # 1X2 + medio tiempo (M5 trae cuotas 1T): la matriz se usa varias veces
    assert len(seen["match_outcomes"]) > len(seen["ensemble_predict"])


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

def test_totals_and_btts_uses_score_rho(monkeypatch):
    """BTTS y la matriz comparten DC_RHO_SCORE: sin fallbacks distintos
    para el mismo tau (era 1.11pp de divergencia si el refit entregaba
    rho bajo). Verificado ejecutando el pipeline (22-sep-26)."""
    from tests.pipeline_harness import run_pipeline
    seen = []
    real = pp.totals_and_btts

    def spy(lh, la, rho=None, **kw):
        seen.append(rho)
        return real(lh, la, rho=rho, **kw)

    run_pipeline(monkeypatch, overrides={"totals_and_btts": spy})
    # el arnés ajusta rho=-0.09 (> 0.03 → tau activa): matriz y BTTS igual
    assert seen and all(r == -0.09 for r in seen)


def test_dc_rho_score_falls_back_to_literature(monkeypatch):
    """Sin fit: la matriz Y BTTS caen JUNTOS al rho de la literatura."""
    seen = _rhos_seen(monkeypatch, dc_params={})
    for name, rhos in seen.items():
        assert rhos and set(rhos) == {pp.RHO_DC_LITERATURE}, f"{name}: {set(rhos)}"
