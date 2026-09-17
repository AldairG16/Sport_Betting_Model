"""
scripts/odds_history.py
=======================
ACUMULADOR DE HISTORIAL DE CUOTAS (fotografías horarias).

El proceso profesional con más infraestructura: predecir hacia dónde se
moverá la LÍNEA, no el partido. Si sabemos que el cierre tenderá a X,
apostamos antes del movimiento. Para eso hace falta el dataset de
movimientos — esta tabla lo construye desde hoy (casi costo cero: lee
upcoming_matches tras cada fetch, sin llamadas extra a la API).

Implementación: un solo INSERT...SELECT del lado del servidor (sin viaje
Python↔DB por fila — evita problemas de NaN/None/tipos y es ~50x más
rápido). Retención: filas >90 días se borran automáticamente.
"""

import sys
from pathlib import Path

from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine

RETENTION_DAYS = 90


def _ensure_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS odds_history (
            id BIGSERIAL PRIMARY KEY,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            match_key TEXT NOT NULL,
            match TEXT,
            league TEXT,
            kickoff TIMESTAMPTZ,
            home_odds NUMERIC, draw_odds NUMERIC, away_odds NUMERIC,
            over25_odds NUMERIC, under25_odds NUMERIC,
            btts_yes_odds NUMERIC, btts_no_odds NUMERIC,
            ah_line NUMERIC, ah_home_odds NUMERIC, ah_away_odds NUMERIC,
            corners_line NUMERIC, cards_line NUMERIC,
            bookmaker_count INT
        )
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_odds_history_key_time
        ON odds_history (match_key, captured_at DESC)
    """))


def capture_snapshot(verbose: bool = True) -> int:
    """
    Fotografía las cuotas actuales de todos los partidos con odds.
    Se llama tras cada fetch de cuotas (morning y closing horario).
    Un solo INSERT...SELECT del lado del servidor.
    """
    inserted = 0
    with engine.begin() as conn:
        _ensure_table(conn)
        r = conn.execute(text("""
            INSERT INTO odds_history (
                captured_at, match_key, match, league, kickoff,
                home_odds, draw_odds, away_odds,
                over25_odds, under25_odds,
                btts_yes_odds, btts_no_odds,
                ah_line, ah_home_odds, ah_away_odds,
                corners_line, cards_line, bookmaker_count
            )
            SELECT
                NOW(),
                match_key,
                home_team || ' vs ' || away_team,
                sport_key,
                match_date,
                home_odds, draw_odds, away_odds,
                over25_odds, under25_odds,
                btts_yes_odds, btts_no_odds,
                ah_line, ah_home_odds, ah_away_odds,
                corners_line, cards_line, bookmaker_count
            FROM upcoming_matches
            WHERE home_odds IS NOT NULL AND home_odds > 1
              AND away_odds IS NOT NULL AND away_odds > 1
        """)
        )
        inserted = r.rowcount or 0

        # Retención: el dataset de movimiento no necesita más de 90 días
        conn.execute(text("""
            DELETE FROM odds_history
            WHERE captured_at < NOW() - INTERVAL '90 days'
        """))

    if verbose:
        print(f"📸 odds_history: {inserted} fotografías de cuotas capturadas")
    return inserted


if __name__ == "__main__":
    capture_snapshot()
