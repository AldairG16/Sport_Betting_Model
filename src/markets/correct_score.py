def best_scores(matrix):

    scores = []

    for i in range(len(matrix)):
        for j in range(len(matrix)):

            scores.append((i, j, matrix[i][j]))

    scores.sort(key=lambda x: x[2], reverse=True)

    return scores[:5]