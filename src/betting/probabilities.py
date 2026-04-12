def market_probabilities(matrix):

    home = 0
    draw = 0
    away = 0

    over25 = 0
    btts = 0

    for i in range(matrix.shape[0]):

        for j in range(matrix.shape[1]):

            p = matrix[i,j]

            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p

            if i + j > 2:
                over25 += p

            if i > 0 and j > 0:
                btts += p

    return {

        "home_win": home,
        "draw": draw,
        "away_win": away,

        "over25": over25,
        "btts": btts
    }