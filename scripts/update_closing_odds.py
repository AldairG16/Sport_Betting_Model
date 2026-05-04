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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from src.utils.team_normalizer import normalize_team


# ============================================================
# MAIN
# ============================================================
def update_closing_odds(only_near_kickoff: bool = True):
    """
    Actualiza closing odds en bets_history.

    only_near_kickoff: si True (default), SOLO procesa bets cuyo partido
        arranca dentro de los próximos 90 minutos. Esto es CRÍTICO porque
        este script suele correr varias veces al día — sin este filtro,
        partidos que arrancan a las 23:00 reciben "closing odds" del
        mediodía, contaminando la métrica CLV.

        El audit del 04-may-26 reveló que 157 bets con CLV+ acumularon
        -23u — síntoma claro de closing odds capturadas demasiado pronto.
    """
    print("\n📡 ACTUALIZANDO CLOSING ODDS (desde DB, sin llamada API)...\n")

    # Bets que aun no tienen closing odds y siguen pendientes.
    # Nota: bets_history usa result='pending' (no NULL) — filtrar por NULL
    # dejaba el script sin trabajo y ningún closing odds se guardaba.
    if only_near_kickoff:
        # Solo bets cuyo partido arranca en <=90 min (closing odds reales)
        bets = pd.read_sql(text("""
            SELECT id, match, market, odds
            FROM bets_history
            WHERE closing_odds IS NULL
              AND (result = 'pending' OR result IS NULL)
              AND match_date <= NOW() AT TIME ZONE 'UTC' + INTERVAL '90 minutes'
              AND match_date >= NOW() AT TIME ZONE 'UTC' - INTERVAL '4 hours'
        """), engine)
    else:
        # Modo backfill: procesa TODAS las pendientes (one_shot_data_quality_cleanup)
        bets = pd.read_sql("""
            SELECT id, match, market, odds
            FROM bets_history
            WHERE closing_odds IS NULL
              AND (result = 'pending' OR result IS NULL)
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
                       over25_odds, under25_odds,
                       btts_yes_odds, btts_no_odds,
                       ah_home_odds, ah_away_odds, ah_line,
                       dnb_home_odds, dnb_away_odds,
                       dc_1x_odds, dc_x2_odds, dc_12_odds,
                       h1_home_odds, h1_draw_odds, h1_away_odds,
                       h2_home_odds, h2_draw_odds, h2_away_odds,
                       corners_over_odds, corners_under_odds, corners_line,
                       cards_over_odds, cards_under_odds, cards_line
                FROM upcoming_matches
                WHERE home_team_norm = :home
                  AND away_team_norm = :away
                ORDER BY match_date DESC
                LIMIT 1
            """), {"home": home_n, "away": away_n}).fetchone()

            if result is None:
                not_found += 1
                continue

            # Mapear mercado → columna (mercados simples)
            market_map = {
                "home_win":  result.home_odds,
                "draw":      result.draw_odds,
                "away_win":  result.away_odds,
                "over25":    result.over25_odds,
                "under25":   result.under25_odds,
                "btts_yes":  result.btts_yes_odds,
                "btts_no":   result.btts_no_odds,
                "dnb_home":  result.dnb_home_odds,
                "dnb_away":  result.dnb_away_odds,
                "dc_1x":     result.dc_1x_odds,
                "dc_x2":     result.dc_x2_odds,
                "dc_12":     result.dc_12_odds,
                "h1_home":   result.h1_home_odds,
                "h1_draw":   result.h1_draw_odds,
                "h1_away":   result.h1_away_odds,
                "h2_home":   result.h2_home_odds,
                "h2_draw":   result.h2_draw_odds,
                "h2_away":   result.h2_away_odds,
            }

            closing_odds = market_map.get(market)

            # Asian Handicap: format "ah_home_+0.5" / "ah_away_-1.5"
            # Solo coincidir si la línea que guardó el modelo es igual a la
            # línea de consenso actual (ah_line). Si no, no podemos dar CO.
            if closing_odds is None and market.startswith("ah_"):
                try:
                    _, side, line_str = market.split("_", 2)  # ah, home/away, +0.5
                    bet_line = float(line_str)
                    if result.ah_line is not None and abs(float(result.ah_line) - bet_line) < 0.01:
                        if side == "home":
                            closing_odds = result.ah_home_odds
                        elif side == "away":
                            closing_odds = result.ah_away_odds
                except (ValueError, AttributeError):
                    pass

            # Corners: format "corners_over_9.5" / "corners_under_9.5"
            if closing_odds is None and market.startswith("corners_"):
                try:
                    _, side, line_str = market.split("_", 2)
                    bet_line = float(line_str)
                    if result.corners_line is not None and abs(float(result.corners_line) - bet_line) < 0.01:
                        if side == "over":
                            closing_odds = result.corners_over_odds
                        elif side == "under":
                            closing_odds = result.corners_under_odds
                except (ValueError, AttributeError):
                    pass

            # Cards: format "cards_over_4.5" / "cards_under_4.5"
            if closing_odds is None and market.startswith("cards_"):
                try:
                    _, side, line_str = market.split("_", 2)
                    bet_line = float(line_str)
                    if result.cards_line is not None and abs(float(result.cards_line) - bet_line) < 0.01:
                        if side == "over":
                            closing_odds = result.cards_over_odds
                        elif side == "under":
                            closing_odds = result.cards_under_odds
                except (ValueError, AttributeError):
                    pass

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
