"""
scripts/restore_backup.py
=========================
Rehace la base desde una copia del dashboard (src/utils/db_backup.py).

Por defecto es un ENSAYO: crea todo dentro de una transacción, compara las
filas con las de la copia y lo deshace. Con --apply lo guarda.

    # ensayo en un esquema aparte de la base actual (no deja nada)
    python scripts/restore_backup.py respaldos/2026-11-11 --schema respaldo_prueba
    # restaurar en una base nueva y vacía (DB_URL apuntando a ella)
    python scripts/restore_backup.py respaldos/2026-11-11 --apply

Sin --schema escribe en public: solo tiene sentido en una base vacía (si ya
existen las tablas, falla sin tocar nada). El historial de momios de
football-data (historical_odds) no viaja en la copia: lo vuelve a bajar el
weekly ("Momios históricos").
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Restaurar una copia de la base")
    ap.add_argument("folder", help="carpeta de la copia (la que tiene manifest.json)")
    ap.add_argument("--schema", help="esquema aparte donde rehacer las tablas (ensayo)")
    ap.add_argument("--apply", action="store_true", help="guardar (sin esto, ensayo)")
    args = ap.parse_args()
    from config.database import engine
    from src.utils.db_backup import restore
    restore(engine, Path(args.folder), schema=args.schema, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
