"""
scripts/collect_match_events.py
================================
Recolecta eventos de partidos (goleadores, penales) desde fuentes gratuitas
y los almacena en la tabla match_events de la DB.

Fuente: martj42/international_results (github) — goalscorers.csv
  Columnas: date, home_team, away_team, team, scorer, minute, own_goal, penalty

Cómo ejecutar:
  python scripts/collect_match_events.py

Se llama desde el orchestrator en modo weekly para mantener el historial
actualizado sin gastar créditos de API.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import pandas as pd
from sqlalchemy import text
from config.database import engine
from src.utils.team_normalizer import normalize_team
from src.utils.db_batch import insert_ignore_conflicts, describe
from src.utils.log import get_logger

log = get_logger(__name__)

GOALS_URL = "https://raw.githubusercontent.com/martj42/international_results/master/goalscorers.csv"


# ============================================================
# SCHEMA
# ============================================================

def _ensure_schema():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS match_events (
                id               SERIAL PRIMARY KEY,
                date             DATE NOT NULL,
                home_team        TEXT NOT NULL,
                away_team        TEXT NOT NULL,
                league           TEXT,
                home_scorers     TEXT,
                away_scorers     TEXT,
                home_yellow      INT DEFAULT 0,
                away_yellow      INT DEFAULT 0,
                home_red         INT DEFAULT 0,
                away_red         INT DEFAULT 0,
                home_corners     INT,
                away_corners     INT,
                penalty_in_match BOOLEAN DEFAULT FALSE,
                UNIQUE(date, home_team, away_team)
            )
        """))


# ============================================================
# MAIN
# ============================================================

def collect_match_events(verbose: bool = True):
    _ensure_schema()

    if verbose:
        print("\n📥 Descargando goalscorers.csv...")

    try:
        import requests, io
        resp = requests.get(GOALS_URL, timeout=60)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
    except Exception as e:
        print(f"❌ Error descargando goalscorers.csv: {e}")
        return

    if verbose:
        print(f"   Registros descargados: {len(df):,}")

    # Normalizar columnas
    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    # Normalizar equipos
    df["home_team"] = df["home_team"].apply(normalize_team)
    df["away_team"] = df["away_team"].apply(normalize_team)
    df["team"]      = df["team"].apply(normalize_team)

    # Normalizar booleans (pueden venir como TRUE/FALSE string o bool)
    for col in ["own_goal", "penalty"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.upper().map({"TRUE": True, "FALSE": False, "1": True, "0": False}).fillna(False)

    # Agrupar por partido
    groups = df.groupby(["date", "home_team", "away_team"])

    rows = []

    for (date, home, away), group in groups:
            # Separar goles de cada equipo.
            # Un own goal (dataset martj42) tiene team = el equipo del jugador
            # que la mandó a su arco, pero el gol cuenta para el RIVAL:
            # la atribuimos al equipo contrario.
            home_rows = pd.concat([
                group[(group["team"] == home) & (group["own_goal"] == False)],
                group[(group["team"] == away) & (group["own_goal"] == True)],
            ])
            away_rows = pd.concat([
                group[(group["team"] == away) & (group["own_goal"] == False)],
                group[(group["team"] == home) & (group["own_goal"] == True)],
            ])

            # Construir JSON de goleadores
            def _build_scorers(rows):
                result = []
                for _, r in rows.iterrows():
                    entry = {"player": str(r.get("scorer", ""))}
                    minute = r.get("minute")
                    if pd.notna(minute):
                        try:
                            entry["minute"] = int(float(str(minute).replace("'", "")))
                        except (ValueError, TypeError):
                            pass
                    entry["penalty"] = bool(r.get("penalty", False))
                    result.append(entry)
                return json.dumps(result, ensure_ascii=False)

            home_scorers = _build_scorers(home_rows)
            away_scorers = _build_scorers(away_rows)

            penalty_in_match = bool(group["penalty"].any())

            rows.append({
                "date":             date.strftime("%Y-%m-%d"),
                "home_team":        home,
                "away_team":        away,
                "home_scorers":     home_scorers,
                "away_scorers":     away_scorers,
                "penalty_in_match": penalty_in_match,
            })

    # Por lotes de verdad (src/utils/db_batch). El executemany de antes
    # decía "un viaje por lote" pero con SQL textual psycopg2 hace uno por
    # fila (21 min por semana), y contaba como insertadas las procesadas.
    with engine.begin() as conn:
        res = insert_ignore_conflicts(
            conn, "match_events",
            ["date", "home_team", "away_team", "home_scorers", "away_scorers",
             "penalty_in_match"],
            rows, ["date", "home_team", "away_team"])

    if verbose:
        print(f"   ✅ match_events: {describe(res)}")
    if res["errors"]:
        log.error(f"❌ match_events: {res['errors']} partidos no se pudieron "
                  f"insertar — {res['first_error']}")


if __name__ == "__main__":
    collect_match_events(verbose=True)
