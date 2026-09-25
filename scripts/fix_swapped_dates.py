"""
scripts/fix_swapped_dates.py
============================
Corrige partidos con el día y el mes intercambiados en `matches`.

Origen (encontrado el 25-sep-26): scripts/load_historical_fbref.py leía las
fechas DD/MM/YYYY de football-data sin dayfirst, así que "03/11/2026"
(3-nov) quedaba como 11-mar... y al revés: 15 partidos de la liga argentina
tenían resultado y fecha FUTURA (p. ej. boca juniors vs san lorenzo el
2026-11-03, jugado el 2026-03-11). Esas fechas desordenan la forma reciente
y los pesos por antigüedad del ajuste Dixon-Coles (una fecha futura pesaba
más que el partido de ayer).

Criterio: resultado registrado, fecha futura y día ≤ 12 (si no, el mes
intercambiado no existe) y la fecha intercambiada ya pasó. Si el partido ya
existe en la fecha corregida no se toca (sería un duplicado) y se reporta.

Por defecto es un ENSAYO; `--apply` actualiza la fecha (UPDATE con guarda,
nunca borra). Uso:
    python scripts/fix_swapped_dates.py            # ensayo
    python scripts/fix_swapped_dates.py --apply
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))


def plan_swaps(rows: pd.DataFrame, existing: set, today: date) -> tuple[list, list]:
    """
    Función pura. rows: id, date, home_team, away_team (partidos con
    resultado y fecha futura). existing: {(home, away, fecha)} ya en la base.
    Devuelve (correcciones [(id, vieja, nueva)], omitidos [(id, motivo)]).
    """
    fixes, skipped = [], []
    for _, r in rows.iterrows():
        d = pd.Timestamp(r["date"]).date()
        if d.day > 12:
            skipped.append((int(r["id"]), "día > 12: no es un intercambio"))
            continue
        new = date(d.year, d.day, d.month)
        if new > today:
            skipped.append((int(r["id"]), f"la fecha intercambiada {new} también es futura"))
            continue
        if (r["home_team"], r["away_team"], new) in existing:
            skipped.append((int(r["id"]), f"ya existe en {new}: duplicado, revisar a mano"))
            continue
        fixes.append((int(r["id"]), d, new))
    return fixes, skipped


def main(apply: bool = False) -> dict:
    from config.database import engine
    rows = pd.read_sql(text("""
        SELECT id, date, league, home_team, away_team, home_goals, away_goals
        FROM matches
        WHERE date > CURRENT_DATE AND home_goals IS NOT NULL
        ORDER BY date
    """), engine)
    ex = pd.read_sql(text("""
        SELECT home_team, away_team, date FROM matches
        WHERE (home_team, away_team) IN (
            SELECT home_team, away_team FROM matches
            WHERE date > CURRENT_DATE AND home_goals IS NOT NULL)
    """), engine)
    existing = {(h, a, pd.Timestamp(d).date()) for h, a, d in ex.itertuples(index=False)}
    fixes, skipped = plan_swaps(rows, existing, date.today())

    by_id = rows.set_index("id")
    for i, old, new in fixes:
        r = by_id.loc[i]
        print(f"  #{i} {r['home_team']} vs {r['away_team']} ({r['league']}) "
              f"{int(r['home_goals'])}-{int(r['away_goals'])}: {old} → {new}")
    for i, why in skipped:
        print(f"  omitido #{i}: {why}")

    updated = 0
    if apply and fixes:
        with engine.begin() as conn:
            for i, old, new in fixes:
                res = conn.execute(text("UPDATE matches SET date = :new WHERE id = :id AND date = :old"),
                                   {"new": new, "id": i, "old": old})
                updated += res.rowcount or 0
    print(f"\n{'APLICADO' if apply else 'ENSAYO'}: {len(fixes)} a corregir, {len(skipped)} omitidos"
          + (f", {updated} actualizados" if apply else " — usa --apply para corregir"))
    return {"fixes": len(fixes), "skipped": len(skipped), "updated": updated}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    main(apply=ap.parse_args().apply)
