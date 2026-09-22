import numpy as np
from scipy.stats import poisson


def build_score_matrix(home_lambda, away_lambda, max_goals=7):

    matrix = np.zeros((max_goals, max_goals))

    for h in range(max_goals):
        for a in range(max_goals):

            matrix[h][a] = (
                poisson.pmf(h, home_lambda)
                * poisson.pmf(a, away_lambda)
            )

    return matrix