
def best_bet(probabilities, odds):

    bets = []

    for market in probabilities:

        prob = probabilities[market]
        odd = odds.get(market)

        if odd is None:
            continue

        implied = 1 / odd

        edge = prob - implied

        bets.append({
            "market": market,
            "probability": prob,
            "odds": odd,
            "edge": edge
        })

    if len(bets) == 0:
        return None

    bets = sorted(bets, key=lambda x: x["edge"], reverse=True)

    return bets[0]