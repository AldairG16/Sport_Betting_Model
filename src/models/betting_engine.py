import pandas as pd


# =========================
# EDGE CALCULATION
# =========================

def calculate_edges(prob, odds):

    if odds is None or odds <= 1:
        return None, None

    implied = 1 / odds

    edge_market = prob - implied
    edge_ev = (prob * odds) - 1

    # 🔥 CAP EDGE (evita locuras)
    edge_market = max(min(edge_market, 0.25), -0.25)
    edge_ev = max(min(edge_ev, 0.50), -0.50)

    return edge_market, edge_ev


# =========================
# VALUE BETS (PRO)
# =========================

def find_value_bets(probabilities, odds):

    bets = []

    for market, prob in probabilities.items():

        odd = odds.get(market)

        if odd is None or pd.isna(odd) or odd <= 1:
            continue

        # 🔥 FILTRO PROBABILIDAD (reduce ruido)
        if prob < 0.05 or prob > 0.95:
            continue

        edge_market, edge_ev = calculate_edges(prob, odd)

        if edge_market is None:
            continue

        # =========================
        # FILTROS PRO
        # =========================

        # 🔥 mínimo edge real
        if edge_market < 0.02:
            continue

        # 🔥 EV mínimo
        if edge_ev < 0:
            continue

        # 🔥 penalización odds altas
        odds_penalty = 1.0
        if odd > 5:
            odds_penalty = 0.85
        if odd > 8:
            odds_penalty = 0.7

        # 🔥 score combinado (CLAVE)
        score = edge_ev * prob * odds_penalty

        bets.append({
            "market": market,
            "probability": prob,
            "odds": odd,
            "edge": edge_ev,
            "edge_market": edge_market,
            "score": score
        })

    # 🔥 ordenar por score, no solo edge
    bets = sorted(bets, key=lambda x: x["score"], reverse=True)

    return bets


# =========================
# KELLY (PRO)
# =========================

def kelly_stake(prob, odds, bankroll=100, kelly_fraction=0.25, max_bet_pct=0.02):

    """
    Kelly PRO (más conservador)

    mejoras:
    - penaliza odds altas
    - reduce varianza
    - evita sobre-apostar
    """

    if prob is None or odds is None or odds <= 1:
        return 0

    b = odds - 1
    q = 1 - prob

    kelly = (prob * b - q) / b

    if kelly <= 0:
        return 0

    # 🔥 penalización odds altas
    if odds > 5:
        kelly *= 0.8
    if odds > 8:
        kelly *= 0.6

    # 🔥 Kelly fraccional
    kelly *= kelly_fraction

    # 🔥 cap riesgo
    kelly = min(kelly, max_bet_pct)

    stake = bankroll * kelly

    return round(stake, 2)