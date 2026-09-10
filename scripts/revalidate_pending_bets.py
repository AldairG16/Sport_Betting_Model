"""
scripts/revalidate_pending_bets.py
===================================
REVALIDACIÓN de bets contra odds FRESCAS pre-kickoff.

Problema que resuelve:
  El ciclo morning (6AM) genera bets con las odds de ese momento. Durante
  el día el mercado se mueve: la odd 2.10 que vimos a las 6AM puede ser
  1.95 al mediodía. Si apostamos la 1.95 "creyendo" que era 2.10, el edge
  que calculamos ya no existe — apostamos peor de lo que el modelo decidió.

Solución (corre en el modo closing, justo antes de los kickoffs):
  Para cada bet pending del día, compara las odds guardadas vs las odds
  frescas de upcoming_matches (re-fetcheadas por step_pre_kickoff_closing):

  1. Odd fresca MEJOR o igual → actualizar la bet a la odd fresca
     (en paper, tomarías el número mejor; el edge sube o se mantiene).
  2. Odd fresca PEOR → recalcular edge con la odd fresca:
     - edge nuevo >= KEEP_EDGE (2%) → mantener la bet a la odd fresca
       (el value sobrevive la movida).
     - edge nuevo < KEEP_EDGE → CANCELAR: marcar result='stale' con
       profit 0 (excluida de métricas, igual que cleanup_stale_bets).
       El mercado absorbió el edge — no apostar.

Todo cambia queda anotado en decision_log.revalidation para autopsia.

READ about créditos: este script NO llama a la API — solo lee las odds
que step_pre_kickoff_closing ya descargó.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine
from src.utils.team_normalizer import normalize_team

# Edge mínimo para MANTENER una bet cuya odd se movió en contra
KEEP_EDGE = 0.02


def _fresh_odds_for(result, market: str):
    """Mapea market → columna de odds frescas (mismo mapping que update_closing_odds)."""
    market_map = {
        "home_win":  result.home_odds,
        "draw":      result.draw_odds,
        "away_win":  result.away_odds,
        "over25":    result.over25_odds,
        "under25":   result.under25_odds,
        "btts_yes":  result.btts_yes_odds,
        "btts_no":   result.btts_no_odds,
        "btts":      result.btts_yes_odds,
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
    odds = market_map.get(market)

    if odds is None and market.startswith("ah_"):
        try:
            _, side, line_str = market.split("_", 2)
            bet_line = float(line_str)
            if result.ah_line is not None and abs(float(result.ah_line) - bet_line) < 0.01:
                odds = result.ah_away_odds if side == "away" else result.ah_home_odds
        except (ValueError, AttributeError):
            pass

    if odds is None and market.startswith("corners_"):
        try:
            _, side, line_str = market.split("_", 2)
            if result.corners_line is not None and abs(float(result.corners_line) - float(line_str)) < 0.01:
                odds = result.corners_under_odds if side == "under" else result.corners_over_odds
        except (ValueError, AttributeError):
            pass

    if odds is None and market.startswith("cards_"):
        try:
            _, side, line_str = market.split("_", 2)
            if result.cards_line is not None and abs(float(result.cards_line) - float(line_str)) < 0.01:
                odds = result.cards_under_odds if side == "under" else result.cards_over_odds
        except (ValueError, AttributeError):
            pass

    if odds is None:
        return None
    try:
        odds = float(odds)
    except (TypeError, ValueError):
        return None
    return odds if odds > 1 else None


def revalidate_pending_bets(verbose: bool = True) -> dict:
    bets = pd.read_sql(text("""
        SELECT id, match, market, probability, odds
        FROM bets_history
        WHERE result = 'pending'
          AND match_date > NOW()
    """), engine)

    if bets.empty:
        if verbose:
            print("✅ No hay bets pendientes futuras para revalidar")
        return {"status": "no_bets"}

    kept_better, kept_edge, cancelled, no_odds, not_found = 0, 0, 0, 0, 0
    now_iso = datetime.now(timezone.utc).isoformat()

    with engine.begin() as conn:
        for _, bet in bets.iterrows():
            market = str(bet["market"])
            try:
                home_raw, away_raw = str(bet["match"]).split(" vs ")
            except ValueError:
                continue

            home_n = normalize_team(home_raw).lower().strip()
            away_n = normalize_team(away_raw).lower().strip()

            row = conn.execute(text("""
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

            if row is None:
                not_found += 1
                continue

            fresh = _fresh_odds_for(row, market)
            if fresh is None:
                no_odds += 1
                continue

            old = float(bet["odds"])
            prob = float(bet["probability"]) if bet["probability"] else None
            note = {"revalidated_at": now_iso, "odds_before": old, "odds_after": fresh}

            if fresh >= old:
                # Odd igual o mejor → tomar el número fresco
                conn.execute(text("""
                    UPDATE bets_history
                    SET odds = :odds,
                        decision_log = COALESCE(decision_log, '{}'::jsonb)
                                      || jsonb_build_object('revalidation',
                                           to_jsonb(:note::jsonb))
                    WHERE id = :id
                """), {"odds": fresh, "note": str(_json_dumps(note)), "id": int(bet["id"])})
                kept_better += 1
            else:
                # Odd peor → ¿el edge sobrevive?
                if prob is not None and (prob - 1.0 / fresh) >= KEEP_EDGE:
                    note["decision"] = "kept_edge_survives"
                    conn.execute(text("""
                        UPDATE bets_history
                        SET odds = :odds,
                            decision_log = COALESCE(decision_log, '{}'::jsonb)
                                          || jsonb_build_object('revalidation',
                                               to_jsonb(:note::jsonb))
                        WHERE id = :id
                    """), {"odds": fresh, "note": str(_json_dumps(note)), "id": int(bet["id"])})
                    kept_edge += 1
                else:
                    note["decision"] = "cancelled_line_moved"
                    note["new_edge"] = round(prob - 1.0 / fresh, 4) if prob else None
                    conn.execute(text("""
                        UPDATE bets_history
                        SET result = 'stale',
                            profit = 0.0,
                            decision_log = COALESCE(decision_log, '{}'::jsonb)
                                          || jsonb_build_object('revalidation',
                                               to_jsonb(:note::jsonb))
                        WHERE id = :id
                    """), {"note": str(_json_dumps(note)), "id": int(bet["id"])})
                    cancelled += 1

    summary = {
        "status": "ok",
        "total": len(bets),
        "kept_better_odds": kept_better,
        "kept_edge_survives": kept_edge,
        "cancelled": cancelled,
        "no_fresh_odds": no_odds,
        "match_not_found": not_found,
    }

    if verbose:
        print(f"\n🔁 REVALIDACIÓN PRE-KICKOFF (umbral edge {KEEP_EDGE:.0%})")
        print(f"   Bets evaluadas:        {len(bets)}")
        print(f"   Odd mejor/igual:       {kept_better} (actualizadas al número fresco)")
        print(f"   Odd peor, edge vive:   {kept_edge} (mantenidas a la odd nueva)")
        print(f"   ❌ Canceladas:          {cancelled} (línea absorbió el edge)")
        print(f"   Sin odd fresca:        {no_odds}  |  Partido no encontrado: {not_found}")

    return summary


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, default=str)


if __name__ == "__main__":
    revalidate_pending_bets()
