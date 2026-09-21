"""
tests/test_round14_audit.py
===========================
Tests de la Ronda 14 (hallazgos de la auditoría final externa):
I1 — un solo rho para la matriz de marcadores (coherencia 1x2/AH/DNB);
I2 — el signo de la motivación en la defensa.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.asian_handicap_model import prob_ah
from src.models.dixon_coles_model import match_outcomes
from src.pipeline.prediction_pipeline import compute_lambdas
from src.features.league_calibration import LEAGUE_FACTORS
from src.features.team_form import KALMAN_BASELINE


# ============================================================
# I1 — AH -0.5 local == victoria local, con UNA matriz
# ============================================================

def test_ah_half_ball_matches_1x2_with_same_rho():
    """
    AH -0.5 local y 'gana el local' son EL MISMO suceso: con el mismo rho
    deben dar la misma probabilidad (antes: 1x2 con rho=-0.13 y AH con
    Poisson pura → 1.6pp de diferencia que sesgaba la selección).
    """
    lh, la = 1.45, 1.15
    for rho in (-0.13, -0.0947, 0.0):
        p_home, _, _ = match_outcomes(lh, la, rho=rho)
        p_cover, _ = prob_ah(lh, la, -0.5, rho=rho)
        # 1e-4: tolera la truncacion de grilla (dixon usa 10 goles, AH 15);
        # el bug I1 era 1.6pp — cuatro ordenes de magnitud mas arriba
        assert abs(p_home - p_cover) < 1e-4, (
            f"rho={rho}: 1x2={p_home:.4f} vs AH-0.5={p_cover:.4f}")


def test_match_outcomes_default_rho_unchanged():
    """Compatibilidad: sin rho explícito se usa el de la literatura."""
    lh, la = 1.45, 1.15
    default = match_outcomes(lh, la)
    explicit = match_outcomes(lh, la, rho=-0.13)
    assert all(abs(a - b) < 1e-12 for a, b in zip(default, explicit))


def test_tau_still_matters_for_low_scores():
    """El rho hace algo medible: con tau activa cambia P(empate 1-1 de
    baja puntuación) frente a Poisson pura."""
    with_tau = match_outcomes(1.2, 1.1, rho=-0.13)
    without = match_outcomes(1.2, 1.1, rho=0.0)
    assert abs(with_tau[1] - without[1]) > 1e-4  # el empate es lo sensible


# ============================================================
# I2 — la motivación empuja attack arriba y defense abajo
# ============================================================

def test_motivation_routes_to_win_prob_not_goals():
    """
    Un local motivado (+8%) debe marcar más y CONCEDER menos: λ_home sube
    y λ_away baja. El código viejo subía ambos (80% del efecto a goles).
    """
    f = LEAGUE_FACTORS["soccer_epl"]
    ha, tempo = f["home_advantage"], f["tempo"]
    b = KALMAN_BASELINE
    base = compute_lambdas(1.45, 1.25, 1.15, 1.35, ha, tempo, baseline=b)
    motiv = compute_lambdas(1.45 * 1.08, 1.25 / 1.08,
                            1.15, 1.35, ha, tempo, baseline=b)
    assert motiv[0] > base[0], "λ_home debe subir con la motivación"
    assert motiv[1] < base[1], "λ_away debe BAJAR (motivado concede menos)"


def test_motivation_mult_sign_in_source():
    """El arreglo I2 en el pipeline: defensa dividida, ataque multiplicada."""
    import inspect
    src = inspect.getsource(__import__(
        "src.pipeline.prediction_pipeline", fromlist=["x"]))
    assert "home_defense /= (1 + home_motiv)" in src
    assert "away_defense /= (1 + away_motiv)" in src
    assert "home_defense *= (1 + home_motiv)" not in src
