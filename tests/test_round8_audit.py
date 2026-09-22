"""
tests/test_round8_audit.py
==========================
Tests de la Ronda 8: rango observable del shadow (C1/R11), histéresis del
gate (C2) y cobertura de la reactivación AH (C3).
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.pipeline.prediction_pipeline as pp
from scripts.clv_gate import (
    BLOCK_STREAK_WEEKS,
    MIN_BETS_STAT,
    _deviation_band,
    merge_gate_state,
    positive_evidence,
    significant_negative,
)
from src.pipeline.prediction_pipeline import SHADOW_MIN_DEV


def _source(module):
    return inspect.getsource(module)


# ============================================================
# C1 — el shadow observa la región que debe medir
# ============================================================

def test_sweep_runs_before_value_filter():
    """El barrido vive ANTES de find_value_bets (que filtra edge<0.02)."""
    src = _source(pp)
    i_sweep = src.index("SHADOW SWEEP")
    i_value = src.index("find_value_bets(clean_probabilities, odds)")
    assert i_sweep < i_value


def test_shadow_min_dev_declares_observable_range():
    """R11: el rango observable está declarado como constante y >= 0."""
    assert SHADOW_MIN_DEV == 0.02
    assert _deviation_band(SHADOW_MIN_DEV) == "0-5"


def test_sweep_requires_reference_price():
    """Sin precio de referencia no hay desvío medible → no se registra."""
    src = _source(pp)
    assert "_spref is None or _sdev is None" in src


# ============================================================
# C2 — histéresis del gate
# ============================================================

def test_block_requires_two_consecutive_windows():
    """Una sola ventana significativo-negativa NO bloquea (multiplicidad)."""
    blocked, streaks, _ = merge_gate_state(
        prev_blocked=set(), prev_streaks={},
        stats={"over25": (35, -0.02, 0.04)})
    assert "over25" not in blocked
    assert streaks["over25"] == 1

    blocked, streaks, _ = merge_gate_state(
        prev_blocked=set(), prev_streaks={"over25": 1},
        stats={"over25": (35, -0.02, 0.04)})
    assert "over25" in blocked
    assert BLOCK_STREAK_WEEKS == 2


def test_blocked_market_survives_empty_window():
    """Histéresis: sin datos, un mercado bloqueado SIGUE bloqueado."""
    blocked, streaks, unblocked = merge_gate_state(
        prev_blocked={"home_win"}, prev_streaks={"home_win": 2},
        stats={})
    assert "home_win" in blocked
    assert unblocked == []
    assert streaks["home_win"] == 2   # congelada


def test_unblock_requires_positive_evidence_not_absence():
    """n<30 con CLV negativo NO desbloquea; n>=30 con CLV>=0 sí."""
    # muestra secada por el bloqueo: sigue
    blocked, _, unblocked = merge_gate_state(
        prev_blocked={"home_win"}, prev_streaks={},
        stats={"home_win": (12, -0.01, 0.04)})
    assert "home_win" in blocked and unblocked == []
    # evidencia positiva real: sale
    blocked, _, unblocked = merge_gate_state(
        prev_blocked={"home_win"}, prev_streaks={},
        stats={"home_win": (35, 0.004, 0.04)})
    assert "home_win" not in blocked and unblocked == ["home_win"]
    assert positive_evidence(35, 0.004, 0.04)
    assert not positive_evidence(35, -0.004, 0.04)


def test_significant_negative_matches_gate_criterion():
    assert significant_negative(35, -0.02, 0.04)
    assert not significant_negative(35, -0.005, 0.04)
    assert not significant_negative(10, -0.05, 0.04)
    assert MIN_BETS_STAT == 30


# ============================================================
# C3 — la reactivación AH tiene datos
# ============================================================

def test_sweep_precedes_ah_fav_block(monkeypatch):
    """Los favoritos AH bloqueados pasan por el sweep (captura previa al
    filtro de candidatos): su CLV de reactivación es medible por
    construcción, no por aproximación. Verificado ejecutando el pipeline."""
    from tests.pipeline_harness import run_pipeline
    out = run_pipeline(monkeypatch)
    assert any(s["market"] == "ah_home_-0.50" for s in out["shadow"])
    assert not any(b["market"] == "ah_home_-0.50" for b in out["bets"])
