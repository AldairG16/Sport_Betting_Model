"""
scripts/cleanup_stale_bets.py
=============================
Marca como 'stale' las bets que quedaron en 'pending' o 'unresolved'
más de N días (default 14).

Motivo:
  - 'pending' nunca debería sobrevivir más de unos días: el partido ya
    jugó y results.py debió resolverlo. Si sigue pendiente es porque
    el partido nunca entró a matches (equipo mal escrito, liga no
    soportada, etc).
  - 'unresolved' se queda cuando faltan datos (córners, tarjetas, HT).
    Después de 2 semanas la API ya no devuelve esos datos, así que no
    tiene sentido dejarlo colgando distorsionando métricas.

Ambos casos ensucian los reportes (count inflado, bankroll skew). Se
marca como 'stale' para mantener visibilidad sin contaminar ROI.

El backtest engine ya excluye 'pending', 'null', '', 'unresolved' y
también debe excluir 'stale' (añadido al filtro).

Uso:
    python scripts/cleanup_stale_bets.py          # 14 días default
    python scripts/cleanup_stale_bets.py --days 7 # más agresivo
    python scripts/cleanup_stale_bets.py --dry    # solo mostrar
"""

import sys
import os
import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine


def cleanup_stale_bets(days: int = 14, dry: bool = False):
    print(f"\n🧹 CLEANUP STALE BETS (> {days} días)\n")

    with engine.connect() as c:
        df = pd.read_sql(
            text("""
                SELECT id, match, match_date, market, result
                FROM bets_history
                WHERE result IN ('pending', 'unresolved')
                  AND match_date < (NOW() - INTERVAL ':days days')
                ORDER BY match_date
            """.replace(":days", str(int(days)))),
            c,
        )

    if df.empty:
        print(f"✅ Sin bets stale (> {days} días)")
        return

    print(f"📊 Encontradas: {len(df)}")
    print(df.groupby("result").size().to_string())
    print()

    if dry:
        print("🏃 Dry-run — no se actualizó nada.")
        return

    with engine.begin() as c:
        res = c.execute(
            text("""
                UPDATE bets_history
                SET result = 'stale'
                WHERE result IN ('pending', 'unresolved')
                  AND match_date < (NOW() - INTERVAL ':days days')
            """.replace(":days", str(int(days))))
        )

    print(f"✅ {res.rowcount} bets marcadas como 'stale'")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--dry", action="store_true")
    args = p.parse_args()
    cleanup_stale_bets(days=args.days, dry=args.dry)
