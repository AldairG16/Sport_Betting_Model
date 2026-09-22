import numpy as np


def compute_1x2(matrix):

    home = np.tril(matrix, -1).sum()
    draw = np.trace(matrix)
    away = np.triu(matrix, 1).sum()

    return home, draw, away