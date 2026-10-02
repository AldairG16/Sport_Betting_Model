"""
scripts/fix_match_stats.py
==========================
Devuelve a `matches` los córners y tiros REALES de football-data (2-oct-26).

El cargador histórico pasaba las estadísticas por advanced_impute(): rellenaba
los huecos con promedios de liga y multiplicaba córners y tiros (también los
reales) por 0.8 + 0.4·goles/2.5. Medido: solo 13% de los córners de la
Premier 2023-24 eran los reales (Burnley 0-3 City: 6-5 guardados como 8-6), y
una apuesta de córners se liquidó con un dato alterado.

Por cada partido de los CSV de temporada (las ligas y temporadas que carga el
weekly), la fila del partido en `matches` (misma fecha, o ±1 día si no hay):
  - córners, tiros y tiros a puerta pasan al valor del CSV;
  - si el CSV no trae el dato, quedan NULL (el guardado era inventado);
  - una fila sin estadísticas (la insertó The Odds API) las recibe.
Las tarjetas no se tocan: el cargador no las alteraba.

Antes, cada fila que cambia se copia tal cual a matches_identity_backup
(acción 'restore_raw_stats'). Después: scripts/fix_stat_settlements.py corrige
las apuestas que se liquidaron con un dato alterado.

Uso:
    python scripts/fix_match_stats.py            # ensayo
    python scripts/fix_match_stats.py --apply
"""

import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from sqlalchemy import text

from src.utils.football_data import SEASON_LEAGUES, SEASON_URL, check_division, fetch_csv, parse_dates
from src.utils.match_backup import backup_matches
from src.utils.team_normalizer import normalize_team

CSV_COLS = {"home_shots": "HS", "away_shots": "AS",
            "home_shots_target": "HST", "away_shots_target": "AST",
            "home_corners": "HC", "away_corners": "AC"}
COLS = list(CSV_COLS)


def _value(v):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def csv_stats(df: pd.DataFrame) -> dict:
    """{(fecha, local, visitante): {columna: valor o None}} de un CSV."""
    dates = parse_dates(df["Date"].astype(str).str.strip()).dt.strftime("%Y-%m-%d")
    out = {}
    for i, (d, h, a) in enumerate(zip(dates, df["HomeTeam"], df["AwayTeam"])):
        if not isinstance(d, str):
            continue
        out[(d, normalize_team(str(h)), normalize_team(str(a)))] = {
            col: _value(df[src].iloc[i]) if src in df.columns else None
            for col, src in CSV_COLS.items()}
    return out


def plan_updates(rows: pd.DataFrame, truth: dict) -> list[dict]:
    """rows: id, d, home_team, away_team y las columnas de estadísticas.
    Devuelve [{id, columna: valor real, ...}] de las filas cuyo dato guardado
    no es el del CSV. Busca la fecha exacta y, si no está, ±1 día."""
    plan = []
    for r in rows.itertuples():
        d = pd.Timestamp(r.d)
        real = None
        for delta in (0, -1, 1):
            real = truth.get(((d + pd.Timedelta(days=delta)).strftime("%Y-%m-%d"),
                              str(r.home_team).lower(), str(r.away_team).lower()))
            if real:
                break
        if not real:
            continue
        stored = {c: _value(getattr(r, c)) for c in COLS}
        if stored != real:
            plan.append({"id": int(r.id), **real, "_stored": stored})
    return plan


def summarize(plan: list[dict]) -> dict:
    s = {"rows": len(plan), "changed": 0, "invented": 0, "filled": 0}
    for p in plan:
        for c in COLS:
            was, now = p["_stored"][c], p[c]
            if was is None and now is not None:
                s["filled"] += 1
            elif was is not None and now is None:
                s["invented"] += 1
            elif was != now:
                s["changed"] += 1
    return s


def update_stats(conn, plan: list[dict], chunk: int = 1000) -> None:
    """Un UPDATE por lote (FROM VALUES, con tipo explícito: una columna toda
    en NULL se inferiría como texto)."""
    for start in range(0, len(plan), chunk):
        part = plan[start:start + chunk]
        values = ", ".join(
            "(CAST(:id{k} AS integer), " + ", ".join(f"CAST(:{c}{k} AS integer)" for c in COLS) + ")"
            for k in range(len(part)))
        params = {}
        for k, p in enumerate(part):
            params[f"id{k}"] = p["id"]
            params.update({f"{c}{k}": p[c] for c in COLS})
        conn.execute(text(f"""
            UPDATE matches m SET {", ".join(f"{c} = v.{c}" for c in COLS)}
            FROM (VALUES {values}) AS v(id, {", ".join(COLS)})
            WHERE m.id = v.id
        """), params)


def main(apply: bool) -> int:
    from config.database import engine
    from scripts.load_historical_data import season_codes
    truth, failures = {}, []
    for code in SEASON_LEAGUES:                     # primero las descargas, después la base
        for season in season_codes():
            df, why = fetch_csv(SEASON_URL.format(season=season, code=code))
            problem = why if df is None else check_division(df, code)
            if problem:
                if df is not None or not why.startswith(("no existe", "HTTP 404")):
                    failures.append(f"{code} {season}: {problem}")
                continue
            truth.update(csv_stats(df))
    engine.dispose()
    rows = pd.read_sql(text(f"""
        SELECT id, date AS d, home_team, away_team, {", ".join(COLS)}
        FROM matches WHERE league = ANY(:l)
    """), engine, params={"l": list(SEASON_LEAGUES.values())})
    plan = plan_updates(rows, truth)
    s = summarize(plan)
    print(f"Partidos en los CSV: {len(truth)} · filas de esas ligas en la base: {len(rows)}")
    print(f"Filas a corregir: {s['rows']} · valores alterados que vuelven al real: {s['changed']} · "
          f"inventados que quedan NULL: {s['invented']} · faltantes que se completan: {s['filled']}")
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
        backup_matches(conn, [p["id"] for p in plan], "restore_raw_stats")
        update_stats(conn, plan)
    print(f"✅ {len(plan)} filas con sus estadísticas reales (respaldo en matches_identity_backup). "
          f"Siguiente paso: python scripts/fix_stat_settlements.py")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="escribe los cambios")
    sys.exit(main(ap.parse_args().apply))
