"""
scripts/fetch_results_tsdb.py
=============================
Marcadores de respaldo desde TheSportsDB (2-nov-26) para las ligas que ni
ESPN ni football-data.co.uk tienen: hoy solo la K-League.

The Odds API /scores no devolvió la jornada 34 de la K-League (31-oct y
1-nov): en cada corrida la liga salía sin "Completados" y el chequeo de
salidas avisó "a soccer_korea_kleague1 le faltan 6 de 6 marcadores". Sus 6
picks se quedaban sin liquidar. TheSportsDB tenía la jornada completa, y su
jornada 33 coincide marcador por marcador con la de The Odds API.

- Clave pública de prueba "123" (THESPORTSDB_KEY si hay otra). Medido el
  2-nov: ~10 llamadas seguidas y luego 429 con Retry-After de 120 s; se
  respeta. Con esa clave `eventsday` trae como mucho 3 partidos por día y
  `eventsround` la jornada completa: se pide `eventsday` de cada fecha para
  saber la jornada y la temporada, y después esa jornada.
- Diario (`fetch_tsdb_backup`): solo inserta los partidos analizados que no
  tienen marcador. Un partido analizado que no empareja con ninguno avisa
  con su nombre: falta un alias en team_name_map.
- Semanal (`load_recent`): las jornadas de los últimos 8 días completas en
  `matches` (~10 llamadas), también los partidos que no se analizaron.
- Historia (`--history 2023 2024 2025 2026 [--apply]`): todas las jornadas
  terminadas de esas temporadas (~40 llamadas por temporada: ~10 minutos
  cada una por el límite). Hasta el 2-nov la K-League tenía 80 partidos en
  `matches` y Dixon-Coles la pesaba al 25% ("algún equipo tiene menos de 15
  partidos"). Un partido que ya está a ±1 día no se repite.

Uso:
  python scripts/fetch_results_tsdb.py                         # respaldo diario
  python scripts/fetch_results_tsdb.py --recent                # jornadas de los últimos 8 días
  python scripts/fetch_results_tsdb.py --history 2024 2025     # ver qué entraría
  python scripts/fetch_results_tsdb.py --history 2024 2025 --apply
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import requests
from sqlalchemy import text

from config.settings import env_str
from src.utils.log import get_logger
from src.utils.team_normalizer import normalize_team

log = get_logger(__name__)

TSDB_LEAGUES = {"soccer_korea_kleague1": "4689"}
TSDB_KEY = env_str("THESPORTSDB_KEY", "123")
TSDB_URL = "https://www.thesportsdb.com/api/v1/json/{key}/{endpoint}.php"
TSDB_PAUSE_S = 2.1                 # entre llamadas
TSDB_MAX_WAIT_S = 150              # Retry-After más largo que se espera (medido: 120)
FINISHED = {"FT", "AET", "PEN", "AP", "Match Finished"}
MAX_ROUNDS = 45                    # K-League: 33 de temporada regular + 5 de la fase final
RECENT_DAYS = 8


def _get(endpoint: str, sleep=time.sleep, get=None, **params) -> list:
    """Los eventos de una llamada ([] si no hay). Ante 429 espera lo que
    dice Retry-After; ante un error de red o 5xx, 20, 40 y 60 s. Hasta 3
    reintentos."""
    get = get or requests.get
    url = TSDB_URL.format(key=TSDB_KEY, endpoint=endpoint)
    for attempt in range(4):
        sleep(TSDB_PAUSE_S)
        wait = 20 * (attempt + 1)
        try:
            r = get(url, params=params, timeout=20)
            if r.status_code == 429:
                ra = str(r.headers.get("retry-after", ""))
                wait = min(int(ra) + 1, TSDB_MAX_WAIT_S) if ra.isdigit() else wait
                raise requests.HTTPError("HTTP 429")
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return (r.json() or {}).get("events") or []
        except (requests.RequestException, ValueError):
            if attempt == 3:
                raise
            sleep(wait)
    return []


def _rounds_of(dates, league_id: str) -> set:
    """(temporada, jornada) de los partidos de esas fechas (eventsday)."""
    rounds = set()
    for d in sorted(set(dates)):
        for e in _get("eventsday", d=d, s="Soccer", l=league_id):
            if e.get("intRound") and e.get("strSeason"):
                rounds.add((str(e["strSeason"]), str(e["intRound"])))
    return rounds


def finished_rows(events: list, sport_key: str, normalize=normalize_team) -> list:
    """Función pura: los partidos terminados con marcador, como filas de
    `matches` (fecha UTC del inicio, nombres normalizados)."""
    out = []
    for e in events:
        if e.get("strStatus") not in FINISHED:
            continue
        try:
            hg, ag = int(e["intHomeScore"]), int(e["intAwayScore"])
        except (KeyError, TypeError, ValueError):
            continue
        date = str(e.get("strTimestamp") or e.get("dateEvent") or "")[:10]
        if len(date) != 10:
            continue
        out.append({"date": date, "home_team": normalize(e["strHomeTeam"]).lower().strip(),
                    "away_team": normalize(e["strAwayTeam"]).lower().strip(),
                    "home_goals": hg, "away_goals": ag, "league": sport_key})
    return out


def pair_with_missing(rows: list, missing: list) -> tuple[list, list]:
    """Función pura. rows: filas de finished_rows; missing: partidos
    analizados sin marcador ({match, match_date}). Devuelve (las filas que
    emparejan con un faltante a ±1 día, los faltantes que no emparejaron)."""
    found, matched = [], set()
    for m in missing:
        d = pd.Timestamp(m["match_date"])
        d = (d.tz_convert("UTC") if d.tzinfo else d).date()
        for r in rows:
            if f"{r['home_team']} vs {r['away_team']}" == m["match"] \
                    and abs((pd.Timestamp(r["date"]).date() - d).days) <= 1:
                if r not in found:
                    found.append(r)
                matched.add((m["match"], str(m["match_date"])))
                break
    left = [m for m in missing if (m["match"], str(m["match_date"])) not in matched]
    return found, left


def new_rows(rows: list, have: list) -> list:
    """Función pura: las filas que no están ya en `matches` (mismo local y
    visitante a ±1 día). have: [{"h", "a", "d"}]."""
    idx = {}
    for m in have:
        idx.setdefault((m["h"], m["a"]), []).append(pd.Timestamp(m["d"]).date())
    out = []
    for r in rows:
        d = pd.Timestamp(r["date"]).date()
        if any(abs((x - d).days) <= 1 for x in idx.get((r["home_team"], r["away_team"]), [])):
            continue
        out.append(r)
        idx.setdefault((r["home_team"], r["away_team"]), []).append(d)
    return out


def _missing(days: int) -> list:
    """Partidos analizados de las ligas de TheSportsDB, ya jugados (hace 2 h o
    más, en los últimos `days` días) y sin marcador en `matches`."""
    from config.database import engine
    from src.utils.output_checks import missing_results
    leagues = list(TSDB_LEAGUES)
    played = pd.read_sql(text("""
        SELECT match, match_date, league FROM model_vs_sharp
        WHERE skipped_at IS NULL AND league = ANY(:l)
          AND match_date BETWEEN NOW() - make_interval(days => :d) AND NOW() - INTERVAL '2 hours'
    """), engine, params={"l": leagues, "d": days}).to_dict("records")
    if not played:
        return []
    have = pd.read_sql(text("""
        SELECT LOWER(home_team) AS h, LOWER(away_team) AS a, date::date AS d, home_goals, away_goals
        FROM matches WHERE home_goals IS NOT NULL AND league = ANY(:l)
          AND date >= CURRENT_DATE - make_interval(days => :d)
    """), engine, params={"l": leagues, "d": days + 2}).to_dict("records")
    return missing_results(played, have, normalize_team)


def fetch_tsdb_backup(days: int = 10, verbose: bool = True) -> int:
    """Inserta los marcadores que faltan de los partidos analizados de las
    ligas de TheSportsDB. Devuelve cuántos insertó."""
    from scripts.fetch_results import insert_results
    missing = _missing(days)
    if not missing:
        if verbose:
            print("   TheSportsDB: ningún partido analizado sin marcador en sus ligas")
        return 0
    inserted = 0
    for sport_key, league_id in TSDB_LEAGUES.items():
        miss = [m for m in missing if m["league"] == sport_key]
        if not miss:
            continue
        rounds = _rounds_of((pd.Timestamp(m["match_date"]).strftime("%Y-%m-%d") for m in miss), league_id)
        rows, status = [], {}
        for season, rnd in sorted(rounds):
            events = _get("eventsround", id=league_id, r=rnd, s=season)
            rows += finished_rows(events, sport_key)
            status.update({f"{normalize_team(e['strHomeTeam']).lower().strip()} vs "
                           f"{normalize_team(e['strAwayTeam']).lower().strip()}": e.get("strStatus")
                           for e in events if e.get("strStatus") not in FINISHED})
        found, left = pair_with_missing(rows, miss)
        n, _ = insert_results(found) if found else (0, 0)
        inserted += n
        if verbose:
            print(f"   TheSportsDB {sport_key}: {len(miss)} sin marcador · jornadas "
                  f"{', '.join(r for _, r in sorted(rounds)) or '—'} · {n} marcadores nuevos")
            for r in found:
                print(f"     {r['home_team']} {r['home_goals']}-{r['away_goals']} {r['away_team']}  ({r['date']})")
        # sin terminar en TheSportsDB (aplazado, en juego): no falta un alias
        waiting = [m for m in left if m["match"] in status]
        for m in waiting:
            print(f"     {m['match']}: en TheSportsDB sigue '{status[m['match']]}'")
        left = [m for m in left if m["match"] not in status]
        if left:
            done = {(r["home_team"], r["away_team"]) for r in rows}
            log.warning(f"⚠️  TheSportsDB {sport_key}: {len(left)} partido(s) sin pareja "
                        f"({'; '.join(m['match'] for m in left[:3])})"
                        + (f" — sus terminados de esas fechas: {'; '.join(f'{h} vs {a}' for h, a in sorted(done)[:4])}"
                           if done else ""))
    return inserted


def _save(sport_key: str, rows: list, apply: bool, what: str, verbose: bool) -> dict:
    """Inserta (con apply) las filas que no están ya en `matches` a ±1 día."""
    from config.database import engine
    from scripts.fetch_results import insert_results
    have = pd.read_sql(text("""SELECT LOWER(home_team) AS h, LOWER(away_team) AS a, date::date AS d
                               FROM matches WHERE league = :l"""), engine,
                       params={"l": sport_key}).to_dict("records")
    add = new_rows(rows, have)
    teams = sorted({r["home_team"] for r in rows} | {r["away_team"] for r in rows})
    n = insert_results(add)[0] if (apply and add) else 0
    if verbose:
        print(f"   TheSportsDB {sport_key}: {len(rows)} terminados ({what}) · {len(add)} nuevos · "
              f"{'insertados ' + str(n) if apply else 'sin --apply: nada se escribe'}")
        if not apply:
            print(f"     equipos: {', '.join(teams)}")
    return {"found": len(rows), "new": len(add), "inserted": n, "teams": teams}


def load_history(seasons: list, apply: bool = False, verbose: bool = True) -> dict:
    """Todas las jornadas terminadas de esas temporadas, para las ligas de
    TheSportsDB. Sin apply solo cuenta lo que entraría."""
    out = {}
    for sport_key, league_id in TSDB_LEAGUES.items():
        rows = []
        for season in seasons:
            empty = 0
            for rnd in range(1, MAX_ROUNDS + 1):
                ev = _get("eventsround", id=league_id, r=rnd, s=str(season))
                if not ev:
                    empty += 1
                    if empty >= 2:
                        break
                    continue
                empty = 0
                rows += finished_rows(ev, sport_key)
        out[sport_key] = _save(sport_key, rows, apply, ", ".join(map(str, seasons)), verbose)
    return out


def load_recent(days: int = RECENT_DAYS, apply: bool = True, verbose: bool = True) -> dict:
    """Las jornadas completas de los últimos `days` días: también los
    partidos que no se analizaron (el respaldo diario solo trae esos)."""
    today = pd.Timestamp.now(tz="UTC").normalize()
    dates = [(today - pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, days + 1)]
    out = {}
    for sport_key, league_id in TSDB_LEAGUES.items():
        rounds = _rounds_of(dates, league_id)
        rows = []
        for season, rnd in sorted(rounds):
            rows += finished_rows(_get("eventsround", id=league_id, r=rnd, s=season), sport_key)
        out[sport_key] = _save(sport_key, rows, apply, f"jornadas {', '.join(r for _, r in sorted(rounds)) or '—'}",
                               verbose)
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--history", type=int, nargs="*", help="temporadas a cargar completas")
    parser.add_argument("--recent", action="store_true", help="jornadas de los últimos 8 días")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.history:
        load_history(args.history, apply=args.apply)
    elif args.recent:
        load_recent(apply=args.apply)
    else:
        fetch_tsdb_backup(days=args.days)
