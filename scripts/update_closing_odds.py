"""
scripts/update_closing_odds.py
================================
Actualiza closing odds de bets pendientes.

SIN LLAMADA EXTRA A LA API:
  Reutiliza las odds que ya estan guardadas en upcoming_matches.
  Solo hace una llamada API de respaldo si el partido ya no esta en DB
  (porque fue eliminado tras completarse).

Esto ahorra todos los creditos que antes consumia get_live_odds().
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine
from src.utils.team_normalizer import normalize_team


# ============================================================
# MAIN
# ============================================================
def update_closing_odds():
    print("\n📡 ACTUALIZANDO CLOSING ODDS (desde DB, sin llamada API)...\n")

    # Bets que aun no tienen closing odds
    bets = pd.read_sql("""
        SELECT id, match, market, odds
        FROM bets_history
        WHERE closing_odds IS NULL
          AND result IS NULL
    """, engine)

    if bets.empty:
        print("✅ No hay bets pendientes de closing odds")
        return

    print(f"📊 Bets pendientes: {len(bets)}")

    updates = 0
    not_found = 0

    with engine.begin() as conn:
        for _, bet in bets.iterrows():
            match   = bet["match"]
            market  = bet["market"]

            try:
                home_raw, away_raw = match.split(" vs ")
            except ValueError:
                continue

            home_n = normalize_team(home_raw).lower().strip()
            away_n = normalize_team(away_raw).lower().strip()

            # Buscar en upcoming_matches (datos ya descargados)
            result = conn.execute(text("""
                SELECT home_odds, draw_odds, away_odds,
                       over25_odds, under25_odds
                FROM upcoming_matches
                WHERE home_team_norm = :home
                  AND away_team_norm = :away
                ORDER BY match_date DESC
                LIMIT 1
            """), {"home": home_n, "away": away_n}).fetchone()

            if result is None:
                not_found += 1
                continue

            # Mapear mercado → columna
            market_map = {
                "home_win": result.home_odds,
                "draw":     result.draw_odds,
                "away_win": result.away_odds,
                "over25":   result.over25_odds,
                "under25":  result.under25_odds,
            }

            closing_odds = market_map.get(market)

            if closing_odds is None:
                continue

            conn.execute(text("""
                UPDATE bets_history
                SET closing_odds = :closing_odds
                WHERE id = :id
            """), {"closing_odds": float(closing_odds), "id": int(bet["id"])})

            updates += 1

    print(f"✅ Closing odds actualizadas: {updates}")
    if not_found > 0:
        print(f"⚠️  Partidos no encontrados en DB (ya completados): {not_found}")


if __name__ == "__main__":
    update_closing_odds()
