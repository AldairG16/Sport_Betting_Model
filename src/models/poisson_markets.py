import numpy as np
from scipy.stats import poisson

# =========================
# MATRIX GENERATOR (CORE)
# =========================
def poisson_matrix(home_lambda, away_lambda, max_goals=10):

    home_probs = [poisson.pmf(i, home_lambda) for i in range(max_goals)]
    away_probs = [poisson.pmf(i, away_lambda) for i in range(max_goals)]

    matrix = np.outer(home_probs, away_probs)

    return matrix


# =========================
# DIXON-COLES ADJUSTED MATRIX (MEJORA #3)
# =========================
# La matriz Poisson cruda subestima 0-0, 1-0, 0-1, 1-1 (en realidad ocurren
# más en fútbol). Esto infla BTTS=YES y desinfla under 1.5/2.5. El ajuste tau
# del paper Dixon-Coles 1997 corrige los 4 scores chicos. Re-normalizamos
# después para que la matriz sume 1.0.
def _dc_tau_matrix(home_lambda, away_lambda, rho, max_goals=10):
    """Construye matriz de multiplicadores tau de DC para los 4 scores chicos."""
    tau = np.ones((max_goals, max_goals))
    tau[0, 0] = 1.0 - home_lambda * away_lambda * rho
    tau[1, 0] = 1.0 + away_lambda * rho
    tau[0, 1] = 1.0 + home_lambda * rho
    tau[1, 1] = 1.0 - rho
    return np.maximum(tau, 1e-10)


def dc_adjusted_matrix(home_lambda, away_lambda, rho=-0.13, max_goals=10):
    """
    Matriz Poisson ajustada con Dixon-Coles tau correction.
    Si rho=0 → equivale a Poisson pura.

    Default rho=-0.13 = valor empírico del paper DC97 (~ promedio fútbol europeo).
    En producción, idealmente cargar el rho fitteado desde data/dc_params.json.
    """
    matrix = poisson_matrix(home_lambda, away_lambda, max_goals)
    tau = _dc_tau_matrix(home_lambda, away_lambda, rho, max_goals)
    matrix = matrix * tau
    # Re-normalización (tau cambia la masa total)
    total = matrix.sum()
    if total > 0:
        matrix = matrix / total
    return matrix


# =========================
# TOTALS + BTTS (FAST)
# =========================
def totals_and_btts(home_lambda, away_lambda, max_goals=10, rho=None):
    """
    Calcula over25/under25/btts.

    MEJORA #3 (06-may-26):
    Si `rho` se provee, usa matriz Dixon-Coles ajustada (corrige los 4
    scores chicos: 0-0, 1-0, 0-1, 1-1) y calcula BTTS por suma directa
    sobre la matriz, NO con la fórmula cerrada Poisson cruda.

    Sin rho → usa Poisson cruda (compatible con código legacy).
    """
    if rho is not None:
        matrix = dc_adjusted_matrix(home_lambda, away_lambda, rho, max_goals)
    else:
        matrix = poisson_matrix(home_lambda, away_lambda, max_goals)

    i, j = np.indices(matrix.shape)
    total_goals = i + j

    # =====================
    # OVER 2.5 (siempre desde matriz)
    # =====================
    over25  = matrix[total_goals > 2].sum()
    under25 = matrix[total_goals <= 2].sum()

    # =====================
    # BTTS — suma directa sobre la matriz (DC-aware si rho dado)
    # =====================
    if rho is not None:
        # BTTS=YES son todos los scores donde i>=1 AND j>=1
        btts_yes = matrix[(i >= 1) & (j >= 1)].sum()
        btts_no  = 1 - btts_yes
    else:
        # Fórmula cerrada Poisson cruda (rápida) — para back-compat
        p_home_zero = np.exp(-home_lambda)
        p_away_zero = np.exp(-away_lambda)
        p_zero_zero = np.exp(-(home_lambda + away_lambda))
        btts_yes = 1 - p_home_zero - p_away_zero + p_zero_zero
        btts_no  = 1 - btts_yes

    return {
        "over25":   float(over25),
        "under25":  float(under25),
        "btts_yes": float(btts_yes),
        "btts_no":  float(btts_no),
    }


# =========================
# EXTENDED TOTALS
# =========================
def totals_extended(home_lambda, away_lambda, max_goals=10):

    matrix = poisson_matrix(home_lambda, away_lambda, max_goals)

    i, j = np.indices(matrix.shape)
    total_goals = i + j

    probs = {}

    lines = [1.5, 2.5, 3.5]

    for line in lines:

        over = matrix[total_goals > line].sum()
        under = matrix[total_goals <= line].sum()

        probs[f"over_{line}"] = over
        probs[f"under_{line}"] = under

    return probs


# =========================
# 1X2 (BONUS PRO)
# =========================
def match_result_probs(home_lambda, away_lambda, max_goals=10):

    matrix = poisson_matrix(home_lambda, away_lambda, max_goals)

    i, j = np.indices(matrix.shape)

    home_win = matrix[i > j].sum()
    draw = matrix[i == j].sum()
    away_win = matrix[i < j].sum()

    return {
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win
    }