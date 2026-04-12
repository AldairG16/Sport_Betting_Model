import pandas as pd

# Mercados agrupados por tipo — para anti-stacking
MARKET_GROUPS = {
    "home_win":   "1x2",
    "draw":       "1x2",
    "away_win":   "1x2",
    "over25":     "totals",
    "under25":    "totals",
    "over15":     "totals",
    "under15":    "totals",
    "over35":     "totals",
    "under35":    "totals",
    "btts":       "btts",
    "btts_no":    "btts",
}

MAX_BETS_PER_MATCH        = 2    # Máximo 2 apuestas por partido
MAX_SAME_GROUP_PER_MATCH  = 1    # Máximo 1 apuesta por grupo (1x2 o totals)


def rank_bets(bets: list) -> pd.DataFrame:
    """
    Rankea apuestas por score y aplica deduplicación por partido.

    Reglas de deduplicación:
      1. Solo 1 apuesta por grupo de mercado por partido
         (no puede haber home_win + draw del mismo partido)
      2. Máximo MAX_BETS_PER_MATCH apuestas en total por partido
      3. Si hay 2 apuestas de distinto grupo (ej. home_win + over25),
         se permite pero se penaliza el stake del segundo con -20%

    Returns:
        DataFrame rankeado y deduplicado
    """
    if len(bets) == 0:
        return pd.DataFrame()

    df = pd.DataFrame(bets)

    # ============================
    # FILTROS DE CALIDAD
    # ============================

    df = df[df["edge"] > 0.03]
    df = df[df["odds"] > 1.5]
    df = df[df["odds"] < 6]

    if df.empty:
        return df

    # ============================
    # SCORE DE APUESTA
    # ============================

    df["score"] = (
        df["edge"] * 0.6 +
        df["probability"] * 0.3 +
        (df["odds"] / 10) * 0.1
    )

    df["market_group"] = df["market"].map(MARKET_GROUPS).fillna("other")

    # ============================
    # RANKING GLOBAL
    # ============================

    df = df.sort_values("score", ascending=False).reset_index(drop=True)

    # ============================
    # DEDUPLICACIÓN POR PARTIDO
    # ============================
    # Para cada partido: toma la mejor bet de cada grupo de mercado,
    # pero no más de MAX_BETS_PER_MATCH en total.

    selected = []

    for match, group in df.groupby("match", sort=False):

        match_bets    = []
        groups_used   = set()
        second_bet    = False

        for _, row in group.iterrows():

            g = row["market_group"]

            # Primer bet del partido — siempre se acepta (mejor score)
            if len(match_bets) == 0:
                match_bets.append(row)
                groups_used.add(g)
                continue

            # Segundo bet — solo si es grupo distinto Y no pasamos el máximo
            if (len(match_bets) < MAX_BETS_PER_MATCH
                    and g not in groups_used):
                row = row.copy()
                row["stake"] = row["stake"] * 0.80   # penalización por correlación
                match_bets.append(row)
                groups_used.add(g)
                second_bet = True
                continue

            # Tercer bet o mismo grupo → rechazado
            break

        selected.extend(match_bets)

    result = pd.DataFrame(selected)

    if result.empty:
        return result

    return result.sort_values("score", ascending=False).reset_index(drop=True)