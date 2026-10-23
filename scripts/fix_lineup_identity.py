"""
scripts/fix_lineup_identity.py
==============================
Alineaciones guardadas con el partido o el equipo equivocado (23-oct-26).

El emparejamiento de ESPN aceptaba un partido con un solo nombre parecido:
la alineación de Gimnasia Mendoza vs Platense quedó también como la de
Gimnasia LP vs Sarmiento, y en el historial había 9 partidos de Argentina de
agosto y septiembre (cuando sus fechas estaban cruzadas y faltaba el partido
verdadero) guardados con otro equipo. Con eso, "titular habitual" y los
avisos de alineación salían mal.

Este script:
  1. encuentra esas filas (espn_lineups.identity_problems: alineaciones que
     casi no comparten jugadores con su equipo);
  2. las respalda en lineups_identity_backup y las borra;
  3. vuelve a bajar de ESPN los partidos terminados de esos días con el
     emparejamiento estricto (backfill_lineups);
  4. con --alerts, respalda en lineup_alerts_backup y borra los avisos de
     alineación que salieron de una alineación equivocada (no cuentan en la
     medición del weekly);
  5. pasa al nombre del sistema las filas guardadas con un nombre de ESPN que
     ahora tiene alias (normalize_team);
  6. con --wrong-league, respalda y borra las filas de esa liga cuyos equipos
     no son de ella (China: alineaciones de Chile de cuando el código de ESPN
     apuntaba a chi.1, corregido el 23-oct-26).

    python scripts/fix_lineup_identity.py                     # en seco
    python scripts/fix_lineup_identity.py --apply --alerts 101,102 --wrong-league soccer_china_superleague
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))


def renames(rows: list, normalize) -> list:
    """Función pura: [(id, nombre nuevo, rival nuevo)] de las filas cuyo nombre
    guardado ya no es el del sistema."""
    out = []
    for r in rows:
        t, o = normalize(r["team"]), normalize(r.get("opponent") or "")
        if t != r["team"] or (r.get("opponent") and o != r["opponent"]):
            out.append((r["id"], t, o or r.get("opponent")))
    return out


def wrong_league(rows: list, league: str, teams: set, normalize) -> list:
    """Función pura: ids de las filas de `league` cuyo equipo no es de esa liga."""
    return [r["id"] for r in rows if r["sport_key"] == league and normalize(r["team"]) not in teams]


def main(apply: bool = False, alert_ids: list | None = None, leagues: list | None = None) -> dict:
    from config.database import engine
    from src.features.espn_lineups import backfill_lineups, identity_problems, load_identity_rows
    from src.utils.team_normalizer import normalize_team
    rows = load_identity_rows(engine)
    found = identity_problems(rows)
    bad = [r for r in rows if r["id"] in set(found["foreign"])]
    junk = []
    for lg in leagues or []:
        with engine.connect() as conn:
            teams = {r[0] for r in conn.execute(text("""
                SELECT LOWER(home_team) FROM matches WHERE league = :lg
                UNION SELECT LOWER(away_team) FROM matches WHERE league = :lg"""), {"lg": lg})}
        junk += wrong_league(rows, lg, teams, normalize_team)
    print(f"Filas de equipos que no son de su liga ({', '.join(leagues or []) or '—'}): {len(junk)}")
    bad += [r for r in rows if r["id"] in set(junk) and r not in bad]
    gone = {r["id"] for r in bad}
    ren = [x for x in renames([r for r in rows if r["id"] not in gone], normalize_team)]
    print(f"Filas con nombre de ESPN que ya tiene alias: {len(ren)}")
    for rid, t, o in ren[:20]:
        print(f"  {rid}: → {t} vs {o}")
    print(f"Alineaciones ajenas a su equipo: {len(bad)} · partidos de ESPN en más de un partido: "
          f"{len(found['shared_events'])}")
    for r in sorted(bad, key=lambda r: pd.Timestamp(r["kickoff"])):
        print(f"  {r['id']:>7} {str(r['kickoff'])[:16]} {r['sport_key']:<36} {r['team']:<22} "
              f"evento {r['event_id']} {'terminado' if r['completed'] else 'por jugar'}")
    alert_ids = [int(a) for a in (alert_ids or [])]
    if alert_ids:
        with engine.connect() as conn:
            al = conn.execute(text("SELECT id, home, away, weakened, market, alerted_at FROM lineup_alerts "
                                   "WHERE id = ANY(:ids)"), {"ids": alert_ids}).mappings().all()
        print(f"Avisos de alineación a quitar: {len(al)}")
        for a in al:
            print(f"  {a['id']} {a['home']} vs {a['away']} · {a['weakened']} · {a['market']} · {a['alerted_at']}")
    if not apply:
        print("En seco: no se escribió nada (usa --apply).")
        return {"lineups": len(bad), "alerts": len(alert_ids)}
    with engine.begin() as conn:
        if bad:
            conn.execute(text("CREATE TABLE IF NOT EXISTS lineups_identity_backup AS "
                              "SELECT *, NOW() AS backed_up_at FROM lineups WHERE FALSE"))
            conn.execute(text("INSERT INTO lineups_identity_backup SELECT *, NOW() FROM lineups WHERE id = ANY(:ids)"),
                         {"ids": [r["id"] for r in bad]})
            conn.execute(text("DELETE FROM lineups WHERE id = ANY(:ids)"), {"ids": [r["id"] for r in bad]})
        if ren:
            conn.execute(text("CREATE TABLE IF NOT EXISTS lineups_identity_backup AS "
                              "SELECT *, NOW() AS backed_up_at FROM lineups WHERE FALSE"))
            conn.execute(text("INSERT INTO lineups_identity_backup SELECT *, NOW() FROM lineups WHERE id = ANY(:ids)"),
                         {"ids": [x[0] for x in ren]})
            for rid, t, o in ren:
                clash = conn.execute(text("""
                    SELECT 1 FROM lineups a JOIN lineups b ON b.sport_key = a.sport_key AND b.kickoff = a.kickoff
                    WHERE a.id = :id AND b.team = :t AND b.id <> a.id"""), {"id": rid, "t": t}).first()
                if clash:          # ya está con el nombre del sistema: sobra la copia
                    conn.execute(text("DELETE FROM lineups WHERE id = :id"), {"id": rid})
                else:
                    conn.execute(text("UPDATE lineups SET team = :t, opponent = :o WHERE id = :id"),
                                 {"id": rid, "t": t, "o": o})
        if alert_ids:
            conn.execute(text("CREATE TABLE IF NOT EXISTS lineup_alerts_backup AS "
                              "SELECT *, NOW() AS backed_up_at FROM lineup_alerts WHERE FALSE"))
            conn.execute(text("INSERT INTO lineup_alerts_backup SELECT *, NOW() FROM lineup_alerts "
                              "WHERE id = ANY(:ids)"), {"ids": alert_ids})
            conn.execute(text("DELETE FROM lineup_alerts WHERE id = ANY(:ids)"), {"ids": alert_ids})
    print(f"✅ {len(bad)} alineaciones y {len(alert_ids)} avisos respaldados y quitados; {len(ren)} renombradas")
    done = [pd.Timestamp(r["kickoff"]) for r in bad if r["completed"] and r["id"] not in set(junk)]
    if done:
        days = (datetime.now(timezone.utc) - min(done).to_pydatetime()).days + 2
        backfill_lineups(days=days, engine=engine)
    after = identity_problems(load_identity_rows(engine))
    print(f"Después: ajenas {len(after['foreign'])}, partidos de ESPN repetidos {len(after['shared_events'])}")
    return {"lineups": len(bad), "alerts": len(alert_ids), "after": after}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--alerts", default="", help="ids de lineup_alerts separados por coma")
    ap.add_argument("--wrong-league", action="append", default=[], help="liga a limpiar de equipos ajenos")
    args = ap.parse_args()
    main(args.apply, [a for a in args.alerts.split(",") if a.strip()], args.wrong_league)
