def market_intelligence_filter(bets):

    filtered = []

    for bet in bets:

        market = bet["market"]
        # `edge_market` = prob - implied (edge real). `edge` (= edge_ev) infla
        # con odds altas. Filtros se aplican sobre el verdadero edge.
        edge = bet.get("edge_market", bet["edge"])
        odds = bet["odds"]
        prob = bet.get("probability", 0.5)

        # =========================
        # 1. FILTRO BASE DINÁMICO
        # =========================

        if odds is None or odds < 1.3 or odds > 10:
            continue

        # 🔥 EDGE DINÁMICO (clave)
        min_edge = 0.035

        if prob < 0.45:
            min_edge += 0.015  # más estrictos con picks débiles

        if odds > 4:
            min_edge += 0.01  # longshots requieren más edge

        if edge < min_edge:
            continue

        # =========================
        # 2. REGLAS POR MERCADO
        # =========================

        if market in ["home_win", "away_win"]:
            if edge < 0.05:
                continue

        elif market == "draw":
            if edge < 0.08:
                continue

        elif market in ["over25", "under25"]:
            if edge < 0.045:
                continue

        elif market == "btts":
            if edge < 0.05 or prob < 0.5:
                continue

        elif market == "btts_no":
            if edge < 0.045:
                continue

        elif "over_" in market or "under_" in market:

            # 🔥 eliminar mercados débiles
            if market == "over_1.5":
                continue

            if edge < 0.06:
                continue

        else:
            continue

        # =========================
        # 3. ANTI-TRAP (CLAVE PRO)
        # =========================

        # evitar edges fake en odds muy altas
        if odds > 7 and prob < 0.35:
            continue

        filtered.append(bet)

    return filtered


def add_market_score(bets):

    for bet in bets:

        prob = bet.get("probability", 0.5)
        edge = bet.get("edge_market", bet["edge"])  # edge real, no EV inflado
        odds = bet["odds"]
        market = bet["market"]

        # =========================
        # 1. BASE SCORE
        # =========================

        score = edge * prob

        # =========================
        # 2. EDGE QUALITY BOOST
        # =========================

        if edge > 0.10:
            score *= 1.25
        elif edge > 0.07:
            score *= 1.15

        # =========================
        # 3. MARKET WEIGHTS
        # =========================

        if market in ["home_win", "away_win"]:
            score *= 1.2

        elif market == "draw":
            score *= 0.9  # draw es más volátil

        elif market == "under25":
            score *= 1.15

        elif market == "btts_no":
            score *= 1.1

        elif "over_" in market:
            score *= 0.95  # overs más inflados por público

        # =========================
        # 4. ODDS RISK ADJUSTMENT
        # =========================

        if odds > 6:
            score *= 0.85
        elif odds > 4:
            score *= 0.92
        elif odds < 1.6:
            score *= 0.95  # evitar favoritos sobrevalorados

        # =========================
        # 5. PROBABILITY CONFIDENCE
        # =========================

        if prob > 0.65:
            score *= 1.1
        elif prob < 0.45:
            score *= 0.9

        bet["score"] = score

    return bets