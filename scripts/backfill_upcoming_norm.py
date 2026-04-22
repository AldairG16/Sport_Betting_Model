"""Backfill home_team_norm / away_team_norm en upcoming_matches.

Bug: el INSERT anterior no persistía estos campos, por eso todas las rows
tienen NULL. Esto hacía que update_closing_odds.py nunca encontrara matches
(filtra por home_team_norm = :home).
"""
import sys, io
sys.path.insert(0, '.')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from config.database import engine
import pandas as pd
from sqlalchemy import text
from src.utils.team_normalizer import normalize_team

with engine.connect() as c:
    df = pd.read_sql(text("""
        SELECT fixture_id, home_team, away_team, match_key
        FROM upcoming_matches
        WHERE home_team_norm IS NULL OR away_team_norm IS NULL
    """), c)

print(f"Rows con norm NULL: {len(df)}")

if len(df) == 0:
    print("Nada que backfillear.")
else:
    with engine.begin() as c:
        n = 0
        for _, row in df.iterrows():
            home_norm = normalize_team(row["home_team"]).lower().strip()
            away_norm = normalize_team(row["away_team"]).lower().strip()
            c.execute(text("""
                UPDATE upcoming_matches
                SET home_team_norm = :h, away_team_norm = :a
                WHERE match_key = :k
            """), {"h": home_norm, "a": away_norm, "k": row["match_key"]})
            n += 1
        print(f"✓ Backfill completo: {n} rows actualizadas")

    # Verificación
    with engine.connect() as c:
        residual = c.execute(text(
            "SELECT COUNT(*) FROM upcoming_matches WHERE home_team_norm IS NULL OR away_team_norm IS NULL"
        )).scalar()
        print(f"Rows con norm NULL residual: {residual}")
