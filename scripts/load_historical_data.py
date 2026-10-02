import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json

import pandas as pd
import numpy as np
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team
from src.utils.db_batch import insert_ignore_conflicts, describe
from src.utils.football_data import (SEASON_LEAGUES, SEASON_URL, check_division,
                                     fetch_csv, is_missing)
from src.utils.log import get_logger

log = get_logger(__name__)


def _ensure_cards_schema():
    """Agrega columnas de tarjetas a matches si no existen."""
    with engine.begin() as conn:
        for col in ["home_yellow", "away_yellow", "home_red", "away_red"]:
            conn.execute(text(f"ALTER TABLE matches ADD COLUMN IF NOT EXISTS {col} INT"))


# football-data NO tiene Champions League: hasta el 1-oct-26 aquí había
# "ECL": "soccer_uefa_champs_league", el servidor redirigía ECL.csv a EC.csv y
# la National League inglesa (5ª división) se guardaba como Champions desde
# 2015 (src/utils/football_data.py).
leagues = SEASON_LEAGUES

def season_codes(today=None, first_start: int = 2015) -> list[str]:
    """Códigos de football-data ('1516' … temporada en curso). La temporada
    europea arranca en julio/agosto; si el CSV nuevo todavía no existe, la
    descarga da 404 y se salta. Hasta el 28-sep-26 la lista era fija y
    terminaba en '2526': desde agosto el weekly ya no cargaba la temporada
    2026/27 y solo la cubría el respaldo diario de los últimos 10 días."""
    from datetime import date
    today = today or date.today()
    last_start = today.year if today.month >= 7 else today.year - 1
    return [f"{y % 100:02d}{(y + 1) % 100:02d}" for y in range(first_start, last_start + 1)]


seasons = season_codes()


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

    # R14-op (21-sep): este paso colgaba el weekly de CI — 143 descargas sin
    # timeout + inserts fila por fila a Neon (~58k viajes). Arreglo:
    #   1. (liga, temporada) ya cargada en DB → SKIP (no re-descargar).
    #   2. descarga con requests + timeout=30.
    #   3. inserts por LOTES (executemany), no fila por fila.
    with engine.connect() as conn:
        _rows = conn.execute(text(
            "SELECT league, season, COUNT(*) FROM matches GROUP BY league, season"
        )).fetchall()
    loaded_counts = {(lg, int(sn)): int(c) for lg, sn, c in _rows if sn is not None}
    failed = []   # descargas que fallaron (no "todavía no existe")

    for code, league in leagues.items():
        for s in seasons:
            season_year = int(s[:2]) + 2000
            existing = loaded_counts.get((league, season_year), 0)

            print(f"Downloading {league} {s} (en DB: {existing})")
            df, why = fetch_csv(SEASON_URL.format(season=s, code=code))
            if df is None:
                print(f"season not available: {why}")
                if not is_missing(why):
                    failed.append(f"{code} {s}: {why}")
                continue
            problem = check_division(df, code)
            if problem:
                log.error(f"❌ Histórico {league} {s}: {problem} — no se carga")
                continue

            # liga+temporada completa en DB → skip (weekly idempotente).
            # La temporada EN CURSO crece cada semana (existing < len) y se
            # re-descarga con ON CONFLICT DO NOTHING — barato por lotes.
            if existing >= len(df):
                print(f"   ya cargada ({existing} filas en DB) — skip")
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

            # Por LOTES de verdad (src/utils/db_batch): una sentencia
            # multi-VALUES por lote. El executemany de antes hacía un viaje a
            # Neon por fila. to_json convierte numpy → nativos y NaN → null
            # (ronda 15).
            cols = list(df.columns)
            rows = json.loads(df.to_json(orient="records", date_format="iso"))
            with engine.begin() as conn:
                res = insert_ignore_conflicts(conn, "matches", cols, rows,
                                              ["date", "home_team", "away_team"])
            print(f"✅ {league} {s}: {describe(res)}")
            if res["errors"]:
                log.error(f"❌ Histórico {league} {s}: {res['errors']} filas no se "
                          f"pudieron insertar — {res['first_error']}")

    if failed:
        log.error(f"❌ Histórico: {len(failed)} descargas fallaron — " + "; ".join(failed[:4]))
    print("🔥 HISTORICAL READY")


if __name__ == "__main__":
    load_historical_data()