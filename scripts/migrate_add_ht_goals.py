"""
scripts/migrate_add_ht_goals.py
================================
Agrega columnas de goles por tiempo (1er + 2do) a la tabla matches.

Motivo:
  save_bets.py:296-322 resuelve bets h1_home/h1_draw/h2_away leyendo
  las columnas home_goals_ht, away_goals_ht, home_goals_h2, away_goals_h2
  — pero la tabla NO tiene esas columnas, así que todas las bets de
  medios tiempos quedan 'unresolved' permanentemente.

  Esta migración solo AGREGA columnas (DDL idempotente con IF NOT EXISTS).
  NO rellena datos históricos — eso requiere fuentes externas
  (football-data.co.uk, FBRef scraping) que se pueden agregar después.

Uso:
    python scripts/migrate_add_ht_goals.py
"""
import sys
import io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text
from config.database import engine


def migrate():
    print("\n🔧 MIGRATION: agregar columnas HT/H2 a matches\n")

    statements = [
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS home_goals_ht INT",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS away_goals_ht INT",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS home_goals_h2 INT",
        "ALTER TABLE matches ADD COLUMN IF NOT EXISTS away_goals_h2 INT",
    ]

    with engine.begin() as c:
        for s in statements:
            c.execute(text(s))
            print(f"  ✓ {s}")

    # Verificar resultado
    with engine.connect() as c:
        cols = c.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'matches'
              AND column_name IN ('home_goals_ht', 'away_goals_ht',
                                  'home_goals_h2', 'away_goals_h2')
            ORDER BY column_name
        """)).scalars().all()

    print(f"\n✅ Columnas presentes: {cols}")


if __name__ == "__main__":
    migrate()
