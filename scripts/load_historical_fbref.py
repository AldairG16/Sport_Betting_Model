import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team


# =========================
# LIGAS EXTRA (NO EUROPA)
# =========================

LEAGUES = {
    "BRAZIL": {
        "league": "soccer_brazil_campeonato",
        "url": "https://www.football-data.co.uk/new/BRA.csv"
    },
    "ARGENTINA": {
        "league": "soccer_argentina_primera_division",
        "url": "https://www.football-data.co.uk/new/ARG.csv"
    },
    "MLS": {
        "league": "soccer_usa_mls",
        "url": "https://www.football-data.co.uk/new/MLS.csv"
    },
    "MEXICO": {
        "league": "soccer_mexico_ligamx",
        "url": "https://www.football-data.co.uk/new/MEX.csv"
    }
}


# =========================
# MAIN
# =========================

def load_extra():

    print("\n📡 LOADING EXTRA LEAGUES (STABLE DATA)\n")

    for name, data in LEAGUES.items():

        print(f"⬇️ {name}")

        try:
            df = pd.read_csv(data["url"])
        except Exception as _dl_err:
            print(f"❌ Failed download: {_dl_err}")
            continue

        df = df.rename(columns={
            "Date":"date",
            "Home":"home_team",
            "Away":"away_team",
            "HG":"home_goals",
            "AG":"away_goals",
            "HS":"home_shots",
            "AS":"away_shots",
            "HST":"home_shots_target",
            "AST":"away_shots_target",
            "HC":"home_corners",
            "AC":"away_corners"
        })

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])

        df["league"] = data["league"]
        df["season"] = df["date"].dt.year

        df["home_team"] = df["home_team"].apply(normalize_team)
        df["away_team"] = df["away_team"].apply(normalize_team)

        # =========================
        # IMPUTACIÓN SI FALTA
        # =========================

        for col in [
            "home_shots","away_shots",
            "home_shots_target","away_shots_target",
            "home_corners","away_corners"
        ]:
            if col not in df.columns:
                df[col] = 0

        df = df.fillna(0)

        df = df[[
            "date","league","season",
            "home_team","away_team",
            "home_goals","away_goals",
            "home_shots","away_shots",
            "home_shots_target","away_shots_target",
            "home_corners","away_corners"
        ]]

        df.to_sql("matches", engine, if_exists="append", index=False, method="multi")

        print(f"✅ Insertados: {len(df)}")

    print("\n🔥 EXTRA LEAGUES DONE\n")


if __name__ == "__main__":
    load_extra()