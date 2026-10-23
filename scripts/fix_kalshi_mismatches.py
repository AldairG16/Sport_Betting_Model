"""
scripts/fix_kalshi_mismatches.py
================================
Precios de Kalshi guardados con el partido equivocado (23-oct-26).

El emparejamiento de Kalshi aceptaba un partido con un solo nombre parecido:
el de Gimnasia Mendoza vs Platense quedó guardado como Gimnasia LP vs
Sarmiento. Se detecta por el precio: el 1X2 de Kalshi difiere de Pinnacle
0.74 pt en promedio; a más de 10 pt es otro partido
(kalshi.stored_mismatches). Este script respalda esas filas en
kalshi_history_backup y las borra, para que no entren en la medición del
laboratorio. La auditoría del sistema avisa si vuelve a pasar.

    python scripts/fix_kalshi_mismatches.py              # en seco
    python scripts/fix_kalshi_mismatches.py --apply [--days 30]
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))


def main(apply: bool = False, days: int = 30) -> int:
    from config.database import engine
    from src.features.kalshi import stored_mismatches
    bad = stored_mismatches(engine, days)
    print(f"Partidos de Kalshi con el 1X2 lejos de Pinnacle (últimos {days} días): {len(bad)}")
    for lg, h, a, kick, kh, ph in bad:
        print(f"  {kick} {lg} {h} vs {a}: Kalshi local {kh:.0%}, Pinnacle {ph:.0%}")
    if not apply or not bad:
        if not apply:
            print("En seco: no se escribió nada (usa --apply).")
        return len(bad)
    keys = [{"l": lg, "h": h, "a": a, "k": kick} for lg, h, a, kick, *_ in bad]
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS kalshi_history_backup AS "
                          "SELECT *, NOW() AS backed_up_at FROM kalshi_history WHERE FALSE"))
        for k in keys:
            conn.execute(text("""INSERT INTO kalshi_history_backup SELECT *, NOW() FROM kalshi_history
                                 WHERE league = :l AND home = :h AND away = :a AND kickoff = :k"""), k)
            conn.execute(text("""DELETE FROM kalshi_history
                                 WHERE league = :l AND home = :h AND away = :a AND kickoff = :k"""), k)
    print(f"✅ {len(bad)} partido(s) respaldados en kalshi_history_backup y quitados")
    return len(bad)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    main(args.apply, args.days)
