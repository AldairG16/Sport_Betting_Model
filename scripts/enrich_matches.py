import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Fix encoding para consola Windows (cp1252 no soporta emojis)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import numpy as np
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team


# =========================
# CACHE
# =========================
team_cache = {}


# =========================
# HELPERS
# =========================

def safe_avg(values):
    if values is None or len(values) == 0:
        return None

    arr = np.array(values, dtype=float)
    arr = arr[~np.isnan(arr)]

    if len(arr) == 0:
        return None

    return float(arr.mean())


def clean_int(value):
    """
    🔥 FIX CRÍTICO: convierte NaN -> None y float -> int seguro
    """
    if value is None:
        return None

    if isinstance(value, float) and np.isnan(value):
        return None

    try:
        return int(round(value))
    except (TypeError, ValueError, OverflowError):
        return None


# =========================
# TEAM STATS
# =========================

def get_team_stats(team):

    team = normalize_team(team).lower().strip()

    if team in team_cache:
        return team_cache[team]

    df = pd.read_sql(text("""
        SELECT 
            home_team, away_team,
            home_shots, away_shots,
            home_shots_target, away_shots_target,
            home_corners, away_corners,
            date
        FROM matches
        ORDER BY date DESC
        LIMIT 300
    """), engine)

    if df.empty:
        return None

    shots = []
    shots_target = []
    corners = []

    for _, row in df.iterrows():

        home_db = normalize_team(row.home_team).lower().strip()
        away_db = normalize_team(row.away_team).lower().strip()

        if team not in [home_db, away_db]:
            continue

        if home_db == team:
            shots.append(row.home_shots)
            shots_target.append(row.home_shots_target)
            corners.append(row.home_corners)
        else:
            shots.append(row.away_shots)
            shots_target.append(row.away_shots_target)
            corners.append(row.away_corners)

        if len(shots) >= 20:
            break

    result = {
        "shots": safe_avg(shots),
        "shots_target": safe_avg(shots_target),
        "corners": safe_avg(corners)
    }

    team_cache[team] = result

    return result


# =========================
# MAIN
# =========================

def enrich_matches():

    print("\n📡 ENRICHING UPCOMING MATCHES...\n")

    df = pd.read_sql("SELECT * FROM upcoming_matches", engine)

    if df.empty:
        print("⚠️ No matches")
        return

    print(f"📊 Matches: {len(df)}")

    with engine.begin() as conn:

        for _, row in df.iterrows():

            try:
                home = row["home_team"]
                away = row["away_team"]

                home_stats = get_team_stats(home)
                away_stats = get_team_stats(away)

                if not home_stats or not away_stats:
                    continue

                params = {
                    "match_key": row["match_key"],

                    "home_shots": clean_int(home_stats["shots"]),
                    "away_shots": clean_int(away_stats["shots"]),

                    "home_shots_target": clean_int(home_stats["shots_target"]),
                    "away_shots_target": clean_int(away_stats["shots_target"]),

                    "home_corners": clean_int(home_stats["corners"]),
                    "away_corners": clean_int(away_stats["corners"]),
                }

                # 🔥 SKIP si todo es None
                if all(
                    params[k] is None
                    for k in [
                        "home_shots", "away_shots",
                        "home_shots_target", "away_shots_target",
                        "home_corners", "away_corners"
                    ]
                ):
                    continue

                conn.execute(text("""
                    UPDATE upcoming_matches
                    SET 
                        home_shots = COALESCE(:home_shots, home_shots),
                        away_shots = COALESCE(:away_shots, away_shots),
                        home_shots_target = COALESCE(:home_shots_target, home_shots_target),
                        away_shots_target = COALESCE(:away_shots_target, away_shots_target),
                        home_corners = COALESCE(:home_corners, home_corners),
                        away_corners = COALESCE(:away_corners, away_corners)
                    WHERE match_key = :match_key
                """), params)

            except Exception as e:
                print("❌ Error:", e)

    print("💾 DONE")


if __name__ == "__main__":
    enrich_matches()