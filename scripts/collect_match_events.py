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
        df = pd.read_csv(GOALS_URL)
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

    inserted = 0
    skipped  = 0

    with engine.begin() as conn:
        for (date, home, away), group in groups:
            # Separar goles de cada equipo (excluir own goals del marcador)
            home_rows = group[(group["team"] == home) & (group["own_goal"] == False)]
            away_rows = group[(group["team"] == away) & (group["own_goal"] == False)]

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

            try:
                conn.execute(text("""
                    INSERT INTO match_events (
                        date, home_team, away_team,
                        home_scorers, away_scorers,
                        penalty_in_match
                    ) VALUES (
                        :date, :home_team, :away_team,
                        :home_scorers, :away_scorers,
                        :penalty_in_match
                    )
                    ON CONFLICT (date, home_team, away_team) DO NOTHING
                """), {
                    "date":             date.strftime("%Y-%m-%d"),
                    "home_team":        home,
                    "away_team":        away,
                    "home_scorers":     home_scorers,
                    "away_scorers":     away_scorers,
                    "penalty_in_match": penalty_in_match,
                })
                inserted += 1
            except Exception as e:
                skipped += 1
                if verbose:
                    print(f"   ⚠️  Skip {date} {home} vs {away}: {e}")

    if verbose:
        print(f"   ✅ Insertados: {inserted:,}  |  Saltados: {skipped:,}")
        print("   match_events actualizado")


if __name__ == "__main__":
    collect_match_events(verbose=True)
