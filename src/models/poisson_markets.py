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
# TOTALS + BTTS (FAST)
# =========================
def totals_and_btts(home_lambda, away_lambda, max_goals=10):

    matrix = poisson_matrix(home_lambda, away_lambda, max_goals)

    # Índices
    i, j = np.indices(matrix.shape)

    total_goals = i + j

    # =====================
    # OVER 2.5
    # =====================
    over25 = matrix[total_goals > 2].sum()
    under25 = matrix[total_goals <= 2].sum()

    # =====================
    # BTTS (FORMA PRO)
    # =====================
    p_home_zero = np.exp(-home_lambda)
    p_away_zero = np.exp(-away_lambda)
    p_zero_zero = np.exp(-(home_lambda + away_lambda))

    btts_yes = 1 - p_home_zero - p_away_zero + p_zero_zero
    btts_no = 1 - btts_yes

    return {
        "over25": over25,
        "under25": under25,
        "btts_yes": btts_yes,
        "btts_no": btts_no
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