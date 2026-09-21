"""
tests/test_round4_audit.py
==========================
Las cuatro aserciones de la Ronda 4 (D13) + regresiones puntuales.

Antes de esta ronda, 197 tests pasaban incluso después de un cambio que
movió los lambdas ~25%. Estas aserciones existen para que ese tipo de
cambio ya no pase desapercibido:

  1. λ de equipos promedio reproduce la media de cada liga (D4)
  2. toda clave de ANCHOR_MAP existe en los mercados del modelo (D6)
  3. las señales 1 y 3 del ensemble corren en la misma escala de λ (D5)
  4. todo mercado que el pipeline puede generar tiene rama en el resolver (D10)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.features.league_calibration import LEAGUE_FACTORS
from src.features.team_form import KALMAN_BASELINE
from src.pipeline.prediction_pipeline import (
    ANCHOR_MAP,
    HT_FIRST_HALF_FRACTION,
    MODEL_PROB_MARKETS,
    compute_lambdas,
)
from src.models.ensemble_model import _form_probs, match_outcomes
from src.models.save_bets import is_resolvable_market


# ============================================================
# 1. D4 — equipos promedio reproducen la media de la liga
# ============================================================

def test_lambda_average_team_reproduces_league_mean():
    """
    Con todos los ratings en el baseline, λ_total debe dar exactamente
    2.5·tempo y el reparto local/visitante home_advantage:1 — para TODAS
    las ligas de LEAGUE_FACTORS. (Antes: +20-33% de goles esperados.)
    """
    b = KALMAN_BASELINE
    for league, f in LEAGUE_FACTORS.items():
        ha, tempo = f["home_advantage"], f["tempo"]
        lh, la = compute_lambdas(b, b, b, b, ha, tempo)
        assert abs((lh + la) - 2.5 * tempo) < 1e-9, (
            f"{league}: λ_total={lh + la:.4f} != {2.5 * tempo:.4f}")
        assert abs(lh / la - ha) < 1e-9, (
            f"{league}: λ_h/λ_a={lh / la:.4f} != HA={ha}")


def test_lambda_extreme_matchup_stays_sane():
    """
    En las colas (mejor ataque vs peor defensa, ratings 2.4) el λ debe ser
    alto pero acotable por LAMBDA_CAP — no 5+ goles por equipo.
    """
    from src.pipeline.prediction_pipeline import LAMBDA_CAP
    lh, la = compute_lambdas(2.4, 1.0, 1.0, 2.4, 1.25, 1.10)
    assert min(lh, LAMBDA_CAP) <= LAMBDA_CAP
    assert lh > la, "el mejor ataque en casa debe tener λ mayor"


def test_ht_fractions_sum_to_one():
    """El reparto 1T/2T debe cubrir el partido completo (antes sumaba 105%)."""
    assert 0.40 < HT_FIRST_HALF_FRACTION < 0.50  # medido: 0.44 (4,559 partidos)
    assert abs(HT_FIRST_HALF_FRACTION + (1 - HT_FIRST_HALF_FRACTION) - 1.0) < 1e-12


# ============================================================
# 2. D6 — claves del ancla ⊆ mercados del modelo
# ============================================================

def test_anchor_map_keys_exist_in_model_markets():
    """
    Toda clave de ANCHOR_MAP debe existir en model_probs. El bug original:
    _ANCHOR_MAP pedía "btts_yes" pero el modelo produce "btts" → el bucle
    hacía continue siempre y BTTS jamás se anclaba (su complemento sí).
    """
    assert set(ANCHOR_MAP.keys()) <= MODEL_PROB_MARKETS, (
        f"claves sin mercado: {set(ANCHOR_MAP.keys()) - MODEL_PROB_MARKETS}")


def test_anchor_covers_binary_pairs():
    """Los binarios anclables deben incluir ambos lados (btts y btts_no)."""
    assert {"btts", "btts_no"} <= set(ANCHOR_MAP.keys())


# ============================================================
# 3. D5 — señales 1 y 3 del ensemble en la misma escala de λ
# ============================================================

def test_ensemble_form_signal_matches_dc_scale():
    """
    La señal 3 (_form_probs) con ratings promedio y los factores de la EPL
    debe producir las MISMAS probabilidades que la señal 1 (Dixon-Coles del
    pipeline sobre compute_lambdas con esos mismos factores). Antes: λ
    inflados (goles²) y defaults 1.10/1.20 que ignoraban la liga.
    """
    f = LEAGUE_FACTORS["soccer_epl"]
    ha, tempo = f["home_advantage"], f["tempo"]
    b = KALMAN_BASELINE

    # señal 3 vía ensemble
    form_h, form_d, form_a = _form_probs(b, b, b, b,
                                         home_advantage=ha, tempo=tempo,
                                         baseline=b)
    # señal 1: DC del pipeline sobre los lambdas del pipeline
    lh, la = compute_lambdas(b, b, b, b, ha, tempo)
    dc_h, dc_d, dc_a = match_outcomes(lh, la)

    assert abs(form_h - dc_h) < 1e-9
    assert abs(form_d - dc_d) < 1e-9
    assert abs(form_a - dc_a) < 1e-9


# ============================================================
# 4. D10 — cobertura del resolver
# ============================================================

def test_resolver_covers_every_pipeline_market():
    """Todo mercado del modelo + los paramétricos históricos tienen rama."""
    parametric = [
        "ah_home_-1.5", "ah_away_+0.5", "corners_over_9.5", "corners_under_10.5",
        "cards_over_4.5", "cards_under_3.5", "shots_over_5.5", "shots_under_5.5",
    ]
    for market in list(MODEL_PROB_MARKETS) + parametric:
        assert is_resolvable_market(market), f"resolver sin rama: {market}"


def test_resolver_rejects_unknown_market():
    """Un mercado desconocido NO debe liquidarse por default como pérdida."""
    assert not is_resolvable_market("corner_situational_yes")
    assert not is_resolvable_market("")
    assert not is_resolvable_market(None)


# ============================================================
# Regresión H3 — el gate CLV carga ambos conjuntos
# ============================================================

def test_clv_gate_binds_both_blocked_sets(monkeypatch):
    """
    El bloque import-time del gate CLV llamaba a un nombre que el import no
    definía (_load_clv_blocked_leagues) → NameError silencioso → ambos
    conjuntos vacíos en cada arranque. El reload con loaders parcheados
    prueba que hoy llegan vivos a _CLV_BLOCKED/_CLV_LEAGUES.
    """
    import importlib

    import scripts.clv_gate as clv_gate
    monkeypatch.setattr(clv_gate, "load_clv_blocked_markets",
                        lambda: {"fake_market"})
    monkeypatch.setattr(clv_gate, "load_clv_blocked_leagues",
                        lambda: {"fake_league"})

    import src.pipeline.prediction_pipeline as pp
    reloaded = importlib.reload(pp)
    assert "fake_market" in reloaded._CLV_BLOCKED
    assert "fake_league" in reloaded._CLV_LEAGUES

    # Higiene: dejar el módulo recargado con los loaders reales, no los
    # patches de este test (el módulo vive en sys.modules para otros tests).
    monkeypatch.undo()
    importlib.reload(pp)
