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
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine


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
        # D1 (ronda 9): antes solo bets 'pending' con partido en
        # [NOW-4h, NOW+90min]. Dos defectos: (1) una bet RESUELTA sin closing
        # quedaba invisible para siempre (btts 0/22 medido), (2) la ventana
        # estrecha perdía confirmaciones tardías. Ahora: resultados finales
        # incluidos y ventana de recuperación de 7 días (la retención de
        # upcoming_matches), preservando el objetivo de capturar el precio
        # cercano al kickoff.
        bets = pd.read_sql(text("""
            SELECT id, match, market, odds, match_date
            FROM bets_history
            WHERE closing_odds IS NULL
              AND result IN ('pending','win','loss','half_win','half_loss','push')
              AND match_date <= NOW() AT TIME ZONE 'UTC' + INTERVAL '90 minutes'
              AND match_date >= NOW() AT TIME ZONE 'UTC' - INTERVAL '7 days'
        """), engine)
    else:
        # Modo backfill (one_shot_data_quality_cleanup): TODO lo rellenable
        bets = pd.read_sql("""
            SELECT id, match, market, odds, match_date
            FROM bets_history
            WHERE closing_odds IS NULL
              AND match_date >= NOW() AT TIME ZONE 'UTC' - INTERVAL '120 days'
        """, engine)

    if bets.empty:
        print("✅ No hay bets pendientes de closing odds")
        return

    print(f"📊 Bets pendientes: {len(bets)}")

    updates = 0
    not_found = 0

    # Un solo mapeo mercado → cuota de cierre para apuestas Y shadow
    # (save_bets._closing_odds_for). Antes este script tenía su propia
    # copia, que buscaba "btts_yes" cuando el mercado se guarda como "btts":
    # ninguna apuesta BTTS recibía cierre ni CLV. Además tomaba la fila más
    # reciente de los dos equipos (podía ser OTRO partido entre ellos); la
    # compartida busca la más cercana al kickoff (±4h).
    from src.models.save_bets import _nearest_market_row, _closing_odds_for

    with engine.begin() as conn:
        for _, bet in bets.iterrows():
            match   = bet["match"]
            market  = bet["market"]

            try:
                home_raw, away_raw = match.split(" vs ")
            except ValueError:
                continue

            try:
                odds_df = _nearest_market_row(home_raw, away_raw,
                                              pd.to_datetime(bet["match_date"], utc=True))
            except Exception as e:
                print(f"⚠️  closing: no se pudo buscar {match}: {type(e).__name__}")
                continue

            if odds_df.empty:
                not_found += 1
                continue

            closing_odds = _closing_odds_for(market, odds_df.iloc[0])
            if closing_odds is None:
                continue

            conn.execute(text("""
                UPDATE bets_history
                SET closing_odds = :closing_odds
                WHERE id = :id
            """), {"closing_odds": float(closing_odds), "id": int(bet["id"])})

            updates += 1

    print(f"✅ Closing odds actualizadas: {updates}")

    # ── C1/D1 (rondas 8-9): closing de las candidatas shadow ──
    # El gemelo de esta función en src/models/save_bets.py casi no corre en
    # producción; el shadow se rellena AQUÍ, donde corre el hourly.
    try:
        from src.models.save_bets import _update_shadow_closing
        _update_shadow_closing()
    except Exception as e:
        print(f"⚠️  shadow closing omitido: {type(e).__name__}")
    if not_found > 0:
        print(f"⚠️  Partidos no encontrados en DB (ya completados): {not_found}")


if __name__ == "__main__":
    update_closing_odds()
