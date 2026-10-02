"""
scripts/fix_league_labels.py
============================
Devuelve a su liga los partidos guardados con la etiqueta de otra (1-oct-26).

football-data.co.uk corrige nombres de archivo: si el pedido no existe,
redirige a uno parecido, y los cargadores guardaban ese otro archivo con la
liga pedida (src/utils/football_data.py ya lo impide). Tres casos:

  1. K-League ← Noruega: KOR.csv → NOR.csv, 3,541 partidos desde 2012.
  2. Champions League ← National League inglesa (5ª división): ECL.csv →
     EC.csv, todas las temporadas desde 2015.
  3. Temporada en curso antes de publicarse: E0 → EC (National League como
     Premier) y SP1 → SC1 / P1 (Escocia y Portugal como La Liga), ago-26.

Cada fila se busca (fecha, local, visitante) en el CSV de su liga real y solo
esas cambian de liga; en el caso 3, solo si el partido NO está en el CSV de la
liga con la que se guardó. Antes se copian tal cual a matches_identity_backup
(acción 'relabel_league'). No se borra nada.

Correr ANTES de scripts/fix_team_identities.py: así los repetidos entre
fuentes (football-data y The Odds API) ya tienen la misma liga.

Uso:
    python scripts/fix_league_labels.py            # ensayo
    python scripts/fix_league_labels.py --apply
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from sqlalchemy import text

from src.utils.football_data import (OTHER_SEASON_LEAGUES, SEASON_LEAGUES, SEASON_URL,
                                     check_division, fetch_csv, is_missing, parse_dates)
from src.utils.match_backup import backup_matches
from src.utils.team_normalizer import normalize_team

KOREA, NORWAY = "soccer_korea_kleague1", "soccer_norway_eliteserien"
CHAMPIONS = "soccer_uefa_champs_league"
NATIONAL = OTHER_SEASON_LEAGUES["EC"]
NOR_URL = "https://www.football-data.co.uk/new/NOR.csv"


def match_keys(df: pd.DataFrame, home: str = "HomeTeam", away: str = "AwayTeam") -> set:
    """(fecha, local, visitante) normalizados de los partidos de un CSV."""
    df = df.rename(columns=lambda c: str(c).strip())
    dates = parse_dates(df["Date"].astype(str).str.strip()).dt.strftime("%Y-%m-%d")
    return {(d, normalize_team(str(h).strip().lower()), normalize_team(str(a).strip().lower()))
            for d, h, a in zip(dates, df[home], df[away]) if isinstance(d, str)}


def row_key(r) -> tuple:
    return (str(r.d)[:10], normalize_team(str(r.home_team)), normalize_team(str(r.away_team)))


def plan_relabels(rows: pd.DataFrame, sources: dict, own: dict | None = None) -> dict:
    """
    rows: id, d, league, home_team, away_team. sources: {liga: claves de su
    CSV}. own: {liga: claves} de la liga con la que se guardó la fila — si el
    partido está ahí, la fila está bien y no se toca.
    Devuelve {id: liga correcta}.
    """
    out = {}
    for r in rows.itertuples():
        k = row_key(r)
        if own and k in own.get(r.league, ()):
            continue
        for league, keys in sources.items():
            if league != r.league and k in keys:
                out[int(r.id)] = league
                break
    return out


def _season_csv(season: str, code: str, failures: list) -> pd.DataFrame | None:
    df, why = fetch_csv(SEASON_URL.format(season=season, code=code))
    if df is None:
        if not is_missing(why):
            failures.append(f"{code} {season}: {why}")
        return None
    problem = check_division(df, code)
    if problem:
        failures.append(problem)
        return None
    return df


def _rows(engine, where: str, params: dict) -> pd.DataFrame:
    return pd.read_sql(text(f"""
        SELECT id, date::date AS d, league, home_team, away_team FROM matches WHERE {where}
    """), engine, params=params)


def main(apply: bool) -> int:
    from config.database import engine
    from scripts.load_historical_data import season_codes
    failures: list = []
    plan: dict = {}          # id -> (liga guardada, liga correcta)

    # Primero TODAS las descargas y después la base, seguido: con consultas
    # intercaladas, la conexión quedaba inactiva durante las descargas y la
    # base la cortaba ("server closed the connection unexpectedly", 2-oct-26).
    nor, why = fetch_csv(NOR_URL)
    if nor is None:
        raise RuntimeError(f"NOR.csv: {why}")
    nor_keys = match_keys(nor, "Home", "Away")
    ec = set()
    for s in season_codes():
        df = _season_csv(s, "EC", failures)
        if df is not None:
            ec |= match_keys(df)
    current = season_codes()[-1]
    keys = {}
    for code, league in {**SEASON_LEAGUES, **OTHER_SEASON_LEAGUES}.items():
        df = _season_csv(current, code, failures)
        if df is not None:
            keys[league] = match_keys(df)
    own = {lg: keys[lg] for lg in SEASON_LEAGUES.values() if lg in keys}
    engine.dispose()             # conexiones nuevas: nada quedó inactivo del pool

    # 1. K-League ← Noruega
    rows = _rows(engine, "league = :l", {"l": KOREA})
    for i, lg in plan_relabels(rows, {NORWAY: nor_keys}).items():
        plan[i] = (KOREA, lg)
    print(f"1. K-League: {len(rows)} filas · noruegas: {sum(1 for v in plan.values() if v[0] == KOREA)}")

    # 2. Champions ← National League, todas las temporadas
    rows = _rows(engine, "league = :l", {"l": CHAMPIONS})
    p2 = plan_relabels(rows, {NATIONAL: ec})
    plan.update({i: (CHAMPIONS, lg) for i, lg in p2.items()})
    print(f"2. Champions: {len(rows)} filas · de la National League inglesa: {len(p2)} · "
          f"quedan como Champions: {len(rows) - len(p2)}")

    # 3. temporada en curso: filas que no están en el CSV de su liga sino en otro
    rows = _rows(engine, "league = ANY(:l) AND season = :s",
                 {"l": sorted(own), "s": int(current[:2]) + 2000})
    p3 = plan_relabels(rows, keys, own=own)
    by_league = rows.set_index("id")["league"].to_dict()
    plan.update({i: (by_league[i], lg) for i, lg in p3.items()})
    print(f"3. Temporada {current}: {len(rows)} filas de football-data · de otra liga: {len(p3)}")

    pairs = Counter(plan.values())
    for (old, new), n in sorted(pairs.items(), key=lambda t: -t[1]):
        print(f"   {n:>5}  {old} → {new}")
    if failures:
        print(f"⚠️  {len(failures)} CSV no se pudieron leer: " + "; ".join(failures[:5]))
    if not apply:
        print("EN SECO: no se escribió nada. Repetir con --apply.")
        return 0
    if not plan:
        print("Nada que corregir.")
        return 0

    engine.dispose()
    with engine.begin() as conn:
        backup_matches(conn, plan, "relabel_league")
        changed = 0
        for (old, new) in pairs:
            ids = [i for i, v in plan.items() if v == (old, new)]
            res = conn.execute(text("""
                UPDATE matches SET league = :new,
                       sport_key = CASE WHEN sport_key = :old THEN :new ELSE sport_key END
                WHERE id = ANY(:ids) AND league = :old
            """), {"new": new, "old": old, "ids": ids})
            changed += res.rowcount
    print(f"✅ {changed} filas cambiadas de liga (respaldo en matches_identity_backup)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="escribe el cambio")
    sys.exit(main(ap.parse_args().apply))
