import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import numpy as np
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team


def _ensure_cards_schema():
    """Agrega columnas de tarjetas a matches si no existen."""
    with engine.begin() as conn:
        for col in ["home_yellow", "away_yellow", "home_red", "away_red"]:
            conn.execute(text(f"ALTER TABLE matches ADD COLUMN IF NOT EXISTS {col} INT"))


leagues = {
    # Big 5 originales
    "E0":  "soccer_epl",
    "D1":  "soccer_germany_bundesliga",
    "I1":  "soccer_italy_serie_a",
    "SP1": "soccer_spain_la_liga",
    "F1":  "soccer_france_ligue_one",
    "ECL": "soccer_uefa_champs_league",

    # Nuevas ligas europeas
    "E1":  "soccer_efl_champ",               # Championship
    "N1":  "soccer_netherlands_eredivisie",   # Eredivisie
    "P1":  "soccer_portugal_primeira_liga",   # Primeira Liga
    "SC0": "soccer_spl",                      # Scottish Premiership

    # Tier 3: mercados blandos (nuevas)
    "T1":  "soccer_turkey_super_league",      # Turkey Super Lig
    "B1":  "soccer_belgium_first_div",        # Belgian First Division
    "G1":  "soccer_greece_super_league",      # Greek Super League
}

seasons = [
    "1516","1617","1718","1819","1920",
    "2021","2122","2223","2324","2425","2526"
]


# =========================
# HEDGE FUND IMPUTATION
# =========================

def advanced_impute(df):

    stats_cols = [
        "home_shots","away_shots",
        "home_shots_target","away_shots_target",
        "home_corners","away_corners"
    ]

    # crear columnas si no existen
    for col in stats_cols:
        if col not in df.columns:
            df[col] = np.nan

    # =========================
    # RECENCY WEIGHT
    # =========================

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date", ascending=False)

    df["recency_weight"] = np.exp(-np.arange(len(df)) / 50)

    # =========================
    # BASELINE POR LIGA
    # =========================

    league_avg = {}

    for col in stats_cols:
        valid = df[col].dropna()
        if len(valid) > 0:
            league_avg[col] = np.average(valid, weights=df.loc[valid.index, "recency_weight"])
        else:
            league_avg[col] = None

    # =========================
    # GLOBAL FALLBACK
    # =========================

    global_defaults = {
        "home_shots": 12,
        "away_shots": 10,
        "home_shots_target": 4,
        "away_shots_target": 3,
        "home_corners": 5,
        "away_corners": 4
    }

    # =========================
    # IMPUTACIÓN
    # =========================

    for col in stats_cols:

        if league_avg[col] is not None:
            df[col] = df[col].fillna(league_avg[col])
        else:
            df[col] = df[col].fillna(global_defaults[col])

    # =========================
    # GOAL TEMPO ADJUSTMENT
    # =========================

    df["tempo"] = (df["home_goals"] + df["away_goals"]) / 2.5

    for col in stats_cols:
        df[col] = df[col] * (0.8 + 0.4 * df["tempo"])

    df.drop(columns=["tempo", "recency_weight"], inplace=True)

    return df


# =========================
# MAIN
# =========================

def load_historical_data():
    _ensure_cards_schema()
    for code, league in leagues.items():
        for s in seasons:
            print("Downloading", league, s)
            url = f"https://www.football-data.co.uk/mmz4281/{s}/{code}.csv"
            try:
                df = pd.read_csv(url)
            except (Exception,) as _dl_err:
                print(f"season not available: {_dl_err}")
                continue

            df = df.rename(columns={
                "Date":"date",
                "HomeTeam":"home_team",
                "AwayTeam":"away_team",
                "FTHG":"home_goals",
                "FTAG":"away_goals",
                "HS":"home_shots",
                "AS":"away_shots",
                "HST":"home_shots_target",
                "AST":"away_shots_target",
                "HC":"home_corners",
                "AC":"away_corners",
                "HY":"home_yellow",
                "AY":"away_yellow",
                "HR":"home_red",
                "AR":"away_red",
            })

            df["league"] = league
            df["season"] = int(s[:2]) + 2000
            df["date"] = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
            df = df.dropna(subset=["date"])
            df["home_team"] = df["home_team"].apply(normalize_team)
            df["away_team"] = df["away_team"].apply(normalize_team)
            df = advanced_impute(df)

            # Agregar columnas de tarjetas si existen en el CSV (opcional)
            for col in ["home_yellow", "away_yellow", "home_red", "away_red"]:
                if col not in df.columns:
                    df[col] = None

            df = df[[
                "date","league","season",
                "home_team","away_team",
                "home_goals","away_goals",
                "home_shots","away_shots",
                "home_shots_target","away_shots_target",
                "home_corners","away_corners",
                "home_yellow","away_yellow",
                "home_red","away_red",
            ]]

            # Insertar con ON CONFLICT DO NOTHING para evitar duplicados
            from sqlalchemy import text as _text
            inserted = 0
            with engine.begin() as conn:
                for _, row in df.iterrows():
                    try:
                        conn.execute(_text("""
                            INSERT INTO matches (
                                date, league, season, home_team, away_team,
                                home_goals, away_goals, home_shots, away_shots,
                                home_shots_target, away_shots_target,
                                home_corners, away_corners,
                                home_yellow, away_yellow, home_red, away_red
                            ) VALUES (
                                :date, :league, :season, :home_team, :away_team,
                                :home_goals, :away_goals, :home_shots, :away_shots,
                                :home_shots_target, :away_shots_target,
                                :home_corners, :away_corners,
                                :home_yellow, :away_yellow, :home_red, :away_red
                            )
                            ON CONFLICT (date, home_team, away_team) DO NOTHING
                        """), row.to_dict())
                        inserted += 1
                    except Exception:
                        pass
            print(f"✅ Procesados: {len(df)} ({inserted} nuevos)")

    print("🔥 HISTORICAL READY")


if __name__ == "__main__":
    load_historical_data()

print("🔥 HISTORICAL READY")