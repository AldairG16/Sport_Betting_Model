"""
scripts/fix_argentina_swapped_dates.py
======================================
Partidos de la liga argentina con el día y el mes cruzados (23-oct-26).

El cargador viejo de FBref (scripts/load_historical_fbref.py, borrado el
23-oct-26) leyó las fechas sin dayfirst: todo partido con día ≤ 12 quedó en
la fecha espejo (6-feb → 2-jun). scripts/fix_swapped_dates.py (25-sep) solo
corrigió los que caían en el FUTURO. Después, load_extra_leagues cargó la
historia correcta de football-data /new/ARG.csv y su chequeo de repetidos
(±1 día) no vio los espejos: 2,266 partidos argentinos quedaron DOS veces, uno
con la fecha mala. La forma, Dixon-Coles, el Elo y el entre-ellos de los
equipos argentinos los contaban doble y fuera de orden.

Verificado contra ESPN: en Brasil, Liga MX y MLS el cargador viejo dejó las
fechas bien (8 de 8 por liga); fuera de Argentina los 4 pares espejo son dos
partidos reales.

Criterio: el archivo de football-data /new/ARG.csv (fechas DD/MM/YYYY,
la fuente de la copia buena). Para cada fila del cargador viejo (id <
OLD_ID_MAX, liga argentina, día ≤ 12 y día ≠ mes):
  - el archivo la tiene en la fecha espejo (±1 día) y NO en la guardada: es
    la copia mala → se borra si en la espejo ya hay un partido de esos dos
    equipos; si no, se le corrige la fecha;
  - el archivo la tiene en la guardada: es real, se deja. Pasa: Talleres –
    Sarmiento 1-0 se jugó el 8-may-22 (Copa de la Liga) y el 5-ago-22;
  - el archivo no la tiene en ninguna: se deja y se reporta.
Todo lo que se toca se copia antes a matches_swap_backup.

Por defecto es un ENSAYO. Uso:
    python scripts/fix_argentina_swapped_dates.py
    python scripts/fix_argentina_swapped_dates.py --apply
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

LEAGUE = "soccer_argentina_primera_division"
OLD_ID_MAX = 1_000_000          # el cargador viejo usó ids < 1M; el nuevo, > 1.4M

_SELECT = f"""
    SELECT o.id, o.date, LOWER(o.home_team) AS h, LOWER(o.away_team) AS a, o.home_goals, o.away_goals,
           (SELECT n.id FROM matches n
             WHERE n.id >= {OLD_ID_MAX} AND n.league = o.league
               AND LOWER(n.home_team) = LOWER(o.home_team) AND LOWER(n.away_team) = LOWER(o.away_team)
               AND n.home_goals = o.home_goals AND n.away_goals = o.away_goals
               AND n.date::date BETWEEN make_date(EXTRACT(YEAR FROM o.date)::int, EXTRACT(DAY FROM o.date)::int,
                                                  EXTRACT(MONTH FROM o.date)::int) - 1
                                    AND make_date(EXTRACT(YEAR FROM o.date)::int, EXTRACT(DAY FROM o.date)::int,
                                                  EXTRACT(MONTH FROM o.date)::int) + 1
             LIMIT 1) AS twin,
           EXISTS (SELECT 1 FROM matches x
                    WHERE x.id <> o.id AND x.league = o.league
                      AND LOWER(x.home_team) = LOWER(o.home_team) AND LOWER(x.away_team) = LOWER(o.away_team)
                      AND x.date::date BETWEEN make_date(EXTRACT(YEAR FROM o.date)::int, EXTRACT(DAY FROM o.date)::int,
                                                         EXTRACT(MONTH FROM o.date)::int) - 1
                                           AND make_date(EXTRACT(YEAR FROM o.date)::int, EXTRACT(DAY FROM o.date)::int,
                                                         EXTRACT(MONTH FROM o.date)::int) + 1) AS occupied
    FROM matches o
    WHERE o.id < {OLD_ID_MAX} AND o.league = :lg AND o.home_goals IS NOT NULL
      AND EXTRACT(DAY FROM o.date) <= 12 AND EXTRACT(DAY FROM o.date) <> EXTRACT(MONTH FROM o.date)
"""


def mirror(d) -> pd.Timestamp:
    d = pd.Timestamp(d)
    return pd.Timestamp(year=d.year, month=d.day, day=d.month)


def source_games(df: pd.DataFrame) -> set:
    """{(local, visita, goles L, goles V, fecha)} del archivo, con fechas
    DD/MM/YYYY explícitas y los nombres canónicos."""
    from src.utils.team_normalizer import normalize_team as N
    d = pd.to_datetime(df["Date"].astype(str).str.strip(), format="%d/%m/%Y", errors="coerce")
    out = set()
    for h, a, hg, ag, day in zip(df["Home"], df["Away"], pd.to_numeric(df["HG"], errors="coerce"),
                                 pd.to_numeric(df["AG"], errors="coerce"), d):
        if pd.isna(day) or pd.isna(hg) or pd.isna(ag):
            continue
        out.add((N(str(h).strip().lower()), N(str(a).strip().lower()), int(hg), int(ag), day.date()))
    return out


def in_source(games: set, h, a, hg, ag, day) -> bool:
    d = pd.Timestamp(day).date()
    return any((h, a, int(hg), int(ag), d + pd.Timedelta(days=k).to_pytimedelta()) in games for k in (-1, 0, 1))


def decide(rows: pd.DataFrame, games: set) -> tuple[list, list, list]:
    """Función pura: (copias malas a borrar [id], fechas a corregir [(id, vieja,
    nueva)], dejadas [(id, motivo)])."""
    delete, fix, keep = [], [], []
    for r in rows.itertuples():
        stored = in_source(games, r.h, r.a, r.home_goals, r.away_goals, r.date)
        at_mirror = in_source(games, r.h, r.a, r.home_goals, r.away_goals, mirror(r.date))
        if stored:
            keep.append((int(r.id), "real en la fecha guardada"))
        elif at_mirror and (pd.notna(r.twin) or bool(r.occupied)):
            delete.append(int(r.id))         # la buena ya está en la espejo (aunque cambie el marcador)
        elif at_mirror:
            fix.append((int(r.id), pd.Timestamp(r.date).date(), mirror(r.date).date()))
        else:
            keep.append((int(r.id), "no está en el archivo"))
    return delete, fix, keep


def main(apply: bool = False, source: pd.DataFrame | None = None) -> dict:
    from config.database import engine
    if source is None:
        from scripts.load_extra_leagues import fetch_csv, BASE_URL
        source, why = fetch_csv(BASE_URL.format(code="ARG"))
        if source is None:
            print(f"❌ No se pudo bajar ARG.csv: {why} — no se toca nada")
            return {"aborted": True}
    rows = pd.read_sql(text(_SELECT), engine, params={"lg": LEAGUE})
    delete, fix, keep = decide(rows, source_games(source))
    print(f"Filas viejas argentinas con día ≤ 12: {len(rows)} · copias malas (se borran): {len(delete)} · "
          f"fechas a corregir: {len(fix)} · se dejan: {len(keep)}")
    for reason in sorted({k[1] for k in keep}):
        print(f"   se dejan por '{reason}': {sum(1 for k in keep if k[1] == reason)}")
    if apply and (delete or fix):
        ids = delete + [f[0] for f in fix]
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS matches_swap_backup AS SELECT * FROM matches WHERE false"))
            conn.execute(text("ALTER TABLE matches_swap_backup ADD COLUMN IF NOT EXISTS backed_up_at TIMESTAMPTZ"))
            conn.execute(text("INSERT INTO matches_swap_backup SELECT m.*, NOW() FROM matches m WHERE m.id = ANY(:ids)"),
                         {"ids": ids})
            if delete:
                conn.execute(text("DELETE FROM matches WHERE id = ANY(:ids) AND id < :mx"),
                             {"ids": delete, "mx": OLD_ID_MAX})
            for i, old, new in fix:
                conn.execute(text("UPDATE matches SET date = :new WHERE id = :id AND date::date = :old"),
                             {"id": i, "old": old, "new": new})
        print(f"APLICADO: {len(delete)} copias malas borradas y {len(fix)} fechas corregidas "
              f"(respaldo en matches_swap_backup)")
    elif not apply:
        print("ENSAYO — usa --apply para corregir")
    return {"aborted": False, "deleted": len(delete), "fixed": len(fix), "kept": len(keep)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    main(apply=ap.parse_args().apply)
