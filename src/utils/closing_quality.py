"""
src/utils/closing_quality.py
============================
Qué cuenta como una cuota de CIERRE válida (una sola definición para el
closing, el CLV gate, el caché de CLV de Kelly y el aprendizaje del peso
del modelo).

Por qué existe (22-sep-26): el "cierre" se tomaba de la fila de
upcoming_matches tal como estuviera, sin saber cuándo se descargó esa
cuota. Sin una descarga cerca del kickoff, el cierre de una bet o de una
candidata shadow era la MISMA cuota de apertura: movimiento exactamente 0
(se veía en el CLV gate: tarjetas, DNB, 1T y AH con CLV +0.0000). Esos
ceros falsos empujan al aprendizaje a concluir que el modelo no anticipa
nada aunque lo haga. Ahora cada cierre guarda closing_fetched_at — cuándo
se DESCARGÓ la cuota usada — y solo cuenta si se descargó poco antes del
kickoff (y después de la apertura).
"""

from datetime import datetime, timedelta

import pandas as pd

# Ventana válida de descarga respecto al kickoff: entre 150 min antes y 2
# después (el margen de 2 min absorbe relojes; más tarde ya es en vivo).
CLOSING_MAX_LEAD_MIN = 150
CLOSING_MAX_LAG_MIN = 2

# Fragmentos SQL (misma regla que is_valid_closing). `a` = alias opcional.
def valid_closing_sql(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return (f"({p}closing_fetched_at IS NOT NULL"
            f" AND {p}closing_fetched_at <= {p}match_date + INTERVAL '{CLOSING_MAX_LAG_MIN} minutes'"
            f" AND {p}closing_fetched_at >= {p}match_date - INTERVAL '{CLOSING_MAX_LEAD_MIN} minutes')")


def _ts(v):
    if v is None:
        return None
    try:
        t = pd.to_datetime(v, utc=True)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(t) else t


def is_valid_closing(fetched_at, match_date, opened_at=None) -> bool:
    """
    ¿Una cuota descargada en `fetched_at` sirve como cierre del partido de
    `match_date`, para una apuesta/candidata abierta en `opened_at`?
    """
    f, m = _ts(fetched_at), _ts(match_date)
    if f is None or m is None:
        return False
    if not (m - timedelta(minutes=CLOSING_MAX_LEAD_MIN) <= f
            <= m + timedelta(minutes=CLOSING_MAX_LAG_MIN)):
        return False
    o = _ts(opened_at)
    return o is None or f > o


def should_update_closing(current_fetched_at, new_fetched_at, match_date) -> bool:
    """
    ¿Reemplazar el cierre guardado por uno nuevo? Solo si el nuevo es de
    antes del kickoff (no en vivo) y MÁS reciente que el guardado. Así el
    cierre se va acercando al kickoff en cada corrida del closing, en vez
    de quedarse con la primera cuota que se vio.
    """
    n, m = _ts(new_fetched_at), _ts(match_date)
    if n is None or m is None:
        return False
    if n > m + timedelta(minutes=CLOSING_MAX_LAG_MIN):
        return False
    c = _ts(current_fetched_at)
    return c is None or n > c


def utc_now() -> datetime:
    return pd.Timestamp.now(tz="UTC").to_pydatetime()


def ensure_closing_columns(engine) -> None:
    """
    Crea closing_fetched_at en bets_history y shadow_bets si falta
    (idempotente). Los lectores la llaman antes de filtrar por ella: el
    weekly puede correr antes que el primer closing con el código nuevo.
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE bets_history ADD COLUMN IF NOT EXISTS "
                          "closing_fetched_at TIMESTAMPTZ"))
        conn.execute(text("ALTER TABLE IF EXISTS shadow_bets ADD COLUMN IF NOT EXISTS "
                          "closing_fetched_at TIMESTAMPTZ"))
