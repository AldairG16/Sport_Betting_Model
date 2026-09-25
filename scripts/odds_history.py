"""
scripts/odds_history.py
=======================
ACUMULADOR DE HISTORIAL DE CUOTAS (movimientos de línea).

El proceso profesional con más infraestructura: predecir hacia dónde se
moverá la LÍNEA, no el partido. Si sabemos que el cierre tenderá a X,
apostamos antes del movimiento. Para eso hace falta el dataset de
movimientos — esta tabla lo construye (casi costo cero: lee
upcoming_matches tras cada fetch, sin llamadas extra a la API).

Solo guarda una foto cuando la cuota de ese partido CAMBIÓ desde la
anterior (25-sep-26). Con el closing cada 30 min, la mayoría de las
corridas reutilizan el caché y la cuota es la misma: el 97% de las filas
de un día eran copias idénticas (24,067 de 24,798) y la tabla crecía
~8 MB/día. Una foto repetida no aporta nada — la anterior ya dice la misma
cuota —, así que no se guarda. Tampoco se borra nada: sin copias la tabla
crece unas 30 veces menos y cabe por años (antes se borraba a los 90 días).

Un solo INSERT...SELECT del lado del servidor (sin viaje Python↔DB por
fila — evita problemas de NaN/None/tipos y es ~50x más rápido).
"""

import sys
from pathlib import Path

from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine

# Lo que define "la misma foto": si ninguna de estas cambió, no se guarda.
SNAPSHOT_COLS = (
    "home_odds", "draw_odds", "away_odds",
    "over25_odds", "under25_odds",
    "btts_yes_odds", "btts_no_odds",
    "ah_line", "ah_home_odds", "ah_away_odds",
    "corners_line", "cards_line", "bookmaker_count",
)


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


def snapshot_sql() -> str:
    """INSERT...SELECT de las filas cuya cuota cambió respecto de la última
    foto del mismo partido (o que aún no tienen foto). Las columnas de
    upcoming_matches son FLOAT y las de aquí NUMERIC: se comparan en NUMERIC,
    el mismo tipo en que quedaron guardadas."""
    cols = ", ".join(SNAPSHOT_COLS)
    new = ", ".join(f"u.{c}::numeric" if c != "bookmaker_count" else f"u.{c}" for c in SNAPSHOT_COLS)
    old = ", ".join(f"prev.{c}" for c in SNAPSHOT_COLS)
    return f"""
        INSERT INTO odds_history (captured_at, match_key, match, league, kickoff, {cols})
        SELECT NOW(), u.match_key, u.home_team || ' vs ' || u.away_team, u.sport_key,
               u.match_date, {", ".join("u." + c for c in SNAPSHOT_COLS)}
        FROM upcoming_matches u
        LEFT JOIN LATERAL (
            SELECT {cols} FROM odds_history h
            WHERE h.match_key = u.match_key
            ORDER BY h.captured_at DESC
            LIMIT 1
        ) prev ON TRUE
        WHERE u.home_odds IS NOT NULL AND u.home_odds > 1
          AND u.away_odds IS NOT NULL AND u.away_odds > 1
          AND (prev.home_odds IS NULL OR ({new}) IS DISTINCT FROM ({old}))
    """


def capture_snapshot(verbose: bool = True) -> int:
    """Fotografía las cuotas que cambiaron. Se llama tras cada fetch
    (morning y cada closing)."""
    with engine.begin() as conn:
        _ensure_table(conn)
        r = conn.execute(text(snapshot_sql()))
        inserted = r.rowcount or 0
    if verbose:
        print(f"📸 odds_history: {inserted} fotos nuevas (solo cuotas que cambiaron)")
    return inserted


if __name__ == "__main__":
    capture_snapshot()
