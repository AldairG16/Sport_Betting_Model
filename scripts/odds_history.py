"""
scripts/odds_history.py
=======================
ACUMULADOR DE HISTORIAL DE CUOTAS (fotografías horarias).

El proceso profesional con más infraestructura: predecir hacia dónde se
moverá la LÍNEA, no el partido. Si sabemos que el cierre tenderá a X,
apostamos antes del movimiento. Para eso hace falta el dataset de
movimientos — esta tabla lo construye desde hoy (casi costo cero: lee
upcoming_matches tras cada fetch, sin llamadas extra a la API).

Retención: filas >90 días se borran automáticamente (1/30 de corridas).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
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
            bookmaker_count INT
        )
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_odds_history_key_time
        ON odds_history (match_key, captured_at DESC)
    """))


def capture_snapshot(verbose: bool = True) -> int:
    """
    Fotografía las cuotas actuales de todos los partidos con odds en
    upcoming_matches. Se llama tras cada fetch de cuotas (morning y closing).
    """
    df = pd.read_sql(text("""
        SELECT
            match_key, match, sport_key AS league, match_date AS kickoff,
            home_odds, draw_odds, away_odds,
            over25_odds, under25_odds,
            btts_yes_odds, btts_no_odds,
            ah_line, ah_home_odds, ah_away_odds,
            bookmaker_count
        FROM upcoming_matches
        WHERE home_odds IS NOT NULL AND home_odds > 1
          AND away_odds IS NOT NULL AND away_odds > 1
    """), engine)
    if df.empty:
        if verbose:
            print("   odds_history: sin cuotas que fotografiar")
        return 0

    inserted = 0
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        _ensure_table(conn)
        for _, r in df.iterrows():
            conn.execute(text("""
                INSERT INTO odds_history (
                    captured_at, match_key, match, league, kickoff,
                    home_odds, draw_odds, away_odds,
                    over25_odds, under25_odds,
                    btts_yes_odds, btts_no_odds,
                    ah_line, ah_home_odds, ah_away_odds, bookmaker_count
                ) VALUES (
                    :now, :match_key, :match, :league, :kickoff,
                    :home_odds, :draw_odds, :away_odds,
                    :over25_odds, :under25_odds,
                    :btts_yes_odds, :btts_no_odds,
                    :ah_line, :ah_home_odds, :ah_away_odds, :bookmaker_count
                )
            """), {
                "now": now, "match_key": r["match_key"], "match": r["match"],
                "league": r["league"], "kickoff": r["kickoff"],
                "home_odds": r["home_odds"], "draw_odds": r["draw_odds"],
                "away_odds": r["away_odds"], "over25_odds": r["over25_odds"],
                "under25_odds": r["under25_odds"],
                "btts_yes_odds": r["btts_yes_odds"],
                "btts_no_odds": r["btts_no_odds"],
                "ah_line": r["ah_line"], "ah_home_odds": r["ah_home_odds"],
                "ah_away_odds": r["ah_away_odds"],
                "bookmaker_count": r["bookmaker_count"],
            })
            inserted += 1

        # Retención (barata: corre siempre, borra poco)
        conn.execute(text("""
            DELETE FROM odds_history
            WHERE captured_at < NOW() - INTERVAL '90 days'
        """))

    if verbose:
        print(f"📸 odds_history: {inserted} fotografías de cuotas capturadas")
    return inserted


if __name__ == "__main__":
    capture_snapshot()
