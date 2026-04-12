import pandas as pd
from config.database import engine


def compute_elo():

    df = pd.read_sql("""
        SELECT home_team, away_team, home_goals, away_goals, date
        FROM matches
        WHERE (league IS NULL OR league LIKE 'soccer%%' OR league LIKE 'fifa%%' OR league LIKE 'uefa%%')
        ORDER BY date
    """, engine)

    elo = {}

    # 🔥 CONFIGURACIÓN PRO
    BASE_ELO = 1500
    K_BASE = 20
    HOME_ADVANTAGE_ELO = 80   # ventaja local en puntos ELO
    GOAL_DIFF_MULT = 0.25     # impacto diferencia de goles
    DECAY = 0.999             # recency (más peso a partidos recientes)

    for _, row in df.iterrows():

        home = row.home_team
        away = row.away_team

        # inicializar
        elo.setdefault(home, BASE_ELO)
        elo.setdefault(away, BASE_ELO)

        # =========================
        # EXPECTED SCORE (con home advantage)
        # =========================

        home_elo_adj = elo[home] + HOME_ADVANTAGE_ELO
        away_elo_adj = elo[away]

        home_exp = 1 / (1 + 10 ** ((away_elo_adj - home_elo_adj) / 400))
        away_exp = 1 - home_exp

        # =========================
        # RESULTADO REAL
        # =========================

        if row.home_goals > row.away_goals:
            home_score = 1
            away_score = 0
        elif row.home_goals < row.away_goals:
            home_score = 0
            away_score = 1
        else:
            home_score = 0.5
            away_score = 0.5

        # =========================
        # 🔥 MARGIN OF VICTORY
        # =========================

        goal_diff = abs(row.home_goals - row.away_goals)

        if goal_diff <= 1:
            mult = 1
        else:
            mult = 1 + (goal_diff - 1) * GOAL_DIFF_MULT

        # =========================
        # 🔥 DYNAMIC K FACTOR
        # =========================

        K = K_BASE * mult

        # =========================
        # UPDATE
        # =========================

        elo[home] += K * (home_score - home_exp)
        elo[away] += K * (away_score - away_exp)

        # =========================
        # 🔥 DECAY (recency weighting)
        # =========================

        elo[home] *= DECAY
        elo[away] *= DECAY

    return elo