"""
tests/test_round7_audit.py
==========================
Tests de la Ronda 7: gate de CLV estadístico y alcanzable (B1/R10),
bloqueo explícito de favoritos AH, shadow logging (B2) y bandas.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.pipeline.prediction_pipeline as pp
import src.models.save_bets as sb
from scripts.clv_gate import (
    MIN_BETS_STAT,
    _deviation_band,
    market_clv_blocked,
)


def _source(module):
    return inspect.getsource(module)


# ============================================================
# B1(a) — criterio estadístico alcanzable
# ============================================================

def test_clv_gate_fires_with_realistic_sample():
    """Con efecto real (CLV −2pt) el gate debe disparar con n=35, no con 100."""
    assert market_clv_blocked(n=35, mean=-0.02, sd=0.04) is True


def test_clv_gate_spares_noise():
    """CLV levemente negativo sin significancia NO bloquea (falso positivo)."""
    assert market_clv_blocked(n=35, mean=-0.005, sd=0.04) is False


def test_clv_gate_requires_minimum_sample():
    assert market_clv_blocked(n=20, mean=-0.05, sd=0.04) is False
    assert MIN_BETS_STAT == 30


def test_clv_gate_large_sample_small_effect():
    """Con n grande, un efecto chico pero real sí bloquea."""
    assert market_clv_blocked(n=200, mean=-0.008, sd=0.04) is True
    assert market_clv_blocked(n=200, mean=-0.001, sd=0.04) is False


# ============================================================
# B2 — shadow logging
# ============================================================

def test_shadow_captures_before_edge_filter():
    """
    (actualizado en ronda 8/C1) La captura ya no vive en el loop de bets
    (que pierde todo con edge_market < 0.02 en find_value_bets): es un
    barrido sobre clean_probabilities × odds ANTES de find_value_bets, con
    piso propio SHADOW_MIN_DEV declarado (R11).
    """
    src = _source(pp)
    assert "SHADOW SWEEP" in src
    assert src.count('shadow_records.append({') == 1
    assert "find_value_bets(clean_probabilities, odds)" in src
    # el sweep debe aparecer ANTES que find_value_bets en el archivo
    assert src.index("SHADOW SWEEP") < src.index("find_value_bets(clean_probabilities")


def test_pipeline_persists_shadow_records():
    src = _source(pp)
    assert "persist_shadow_bets(shadow_records)" in src
    assert "shadow_records: list = []" in src


def test_shadow_table_dedupes_reruns():
    """Las re-corridas del mismo slate no duplican filas."""
    assert "UNIQUE (match, market, match_date)" in sb.SHADOW_TABLE_SQL


def test_shadow_closing_reuses_same_mapping():
    """El closing del shadow usa las MISMAS funciones que bets_history."""
    src = _source(sb)
    assert src.count("_closing_odds_for(market, odds") >= 2
    assert src.count("_nearest_market_row(home, away, bet_match_date)") >= 2
    assert "_update_shadow_closing" in src


# ============================================================
# B2 — bandas de desvío para el análisis CLV
# ============================================================

def test_deviation_bands_cover_the_window():
    """Las bandas de A: [0-5), [5-10), [10-15), [15-19), [19-25), [25-30), 30+."""
    assert _deviation_band(0.04) == "0-5"
    assert _deviation_band(0.07) == "5-10"
    assert _deviation_band(0.12) == "10-15"
    assert _deviation_band(0.17) == "15-19"
    assert _deviation_band(0.22) == "19-25"
    assert _deviation_band(0.28) == "25-30"
    assert _deviation_band(0.35) == "30+"
    assert _deviation_band(None) == "sin_ref"


# ============================================================
# B1(b) — favoritos AH bloqueados explícitamente
# ============================================================

def test_ah_favorites_explicitly_blocked():
    """Bloqueo declarado en el filtro (no piso inalcanzable), paper excluido."""
    src = _source(pp)
    assert '_ah_group(mkt) in ("ah_home_fav", "ah_away_fav")' in src


# ============================================================
# R8 — los parámetros de la ronda 6 siguen intactos
# ============================================================

def test_round6_thresholds_unchanged():
    assert pp.MIN_EDGE == 0.05
    assert pp.MAX_MODEL_DEVIATION == 0.30
    assert max(pp.MIN_EDGE_BY_MARKET.values()) <= 0.06
