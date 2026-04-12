def compute_btts(matrix):

    prob = 0

    for i in range(1, len(matrix)):
        for j in range(1, len(matrix)):

            prob += matrix[i][j]

    return prob, 1 - prob