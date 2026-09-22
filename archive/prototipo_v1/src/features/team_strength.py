import pandas as pd
import numpy as np
from config.database import engine
from src.utils.team_normalizer import normalize_team
from sqlalchemy import text


def safe_mean(values):
    if values is None or len(values) == 0:
        return None

    arr = np.array(values, dtype=float)
    arr = arr[~np.isnan(arr)]

    if len(arr) == 0:
        return None

    return float(arr.mean())


def get_team_strength(team):

    # =========================
    # NORMALIZACIÓN GLOBAL
    # =========================
    team = normalize_team(team).lower().strip()

    # =========================
    # QUERY OPTIMIZADA
    # =========================
    df = pd.read_sql(text("""
        SELECT *
        FROM matches
        WHERE home_team IS NOT NULL
        ORDER BY date DESC
        LIMIT 500
    """), engine)

    if df.empty:
        return {"attack": 1.0, "defense": 1.0}

    goals_for = []
    goals_against = []
    shots = []
    shots_target = []

    # =========================
    # LOOP MATCHES
    # =========================
    for _, row in df.iterrows():

        home = normalize_team(row.home_team).lower().strip()
        away = normalize_team(row.away_team).lower().strip()

        if team not in [home, away]:
            continue

        if home == team:
            gf = row.home_goals
            ga = row.away_goals
            s = row.home_shots
            st = row.home_shots_target
        else:
            gf = row.away_goals
            ga = row.home_goals
            s = row.away_shots
            st = row.away_shots_target

        # =========================
        # VALIDACIÓN
        # =========================
        if pd.isna(gf) or pd.isna(ga):
            continue

        goals_for.append(gf)
        goals_against.append(ga)

        if not pd.isna(s):
            shots.append(s)

        if not pd.isna(st):
            shots_target.append(st)

        if len(goals_for) >= 20:
            break

    # =========================
    # FALLBACKS
    # =========================
    if len(goals_for) < 3:
        return {"attack": 1.05, "defense": 1.05}

    # =========================
    # MÉTRICAS BASE
    # =========================
    gpg = np.mean(goals_for)
    gcpg = np.mean(goals_against)

    spg = safe_mean(shots) or 10
    stpg = safe_mean(shots_target) or 3

    # =========================
    # NORMALIZACIÓN PRO
    # =========================
    attack = (
        (gpg / 1.4) * 0.6 +
        (stpg / 4) * 0.3 +
        (spg / 12) * 0.1
    )

    defense = (gcpg / 1.4)

    # =========================
    # CLAMP (ESTABILIDAD)
    # =========================
    attack = float(np.clip(attack, 0.75, 1.6))
    defense = float(np.clip(defense, 0.75, 1.6))

    return {
        "attack": attack,
        "defense": defense
    }