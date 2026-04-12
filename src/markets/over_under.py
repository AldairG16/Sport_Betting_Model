def compute_over_under(matrix):

    over25 = 0

    for i in range(len(matrix)):
        for j in range(len(matrix)):

            if i + j > 2:
                over25 += matrix[i][j]

    return over25, 1 - over25